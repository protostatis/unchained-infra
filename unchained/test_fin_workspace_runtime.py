"""Tests for the account-runtime durable sleep + idle scheduler.

Covers:
  - runtime_provider_flush (S2S through the host-side provider)
  - runtime_sleep_durable (flush-then-stop, fail closed when flush fails)
  - FinancialWorkspaceRuntimeScheduler (idle candidates, active-WebSocket
    preservation, back-off after a failed flush)
  - the /internal/financial-workspace/runtime/sleep handler (durable)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from checkpoint_store import LocalCheckpointStore
from financial_workspace import (
    FinancialWorkspace,
    FinancialWorkspaceRuntimeScheduler,
    runtime_provider_flush,
    workspace_runtime_slug,
)
from web_app.handlers import fin_workspace


def _env_cleanup(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _Request:
    def __init__(self, *, body=None, token="", cookies=None, headers=None,
                 match_info=None, query=None, method="POST"):
        self._body = body
        self._headers = headers or {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._cookies = cookies or {}
        self.match_info = match_info or {}
        self.query = query or {}
        self.method = method
        self.cookies = self._cookies
        self.headers = self._headers

    async def json(self):
        return self._body


def _payload(response) -> dict:
    return json.loads(response.body.decode())


class RuntimeProviderFlushTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
            "FIN_WORKSPACE_CONTROL_TOKEN",
        )}

    def tearDown(self):
        _env_cleanup(self._saved)

    def test_flush_posts_to_provider_with_control_token(self):
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "c" * 40
        slug = workspace_runtime_slug("u-1")
        with patch("httpx.Client") as m_client:
            m_client.return_value.__enter__.return_value.post.return_value.status_code = 200
            m_client.return_value.__enter__.return_value.post.return_value.json.return_value = {
                "ok": True, "snapshot_id": "fsn-1",
            }
            result = runtime_provider_flush(slug)
        self.assertTrue(result["ok"])
        url, kwargs = m_client.return_value.__enter__.return_value.post.call_args
        self.assertIn(f"/v1/accounts/{slug}/flush", url[0])
        self.assertEqual(
            kwargs["json"]["controlUrl"], "http://fin-terminal-workspace-control:8790"
        )
        self.assertEqual(kwargs["json"]["controlToken"], "c" * 40)

    def test_flush_fails_closed_without_provider(self):
        os.environ.pop("FIN_WORKSPACE_RUNTIME_PROVIDER_URL", None)
        result = runtime_provider_flush(workspace_runtime_slug("u-1"))
        self.assertFalse(result["ok"])


class DurableSleepTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.fw = FinancialWorkspace(self.db_path, LocalCheckpointStore())
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
            "FIN_WORKSPACE_CONTROL_TOKEN",
        )}
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "c" * 40
        self._create_workspace("u-dur", "dur@example.com")

    def tearDown(self):
        _env_cleanup(self._saved)
        self._temp_dir.cleanup()

    def _create_workspace(self, user_id, email):
        chk = self.fw.create_checkpoint(
            request_id=f"req-{user_id}", session_id="s", worker_id="w",
            checkpoint=b'{"holdings":[]}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="n", audience="github",
        )
        self.fw.bind_oauth_state(claim["claim_id"], "st", audience="github")
        return self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id=user_id, final_account_email=email,
            browser_nonce="n", oauth_state="st",
        )

    def test_sleep_only_after_durable_flush(self):
        """runtime_sleep_durable flushes first and only then sleeps."""
        self.fw.runtime_wake("u-dur")
        slug = workspace_runtime_slug("u-dur")
        with patch("financial_workspace.runtime_provider_flush",
                   return_value={"ok": True, "snapshot_id": "fsn-1"}) as m_flush, \
             patch("financial_workspace.runtime_provider_sleep",
                   return_value=True) as m_sleep:
            result = self.fw.runtime_sleep_durable("u-dur", reason="idle")
        self.assertEqual(result["runtime_state"], "asleep")
        self.assertTrue(result["flush"]["ok"])
        m_flush.assert_called_once_with(slug)
        m_sleep.assert_called_once_with(slug)

    def test_sleep_fails_closed_when_flush_fails(self):
        """A failed flush keeps the runtime awake — no state loss."""
        self.fw.runtime_wake("u-dur")
        slug = workspace_runtime_slug("u-dur")
        with patch("financial_workspace.runtime_provider_flush",
                   return_value={"ok": False, "reason": "export failed"}), \
             patch("financial_workspace.runtime_provider_sleep") as m_sleep:
            result = self.fw.runtime_sleep_durable("u-dur", reason="idle")
        self.assertEqual(result["runtime_state"], "awake")
        self.assertIn("error", result)
        m_sleep.assert_not_called()

    def test_sleep_is_idempotent_when_already_asleep(self):
        with patch("financial_workspace.runtime_provider_flush") as m_flush, \
             patch("financial_workspace.runtime_provider_sleep") as m_sleep:
            result = self.fw.runtime_sleep_durable("u-dur", reason="idle")
        self.assertEqual(result["runtime_state"], "asleep")
        self.assertTrue(result["flush"].get("skipped"))
        m_flush.assert_not_called()
        m_sleep.assert_not_called()


class RuntimeSleepHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.fw = FinancialWorkspace(self.db_path, LocalCheckpointStore())
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
            "FIN_WORKSPACE_CONTROL_TOKEN",
        )}
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "c" * 40
        chk = self.fw.create_checkpoint(
            request_id="req-h", session_id="s", worker_id="w",
            checkpoint=b'{"holdings":[]}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"], browser_nonce="n", audience="github",
        )
        self.fw.bind_oauth_state(claim["claim_id"], "st", audience="github")
        self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id="u-h", final_account_email="h@example.com",
            browser_nonce="n", oauth_state="st",
        )

    def tearDown(self):
        _env_cleanup(self._saved)
        self._temp_dir.cleanup()

    async def test_sleep_handler_requires_control_token(self):
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._verify_control_token",
                   return_value=False):
            resp = await fin_workspace.handle_fin_workspace_runtime_sleep(
                _Request(query={"user_id": "u-h"})
            )
        self.assertEqual(resp.status, 401)

    async def test_sleep_handler_durable_flush_then_sleep(self):
        self.fw.runtime_wake("u-h")
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._verify_control_token",
                   return_value=True), \
             patch("financial_workspace.runtime_provider_flush",
                   return_value={"ok": True}), \
             patch("financial_workspace.runtime_provider_sleep",
                   return_value=True):
            resp = await fin_workspace.handle_fin_workspace_runtime_sleep(
                _Request(query={"user_id": "u-h"})
            )
        self.assertEqual(resp.status, 200)
        data = _payload(resp)
        self.assertEqual(data["runtime_state"], "asleep")

    async def test_sleep_handler_fails_closed_when_flush_fails(self):
        self.fw.runtime_wake("u-h")
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._verify_control_token",
                   return_value=True), \
             patch("financial_workspace.runtime_provider_flush",
                   return_value={"ok": False, "reason": "boom"}), \
             patch("financial_workspace.runtime_provider_sleep") as m_sleep:
            resp = await fin_workspace.handle_fin_workspace_runtime_sleep(
                _Request(query={"user_id": "u-h"})
            )
        self.assertEqual(resp.status, 409)
        m_sleep.assert_not_called()


class RuntimeSchedulerTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.fw = FinancialWorkspace(self.db_path, LocalCheckpointStore())
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
            "FIN_WORKSPACE_CONTROL_TOKEN",
        )}
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "c" * 40
        self._create_workspace("u-1", "one@example.com")
        self._create_workspace("u-2", "two@example.com")
        self.now = [1_000_000.0]

    def tearDown(self):
        _env_cleanup(self._saved)
        self._temp_dir.cleanup()

    def _create_workspace(self, user_id, email):
        chk = self.fw.create_checkpoint(
            request_id=f"req-{user_id}", session_id="s", worker_id="w",
            checkpoint=b'{"holdings":[]}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="n", audience="github",
        )
        self.fw.bind_oauth_state(claim["claim_id"], "st", audience="github")
        return self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id=user_id, final_account_email=email,
            browser_nonce="n", oauth_state="st",
        )

    def _scheduler(self, idle=60):
        return FinancialWorkspaceRuntimeScheduler(
            self.fw, idle_seconds=float(idle), backoff_seconds=120.0, now=lambda: self.now[0],
        )

    def test_active_websocket_never_becomes_a_candidate(self):
        self.fw.runtime_wake("u-1")
        sched = self._scheduler(idle=1)
        sched.attach("u-1")
        self.now[0] += 10_000
        self.assertEqual(sched.idle_candidates(), [])

    def test_idle_after_window_becomes_candidate(self):
        self.fw.runtime_wake("u-1")
        sched = self._scheduler(idle=1)
        sched.touch("u-1")
        self.now[0] += 10_000
        self.assertEqual(sched.idle_candidates(), ["u-1"])

    def test_tick_sleeps_only_after_durable_flush(self):
        self.fw.runtime_wake("u-1")
        sched = self._scheduler(idle=1)
        sched.touch("u-1")
        self.now[0] += 10_000
        with patch("financial_workspace.runtime_provider_flush",
                   return_value={"ok": True, "snapshot_id": "fsn-1"}), \
             patch("financial_workspace.runtime_provider_sleep",
                   return_value=True):
            results = sched.tick()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["runtime_state"], "asleep")
        status = self.fw.runtime_status("u-1")
        self.assertEqual(status["runtime_state"], "asleep")

    def test_tick_fails_closed_when_flush_fails_and_backs_off(self):
        self.fw.runtime_wake("u-1")
        sched = self._scheduler(idle=1)
        sched.touch("u-1")
        self.now[0] += 10_000
        with patch("financial_workspace.runtime_provider_flush",
                   return_value={"ok": False, "reason": "export failed"}), \
             patch("financial_workspace.runtime_provider_sleep") as m_sleep:
            results = sched.tick()
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        m_sleep.assert_not_called()
        self.assertEqual(self.fw.runtime_status("u-1")["runtime_state"], "awake")
        # Back-off: within the backoff window the same account is not retried.
        self.now[0] += 10
        self.assertEqual(sched.idle_candidates(), [])

    def test_touch_resets_idle_window(self):
        self.fw.runtime_wake("u-1")
        sched = self._scheduler(idle=60)
        sched.touch("u-1")
        self.now[0] += 30
        sched.touch("u-1")  # renewed
        self.now[0] += 30
        self.assertEqual(sched.idle_candidates(), [])
        self.now[0] += 31
        self.assertEqual(sched.idle_candidates(), ["u-1"])

    def test_attach_detach_counts(self):
        sched = self._scheduler()
        sched.attach("u-1")
        sched.attach("u-1")
        self.assertEqual(sched.active_socket_count("u-1"), 2)
        sched.detach("u-1")
        self.assertEqual(sched.active_socket_count("u-1"), 1)
        sched.detach("u-1")
        self.assertEqual(sched.active_socket_count("u-1"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
