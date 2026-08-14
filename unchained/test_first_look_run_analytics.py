"""Behavioral contracts for server-owned First Look run analytics."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from web_app.handlers import chat_stream


class _Request:
    def __init__(self, body: dict):
        self._body = body
        self.path = "/web/chat"
        self.headers = {}

    async def json(self):
        return self._body


class _StreamResponse:
    def __init__(self, *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.writes: list[bytes] = []
        self.prepared = False

    async def prepare(self, _request):
        self.prepared = True
        return self

    async def write(self, data: bytes):
        self.writes.append(data)


class _DisconnectedStreamResponse(_StreamResponse):
    async def prepare(self, _request):
        raise ConnectionResetError("client closed")


class _CancelledStreamResponse(_StreamResponse):
    async def prepare(self, _request):
        raise asyncio.CancelledError


class _AgentSocket:
    def __init__(
        self,
        core,
        events: list[dict] | None = None,
        *,
        fail_send=False,
        disconnect_after_send=False,
    ):
        self.core = core
        self.events = list(events or [])
        self.fail_send = fail_send
        self.disconnect_after_send = disconnect_after_send
        self.closed = False
        self.messages: list[dict] = []

    async def send_json(self, message: dict):
        self.messages.append(dict(message))
        if self.fail_send:
            raise ConnectionError("agent unavailable")
        queue = self.core._response_queues[message["session_id"]]
        for raw_event in self.events:
            event = dict(raw_event)
            event.setdefault("session_id", message["session_id"])
            event.setdefault("req_id", message["req_id"])
            await queue.put(event)
        if self.disconnect_after_send:
            self.closed = True
            self.core._chat_agents.pop("trial-agent", None)


class FirstLookRunAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    REQUEST_ID = "request-secret-id"
    SESSION_ID = "s-guest-abc12345-run00001"
    RESULT_ID = "ag0000000001"

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "auth.db")

    def tearDown(self):
        self._temp_dir.cleanup()

    def _body(self, **overrides) -> dict:
        body = {
            "message": "private prompt must never enter analytics",
            "agent_id": "guest-abc12345",
            "session_id": self.SESSION_ID,
            "model": "google/gemini-3.1-flash-lite",
            "headless": True,
            "first_look_guest": True,
            "ref": "searchagentsky-result",
            "task": "search-result",
            "from_result": self.RESULT_ID,
            "untrusted_payload": {"secret": "arbitrary-body-secret"},
        }
        body.update(overrides)
        return body

    def _core(
        self,
        *,
        events: list[dict] | None = None,
        fail_send: bool = False,
        disconnect_after_send: bool = False,
        headless_agent_id: str = "headless-agent",
        quota_count: int = 0,
    ):
        track_calls: list[dict] = []

        def track_event(_request, event, **kwargs):
            captured = dict(kwargs)
            captured["meta"] = dict(kwargs.get("meta") or {})
            track_calls.append({"event": event, **captured})
            return True

        async def ensure_session_tab(_session_id, _agent_id):
            return "tab-guest-1"

        core = SimpleNamespace(
            HEADLESS_AGENT_ID=headless_agent_id,
            TRIAL_AGENT_ID="trial-agent",
            _FIRST_LOOK_GUEST_PROMPT_LIMIT=20,
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _OPENROUTER_TRIAL_FALLBACK_MODEL="nvidia/nemotron-3.5-lightning:free",
            _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS=(
                "nvidia/nemotron-3.5-lightning:free",
            ),
            _authenticate=lambda _request: None,
            _first_look_guest_auth=lambda _request: (
                {
                    "user_id": "",
                    "key_hash": "abc12345",
                    "agent_id": "guest-abc12345",
                    "email": "",
                    "user_type": "guest",
                },
                "guest-id-not-analytics",
                quota_count,
            ),
            _attach_first_look_guest_cookies=lambda *_args, **_kwargs: None,
            _request_id=lambda _request: self.REQUEST_ID,
            _trace=lambda *_args, **_kwargs: None,
            _track_event=track_event,
            _analytics_session_id_from_request=lambda _request: "analytics-session",
            _analytics_page_view_id_from_request=lambda _request: "analytics-page-view",
            _analytics_route_from_request=lambda _request: "/first-look",
            _is_pending_user=lambda _auth: False,
            _is_claude_sdk_model=lambda _model: False,
            _is_codex_sdk_model=lambda _model: False,
            _is_codex_cli_model=lambda _model: False,
            _is_opencode_cli_model=lambda _model: False,
            _is_openrouter_model=lambda model: str(model).startswith("google/"),
            _is_openrouter_post_cap_allowed_model=lambda _model: True,
            _resolve_chat_agent_id=lambda auth, _model: auth["agent_id"],
            _mint_scheduler_turn_grant=lambda *_args: "",
            _ensure_session_tab=ensure_session_tab,
            _response_queues={},
            _response_req_ids={},
            _session_tabs={},
            _session_profile_paths={},
            _expired_profile_sessions={},
            _session_agent_map={},
            _session_last_active={},
            _session_agents={},
            _overlay_sessions={},
            _scheduler_turn_grants={},
            _chat_turns=None,
            _auth=SimpleNamespace(db_path=self._db_path),
        )
        socket = _AgentSocket(
            core,
            events,
            fail_send=fail_send,
            disconnect_after_send=disconnect_after_send,
        )
        core._chat_agents = {"trial-agent": socket}
        core.track_calls = track_calls
        core.socket = socket
        return core

    async def _run(self, core, body: dict | None = None, response_cls=_StreamResponse):
        request = _Request(body or self._body())
        with (
            patch.object(chat_stream, "_core", return_value=core),
            patch.object(chat_stream.web, "StreamResponse", response_cls),
        ):
            response = await chat_stream.handle_chat_msg(request)
        return response

    def _events(self, core, name: str) -> list[dict]:
        return [event for event in core.track_calls if event["event"] == name]

    def test_search_attribution_requires_the_exact_bounded_triad(self):
        valid = chat_stream._first_look_search_attribution(self._body())
        self.assertEqual(
            valid,
            {
                "ref": "searchagentsky-result",
                "task": "search-result",
                "from_result": self.RESULT_ID,
            },
        )
        invalid_cases = (
            {"ref": "other-campaign"},
            {"task": "research"},
            {"from_result": "short"},
            {"from_result": "ag000000000!"},
            {"from_result": self.RESULT_ID + "extra"},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    chat_stream._first_look_search_attribution(
                        self._body(**overrides)
                    ),
                    {},
                )

    def test_queue_supersession_is_correlated_only_for_guest_first_look(self):
        guest_queue = asyncio.Queue(maxsize=8)
        chat_stream._signal_superseded_response_queue(
            guest_queue,
            guest_mode=True,
            session_id=self.SESSION_ID,
            req_id=self.REQUEST_ID,
        )
        self.assertEqual(
            [guest_queue.get_nowait(), guest_queue.get_nowait()],
            [
                {
                    "type": "cancelled",
                    "session_id": self.SESSION_ID,
                    "req_id": self.REQUEST_ID,
                },
                {
                    "type": "done",
                    "session_id": self.SESSION_ID,
                    "req_id": self.REQUEST_ID,
                },
            ],
        )

        legacy_queue = asyncio.Queue(maxsize=8)
        chat_stream._signal_superseded_response_queue(
            legacy_queue,
            guest_mode=False,
            session_id=self.SESSION_ID,
            req_id=self.REQUEST_ID,
        )
        self.assertEqual(legacy_queue.get_nowait(), {"type": "done"})
        self.assertTrue(legacy_queue.empty())

    async def test_success_records_accepted_then_one_completed_terminal(self):
        core = self._core(events=[{"type": "done"}])

        response = await self._run(core)

        self.assertTrue(response.prepared)
        accepted = self._events(core, "first_look_run_accepted")
        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(terminal), 1)
        dispatched = core.socket.messages[-1]
        self.assertEqual(dispatched["model"], "nvidia/nemotron-3.5-lightning:free")
        self.assertTrue(dispatched.get("billing_run_id"))
        from credit import CreditLedger
        run = CreditLedger(self._db_path).get_run(dispatched["billing_run_id"])
        self.assertEqual(run["user_id"], "system:first-look")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(terminal[0]["meta"]["outcome"], "completed")
        self.assertEqual(accepted[0]["meta"]["run_id"], terminal[0]["meta"]["run_id"])
        self.assertRegex(accepted[0]["meta"]["run_id"], re.compile(r"^[0-9a-f]{20}$"))
        self.assertEqual(terminal[0]["meta"]["from_result"], self.RESULT_ID)
        self.assertEqual(accepted[0]["source"], "server")
        self.assertFalse(self._events(core, "chat_message_send"))

        captured = json.dumps(core.track_calls, sort_keys=True)
        self.assertNotIn("private prompt", captured)
        self.assertNotIn("guest-id-not-analytics", captured)
        self.assertNotIn(self.REQUEST_ID, captured)
        self.assertNotIn("arbitrary-body-secret", captured)
        for event in accepted + terminal:
            self.assertNotIn("user_id", event)
            self.assertNotIn("email", event)

    async def test_error_then_done_records_only_error_terminal(self):
        core = self._core(
            events=[
                {"type": "error", "data": "raw provider exception must not persist"},
                {"type": "done"},
            ]
        )

        await self._run(core)

        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "error")
        self.assertEqual(terminal[0]["error_code"], "agent_error")
        self.assertNotIn("raw provider exception", json.dumps(terminal))

    async def test_cancelled_then_done_records_only_cancelled_terminal(self):
        core = self._core(events=[{"type": "cancelled"}, {"type": "done"}])

        await self._run(core)

        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "cancelled")

    async def test_stale_terminal_does_not_end_or_count_current_run(self):
        core = self._core(
            events=[
                {"type": "done", "req_id": "stale-request"},
                {"type": "done"},
            ]
        )

        response = await self._run(core)

        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "completed")
        streamed = b"".join(response.writes).decode()
        self.assertNotIn("stale-request", streamed)

    async def test_reused_caller_request_id_gets_distinct_server_run_ids(self):
        core = self._core(events=[{"type": "done"}])

        await self._run(core)
        await self._run(core)

        forwarded_req_ids = [message["req_id"] for message in core.socket.messages]
        self.assertEqual(len(forwarded_req_ids), 2)
        self.assertEqual(len(set(forwarded_req_ids)), 2)
        self.assertNotIn(self.REQUEST_ID, forwarded_req_ids)
        accepted = self._events(core, "first_look_run_accepted")
        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(terminal), 2)
        self.assertEqual(len({event["meta"]["run_id"] for event in accepted}), 2)

    async def test_connection_reset_after_dispatch_records_client_disconnected(self):
        core = self._core(events=[])

        await self._run(core, response_cls=_DisconnectedStreamResponse)

        self.assertEqual(len(self._events(core, "first_look_run_accepted")), 1)
        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "client_disconnected")
        self.assertEqual(terminal[0]["error_code"], "client_disconnected")

    async def test_handler_task_cancellation_records_one_terminal_then_reraises(self):
        core = self._core(events=[])

        with self.assertRaises(asyncio.CancelledError):
            await self._run(core, response_cls=_CancelledStreamResponse)

        self.assertEqual(len(self._events(core, "first_look_run_accepted")), 1)
        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "client_disconnected")
        self.assertEqual(terminal[0]["error_code"], "handler_cancelled")

    async def test_agent_disconnect_after_acceptance_records_one_error_terminal(self):
        core = self._core(disconnect_after_send=True)

        async def timeout_now(awaitable, timeout):
            del timeout
            awaitable.close()
            raise asyncio.TimeoutError

        with patch.object(chat_stream.asyncio, "wait_for", new=timeout_now):
            response = await self._run(core)

        self.assertTrue(response.prepared)
        self.assertEqual(len(self._events(core, "first_look_run_accepted")), 1)
        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "error")
        self.assertEqual(terminal[0]["error_code"], "agent_disconnected")

    async def test_agent_silence_timeout_records_one_error_terminal(self):
        core = self._core()

        async def timeout_now(awaitable, timeout):
            del timeout
            awaitable.close()
            raise asyncio.TimeoutError

        with (
            patch.object(chat_stream.asyncio, "wait_for", new=timeout_now),
            patch.object(chat_stream, "_FIRST_LOOK_AGENT_SILENCE_TIMEOUT_S", 0),
        ):
            await self._run(core)

        terminal = self._events(core, "first_look_run_terminal")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["meta"]["outcome"], "error")
        self.assertEqual(terminal[0]["error_code"], "agent_timeout")

    async def test_failed_agent_send_is_rejected_not_accepted(self):
        core = self._core(fail_send=True)

        response = await self._run(core)

        self.assertEqual(response.status, 502)
        rejected = self._events(core, "first_look_run_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "agent_dispatch_failed")
        self.assertEqual(rejected[0]["status_code"], 502)
        self.assertFalse(self._events(core, "first_look_run_accepted"))
        self.assertFalse(self._events(core, "first_look_run_terminal"))

    async def test_bounded_pre_dispatch_rejection_preserves_only_valid_attribution(self):
        core = self._core(headless_agent_id="")

        response = await self._run(core)

        self.assertEqual(response.status, 503)
        rejected = self._events(core, "first_look_run_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "headless_bridge_not_configured")
        self.assertEqual(
            set(rejected[0]["meta"]),
            {"run_id", "ref", "task", "from_result"},
        )
        self.assertNotIn(self.REQUEST_ID, json.dumps(rejected))

    async def test_hosted_user_prompt_limit_rejects_before_agent_dispatch(self):
        core = self._core()
        prompt = "x" * 21

        with patch.object(chat_stream, "_HOSTED_MAX_USER_PROMPT_CHARS", 20):
            response = await self._run(core, body=self._body(message=prompt))

        self.assertEqual(response.status, 413)
        self.assertFalse(core.socket.messages)
        rejected = self._events(core, "first_look_run_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "hosted_user_prompt_too_large")
        self.assertNotIn(prompt, json.dumps(rejected))

    async def test_authenticated_hosted_user_prompt_limit_rejects_before_dispatch(self):
        core = self._core()
        core._authenticate = lambda _request: {
            "user_id": "u-prompt-limit",
            "key_hash": "abc12345",
            "agent_id": "claude-abc12345",
            "email": "",
            "user_type": "claude",
        }
        prompt = "x" * 21

        with patch.object(chat_stream, "_HOSTED_MAX_USER_PROMPT_CHARS", 20):
            response = await self._run(
                core,
                body=self._body(
                    message=prompt,
                    first_look_guest=False,
                    headless=False,
                    agent_id="claude-abc12345",
                    session_id="s-claude-abc12345-prompt-limit",
                ),
            )

        self.assertEqual(response.status, 413)
        self.assertFalse(core.socket.messages)
        self.assertFalse(core.track_calls)

    async def test_invalid_attribution_triad_is_dropped_by_handler(self):
        core = self._core(headless_agent_id="")

        response = await self._run(
            core,
            body=self._body(task="research", from_result="not-a-result-id"),
        )

        self.assertEqual(response.status, 503)
        rejected = self._events(core, "first_look_run_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(set(rejected[0]["meta"]), {"run_id"})
        self.assertNotIn("research", json.dumps(rejected))
        self.assertNotIn("not-a-result-id", json.dumps(rejected))

    async def test_profile_clear_failure_is_a_bounded_rejection(self):
        core = self._core()
        core._session_profile_paths[self.SESSION_ID] = "/private/profile/path"

        async def fail_close(*_args, **_kwargs):
            raise RuntimeError("raw profile close failure")

        core._close_session_tab = fail_close
        response = await self._run(core, body=self._body(profile_path=""))

        self.assertEqual(response.status, 502)
        rejected = self._events(core, "first_look_run_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "profile_clear_failed")
        self.assertNotIn("/private/profile/path", json.dumps(rejected))
        self.assertNotIn("raw profile close failure", json.dumps(rejected))

    async def test_expired_profile_state_is_a_bounded_rejection(self):
        core = self._core()
        core._expired_profile_sessions[self.SESSION_ID] = {"expired": True}

        response = await self._run(core)

        self.assertEqual(response.status, 409)
        rejected = self._events(core, "first_look_run_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "profile_session_expired")

    async def test_non_guest_or_incomplete_guest_shape_emits_no_lifecycle_event(self):
        invalid_shapes = (
            {"headless": False},
            {"headless": 1},
            {"first_look_guest": "true"},
        )
        for overrides in invalid_shapes:
            with self.subTest(overrides=overrides):
                core = self._core()
                response = await self._run(core, body=self._body(**overrides))

                self.assertEqual(response.status, 401)
                self.assertFalse(core.track_calls)


class TestDeepSeekGuestGate(unittest.IsolatedAsyncioTestCase):
    """Guests selecting paid DeepSeek models must be rejected, never silently
    rerouted to the free OpenRouter fallback."""

    def _core(self):
        return SimpleNamespace(
            HEADLESS_AGENT_ID="headless-1",
            TRIAL_AGENT_ID="trial-agent",
            ADMIN_EMAILS=[],
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _OPENROUTER_TRIAL_FALLBACK_MODEL="nvidia/nemotron-3.5-lightning:free",
            _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS=(
                "nvidia/nemotron-3.5-lightning:free",
            ),
            _authenticate=lambda r: None,
            _first_look_guest_auth=lambda r: (
                {
                    "user_id": "",
                    "key_hash": "abc123",
                    "agent_id": "guest-abc123",
                    "email": "",
                    "user_type": "guest",
                },
                "guest-id",
                0,
            ),
            _attach_first_look_guest_cookies=lambda *a, **k: None,
            _trace=lambda *a, **k: None,
            _track_event=lambda *a, **k: True,
            _analytics_session_id_from_request=lambda r: "s",
            _analytics_page_view_id_from_request=lambda r: "p",
            _analytics_route_from_request=lambda r: "/trial",
            _is_pending_user=lambda a: False,
            _is_claude_sdk_model=lambda m: False,
            _is_codex_sdk_model=lambda m: False,
            _is_codex_cli_model=lambda m: False,
            _is_opencode_cli_model=lambda m: False,
            # Mimics the real web.py predicate: deepseek-* counts as hosted.
            _is_openrouter_model=lambda m: "/" in m or m.startswith("deepseek-"),
            _is_deepseek_model=lambda m: m.startswith("deepseek-"),
            _resolve_chat_agent_id=lambda auth, _model: auth["agent_id"],
            _mint_scheduler_turn_grant=lambda *_args: "",
            _response_queues={},
            _response_req_ids={},
            _session_tabs={},
            _session_profile_paths={},
            _expired_profile_sessions={},
            _session_agent_map={},
            _session_last_active={},
            _session_agents={},
            _overlay_sessions={},
            _scheduler_turn_grants={},
            _chat_turns=None,
            _chat_agents={},  # no trial ws → hosted branch returns 503 cleanly
            _auth=SimpleNamespace(db_path=""),
        )

    async def _run(self, core, body: dict):
        request = _Request(body)
        with patch.object(chat_stream, "_core", return_value=core):
            return await chat_stream.handle_chat_msg(request)

    async def test_guest_selecting_deepseek_is_rejected(self):
        core = self._core()
        resp = await self._run(
            core,
            {
                "first_look_guest": True,
                "headless": True,
                "message": "hi",
                "model": "deepseek-v4-flash",
            },
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(
            json.loads(resp.body).get("error"),
            "guest_mode_requires_openrouter_model",
        )

    async def test_guest_selecting_deepseek_pro_is_rejected(self):
        core = self._core()
        resp = await self._run(
            core,
            {
                "first_look_guest": True,
                "headless": True,
                "message": "hi",
                "model": "deepseek-v4-pro",
            },
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(
            json.loads(resp.body).get("error"),
            "guest_mode_requires_openrouter_model",
        )

    async def test_guest_openrouter_deepseek_is_not_gated_as_direct(self):
        """deepseek/deepseek-v4-flash (OpenRouter form, with slash) must NOT be
        treated as the paid direct lane by the guest gate — it routes through
        OpenRouter and proceeds (the guest branch then forces the free
        fallback, which is a different code path than the rejection)."""
        core = self._core()
        resp = await self._run(
            core,
            {
                "first_look_guest": True,
                "headless": True,
                "message": "hi",
                "model": "deepseek/deepseek-v4-flash",
            },
        )
        body_text = ""
        if hasattr(resp, "body"):
            body_text = resp.body or b""
            if isinstance(body_text, bytes):
                body_text = body_text.decode("utf-8", "replace")
        self.assertNotIn("guest_mode_requires_openrouter_model", body_text)
        # Positive path: it proceeds into the hosted branch (guest forced to the
        # free fallback) and fails only because no trial ws exists in the mock.
        self.assertEqual(resp.status, 503)
        self.assertIn("Trial agent is not available", body_text)


if __name__ == "__main__":
    unittest.main()
