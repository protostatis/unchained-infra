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
        self.assertFalse(
            chat_stream._scheduler_trigger_supported(guest_mode=False, is_openrouter=True)
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
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: True,
            _scheduler_read_jobs_payload=lambda _user_id: {"jobs": []},
            _scheduler_write_jobs_payload=lambda _user_id, payload: writes.append(payload),
            _scheduler_preview_rows=lambda _user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
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
        core = SimpleNamespace(
            _authenticate=lambda _req: {"user_id": "u-1"},
            _is_pending_user=lambda _auth: False,
            _validate_scheduler_turn_grant=lambda _user_id, _session_id, _grant_id: True,
            _scheduler_read_jobs_payload=lambda _user_id: existing_payload,
            _scheduler_write_jobs_payload=lambda _user_id, payload: writes.append(payload),
            _scheduler_preview_rows=lambda _user_id, jobs: [{"id": job.id, "next_run_at": None} for job in jobs],
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


if __name__ == "__main__":
    unittest.main()
