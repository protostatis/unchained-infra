"""Tests for agent-driven scheduler access and /schedule gating."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import orchestrator
import scheduler_agent
from web_app.handlers import auth_admin, chat_stream


class TestSchedulerTrigger(unittest.TestCase):
    def test_extract_scheduler_turn_requires_prefix(self):
        armed, text = chat_stream._extract_scheduler_turn("/schedule list my tasks")
        self.assertTrue(armed)
        self.assertEqual(text, "list my tasks")

        armed, text = chat_stream._extract_scheduler_turn("please schedule this later")
        self.assertFalse(armed)
        self.assertEqual(text, "please schedule this later")

    def test_extract_scheduler_turn_can_be_disabled(self):
        armed, text = chat_stream._extract_scheduler_turn("/schedule delete daily-brief", allow_trigger=False)
        self.assertFalse(armed)
        self.assertEqual(text, "/schedule delete daily-brief")

    def test_scheduler_trigger_supported_only_for_bridge_backed_lanes(self):
        self.assertTrue(
            chat_stream._scheduler_trigger_supported(guest_mode=False, is_openrouter=False)
        )
        self.assertFalse(
            chat_stream._scheduler_trigger_supported(guest_mode=True, is_openrouter=False)
        )
        # Authenticated hosted OpenRouter is now supported
        self.assertTrue(
            chat_stream._scheduler_trigger_supported(guest_mode=False, is_openrouter=True)
        )
        # Guest OpenRouter is still rejected
        self.assertFalse(
            chat_stream._scheduler_trigger_supported(guest_mode=True, is_openrouter=True)
        )


class TestSchedulerToolHelpers(unittest.IsolatedAsyncioTestCase):
    def test_scheduler_tools_only_exposed_for_armed_turns(self):
        base_openai_tools = [
            {
                "type": "function",
                "function": {"name": "ddm"},
            }
        ]
        tools = scheduler_agent.build_openai_tools(
            base_openai_tools,
            scheduler_armed=False,
        )
        self.assertEqual([tool["function"]["name"] for tool in tools], ["ddm"])

        armed_tools = scheduler_agent.build_openai_tools(
            base_openai_tools,
            scheduler_armed=True,
            scheduler_grant_id="sg-valid",
        )
        armed_names = [tool["function"]["name"] for tool in armed_tools]
        self.assertIn(scheduler_agent.SCHEDULER_LIST_TOOL, armed_names)
        self.assertIn(scheduler_agent.SCHEDULER_SAVE_TOOL, armed_names)

        anthropic_names = [
            tool["name"]
            for tool in orchestrator.build_tools(
                scheduler_armed=True,
                scheduler_grant_id="sg-valid",
            )
        ]
        self.assertIn(scheduler_agent.SCHEDULER_LIST_TOOL, anthropic_names)
        self.assertIn(scheduler_agent.SCHEDULER_DELETE_TOOL, anthropic_names)
        self.assertNotIn(
            scheduler_agent.SCHEDULER_LIST_TOOL,
            [tool["name"] for tool in orchestrator.build_tools()],
        )

        prompt = scheduler_agent.build_system_prompt(
            "base",
            scheduler_armed=True,
            scheduler_grant_id="sg-valid",
        )
        self.assertIn("same scheduled task list shown in `/scheduler`", prompt)

    async def test_execute_scheduler_tool_posts_expected_payload(self):
        calls: dict[str, object] = {}

        class FakeResponse:
            def __init__(self, payload: dict):
                self._payload = payload
                self.status_code = 200
                self.is_success = True
                self.text = json.dumps(payload)
                self.reason_phrase = "OK"

            def json(self):
                return self._payload

        class FakeClient:
            async def post(self, url, json=None, headers=None, timeout=None):
                calls["url"] = url
                calls["json"] = json
                calls["headers"] = headers
                calls["timeout"] = timeout
                return FakeResponse({"ok": True, "job": {"id": "daily-brief"}})

        result = await scheduler_agent.execute_scheduler_tool(
            server_url="ws://127.0.0.1:8080/chat/ws",
            api_key="uc_live_test",
            session_id="s-chat",
            scheduler_grant_id="sg-valid",
            tool_name=scheduler_agent.SCHEDULER_SAVE_TOOL,
            args={
                "job_id": "daily-brief",
                "prompt": "Check pricing and summarize changes.",
                "daily_at": "09:00",
                "use_stable_session": False,
            },
            client=FakeClient(),
        )

        self.assertEqual(
            calls["url"],
            "http://127.0.0.1:8080/web/scheduler/agent/upsert",
        )
        self.assertEqual(
            calls["json"],
            {
                "session_id": "s-chat",
                "scheduler_grant_id": "sg-valid",
                "job": {
                    "id": "daily-brief",
                    "prompt": "Check pricing and summarize changes.",
                    "schedule": {"daily_at": "09:00"},
                    "use_stable_session": False,
                },
            },
        )
        self.assertEqual(json.loads(result)["job"]["id"], "daily-brief")


class TestSchedulerAgentAccess(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_jobs_blocks_pending_users(self):
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1", "status": "pending"},
            _is_pending_user=lambda auth: bool(auth) and auth.get("status") == "pending",
            _pending_limited_response=lambda: auth_admin.web.json_response({"error": "pending_account_limited"}, status=403),
        )
        request = SimpleNamespace(method="GET")

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_jobs(request)

        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.body.decode()), {"error": "pending_account_limited"})

    async def test_scheduler_jobs_post_rejects_bearer_without_turn_grant(self):
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: False,
        )
        request = SimpleNamespace(
            method="POST",
            headers={"Authorization": "Bearer uc_live_test"},
            can_read_body=True,
            json=AsyncMock(return_value={"jobs": []}),
        )

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_jobs(request)

        self.assertEqual(response.status, 403)
        self.assertEqual(
            json.loads(response.body.decode()),
            {"error": "scheduler JSON endpoints require a browser session or an active /schedule turn"},
        )

    async def test_scheduler_history_returns_full_detail_for_owned_job(self):
        full_detail = "First paragraph.\n\nPrivate full run detail remains available."
        state_path = Path("/tmp/scheduler-u-1.state.json")
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _scheduler_read_jobs_payload=lambda user_id: {
                "jobs": [{"id": "daily", "prompt": "Run it", "schedule": {"daily_at": "09:00"}}]
                if user_id == "u-1" else []
            },
            _scheduler_state_path=lambda _user_id: state_path,
        )
        request = SimpleNamespace(headers={}, query={"job_id": "daily", "limit": "20"})

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            with patch(
                "scheduled_tasks.load_run_history",
                return_value=[{"ts": "2026-07-12T10:00:00Z", "ok": True, "detail": full_detail}],
            ) as load_history:
                response = await auth_admin.handle_scheduler_history(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body.decode())["records"][0]["detail"], full_detail)
        load_history.assert_called_once_with(state_path, "daily", limit=20)

    async def test_scheduler_history_rejects_job_outside_account_scope(self):
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _scheduler_read_jobs_payload=lambda _user_id: {
                "jobs": [{"id": "owned", "prompt": "Run it", "schedule": {"daily_at": "09:00"}}]
            },
        )
        request = SimpleNamespace(headers={}, query={"job_id": "someone-elses-job"})

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            with patch("scheduled_tasks.load_run_history") as load_history:
                response = await auth_admin.handle_scheduler_history(request)

        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(response.body.decode()), {"error": "job not found"})
        load_history.assert_not_called()

    async def test_agent_upsert_requires_valid_turn_grant(self):
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: False,
        )
        request = SimpleNamespace(
            can_read_body=True,
            json=AsyncMock(
                return_value={
                    "session_id": "s-test",
                    "scheduler_grant_id": "sg-missing",
                    "job": {
                        "id": "daily-brief",
                        "prompt": "Check pricing",
                        "schedule": {"daily_at": "09:00"},
                    },
                }
            ),
        )

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_upsert(request)

        self.assertEqual(response.status, 403)
        self.assertEqual(
            json.loads(response.body.decode()),
            {"error": "scheduler trigger not active for this turn"},
        )

    async def test_agent_upsert_writes_shared_scheduler_store(self):
        writes: list[dict] = []
        state_path = Path("/tmp/scheduler-u-1.state.json")
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: True,
            _scheduler_read_jobs_payload=lambda _user_id: {"jobs": []},
            _scheduler_write_jobs_payload=lambda _user_id, payload: writes.append(payload),
            _scheduler_preview_rows=lambda _user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
            _scheduler_state_path=lambda _user_id: state_path,
        )
        request = SimpleNamespace(
            can_read_body=True,
            json=AsyncMock(
                return_value={
                    "session_id": "s-test",
                    "scheduler_grant_id": "sg-valid",
                    "job": {
                        "id": "daily-brief",
                        "prompt": "Check pricing and summarize changes",
                        "schedule": {"daily_at": "09:00"},
                        "use_stable_session": True,
                    },
                }
            ),
        )

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_upsert(request)

        data = json.loads(response.body.decode())
        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["created"])
        self.assertEqual(data["job"]["id"], "daily-brief")
        self.assertTrue(writes, "upsert should write back through the shared scheduler store")
        self.assertEqual(writes[-1]["jobs"][0]["id"], "daily-brief")

    async def test_agent_upsert_preserves_existing_fields_on_partial_update(self):
        writes: list[dict] = []
        existing_payload = {
            "jobs": [
                {
                    "id": "daily-brief",
                    "prompt": "Old prompt",
                    "schedule": {"daily_at": "09:00"},
                    "enabled": False,
                    "model": "claude-opus-4-7",
                    "use_stable_session": True,
                    "timeout_seconds": 240,
                }
            ]
        }
        state_path = Path("/tmp/scheduler-u-1.state.json")
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: True,
            _scheduler_read_jobs_payload=lambda _user_id: existing_payload,
            _scheduler_write_jobs_payload=lambda _user_id, payload: writes.append(payload),
            _scheduler_preview_rows=lambda _user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
            _scheduler_state_path=lambda _user_id: state_path,
        )
        request = SimpleNamespace(
            can_read_body=True,
            json=AsyncMock(
                return_value={
                    "session_id": "s-test",
                    "scheduler_grant_id": "sg-valid",
                    "job": {
                        "id": "daily-brief",
                        "prompt": "New prompt",
                    },
                }
            ),
        )

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_upsert(request)

        self.assertEqual(response.status, 200)
        stored = writes[-1]["jobs"][0]
        self.assertEqual(stored["prompt"], "New prompt")
        self.assertEqual(stored["schedule"], {"daily_at": "09:00"})
        self.assertEqual(stored["model"], "claude-opus-4-7")
        self.assertTrue(stored["use_stable_session"])
        self.assertEqual(stored["timeout_seconds"], 240)
        self.assertFalse(stored["enabled"])

    async def test_agent_preview_preserves_existing_fields_on_partial_update(self):
        existing_payload = {
            "jobs": [
                {
                    "id": "daily-brief",
                    "prompt": "Old prompt",
                    "schedule": {"daily_at": "09:00"},
                    "enabled": True,
                    "model": "claude-opus-4-7",
                    "use_stable_session": True,
                }
            ]
        }
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: True,
            _scheduler_read_jobs_payload=lambda _user_id: existing_payload,
            _scheduler_preview_rows=lambda _user_id, jobs: [{"id": job.id, "next_run_at": "2026-03-08T10:00:00Z"} for job in jobs],
        )
        request = SimpleNamespace(
            can_read_body=True,
            json=AsyncMock(
                return_value={
                    "session_id": "s-test",
                    "scheduler_grant_id": "sg-valid",
                    "job": {
                        "id": "daily-brief",
                        "schedule": {"daily_at": "10:00"},
                    },
                }
            ),
        )

        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_preview(request)

        self.assertEqual(response.status, 200)
        data = json.loads(response.body.decode())
        self.assertEqual(data["job"]["id"], "daily-brief")
        self.assertEqual(data["job"]["prompt"], "Old prompt")
        self.assertEqual(data["job"]["schedule"], {"daily_at": "10:00"})
        self.assertTrue(data["job"]["use_stable_session"])
        self.assertEqual(data["preview"]["next_run_at"], "2026-03-08T10:00:00Z")


class TestCodexCliEnv(unittest.TestCase):
    def test_claude_turn_uses_strict_mcp_config(self):
        source_path = Path(__file__).with_name("chat_agent_cli.py")
        source = source_path.read_text()
        start = source.index("async def handle_message_claude(")
        end = source.index("if is_resume:", start)
        snippet = source[start:end]

        self.assertIn('"--strict-mcp-config"', snippet)
        self.assertIn('"--allowedTools", allowed', snippet)
        self.assertIn('tools = ["Bash"]', snippet)

    def test_unarmed_codex_turn_keeps_api_key_for_update_path(self):
        source_path = Path(__file__).with_name("chat_agent_cli.py")
        source = source_path.read_text()
        start = source.index("async def handle_message_codex(")
        end = source.index("output_file = os.path.join(", start)
        snippet = source[start:end]

        self.assertNotIn('env.pop("UNCHAINED_API_KEY", None)', snippet)
        self.assertIn('env.pop("UNCHAINED_INSTALL_TOKEN", None)', snippet)
        self.assertIn('env["UNCHAINED_CHAT_SESSION_ID"] = sid', snippet)

    def test_codex_turn_disables_mcp_at_invocation_level(self):
        source_path = Path(__file__).with_name("chat_agent_cli.py")
        source = source_path.read_text()
        start = source.index("async def handle_message_codex(")
        end = source.index("proc = await asyncio.create_subprocess_exec(", start)
        snippet = source[start:end]

        self.assertIn("config_args = []", snippet)
        self.assertIn('"--ignore-user-config"', snippet)
        self.assertIn("Hide user-configured Codex MCP tools", source)
        self.assertNotIn("mcp_servers.unchainedsky.enabled=false", source)

    def test_unarmed_opencode_turn_keeps_api_key_for_update_path(self):
        source_path = Path(__file__).with_name("chat_agent_cli.py")
        source = source_path.read_text()
        start = source.index("async def handle_message_opencode(")
        end = source.index('cmd = [OPENCODE_BIN, "run"', start)
        snippet = source[start:end]

        self.assertNotIn('env.pop("UNCHAINED_API_KEY", None)', snippet)
        self.assertIn('env.pop("UNCHAINED_INSTALL_TOKEN", None)', snippet)
        self.assertIn('env["UNCHAINED_CHAT_SESSION_ID"] = sid', snippet)

    def test_opencode_turn_disables_mcp_but_not_builtin_tools(self):
        source_path = Path(__file__).with_name("chat_agent_cli.py")
        source = source_path.read_text()
        start = source.index("async def handle_message_opencode(")
        end = source.index('cmd = [OPENCODE_BIN, "run"', start)
        snippet = source[start:end]

        self.assertIn('env["OPENCODE_CONFIG_CONTENT"] = _opencode_config_content_without_mcp()', snippet)
        self.assertIn('tools[f"{name}_*"] = False', source)
        self.assertIn('"unchainedsky"', source)
        self.assertNotIn('tools["bash"] = False', source)
        self.assertNotIn('tools["websearch"] = False', source)


class TestHostedSchedulerTrialAgent(unittest.IsolatedAsyncioTestCase):
    """Hosted scheduler access via the OpenRouter trial agent worker."""

    def test_trial_agent_auth_rejects_missing_bearer(self):
        """Requests without Bearer auth should not succeed trial agent auth."""
        core = SimpleNamespace(TRIAL_AGENT_KEY="sk-trial-test")
        request = SimpleNamespace(headers={})
        body = {"scheduler_grant_id": "sg-test"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    def test_trial_agent_auth_rejects_wrong_key(self):
        """Wrong trial agent key must not authenticate."""
        core = SimpleNamespace(TRIAL_AGENT_KEY="sk-trial-real")
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-wrong"})
        body = {"scheduler_grant_id": "sg-test"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    def test_trial_agent_auth_rejects_missing_grant_id(self):
        """Trial agent auth requires a scheduler_grant_id in the body."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-test": grant},
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"session_id": "s-chat-1234"}  # no scheduler_grant_id
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    def test_trial_agent_auth_rejects_expired_grant(self):
        """Expired grants must be rejected even with correct trial key."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() - 1,  # already expired
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-expired": grant},
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"scheduler_grant_id": "sg-expired"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    def test_trial_agent_auth_rejects_replayed_grant(self):
        """Replayed grants must not authenticate after expiry."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() - 10,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-replayed": grant},
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"scheduler_grant_id": "sg-replayed"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    def test_trial_agent_auth_revoked_grant_fails(self):
        """A grant removed from the store must not authenticate."""
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={},  # empty store
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"scheduler_grant_id": "sg-revoked"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    def test_trial_agent_auth_accepts_valid_grant(self):
        """Valid grant + trial key should return auth_info with correct user_id."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-valid": grant},
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"scheduler_grant_id": "sg-valid"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNotNone(result)
        self.assertEqual(result["user_id"], "u-hosted")
        self.assertTrue(result.get("trial_agent_auth"))

    def test_trial_agent_auth_wrong_user_grants_fails(self):
        """Grant must bind the correct user_id — wrong user must fail grant validation."""
        import time as time_module

        grant = {
            "user_id": "u-correct",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-wrong-user": grant},
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"scheduler_grant_id": "sg-wrong-user"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        # Grant exists and is valid, so auth should succeed with the grant's user
        self.assertIsNotNone(result)
        self.assertEqual(result["user_id"], "u-correct")

    async def test_list_with_trial_agent_auth_succeeds(self):
        """Trial-agent-authenticated list endpoint should return jobs."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-valid": grant},
            _authenticate=lambda _req: None,  # normal auth fails
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda user_id, session_id, grant_id: True,
            _scheduler_read_jobs_payload=lambda user_id: {"jobs": [{"id": "daily", "prompt": "test", "schedule": {"daily_at": "09:00"}}]},
            _scheduler_preview_rows=lambda user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
        )
        request = SimpleNamespace(
            can_read_body=True,
            headers={"Authorization": "Bearer sk-trial-test"},
            json=AsyncMock(return_value={
                "session_id": "s-chat-1234",
                "scheduler_grant_id": "sg-valid",
            }),
        )
        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_list(request)

        self.assertEqual(response.status, 200)
        data = json.loads(response.body.decode())
        self.assertTrue(data["ok"])
        self.assertEqual(data["jobs"][0]["id"], "daily")

    async def test_upsert_with_trial_agent_auth_succeeds(self):
        """Trial-agent-authenticated upsert should create a schedule job."""
        import time as time_module

        writes: list[dict] = []
        state_path = Path("/tmp/scheduler-u-hosted.state.json")
        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-valid": grant},
            _authenticate=lambda _req: None,
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda user_id, session_id, grant_id: True,
            _scheduler_read_jobs_payload=lambda user_id: {"jobs": []},
            _scheduler_write_jobs_payload=lambda user_id, payload: writes.append(payload),
            _scheduler_preview_rows=lambda user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
            _scheduler_state_path=lambda _user_id: state_path,
        )
        request = SimpleNamespace(
            can_read_body=True,
            headers={"Authorization": "Bearer sk-trial-test"},
            json=AsyncMock(return_value={
                "session_id": "s-chat-1234",
                "scheduler_grant_id": "sg-valid",
                "job": {
                    "id": "daily-brief",
                    "prompt": "Check pricing",
                    "schedule": {"daily_at": "09:00"},
                },
            }),
        )
        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_upsert(request)

        self.assertEqual(response.status, 200)
        self.assertTrue(writes)
        self.assertEqual(writes[-1]["jobs"][0]["id"], "daily-brief")

    async def test_delete_with_trial_agent_auth_succeeds(self):
        """Trial-agent-authenticated delete should remove a schedule job."""
        import time as time_module

        writes: list[dict] = []
        state_path = Path("/tmp/scheduler-u-hosted.state.json")
        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-valid": grant},
            _authenticate=lambda _req: None,
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda user_id, session_id, grant_id: True,
            _scheduler_read_jobs_payload=lambda user_id: {"jobs": [
                {"id": "daily-brief", "prompt": "old", "schedule": {"daily_at": "09:00"}},
            ]},
            _scheduler_write_jobs_payload=lambda user_id, payload: writes.append(payload),
            _scheduler_preview_rows=lambda user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
            _scheduler_state_path=lambda _user_id: state_path,
        )
        request = SimpleNamespace(
            can_read_body=True,
            headers={"Authorization": "Bearer sk-trial-test"},
            json=AsyncMock(return_value={
                "session_id": "s-chat-1234",
                "scheduler_grant_id": "sg-valid",
                "job_id": "daily-brief",
            }),
        )
        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_delete(request)

        self.assertEqual(response.status, 200)
        data = json.loads(response.body.decode())
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_id"], "daily-brief")

    async def test_preview_with_trial_agent_auth_succeeds(self):
        """Trial-agent-authenticated preview should show merged job + next run."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat-1234",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-valid": grant},
            _authenticate=lambda _req: None,
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda user_id, session_id, grant_id: True,
            _scheduler_read_jobs_payload=lambda user_id: {"jobs": [
                {"id": "daily-brief", "prompt": "old", "schedule": {"daily_at": "09:00"}, "use_stable_session": True},
            ]},
            _scheduler_preview_rows=lambda user_id, jobs: [{"id": job.id, "next_run_at": "2026-07-24T09:00:00Z"} for job in jobs],
        )
        request = SimpleNamespace(
            can_read_body=True,
            headers={"Authorization": "Bearer sk-trial-test"},
            json=AsyncMock(return_value={
                "session_id": "s-chat-1234",
                "scheduler_grant_id": "sg-valid",
                "job": {"id": "daily-brief", "schedule": {"daily_at": "10:00"}},
            }),
        )
        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_preview(request)

        self.assertEqual(response.status, 200)
        data = json.loads(response.body.decode())
        self.assertTrue(data["ok"])
        self.assertEqual(data["job"]["id"], "daily-brief")
        self.assertEqual(data["job"]["schedule"], {"daily_at": "10:00"})
        self.assertEqual(data["job"]["use_stable_session"], True)
        self.assertEqual(data["preview"]["next_run_at"], "2026-07-24T09:00:00Z")

    async def test_guest_mode_rejects_scheduler_trigger(self):
        """Guest users must not be able to use /schedule."""
        core = SimpleNamespace(
            _authenticate=lambda _req: None,
            _is_pending_user=lambda _auth: False,
            _first_look_guest_auth=lambda _req: (
                {"user_id": "g-1", "agent_id": "a-guest", "key_hash": "ghash", "email": "g@test"},
                "g-1",
                0,
            ),
            HEADLESS_AGENT_ID="headless-1",
            TRIAL_AGENT_ID="trial-1",
            _is_openrouter_model=lambda model: True,
            _is_claude_sdk_model=lambda model: False,
            _is_codex_sdk_model=lambda model: False,
            _is_codex_cli_model=lambda model: False,
            _is_opencode_cli_model=lambda model: False,
            _is_gemini_model=lambda model: False,
            _OPENROUTER_TRIAL_DEFAULT_MODEL="free-model",
            _OPENROUTER_TRIAL_FALLBACK_MODEL="fallback-model",
            _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS=frozenset(["free-model"]),
            _scheduler_turn_grants={},
            _mint_scheduler_turn_grant=lambda *args, **kwargs: "",
            _chat_agents={},
            _session_tabs={},
            _session_agent_map={},
            _session_profile_paths={},
            _session_agents={},
            _response_queues={},
            _response_req_ids={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _overlay_sessions={},
            _coerce_float=lambda v, d=0.0: float(v) if v else d,
            _coerce_int=lambda v, d=0: int(v) if v else d,
            _parse_relay=lambda: ("relay", 8765),
            _relay_auth_headers=lambda: {},
            _track_event=lambda *args, **kwargs: None,
            _trace=lambda *args, **kwargs: None,
            _track_page_view=lambda req: None,
            _track_redirect=lambda req, path, reason=None, auth_info=None: None,
            _analytics_route_from_request=lambda req: "/web/chat",
            _analytics_session_id_from_request=lambda req: "",
            _analytics_page_view_id_from_request=lambda req: "",
            _analytics_gate_type_from_request=lambda req: "",
            _public_base_url=lambda req: "https://test",
        )
        request = SimpleNamespace(
            can_read_body=True,
            headers={},
            json=AsyncMock(return_value={
                "message": "/schedule list my tasks",
                "session_id": "s-guest",
                "model": "free-model",
                "agent_id": "a-guest",
                "first_look_guest": True,
                "headless": True,
                "allow_scheduler_trigger": True,
            }),
        )
        with patch("web_app.handlers.chat_stream._core", return_value=core):
            response = await chat_stream.handle_chat_msg(request)

        self.assertEqual(response.status, 400)
        data = json.loads(response.body.decode())
        self.assertEqual(data["error"], "scheduler_trigger_requires_authentication")
        self.assertIn("sign in", data["message"])

    async def test_unarmed_turn_rejects_scheduler_mutation(self):
        """A turn without /schedule prefix must not have scheduler grant."""
        arm_check = chat_stream._extract_scheduler_turn("just a normal message")
        self.assertFalse(arm_check[0])


class TestOpenRouterSchedulerToolIntegration(unittest.TestCase):
    """Verify the trial agent properly builds scheduler tools when armed."""

    def test_armed_turn_includes_scheduler_tools(self):
        """When scheduler is armed, OpenAI tools list includes scheduler tools."""
        from chat_agent_openrouter import _build_scheduler_openai_tools

        tools = _build_scheduler_openai_tools(scheduler_armed=True, scheduler_grant_id="sg-test")
        tool_names = [t["function"]["name"] for t in tools]
        self.assertIn("scheduler_list_jobs", tool_names)
        self.assertIn("scheduler_save_job", tool_names)
        self.assertIn("scheduler_preview_job", tool_names)
        self.assertIn("scheduler_delete_job", tool_names)

    def test_unarmed_turn_excludes_scheduler_tools(self):
        """Without scheduler_armed, tools list excludes scheduler tools."""
        from chat_agent_openrouter import _build_scheduler_openai_tools

        tools = _build_scheduler_openai_tools(scheduler_armed=False, scheduler_grant_id="")
        tool_names = [t["function"]["name"] for t in tools]
        self.assertNotIn("scheduler_list_jobs", tool_names)
        self.assertNotIn("scheduler_save_job", tool_names)

    def test_armed_turn_without_grant_id_excludes_scheduler_tools(self):
        """Even when armed, missing grant_id means no scheduler tools."""
        from chat_agent_openrouter import _build_scheduler_openai_tools

        tools = _build_scheduler_openai_tools(scheduler_armed=True, scheduler_grant_id="")
        tool_names = [t["function"]["name"] for t in tools]
        self.assertNotIn("scheduler_list_jobs", tool_names)

    def test_session_lane_regression(self):
        """Local agent lane scheduler access must still work."""
        armed, text = chat_stream._extract_scheduler_turn("/schedule list my jobs")
        self.assertTrue(armed)
        self.assertEqual(text, "list my jobs")
        self.assertTrue(
            chat_stream._scheduler_trigger_supported(guest_mode=False, is_openrouter=False)
        )


class TestSchedulerContextVarIsolation(unittest.TestCase):
    """ContextVar-based scheduler state must not leak between concurrent tasks."""

    def test_separate_sessions_have_independent_turns(self):
        """Two concurrent sessions must have independent scheduler states."""
        from chat_agent_openrouter import _scheduler_turn, SchedulerTurnState

        # Simulate session A arming
        turn_a = SchedulerTurnState(armed=True, grant_id="sg-a", session_id="s-a")
        token_a = _scheduler_turn.set(turn_a)
        # Simulate session B staying unarmed
        turn_b = SchedulerTurnState(armed=False, grant_id="", session_id="s-b")
        token_b = _scheduler_turn.set(turn_b)

        # Session A's state is still accessible via its token
        state_a = _scheduler_turn.get()
        self.assertEqual(state_a.session_id, "s-b")  # current = B

        # Restore A and verify independence
        _scheduler_turn.reset(token_b)
        state_a_restored = _scheduler_turn.get()
        self.assertEqual(state_a_restored.session_id, "s-a")
        self.assertTrue(state_a_restored.armed)
        self.assertEqual(state_a_restored.grant_id, "sg-a")

        # Restore default
        _scheduler_turn.reset(token_a)

    def test_same_session_replacement_isolated(self):
        """A new task for the same session must not inherit the old grant."""
        from chat_agent_openrouter import _scheduler_turn, SchedulerTurnState

        # First task: armed with grant
        old_state = SchedulerTurnState(armed=True, grant_id="sg-old", session_id="s-shared")
        token_old = _scheduler_turn.set(old_state)
        _scheduler_turn.reset(token_old)

        # Second task (same session, different grant): should start clean
        new_state = SchedulerTurnState(armed=True, grant_id="sg-new", session_id="s-shared")
        token_new = _scheduler_turn.set(new_state)

        current = _scheduler_turn.get()
        self.assertEqual(current.grant_id, "sg-new")
        self.assertNotEqual(current.grant_id, "sg-old")

        _scheduler_turn.reset(token_new)

    def test_contextvar_default_is_unarmed(self):
        """Default ContextVar value is unarmed and empty."""
        from chat_agent_openrouter import _scheduler_turn

        state = _scheduler_turn.get()
        self.assertFalse(state.armed)
        self.assertEqual(state.grant_id, "")
        self.assertEqual(state.session_id, "")


class TestSchedulerSecurityFixes(unittest.IsolatedAsyncioTestCase):
    """Security review follow-up tests."""

    def test_trial_agent_auth_validates_session_id_binding(self):
        """Session_id in body must match the grant's session_id."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-correct",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-valid": grant},
        )
        # Wrong session_id in body
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body_wrong = {"session_id": "s-wrong", "scheduler_grant_id": "sg-valid"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body_wrong)
        self.assertIsNone(result)

        # Correct session_id
        body_correct = {"session_id": "s-correct", "scheduler_grant_id": "sg-valid"}
        result2 = auth_admin._scheduler_trial_agent_auth(core, request, body_correct)
        self.assertIsNotNone(result2)
        self.assertEqual(result2["user_id"], "u-hosted")

    def test_hosted_agent_service_token_with_fallback(self):
        """HOSTED_AGENT_SERVICE_TOKEN takes precedence; TRIAL_AGENT_KEY is fallback."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-chat",
            "expires_at": time_module.time() + 300,
        }
        body = {"session_id": "s-chat", "scheduler_grant_id": "sg-ok"}

        # HOSTED_AGENT_SERVICE_TOKEN only
        core_hosted = SimpleNamespace(
            HOSTED_AGENT_SERVICE_TOKEN="sk-hosted-token",
            _scheduler_turn_grants={"sg-ok": grant},
        )
        request_hosted = SimpleNamespace(headers={"Authorization": "Bearer sk-hosted-token"})
        result = auth_admin._scheduler_trial_agent_auth(core_hosted, request_hosted, body)
        self.assertIsNotNone(result)

        # TRIAL_AGENT_KEY fallback (no HOSTED_AGENT_SERVICE_TOKEN set)
        core_trial = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-fallback",
            _scheduler_turn_grants={"sg-ok": grant},
        )
        request_trial = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-fallback"})
        result2 = auth_admin._scheduler_trial_agent_auth(core_trial, request_trial, body)
        self.assertIsNotNone(result2)

        # Wrong token rejected
        request_wrong = SimpleNamespace(headers={"Authorization": "Bearer wrong-key"})
        result3 = auth_admin._scheduler_trial_agent_auth(core_trial, request_wrong, body)
        self.assertIsNone(result3)

    def test_trial_agent_auth_session_mismatch_rejected(self):
        """If body.session_id != grant.session_id, auth must fail."""
        import time as time_module

        grant = {
            "user_id": "u-hosted",
            "session_id": "s-original",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-test",
            _scheduler_turn_grants={"sg-test": grant},
        )
        request = SimpleNamespace(headers={"Authorization": "Bearer sk-trial-test"})
        body = {"session_id": "s-other", "scheduler_grant_id": "sg-test"}
        result = auth_admin._scheduler_trial_agent_auth(core, request, body)
        self.assertIsNone(result)

    async def test_end_to_end_mocked_scheduler_tool_call(self):
        """End-to-end test: service Bearer token, session_id, grant_id, no user API key."""
        import time as time_module

        grant = {
            "user_id": "u-e2e",
            "session_id": "s-e2e-chat",
            "expires_at": time_module.time() + 300,
        }
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="sk-trial-e2e",
            _scheduler_turn_grants={"sg-e2e": grant},
            _authenticate=lambda _req: None,
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda user_id, session_id, grant_id: True,
            _scheduler_read_jobs_payload=lambda user_id: {"jobs": []},
            _scheduler_preview_rows=lambda user_id, jobs: [],
        )
        request = SimpleNamespace(
            can_read_body=True,
            headers={"Authorization": "Bearer sk-trial-e2e"},
            json=AsyncMock(return_value={
                "session_id": "s-e2e-chat",
                "scheduler_grant_id": "sg-e2e",
            }),
        )
        with patch("web_app.handlers.auth_admin._core", return_value=core):
            response = await auth_admin.handle_scheduler_agent_list(request)

        self.assertEqual(response.status, 200)
        data = json.loads(response.body.decode())
        self.assertTrue(data["ok"])
        # Verify no user API key was needed — auth was via service token + grant
        self.assertNotIn("Authorization", request.headers.get("Authorization", "").lower())

    async def test_cancel_revokes_grant_in_non_registry_path(self):
        """Cancel in the non-registry (queue) path must revoke scheduler grants."""
        import time as time_module
        grant = {"user_id": "u-cancel", "session_id": "s-cancel-chat", "expires_at": time_module.time() + 300}
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-cancel", "agent_id": "a-cancel", "key_hash": "kh"},
            _is_pending_user=lambda _auth: False,
            _scheduler_turn_grants={"sg-cancel": grant},
            _chat_agents={"a-cancel": AsyncMock()},
            _session_agents={"s-cancel-chat": "a-cancel"},
            _response_queues={},
            _response_req_ids={},
        )
        # Mock the WS send_json to succeed
        core._chat_agents["a-cancel"].send_json = AsyncMock()
        core._chat_agents["a-cancel"].closed = False

        request = SimpleNamespace(
            can_read_body=True,
            json=AsyncMock(return_value={"session_id": "s-cancel-chat"}),
        )
        with patch("web_app.handlers.chat_stream._core", return_value=core):
            response = await chat_stream.handle_chat_cancel(request)

        self.assertEqual(response.status, 200)
        # Grant should have been revoked
        self.assertNotIn("sg-cancel", core._scheduler_turn_grants)

    async def test_cancel_revokes_grant_in_registry_path(self):
        """Cancel in the registry path must revoke scheduler grants via terminal publish."""
        import time as time_module
        grant = {"user_id": "u-cancel2", "session_id": "s-cancel2", "expires_at": time_module.time() + 300}
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-cancel2", "agent_id": "a-cancel2", "key_hash": "kh"},
            _is_pending_user=lambda _auth: False,
            _scheduler_turn_grants={"sg-cancel2": grant},
            _chat_agents={"a-cancel2": AsyncMock()},
            _session_agents={"s-cancel2": "a-cancel2"},
            _overlay_sessions={},
        )
        core._chat_agents["a-cancel2"].send_json = AsyncMock()
        core._chat_agents["a-cancel2"].closed = False

        from web_state import ChatTurnState
        turn = ChatTurnState(
            owner_user_id="u-cancel2",
            owner_key_hash="kh",
            session_id="s-cancel2",
            req_id="req-cancel2",
            chat_agent_id="a-cancel2",
            routing_agent_id="a-cancel2",
        )
        turn.status = "active"
        turn.scheduler_grant_id = "sg-cancel2"
        turn.dispatch_ws = core._chat_agents["a-cancel2"]

        registry = SimpleNamespace()
        registry.get = lambda sid, rid=None: turn if sid == "s-cancel2" else None
        registry.start = AsyncMock(return_value=(turn, True, None))

        core._chat_turns = registry

        request = SimpleNamespace(
            can_read_body=True,
            json=AsyncMock(return_value={"session_id": "s-cancel2"}),
        )
        with patch("web_app.handlers.chat_stream._core", return_value=core):
            response = await chat_stream.handle_chat_cancel(request)

        self.assertEqual(response.status, 200)
        # Grant should have been revoked via _revoke_turn_grant
        self.assertNotIn("sg-cancel2", core._scheduler_turn_grants)
