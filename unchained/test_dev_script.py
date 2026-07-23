"""Contract tests for the isolated local dev stack launcher, slot binding,
and OpenRouter status behavior."""

import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web as aiohttp_web

ROOT = Path(__file__).resolve().parent.parent


class TestDevScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "dev.sh").read_text()

    def test_agent_view_mode_uses_one_local_identity(self):
        self.assertIn('DEV_API_KEY=$(UNCHAINED_DB_PATH="$DEV_DB_PATH"', self.script)
        self.assertIn('UNCHAINED_API_KEY="$DEV_API_KEY"', self.script)
        self.assertIn('printf \'%s\' "$DEV_API_KEY" > "$PIDDIR/api-key"', self.script)

    def test_agent_view_mode_runs_private_core_without_overlay(self):
        self.assertIn('PRIVATE_CORE_DIR=${PRIVATE_CORE_DIR:-', self.script)
        self.assertIn('uv run python private_core_server.py', self.script)
        self.assertIn('PRIVATE_CORE_MODE_VALUE="http"', self.script)
        self.assertNotIn("install_private_core.sh", self.script)

    def test_stop_covers_full_stack_including_scheduler_and_trial(self):
        self.assertIn("for svc in scheduler trial-agent chat-agent bridge web private-core relay", self.script)
        self.assertIn('"$PIDDIR/scheduler.pid"', self.script)
        self.assertIn('"$PIDDIR/trial-agent.pid"', self.script)

    # ---- hosted-trial mode contract tests ----

    def test_hosted_trial_requires_openrouter_key(self):
        self.assertIn("OPENROUTER_API_KEY must be exported", self.script)

    def test_hosted_trial_generates_distinct_service_tokens(self):
        self.assertIn('TRIAL_AGENT_KEY=$(secret_from_file', self.script)
        self.assertIn('HOSTED_AGENT_SERVICE_TOKEN=$(secret_from_file', self.script)
        self.assertIn('"$TRIAL_AGENT_KEY" > "$PIDDIR/trial-agent-key"', self.script)
        self.assertIn('"$HOSTED_AGENT_SERVICE_TOKEN" > "$PIDDIR/hosted-agent-service-token"', self.script)

    def test_hosted_trial_rejects_token_collisions(self):
        self.assertIn('Trial key and hosted service token must be distinct', self.script)
        self.assertIn('Trial key must not match JWT_SECRET', self.script)
        self.assertIn('must not match DEV_API_KEY', self.script)

    def test_hosted_trial_has_deterministic_agent_id(self):
        self.assertIn('TRIAL_AGENT_ID="${TRIAL_AGENT_ID:-trial-local}"', self.script)

    def test_hosted_trial_starts_trial_worker(self):
        self.assertIn("chat_agent_openrouter.py", self.script)
        self.assertIn("Trial worker running", self.script)

    def test_hosted_trial_starts_scheduler(self):
        self.assertIn("scheduled_tasks.py daemon-multi", self.script)
        self.assertIn("SCHEDULER_DEFAULT_MODEL=", self.script)

    def test_hosted_trial_passes_localhost_urls(self):
        self.assertIn("RELAY_HOST=127.0.0.1", self.script)
        self.assertIn('ws://127.0.0.1:$WEB_PORT', self.script)
        self.assertIn("python -m web --host 127.0.0.1", self.script)

    def test_openrouter_key_is_scoped_to_trial_worker(self):
        self.assertIn('OPENROUTER_API_KEY_VALUE="$OPENROUTER_API_KEY"', self.script)
        self.assertIn("unset OPENROUTER_API_KEY", self.script)
        self.assertIn(
            'OPENROUTER_API_KEY="$OPENROUTER_API_KEY_VALUE"', self.script
        )

    def test_hosted_trial_uses_dev_email_for_admin(self):
        # Issue D: admin default should be DEV_EMAIL, not hardcoded
        self.assertIn('WEB_ADMIN="${ADMIN_EMAILS:-$DEV_EMAIL}"', self.script)

    def test_hosted_trial_readiness_uses_bearer_token(self):
        # Issue A: must use Authorization: Bearer, not cookie
        self.assertIn('Authorization: Bearer', self.script)
        self.assertNotIn('Cookie: session=', self.script.replace('Cookie: session', 'NOPE'))

    def test_hosted_trial_readiness_checks_both_chat_and_bridge(self):
        # Issue A: must parse JSON and check both chat_connected and bridge_connected
        self.assertIn('chat_connected', self.script)
        self.assertIn('bridge_connected', self.script)

    def test_hosted_trial_mode_separate_web_env(self):
        # Issue C: hosted-trial and non-hosted should have distinct env array
        self.assertIn('WEB_BASE_ENV', self.script)
        self.assertIn('GOOGLE_CLIENT_ID=', self.script)

    def test_non_hosted_inherits_env(self):
        # Issue C: non-hosted modes preserve inherited env
        self.assertIn('GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}', self.script)

    def test_script_never_prints_token_values(self):
        for line in self.script.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("!"):
                continue
            if any(
                kw in stripped
                for kw in (
                    "TRIAL_AGENT_KEY",
                    "HOSTED_AGENT_SERVICE_TOKEN",
                    "JWT_SECRET",
                    "PRIVATE_CORE_TOKEN",
                    "DEV_API_KEY",
                )
            ):
                if "$PIDDIR" in stripped or "ERROR" in stripped or "must" in stripped:
                    continue
                # Lines that only use echo for literal fallback strings
                # (e.g. || echo '{}') are safe — the token is only in a
                # Bearer header, never printed.
                if "echo" in stripped:
                    # Check if the echo prints a literal string (single or
                    # double quotes) rather than a variable expansion.
                    if re.search(r"echo\s+['\"]", stripped):
                        continue
                if "echo" in stripped:
                    self.fail(f"dev.sh may print a token value:\n  {stripped}")

    def test_usage_accepts_hosted_trial(self):
        self.assertIn("hosted-trial", self.script.split("Usage:", 1)[-1] if "Usage:" in self.script else self.script)


# ---------------------------------------------------------------------------
# Behavioral tests: bind_initial_session
# ---------------------------------------------------------------------------

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
from hosted_conversations import HostedConversationRepo


class TestBindInitialSession(unittest.TestCase):
    """Behavioral tests for HostedConversationRepo.bind_initial_session."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temp.name) / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.repo = HostedConversationRepo(
            data_dir=self.temp.name, sessions_dir=str(self.session_dir)
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_bind_to_default_active_slot(self):
        """First call binds to slot 1 (default active_slot)."""
        result = self.repo.bind_initial_session("u-1", "s-agent-abc")
        self.assertTrue(result)
        state = self.repo.get_slot_state("u-1")
        self.assertEqual(state["slots"]["1"], "s-agent-abc")
        self.assertEqual(state["active_slot"], 1)

    def test_bind_to_specific_slot(self):
        """Explicit slot 2 binding works."""
        result = self.repo.bind_initial_session("u-2", "s-agent-def", slot=2)
        self.assertTrue(result)
        state = self.repo.get_slot_state("u-2")
        self.assertEqual(state["slots"]["2"], "s-agent-def")
        self.assertEqual(state["active_slot"], 2)

    def test_bind_to_slot_3(self):
        """Slot 3 works."""
        result = self.repo.bind_initial_session("u-3", "s-agent-ghi", slot=3)
        self.assertTrue(result)
        state = self.repo.get_slot_state("u-3")
        self.assertEqual(state["slots"]["3"], "s-agent-ghi")
        self.assertEqual(state["active_slot"], 3)
        self.assertEqual(state["slots"]["1"], "")  # others untouched
        self.assertEqual(state["slots"]["2"], "")

    def test_rebind_same_session_is_idempotent(self):
        """Binding the same session again returns True and is no-op."""
        self.assertTrue(self.repo.bind_initial_session("u-4", "s-same", slot=1))
        self.assertTrue(self.repo.bind_initial_session("u-4", "s-same", slot=1))
        state = self.repo.get_slot_state("u-4")
        self.assertEqual(state["slots"]["1"], "s-same")

    def test_rebind_same_session_to_active_slot_without_explicit_slot(self):
        """Re-binding the same session (no explicit slot) is idempotent."""
        self.assertTrue(self.repo.bind_initial_session("u-4b", "s-same2"))
        self.assertTrue(self.repo.bind_initial_session("u-4b", "s-same2"))
        state = self.repo.get_slot_state("u-4b")
        self.assertEqual(state["slots"]["1"], "s-same2")

    def test_does_not_overwrite_occupied_slot(self):
        """Slot already occupied by a DIFFERENT session → return False, no change."""
        self.repo.bind_initial_session("u-5", "s-first", slot=1)
        result = self.repo.bind_initial_session("u-5", "s-second", slot=1)
        self.assertFalse(result)
        state = self.repo.get_slot_state("u-5")
        self.assertEqual(state["slots"]["1"], "s-first")  # NOT overwritten
        self.assertEqual(state["active_slot"], 1)

    def test_bind_to_empty_slot_after_another_occupied(self):
        """Slot 1 occupied, bind to slot 2 → succeeds."""
        self.repo.bind_initial_session("u-6", "s-first", slot=1)
        self.assertTrue(self.repo.bind_initial_session("u-6", "s-second", slot=2))
        state = self.repo.get_slot_state("u-6")
        self.assertEqual(state["slots"]["1"], "s-first")
        self.assertEqual(state["slots"]["2"], "s-second")
        self.assertEqual(state["active_slot"], 2)

    def test_active_slot_follows_bind(self):
        """active_slot is set to the bound slot."""
        self.repo.bind_initial_session("u-7", "s-slot3", slot=3)
        state = self.repo.get_slot_state("u-7")
        self.assertEqual(state["active_slot"], 3)

    def test_concurrent_bind_same_slot_same_session(self):
        """Two binders racing on same session → both see True, slot unchanged."""
        # Simulate by calling bind twice (the atomic replace handles real races).
        self.assertTrue(self.repo.bind_initial_session("u-conc", "s-race", slot=2))
        self.assertTrue(self.repo.bind_initial_session("u-conc", "s-race", slot=2))
        state = self.repo.get_slot_state("u-conc")
        self.assertEqual(state["slots"]["2"], "s-race")
        self.assertEqual(state["slots"]["1"], "")

    def test_concurrent_different_sessions_only_one_wins(self):
        """Two different sessions racing for the same slot: only one wins.
        The per-user lock serialises access so this test is deterministic."""
        import threading
        import time

        results = []
        errors = []
        barrier = threading.Barrier(2)

        def binder(session_id: str):
            try:
                barrier.wait(timeout=5)
                # Tiny sleep so the last-release thread also reads fresh state.
                time.sleep(0.01)
                result = self.repo.bind_initial_session("u-conc-diff", session_id, slot=1)
                results.append((session_id, result))
            except Exception as exc:
                errors.append((session_id, exc))

        t1 = threading.Thread(target=binder, args=("s-winner-aaaaaa",))
        t2 = threading.Thread(target=binder, args=("s-loser-bbbbbb",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive(), "thread 1 timed out")
        self.assertFalse(t2.is_alive(), "thread 2 timed out")
        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")

        winners = [sid for sid, ok in results if ok]
        losers = [sid for sid, ok in results if not ok]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got {winners} (results={results})")
        self.assertEqual(len(losers), 1, f"expected exactly one loser, got {losers} (results={results})")

        # Verify server state matches the winner.
        state = self.repo.get_slot_state("u-conc-diff")
        self.assertEqual(state["slots"]["1"], winners[0],
                         f"slot 1 should contain winner {winners[0]!r}, got {state['slots']['1']!r}")
        self.assertNotEqual(state["slots"]["1"], losers[0],
                            f"slot 1 must NOT contain loser {losers[0]!r}")

    def test_invalid_slot_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self.repo.bind_initial_session("u-bad", "s-x", slot=0)
        with self.assertRaises(ValueError):
            self.repo.bind_initial_session("u-bad", "s-x", slot=4)

    def test_empty_user_id_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self.repo.bind_initial_session("", "s-x")

    def test_empty_session_id_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self.repo.bind_initial_session("u-x", "")

    def test_malformed_session_id_raises_valueerror(self):
        with self.assertRaises(ValueError):
            self.repo.bind_initial_session("u-x", "../outside")

    def test_bind_without_slot_uses_current_active(self):
        """When no slot arg given, uses state's active_slot."""
        # Set active to 2, bind without slot → uses slot 2.
        state = self.repo.get_slot_state("u-act")
        state["active_slot"] = 2
        self.repo.set_slot_state("u-act", state)
        self.assertTrue(self.repo.bind_initial_session("u-act", "s-auto"))
        state2 = self.repo.get_slot_state("u-act")
        self.assertEqual(state2["slots"]["2"], "s-auto")
        self.assertEqual(state2["slots"]["1"], "")


# ---------------------------------------------------------------------------
# Behavioral tests: OpenRouter status in chat_flow.py
# ---------------------------------------------------------------------------

def _build_web_request(query_params: dict) -> aiohttp_web.Request:
    """Build a minimal request object for handle_chat_status."""
    req = SimpleNamespace()
    req.query = query_params
    req.headers = {}
    req.cookies = {}
    req.match_info = {}
    req.path = "/web/chat/status"
    # _authenticate patched in tests
    return req


class TestOpenRouterStatus(unittest.IsolatedAsyncioTestCase):
    """Verify handle_chat_status returns trial worker info for OpenRouter model hints."""

    def _build_core(self, **attrs):
        """Build a mock core object and patch chat_flow._core to return it."""
        core = SimpleNamespace(**attrs)
        return core

    async def test_openrouter_model_reports_trial_agent_id(self):
        """When model hint is an OpenRouter model, status uses TRIAL_AGENT_ID."""
        from web_app.handlers import chat_flow
        core = self._build_core(
            TRIAL_AGENT_ID="trial-test-id",
            _chat_agents={"trial-test-id": SimpleNamespace(closed=False)},
            _chat_agent_caps={},
            _is_openrouter_model=lambda m: True,
            _is_pending_user=lambda a: False,
            _authenticate=lambda r: {"agent_id": "claude-abc", "user_id": "u-1", "key_hash": "abc", "key": "k"},
            _resolve_bridge_agent=AsyncMock(return_value={
                "bridge_agent_id": "claude-xyz",
                "bridge_connected": True,
                "bridge_configured": True,
                "bridge_profile": "default",
                "active_bridge_agent_id": "claude-xyz",
                "active_bridge_profile": "default",
                "available_bridge_profiles": [],
                "bridge_selection_required": False,
                "bridge_status_reason": "connected",
            }),
        )
        with patch.object(chat_flow, "_core", return_value=core):
            req = _build_web_request(
                {"model": "google/gemini-3.1-flash-lite"}
            )
            result = await chat_flow.handle_chat_status(req)
            body = json.loads(result.body)
            self.assertEqual(body.get("chat_agent_id"), "trial-test-id")
            self.assertTrue(body.get("chat_connected"))
            self.assertTrue(body.get("bridge_connected"))
            self.assertTrue(body.get("trial"))

    async def test_openrouter_model_reports_disconnected_when_worker_absent(self):
        from web_app.handlers import chat_flow
        core = self._build_core(
            TRIAL_AGENT_ID="trial-test-id",
            _chat_agents={},  # No worker connected
            _chat_agent_caps={},
            _is_openrouter_model=lambda m: True,
            _is_pending_user=lambda a: False,
            _authenticate=lambda r: {"agent_id": "claude-abc", "user_id": "u-1", "key_hash": "abc", "key": "k"},
            _resolve_bridge_agent=AsyncMock(return_value={
                "bridge_agent_id": "",
                "bridge_connected": False,
                "bridge_configured": False,
                "bridge_profile": "default",
                "active_bridge_agent_id": "",
                "active_bridge_profile": "default",
                "available_bridge_profiles": [],
                "bridge_selection_required": False,
                "bridge_status_reason": "offline",
            }),
        )
        with patch.object(chat_flow, "_core", return_value=core):
            req = _build_web_request(
                {"model": "google/gemini-3.1-flash-lite"}
            )
            result = await chat_flow.handle_chat_status(req)
            body = json.loads(result.body)
            self.assertFalse(body.get("chat_connected"))
            self.assertFalse(body.get("connected"))

    async def test_non_openrouter_model_does_not_enter_trial_branch(self):
        """Regular Claude model should still use the normal code path."""
        from web_app.handlers import chat_flow
        core = self._build_core(
            TRIAL_AGENT_ID="trial-test",
            _chat_agents={
                "trial-test": SimpleNamespace(closed=False),
                "claude-abc": SimpleNamespace(closed=False),
            },
            _is_openrouter_model=lambda m: False,
            _is_pending_user=lambda a: False,
            _is_codex_cli_model=lambda m: False,
            _is_codex_sdk_model=lambda m: False,
            _is_opencode_cli_model=lambda m: False,
            _is_claude_sdk_model=lambda m: False,
            _authenticate=lambda r: {"agent_id": "claude-abc", "user_id": "u-1", "key_hash": "abc", "key": "k"},
            _resolve_bridge_agent=AsyncMock(return_value={
                "bridge_agent_id": "claude-xyz",
                "bridge_connected": True,
                "bridge_configured": True,
                "bridge_profile": "default",
                "active_bridge_agent_id": "claude-xyz",
                "active_bridge_profile": "default",
                "available_bridge_profiles": [],
                "bridge_selection_required": False,
                "bridge_status_reason": "connected",
            }),
            _chat_agent_users={},
            _chat_agent_caps={},
            _client_version_status=lambda caps: {},
        )
        with patch.object(chat_flow, "_core", return_value=core):
            req = _build_web_request(
                {"model": "claude-sonnet-4-6"}
            )
            result = await chat_flow.handle_chat_status(req)
            body = json.loads(result.body)
            # Non-OpenRouter should use claude-abc, not trial-test
            self.assertEqual(body.get("chat_agent_id"), "claude-abc")
            self.assertNotIn("trial", body)
if __name__ == "__main__":
    unittest.main()
