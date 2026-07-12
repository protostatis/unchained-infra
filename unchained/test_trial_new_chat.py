"""Focused backend, template, and browser-runtime tests for trial New Chat."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web as aiohttp_web

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from web_app import templates


def _js_function(source: str, name: str, *, async_function: bool = False) -> str:
    marker = f"{'async ' if async_function else ''}function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class TestTrialNewChatBackend(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from web_app.handlers import chat_flow

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_status_dir = chat_flow._TRIAL_NEW_CHAT_STATUS_DIR
        chat_flow._TRIAL_NEW_CHAT_STATUS_DIR = os.path.join(
            self.temp_dir.name, "transition-status"
        )
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()

    def tearDown(self):
        from web_app.handlers import chat_flow

        chat_flow._TRIAL_NEW_CHAT_STATUS_DIR = self.original_status_dir
        self.temp_dir.cleanup()

    def _core(self):
        return SimpleNamespace(
            _authenticate=lambda request: {"agent_id": "trial-agent"},
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested,
            _session_tabs={},
            _session_agent_map={},
            _session_allowed_tabs={},
            _session_profile_paths={},
            _session_last_active={},
            _chat_preview_generations={},
            _delete_trial_session=Mock(),
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            JWT_SECRET="test-jwt-secret",
            _attach_first_look_guest_cookies=Mock(),
        )

    def _new_request(self, request_id: str, session_id: str = "s-trial-agent-current"):
        return SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "session_id": session_id,
                    "slot": 2,
                }
            )
        )

    def _ack_request(
        self, request_id: str, old_session: str, new_session: str, commit_token: str
    ):
        return SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "commit_token": commit_token,
                    "previous_session_id": old_session,
                    "session_id": new_session,
                    "slot": 2,
                }
            )
        )

    def _core_with_persisted_history(self):
        core = self._core()
        deleted = []

        def session_path(session_id: str) -> str:
            return str(self.session_dir / f"{session_id}.json")

        def delete_session(session_id: str) -> None:
            deleted.append(session_id)
            try:
                os.remove(session_path(session_id))
            except FileNotFoundError:
                pass

        core._trial_session_path = session_path
        core._delete_trial_session = delete_session
        return core, deleted

    def _write_history(self, core, session_id: str, marker: str) -> Path:
        path = Path(core._trial_session_path(session_id))
        path.write_text(
            json.dumps({"messages": [{"role": "user", "content": marker}]}),
            encoding="utf-8",
        )
        return path

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_reservation_preserves_old_session_until_ack(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new

        old_session = "s-trial-agent-current"
        core = self._core()
        core._session_tabs[old_session] = "active-tab"
        core._session_agent_map[old_session] = "active-agent"
        mock_core.return_value = core

        response = await handle_chat_new(
            self._new_request("request-0000000000000001", old_session)
        )
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertFalse(data["replayed"])
        self.assertEqual(data["request_id"], "request-0000000000000001")
        self.assertEqual(data["commit_request_id"], data["request_id"])
        self.assertEqual(data["commit_issued_at"], 1234)
        self.assertEqual(data["commit_expires_at"], 1234 + 24 * 60 * 60)
        self.assertRegex(data["commit_token"], r"^1234\.87634\.[a-f0-9]{64}$")
        self.assertEqual(data["previous_session_id"], old_session)
        self.assertEqual(data["active_slot"], 2)
        self.assertNotEqual(data["session_id"], old_session)
        self.assertEqual(core._session_tabs, {old_session: "active-tab"})
        self.assertEqual(core._session_agent_map, {old_session: "active-agent"})
        core._delete_trial_session.assert_not_called()

    @patch("web_app.handlers.chat_flow._core")
    async def test_lost_response_retry_replays_commit_then_ack_is_idempotent(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000002"
        old_session = "s-trial-agent-current"
        other_session = "s-trial-agent-other"
        core = self._core()
        core._session_tabs.update({old_session: "active-tab", other_session: "other-tab"})
        core._session_agent_map.update({old_session: "active-agent", other_session: "other-agent"})
        core._session_allowed_tabs[old_session] = {"active-tab"}
        core._session_profile_paths[old_session] = "/profiles/trial"
        core._session_last_active[old_session] = 100.0
        mock_core.return_value = core

        first = await handle_chat_new(self._new_request(request_id, old_session))
        first_data = json.loads(first.body.decode())
        # The first response is intentionally ignored to model transport loss.
        retry = await handle_chat_new(self._new_request(request_id, old_session))
        retry_data = json.loads(retry.body.decode())

        self.assertTrue(retry_data["replayed"])
        self.assertEqual(retry_data["session_id"], first_data["session_id"])
        self.assertIn(old_session, core._session_tabs)
        core._delete_trial_session.assert_not_called()

        from web_app.handlers import chat_flow

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()

        forged = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                retry_data["session_id"],
                retry_data["commit_token"][:-1]
                + ("0" if retry_data["commit_token"][-1] != "0" else "1"),
            )
        )
        self.assertEqual(forged.status, 409)
        core._delete_trial_session.assert_not_called()

        ack = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                retry_data["session_id"],
                retry_data["commit_token"],
            )
        )
        ack_data = json.loads(ack.body.decode())

        self.assertTrue(ack_data["acknowledged"])
        self.assertNotIn(old_session, core._session_tabs)
        self.assertEqual(core._session_tabs[retry_data["session_id"]], "active-tab")
        self.assertEqual(core._session_tabs[other_session], "other-tab")
        self.assertEqual(core._session_agent_map[other_session], "other-agent")
        self.assertEqual(core._session_allowed_tabs[retry_data["session_id"]], {"active-tab"})
        self.assertEqual(core._session_profile_paths[retry_data["session_id"]], "/profiles/trial")
        core._delete_trial_session.assert_called_once_with(old_session)

        repeated_ack = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                retry_data["session_id"],
                retry_data["commit_token"],
            )
        )
        self.assertEqual(repeated_ack.status, 200)
        core._delete_trial_session.assert_called_once_with(old_session)

        request_count = len(chat_flow._trial_new_chat_requests)
        source_count = len(chat_flow._trial_new_chat_sources)
        alias_ack = await handle_chat_new_ack(
            self._ack_request(
                "request-0000000000000099",
                old_session,
                retry_data["session_id"],
                retry_data["commit_token"],
            )
        )
        self.assertEqual(alias_ack.status, 409)
        self.assertEqual(len(chat_flow._trial_new_chat_requests), request_count)
        self.assertEqual(len(chat_flow._trial_new_chat_sources), source_count)
        core._delete_trial_session.assert_called_once_with(old_session)

    @patch("web_app.handlers.chat_flow._core")
    async def test_pruned_commit_token_cannot_be_acknowledged_after_expiry(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000005"
        old_session = "s-trial-agent-current"
        core = self._core()
        mock_core.return_value = core
        reservation = await handle_chat_new(self._new_request(request_id, old_session))
        data = json.loads(reservation.body.decode())
        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()

        issued_at, expires_at, signature = data["commit_token"].split(".")
        tampered_token = (
            f"{int(issued_at) + 1}.{int(expires_at) + 1}.{signature}"
        )
        tampered = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                data["session_id"],
                tampered_token,
            )
        )
        self.assertEqual(tampered.status, 409)

        core.time = SimpleNamespace(
            time=lambda: data["commit_expires_at"] + 1
        )

        expired = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                data["session_id"],
                data["commit_token"],
            )
        )

        self.assertEqual(expired.status, 409)
        self.assertEqual(
            json.loads(expired.body.decode())["error"],
            "New-chat commit token expired",
        )
        expired_data = json.loads(expired.body.decode())
        self.assertEqual(expired_data["recovery_decision"], "source")
        self.assertEqual(expired_data["recovery_session_id"], old_session)
        self.assertTrue(expired_data["recovery_terminal"])
        core._delete_trial_session.assert_not_called()
        self.assertEqual(chat_flow._trial_new_chat_requests, {})
        self.assertEqual(chat_flow._trial_new_chat_sources, {})

    @patch("web_app.handlers.chat_flow._core")
    async def test_expired_uncommitted_transition_authoritatively_keeps_source(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000006"
        old_session = "s-trial-agent-uncommitted"
        core, deleted = self._core_with_persisted_history()
        old_history = self._write_history(core, old_session, "source history")
        core._session_tabs[old_session] = "source-tab"
        core._session_agent_map[old_session] = "source-agent"
        core._session_allowed_tabs[old_session] = {"source-tab"}
        mock_core.return_value = core

        reservation = await handle_chat_new(self._new_request(request_id, old_session))
        data = json.loads(reservation.body.decode())
        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()
        core.time = SimpleNamespace(time=lambda: data["commit_expires_at"] + 1)

        recovery = await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )
        recovery_data = json.loads(recovery.body.decode())

        self.assertEqual(recovery.status, 409)
        self.assertEqual(recovery_data["recovery_decision"], "source")
        self.assertEqual(recovery_data["recovery_session_id"], old_session)
        self.assertEqual(
            json.loads(old_history.read_text(encoding="utf-8"))["messages"][0]["content"],
            "source history",
        )
        self.assertEqual(core._session_tabs, {old_session: "source-tab"})
        self.assertEqual(core._session_agent_map, {old_session: "source-agent"})
        self.assertEqual(core._session_allowed_tabs, {old_session: {"source-tab"}})
        self.assertEqual(deleted, [])

    @patch("web_app.handlers.chat_flow._core")
    async def test_expired_committed_transition_authoritatively_keeps_destination(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000007"
        old_session = "s-trial-agent-committed"
        core, deleted = self._core_with_persisted_history()
        old_history = self._write_history(core, old_session, "history to archive")
        core._session_tabs[old_session] = "migrated-tab"
        core._session_agent_map[old_session] = "migrated-agent"
        core._session_allowed_tabs[old_session] = {"migrated-tab"}
        mock_core.return_value = core

        reservation = await handle_chat_new(self._new_request(request_id, old_session))
        data = json.loads(reservation.body.decode())
        # The ACK commits, but its successful response is lost to the browser.
        committed = await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )
        self.assertEqual(committed.status, 200)
        self.assertFalse(old_history.exists())
        destination_history = self._write_history(
            core, data["session_id"], "fresh destination history"
        )
        self.assertEqual(deleted, [old_session])

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()
        core.time = SimpleNamespace(time=lambda: data["commit_expires_at"] + 1)
        recovery = await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )
        recovery_data = json.loads(recovery.body.decode())

        self.assertEqual(recovery.status, 409)
        self.assertEqual(recovery_data["recovery_decision"], "destination")
        self.assertEqual(recovery_data["recovery_session_id"], data["session_id"])
        self.assertFalse(old_history.exists())
        self.assertEqual(
            json.loads(destination_history.read_text(encoding="utf-8"))["messages"][0]["content"],
            "fresh destination history",
        )
        self.assertEqual(core._session_tabs, {data["session_id"]: "migrated-tab"})
        self.assertEqual(core._session_agent_map, {data["session_id"]: "migrated-agent"})
        self.assertEqual(
            core._session_allowed_tabs,
            {data["session_id"]: {"migrated-tab"}},
        )
        self.assertEqual(deleted, [old_session])

    @patch("web_app.handlers.chat_flow._core")
    async def test_ttl_pruned_status_falls_back_to_destination_without_mutation(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000008"
        old_session = "s-trial-agent-ttl-pruned"
        core = self._core()
        core._session_tabs[old_session] = "source-tab"
        core._session_agent_map[old_session] = "source-agent"
        mock_core.return_value = core
        reservation = await handle_chat_new(self._new_request(request_id, old_session))
        data = json.loads(reservation.body.decode())

        recovery_time = (
            data["commit_expires_at"] + chat_flow._TRIAL_NEW_CHAT_STATUS_TTL + 1
        )
        chat_flow._prune_trial_new_chat_statuses(recovery_time)
        core.time = SimpleNamespace(time=lambda: recovery_time)
        recovery = await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )
        recovery_data = json.loads(recovery.body.decode())

        self.assertEqual(recovery.status, 409)
        self.assertTrue(recovery_data["recovery_terminal"])
        self.assertEqual(recovery_data["recovery_decision"], "destination")
        self.assertEqual(recovery_data["recovery_session_id"], data["session_id"])
        self.assertEqual(core._session_tabs, {old_session: "source-tab"})
        self.assertEqual(core._session_agent_map, {old_session: "source-agent"})
        core._delete_trial_session.assert_not_called()
        self.assertNotIn(
            ("trial-agent", "request-0000000000000008"),
            chat_flow._trial_new_chat_requests,
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_cap_evicted_status_falls_back_to_destination_without_mutation(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        clock = [1234.5]
        core = self._core()
        core.time = SimpleNamespace(time=lambda: clock[0])
        old_session = "s-trial-agent-cap-evicted"
        core._session_tabs[old_session] = "source-tab"
        core._session_agent_map[old_session] = "source-agent"
        mock_core.return_value = core
        first = await handle_chat_new(
            self._new_request("request-0000000000000009", old_session)
        )
        first_data = json.loads(first.body.decode())

        clock[0] += 1
        with patch.object(chat_flow, "_TRIAL_NEW_CHAT_STATUS_MAX_RECORDS", 1):
            second = await handle_chat_new(
                self._new_request(
                    "request-0000000000000030", "s-trial-agent-newer-status"
                )
            )
        self.assertEqual(second.status, 200)
        self.assertFalse(
            Path(
                chat_flow._trial_new_chat_status_path(first_data["commit_token"])
            ).exists()
        )

        recovery = await handle_chat_new_ack(
            self._ack_request(
                "request-0000000000000009",
                old_session,
                first_data["session_id"],
                first_data["commit_token"],
            )
        )
        recovery_data = json.loads(recovery.body.decode())

        self.assertEqual(recovery.status, 409)
        self.assertTrue(recovery_data["recovery_terminal"])
        self.assertEqual(recovery_data["recovery_decision"], "destination")
        self.assertEqual(
            recovery_data["recovery_session_id"], first_data["session_id"]
        )
        self.assertEqual(core._session_tabs, {old_session: "source-tab"})
        self.assertEqual(core._session_agent_map, {old_session: "source-agent"})
        core._delete_trial_session.assert_not_called()
        self.assertNotIn(
            ("trial-agent", "request-0000000000000009"),
            chat_flow._trial_new_chat_requests,
        )

    async def test_status_cleanup_context_prunes_and_awaits_its_task(self):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new

        core = self._core()
        with patch("web_app.handlers.chat_flow._core", return_value=core):
            reservation = await handle_chat_new(
                self._new_request(
                    "request-0000000000000031", "s-trial-agent-cleanup"
                )
            )
        self.assertEqual(reservation.status, 200)
        self.assertTrue(
            list(Path(chat_flow._TRIAL_NEW_CHAT_STATUS_DIR).glob("*.json"))
        )

        app = aiohttp_web.Application()
        app.cleanup_ctx.append(chat_flow.trial_new_chat_status_cleanup_context)
        runner = aiohttp_web.AppRunner(app)
        cleanup_task = None
        try:
            await runner.setup()
            cleanup_task = app[chat_flow._TRIAL_NEW_CHAT_STATUS_CLEANUP_TASK_KEY]
            await asyncio.sleep(0.01)
            self.assertEqual(
                list(Path(chat_flow._TRIAL_NEW_CHAT_STATUS_DIR).glob("*.json")),
                [],
            )
            self.assertFalse(cleanup_task.done())
        finally:
            await runner.cleanup()

        self.assertIsNotNone(cleanup_task)
        self.assertTrue(cleanup_task.done())
        self.assertNotIn(
            "trial-new-chat-status-cleanup",
            {task.get_name() for task in asyncio.all_tasks()},
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_competing_requests_for_same_source_replay_one_transition(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new

        old_session = "s-trial-agent-current"
        core = self._core()
        mock_core.return_value = core

        first = await handle_chat_new(
            self._new_request("request-0000000000000003", old_session)
        )
        later = await handle_chat_new(
            self._new_request("request-0000000000000004", old_session)
        )
        first_data = json.loads(first.body.decode())
        later_data = json.loads(later.body.decode())

        self.assertEqual(later_data["request_id"], "request-0000000000000004")
        self.assertEqual(later_data["commit_request_id"], "request-0000000000000003")
        self.assertTrue(later_data["replayed"])
        self.assertEqual(later_data["session_id"], first_data["session_id"])
        self.assertEqual(len(chat_flow._trial_new_chat_requests), 1)
        self.assertEqual(len(chat_flow._trial_new_chat_sources), 1)
        core._delete_trial_session.assert_not_called()

        reused = await handle_chat_new(
            self._new_request(
                "request-0000000000000003", "s-trial-agent-different"
            )
        )
        self.assertEqual(reused.status, 409)

    @patch("web_app.handlers.chat_flow._core")
    async def test_guest_reset_needs_no_request_id_and_is_rate_limited(self, mock_core):
        from rate_limit import SlidingWindowRateLimiter
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new

        core = self._core()
        guest_auth = {"agent_id": "guest-abcd1234"}
        core._first_look_guest_auth = lambda request: (guest_auth, "guest-id", 0)
        core._session_tabs["s-guest-abcd1234-current"] = "guest-tab"
        mock_core.return_value = core
        chat_flow._FIRST_LOOK_PUBLIC_RATE_LIMITER = SlidingWindowRateLimiter()
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "session_id": "s-guest-abcd1234-current",
                    "first_look_guest": True,
                }
            ),
            headers={"X-Forwarded-For": "203.0.113.44"},
            remote="203.0.113.44",
        )

        with patch.object(chat_flow, "_FIRST_LOOK_NEW_CHAT_LIMIT", 1):
            response = await handle_chat_new(request)
            limited = await handle_chat_new(request)

        data = json.loads(response.body.decode())
        self.assertEqual(response.status, 200)
        self.assertTrue(data["guest"])
        self.assertTrue(data["session_id"].startswith("s-guest-abcd1234-"))
        self.assertEqual(limited.status, 429)
        self.assertEqual(
            core._session_tabs, {"s-guest-abcd1234-current": "guest-tab"}
        )
        core._delete_trial_session.assert_not_called()
        self.assertEqual(chat_flow._trial_new_chat_sources, {})
        self.assertEqual(chat_flow._trial_new_chat_requests, {})

    @patch("web_app.handlers.chat_flow._core")
    async def test_pending_reservations_are_bounded_and_expired_records_prune(self, mock_core):
        from web_app.handlers import chat_flow
        from web_app.handlers.chat_flow import handle_chat_new

        core = self._core()
        mock_core.return_value = core
        with patch.object(chat_flow, "_TRIAL_NEW_CHAT_MAX_PENDING_PER_AGENT", 2):
            first = await handle_chat_new(
                self._new_request("request-0000000000000010", "s-trial-agent-one")
            )
            second = await handle_chat_new(
                self._new_request("request-0000000000000011", "s-trial-agent-two")
            )
            limited = await handle_chat_new(
                self._new_request("request-0000000000000012", "s-trial-agent-three")
            )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(limited.status, 429)
        self.assertLessEqual(len(chat_flow._trial_new_chat_sources), 2)
        self.assertLessEqual(len(chat_flow._trial_new_chat_requests), 2)

        chat_flow._prune_trial_new_chat_commits(
            1234.5 + chat_flow._TRIAL_NEW_CHAT_COMMIT_TTL + 1
        )
        self.assertEqual(chat_flow._trial_new_chat_sources, {})
        self.assertEqual(chat_flow._trial_new_chat_requests, {})
        self.assertEqual(
            len(list(Path(chat_flow._TRIAL_NEW_CHAT_STATUS_DIR).glob("*.json"))),
            2,
        )
        chat_flow._prune_trial_new_chat_statuses(
            1234.5
            + chat_flow._TRIAL_NEW_CHAT_COMMIT_TTL
            + chat_flow._TRIAL_NEW_CHAT_STATUS_TTL
            + 1
        )
        self.assertEqual(
            list(Path(chat_flow._TRIAL_NEW_CHAT_STATUS_DIR).glob("*.json")),
            [],
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_late_ack_does_not_overwrite_new_session_resources(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000020"
        old_session = "s-trial-agent-current"
        core = self._core()
        core._session_tabs[old_session] = "old-tab"
        core._session_agent_map[old_session] = "old-agent"
        mock_core.return_value = core
        reservation = await handle_chat_new(self._new_request(request_id, old_session))
        data = json.loads(reservation.body.decode())
        core._session_tabs[data["session_id"]] = "new-tab"
        core._session_agent_map[data["session_id"]] = "new-agent"

        ack = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                data["session_id"],
                data["commit_token"],
            )
        )

        self.assertEqual(ack.status, 200)
        self.assertEqual(core._session_tabs[data["session_id"]], "new-tab")
        self.assertEqual(core._session_agent_map[data["session_id"]], "new-agent")
        self.assertEqual(core._session_tabs[old_session], "old-tab")
        self.assertEqual(core._session_agent_map[old_session], "old-agent")


class TestTrialNewChatTemplate(unittest.TestCase):
    def test_trial_template_has_accessible_transactional_feedback(self):
        html = templates.TRIAL_CHAT_HTML

        self.assertIn('id="new-chat-feedback" role="status" aria-live="polite"', html)
        self.assertIn("if (sending || newChatPending) return;", html)
        self.assertIn("const previousSessionId = sessionId;", html)
        self.assertIn("const data = await r.json().catch(() => ({}));", html)
        self.assertIn("nextSessionId === previousSessionId", html)
        self.assertIn("request_id: pending.request_id", html)
        self.assertIn("/web/chat/new/ack", html)
        self.assertIn("data.request_id !== pending.request_id", html)
        self.assertIn("data.commit_request_id", html)
        self.assertIn("data.commit_token", html)
        self.assertIn("[0-9]{1,12}\\.[0-9]{1,12}\\.[a-f0-9]{64}", html)
        self.assertIn("data.recovery_terminal === true", html)
        self.assertIn("await recoverExpiredNewChat(pending, outcome);", html)
        self.assertIn("decision === 'destination'", html)
        self.assertIn("Your previous chat was restored; select New Chat to try again.", html)
        self.assertIn("_newChatRecoveryBlocked()", html)
        self.assertIn("Finish recovering the new chat", html)
        self.assertIn("recoverPendingNewChat();", html)
        self.assertIn("resetNewChatUi();", html)
        self.assertIn("Your current chat is unchanged.", html)


class TestTrialNewChatRuntime(unittest.TestCase):
    def test_lost_response_retry_ack_recovery_and_stale_response_ordering(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the browser-runtime harness")

        html = templates.TRIAL_CHAT_HTML
        # These extracted functions intentionally run together as one contract;
        # update this list when the inline New Chat runtime gains a dependency.
        runtime = "\n".join(
            [
                _js_function(html, "_newChatStateKey"),
                _js_function(html, "_newChatRequestId"),
                _js_function(html, "_loadPendingNewChat"),
                _js_function(html, "_savePendingNewChat"),
                _js_function(html, "_clearPendingNewChat"),
                _js_function(html, "_newChatRecoveryBlocked"),
                _js_function(html, "_syncNewChatRecoveryLock"),
                _js_function(html, "acknowledgeNewChatTransition", async_function=True),
                _js_function(html, "recoverExpiredNewChat", async_function=True),
                _js_function(html, "recoverPendingNewChat", async_function=True),
                _js_function(html, "updateSendAvailability"),
                _js_function(html, "setNewChatFeedback"),
                _js_function(html, "resetNewChatUi"),
                _js_function(html, "doNewChat", async_function=True),
                _js_function(html, "doSend", async_function=True),
            ]
        )
        harness = f"""
const assert = require('assert');
function classList() {{
  const values = new Set();
  return {{
    add: v => values.add(v),
    remove: v => values.delete(v),
    contains: v => values.has(v),
    toggle: (v, force) => force === undefined ? (values.has(v) ? values.delete(v) : values.add(v)) : (force ? values.add(v) : values.delete(v)),
  }};
}}
const storage = new Map();
global.localStorage = {{
  getItem: key => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: key => storage.delete(key),
}};
let generatedRequest = 0;
Object.defineProperty(globalThis, 'crypto', {{
  configurable: true,
  value: {{randomUUID: () => 'request-000000000000000' + (++generatedRequest)}},
}});
const elements = {{
  'new-chat-feedback': {{textContent:'', className:'', attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}}},
  chat: {{innerHTML:'existing history'}},
  msginput: {{value:'draft prompt', style:{{height:'88px'}}, disabled:false, placeholder:''}},
  'agent-action': {{textContent:'Browsing'}},
  'turn-ctr': {{textContent:'t4'}},
  'agent-bar': {{classList:classList()}},
  sendbtn: {{style:{{display:'none'}}, disabled:false, title:'', classList:classList(), attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}}},
  cancelbtn: {{style:{{display:'block'}}}},
  slotbar: {{classList:classList()}},
}};
elements['agent-bar'].classList.add('active');
global.document = {{getElementById: id => elements[id]}};
let agentId = 'trial-agent';
let sessionId = 's-trial-agent-old';
let activeSlot = 2;
let sending = false;
let newChatPending = false;
let lastLocalSetupReady = true;
let _cancelCtrl = {{stale:true}};
let _turnCount = 4;
let _navTrail = ['example.com'];
let persisted = '';
let activePersisted = '';
let hintsShown = 0;
let syncCount = 0;
let sendAttemptAdvanced = 0;
let historyLoads = 0;
function currentModel() {{ return 'google/gemini-3.1-flash-lite'; }}
function _sessionStoreKey() {{ return 'unchained_session_trial-agent_openrouter'; }}
function _slotLabel(slot) {{ return ['Lane A','Lane B','Lane C'][slot - 1]; }}
function _persistSessionId(value) {{ persisted = value; }}
function _setActiveSlotSession(value) {{ activePersisted = value; }}
function _syncSlotButtons() {{ syncCount += 1; }}
function loadSidebarHistory() {{}}
async function loadHistory() {{ historyLoads += 1; elements.chat.innerHTML = 'restored history for ' + sessionId; }}
function showHintsIfEmpty() {{ hintsShown += 1; elements.chat.innerHTML = 'fresh hints'; }}
function _finalizeGroup() {{}}
function renderNavTrail() {{}}
function _incTrialMsgCount() {{ sendAttemptAdvanced += 1; }}
{runtime}

function restoreCurrentChat(sid) {{
  sessionId = sid;
  elements.chat.innerHTML = 'existing history';
  elements.msginput.value = 'draft prompt';
  elements.msginput.style.height = '88px';
  elements['agent-action'].textContent = 'Browsing';
  elements['new-chat-feedback'].textContent = '';
  persisted = '';
  activePersisted = '';
}}

(async () => {{
  let mode = 'lost-first';
  let ackMode = 'success';
  let reservedSession = '';
  const reserveRequestIds = [];
  let ackCalls = 0;
  global.fetch = async (url, options) => {{
    const body = JSON.parse(options.body);
    if (url === '/web/chat/new/ack') {{
      ackCalls += 1;
      if (ackMode === 'lost') throw new Error('ack response lost');
      if (ackMode === 'expired-source') return {{ok:false, status:409, json:async () => ({{
        error:'New-chat commit token expired', recovery_terminal:true,
        recovery_decision:'source', recovery_session_id:body.previous_session_id,
      }})}};
      if (ackMode === 'expired-destination') return {{ok:false, status:409, json:async () => ({{
        error:'New-chat transition status unavailable', recovery_terminal:true,
        recovery_decision:'destination', recovery_session_id:body.session_id,
      }})}};
      return {{ok:true, json:async () => ({{
        ok:true, acknowledged:true, request_id:body.request_id,
        previous_session_id:body.previous_session_id, session_id:body.session_id,
      }})}};
    }}
    assert.strictEqual(url, '/web/chat/new');
    assert.strictEqual(elements.chat.innerHTML, 'existing history', 'history cleared before reservation response');
    reserveRequestIds.push(body.request_id);
    if (!reservedSession) reservedSession = 's-trial-agent-fresh-' + generatedRequest;
    if (mode === 'lost-first') {{
      mode = 'replay';
      throw new Error('reservation response lost');
    }}
    const responseRequestId = mode === 'stale' ? 'request-stale-00000000000' : body.request_id;
    return {{ok:true, json:async () => ({{
      ok:true, request_id:responseRequestId, commit_request_id:body.request_id,
      commit_token:'1234.87634.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      previous_session_id:body.session_id,
      session_id:reservedSession, active_slot:body.slot, replayed:mode === 'replay',
    }})}};
  }};

  // The server committed, but the first response was lost. UI and draft remain,
  // and the retry reuses the persisted request ID to recover that exact commit.
  restoreCurrentChat('s-trial-agent-old');
  await doNewChat();
  const ambiguous = _loadPendingNewChat();
  assert.ok(ambiguous);
  assert.strictEqual(ambiguous.session_id, '');
  assert.strictEqual(sessionId, 's-trial-agent-old');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.strictEqual(elements.msginput.value, 'draft prompt');
  assert.match(elements['new-chat-feedback'].textContent, /retry safely/);

  await doNewChat();
  assert.strictEqual(reserveRequestIds[0], reserveRequestIds[1]);
  assert.strictEqual(sessionId, reservedSession);
  assert.strictEqual(persisted, sessionId);
  assert.strictEqual(activePersisted, sessionId);
  assert.strictEqual(activeSlot, 2);
  assert.strictEqual(elements.chat.innerHTML, 'fresh hints');
  assert.strictEqual(elements.msginput.value, '');
  assert.strictEqual(elements.msginput.style.height, 'auto');
  assert.strictEqual(elements['agent-action'].textContent, '');
  assert.strictEqual(elements['turn-ctr'].textContent, '');
  assert.strictEqual(elements['new-chat-feedback'].className, 'success');
  assert.match(elements['new-chat-feedback'].textContent, /Fresh Lane B ready/);
  assert.strictEqual(hintsShown, 1);
  assert.strictEqual(syncCount, 1);
  assert.strictEqual(newChatPending, false);
  assert.strictEqual(elements.slotbar.classList.contains('locked'), false);
  assert.strictEqual(elements.sendbtn.disabled, false);
  assert.strictEqual(_loadPendingNewChat(), null, 'successful ack clears recovery state');

  // If the destructive ack commits but its response is lost, the client has
  // already adopted the new session and retains enough state to retry the ack.
  storage.clear();
  reservedSession = '';
  mode = 'normal';
  ackMode = 'lost';
  restoreCurrentChat('s-trial-agent-second');
  await doNewChat();
  const unacked = _loadPendingNewChat();
  assert.strictEqual(sessionId, reservedSession);
  assert.strictEqual(unacked.session_id, reservedSession);
  assert.strictEqual(elements['new-chat-feedback'].className, 'error');
  assert.strictEqual(elements.sendbtn.disabled, true);
  assert.strictEqual(elements.msginput.disabled, true);
  assert.strictEqual(elements.slotbar.classList.contains('locked'), true);

  elements.msginput.value = 'must not send';
  await doSend();
  assert.strictEqual(sendAttemptAdvanced, 0, 'send advanced while ACK recovery was pending');

  const reservationsBeforeRetry = reserveRequestIds.length;
  const ackCallsBeforeRetry = ackCalls;
  await doNewChat();
  assert.strictEqual(reserveRequestIds.length, reservationsBeforeRetry, 'second New Chat created another reservation');
  assert.strictEqual(ackCalls, ackCallsBeforeRetry + 1, 'second New Chat did not retry ACK');
  assert.strictEqual(_loadPendingNewChat().request_id, unacked.request_id);
  assert.strictEqual(elements.sendbtn.disabled, true);

  ackMode = 'success';
  await recoverPendingNewChat();
  assert.strictEqual(_loadPendingNewChat(), null, 'reload recovery retries lost ack');
  assert.strictEqual(elements.sendbtn.disabled, false);
  assert.strictEqual(elements.msginput.disabled, false);
  assert.strictEqual(elements.slotbar.classList.contains('locked'), false);

  // If an ACK was lost and the signed reservation expires before recovery,
  // rollback is terminal: restore the prior session and never retry that ACK.
  storage.clear();
  reservedSession = '';
  mode = 'normal';
  ackMode = 'lost';
  restoreCurrentChat('s-trial-agent-expiring');
  await doNewChat();
  const expiring = _loadPendingNewChat();
  assert.strictEqual(sessionId, expiring.session_id);
  assert.strictEqual(elements.sendbtn.disabled, true);
  assert.strictEqual(elements.msginput.disabled, true);

  ackMode = 'expired-source';
  const ackCallsBeforeExpiry = ackCalls;
  const historyLoadsBeforeExpiry = historyLoads;
  await recoverPendingNewChat();
  assert.strictEqual(ackCalls, ackCallsBeforeExpiry + 1);
  assert.strictEqual(_loadPendingNewChat(), null, 'expired recovery state was retained');
  assert.strictEqual(sessionId, expiring.previous_session_id);
  assert.strictEqual(persisted, expiring.previous_session_id);
  assert.strictEqual(activePersisted, expiring.previous_session_id);
  assert.strictEqual(historyLoads, historyLoadsBeforeExpiry + 1);
  assert.strictEqual(elements.chat.innerHTML, 'restored history for ' + expiring.previous_session_id);
  assert.strictEqual(elements.sendbtn.disabled, false);
  assert.strictEqual(elements.msginput.disabled, false);
  assert.strictEqual(elements.slotbar.classList.contains('locked'), false);
  assert.match(elements['new-chat-feedback'].textContent, /expired before confirmation.*previous chat was restored.*try again/i);
  await recoverPendingNewChat();
  assert.strictEqual(ackCalls, ackCallsBeforeExpiry + 1, 'expired ACK was retried after terminal recovery');

  // If the server confirms that the ACK committed before its response was
  // lost, terminal recovery must retain the destination and its resources.
  storage.clear();
  reservedSession = '';
  mode = 'normal';
  ackMode = 'lost';
  restoreCurrentChat('s-trial-agent-committed-source');
  await doNewChat();
  const committedPending = _loadPendingNewChat();
  const committedDestination = committedPending.session_id;
  ackMode = 'expired-destination';
  const ackCallsBeforeCommittedRecovery = ackCalls;
  await recoverPendingNewChat();
  assert.strictEqual(ackCalls, ackCallsBeforeCommittedRecovery + 1);
  assert.strictEqual(_loadPendingNewChat(), null);
  assert.strictEqual(sessionId, committedDestination);
  assert.strictEqual(persisted, committedDestination);
  assert.strictEqual(activePersisted, committedDestination);
  assert.strictEqual(elements.chat.innerHTML, 'restored history for ' + committedDestination);
  assert.strictEqual(elements.sendbtn.disabled, false);
  assert.strictEqual(elements.msginput.disabled, false);
  assert.strictEqual(elements.slotbar.classList.contains('locked'), false);
  assert.strictEqual(elements['new-chat-feedback'].className, 'success');
  assert.match(elements['new-chat-feedback'].textContent, /confirmed before its response was lost.*fresh chat was restored/i);
  await recoverPendingNewChat();
  assert.strictEqual(ackCalls, ackCallsBeforeCommittedRecovery + 1, 'committed expired ACK was retried');

  // A stale response from an older request cannot replace the active session.
  storage.clear();
  reservedSession = '';
  mode = 'stale';
  ackMode = 'success';
  restoreCurrentChat('s-trial-agent-third');
  await doNewChat();
  const stalePending = _loadPendingNewChat();
  assert.strictEqual(sessionId, 's-trial-agent-third');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.strictEqual(elements.msginput.value, 'draft prompt');
  assert.match(elements['new-chat-feedback'].textContent, /stale or invalid/);
  mode = 'replay';
  await doNewChat();
  assert.strictEqual(reserveRequestIds.at(-1), stalePending.request_id);
  assert.strictEqual(sessionId, reservedSession);
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
