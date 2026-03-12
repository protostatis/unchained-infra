"""Tests for local chat archive restore and archive ID handling."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))

import web


class TestChatAgentCliArchives(unittest.TestCase):
    def _load_module(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        env_patch = patch.dict(os.environ, {"UNCHAINED_DATA_DIR": tempdir.name}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        module_name = "chat_agent_cli_archive_test"
        sys.modules.pop(module_name, None)
        self.addCleanup(lambda: sys.modules.pop(module_name, None))
        module_path = os.path.join(os.path.dirname(__file__), "chat_agent_cli.py")
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    def test_archive_slot_ids_stay_unique_with_same_timestamp(self):
        mod = self._load_module()
        mod._save_chat({"messages": [{"role": "user", "content": "first"}]}, 1)
        uuids = [
            SimpleNamespace(hex="aaaabbbbccccdddd"),
            SimpleNamespace(hex="eeeeffff00001111"),
        ]
        with patch.object(mod.time, "time", side_effect=[1000.1234] * 4):
            with patch.object(mod.uuid, "uuid4", side_effect=uuids):
                first_id = mod._archive_slot(1)
                mod._save_chat({"messages": [{"role": "user", "content": "second"}]}, 1)
                second_id = mod._archive_slot(1)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(
            sorted(os.listdir(mod.ARCHIVE_DIR)),
            [f"{first_id}.json", f"{second_id}.json"],
        )

    def test_restore_archive_into_slot_archives_current_chat_and_restores_session(self):
        mod = self._load_module()
        mod._save_meta({"active_slot": 1})
        current_sid = "s-agent-current"
        restored_sid = "s-agent-restored"
        mod._save_chat(
            {
                "messages": [{"role": "user", "content": "current chat"}],
                "claude_session": {
                    "chat_session_id": current_sid,
                    "session_id": "claude-current",
                },
                "codex_session": {},
            },
            1,
        )
        os.makedirs(mod.ARCHIVE_DIR, exist_ok=True)
        with open(os.path.join(mod.ARCHIVE_DIR, "arc.json"), "w") as f:
            json.dump(
                {
                    "slot_data": {
                        "messages": [{"role": "user", "content": "restored chat"}],
                        "claude_session": {
                            "chat_session_id": restored_sid,
                            "session_id": "claude-restored",
                        },
                        "codex_session": {},
                    },
                    "archived_at": 123,
                    "slot": 1,
                    "preview": "restored chat",
                    "message_count": 1,
                },
                f,
            )

        slot_data, chat_session_id = mod._restore_archive_into_slot("arc")

        self.assertIsNotNone(slot_data)
        self.assertEqual(chat_session_id, restored_sid)
        self.assertEqual(mod._load_chat(1)["messages"][0]["content"], "restored chat")
        self.assertEqual(mod.claude_sessions.get(restored_sid), "claude-restored")
        self.assertNotIn(current_sid, mod.claude_sessions)

        previews = {entry["preview"] for entry in mod._list_archives(limit=10)}
        self.assertIn("restored chat", previews)
        self.assertIn("current chat", previews)


class TestChatArchiveHandlers(unittest.IsolatedAsyncioTestCase):
    def _core_stub(self):
        return SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": "u-test",
                "agent_id": "claude-abc12345",
                "key_hash": "abc12345",
                "key": "uc_live_test",
                "email": "dev@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: False,
            _is_claude_sdk_model=lambda model: False,
            _is_codex_sdk_model=lambda model: False,
            _is_codex_cli_model=lambda model: False,
            _resolve_chat_agent_id=lambda auth_info, model: "claude-abc12345",
            _agent_request=AsyncMock(),
            _chat_agents={},
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_history_passes_through_restored_session_id(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_history

        core = self._core_stub()
        core._agent_request.return_value = {
            "messages": [{"role": "user", "content": "restored chat"}],
            "session_id": "s-agent-restored",
        }
        mock_core.return_value = core

        request = SimpleNamespace(query={"model": "claude-sonnet-4-6", "session_id": "s-agent-current"})
        response = await handle_chat_history(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["session_id"], "s-agent-restored")
        self.assertEqual(data["messages"][0]["content"], "restored chat")
        core._agent_request.assert_awaited_once()

    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_restore_archive_returns_session_id(self, mock_core, mock_agent_request):
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        core = self._core_stub()
        core._chat_agents["claude-abc12345"] = SimpleNamespace(closed=False)
        mock_core.return_value = core
        mock_agent_request.return_value = {
            "type": "restore_archive_ok",
            "active_slot": 1,
            "session_id": "s-agent-restored",
        }

        request = SimpleNamespace(
            json=AsyncMock(return_value={"archive_id": "arc", "model": "claude-sonnet-4-6"})
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["session_id"], "s-agent-restored")
        self.assertTrue(data["ok"])

    @patch("web_app.handlers.chat_flow.asyncio.sleep", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_restore_archive_waits_for_reconnect_before_single_send(
        self, mock_core, mock_agent_request, mock_sleep
    ):
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        core = self._core_stub()
        core._chat_agents["claude-abc12345"] = SimpleNamespace(closed=True)
        mock_core.return_value = core
        mock_agent_request.return_value = {
            "type": "restore_archive_ok",
            "active_slot": 2,
            "session_id": "s-agent-restored",
        }

        async def reconnect(_delay):
            core._chat_agents["claude-abc12345"] = SimpleNamespace(closed=False)

        mock_sleep.side_effect = reconnect
        request = SimpleNamespace(
            json=AsyncMock(return_value={"archive_id": "arc", "model": "claude-sonnet-4-6"})
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["session_id"], "s-agent-restored")
        self.assertEqual(mock_agent_request.await_count, 1)
        mock_sleep.assert_awaited_once()

    @patch("web_app.handlers.chat_flow.asyncio.sleep", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_restore_archive_returns_reconnect_error_when_still_offline(
        self, mock_core, mock_agent_request, mock_sleep
    ):
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        core = self._core_stub()
        core._chat_agents["claude-abc12345"] = SimpleNamespace(closed=True)
        mock_core.return_value = core
        mock_agent_request.return_value = None
        request = SimpleNamespace(
            json=AsyncMock(return_value={"archive_id": "arc", "model": "claude-sonnet-4-6"})
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 503)
        self.assertIn("reconnecting", data["error"])
        mock_sleep.assert_awaited_once()

    @patch("web_app.handlers.chat_flow.asyncio.sleep", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_restore_archive_does_not_replay_after_lost_response(
        self, mock_core, mock_agent_request, mock_sleep
    ):
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        core = self._core_stub()
        core._chat_agents["claude-abc12345"] = SimpleNamespace(closed=False)
        mock_core.return_value = core
        mock_agent_request.return_value = None

        request = SimpleNamespace(
            json=AsyncMock(return_value={"archive_id": "arc", "model": "claude-sonnet-4-6"})
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 503)
        self.assertIn("reconnecting", data["error"])
        self.assertEqual(mock_agent_request.await_count, 1)
        mock_sleep.assert_not_awaited()


class TestLocalArchiveTemplate(unittest.TestCase):
    def test_restore_archive_persists_restored_session_before_reload(self):
        self.assertIn("if (data.session_id) {", web.CLAUDE_CHAT_HTML)
        self.assertIn("_setActiveSlotSession(sessionId);", web.CLAUDE_CHAT_HTML)

    def test_local_template_defines_slot_session_helpers(self):
        self.assertIn("function _slotStateKey() {", web.CLAUDE_CHAT_HTML)
        self.assertIn("function _setActiveSlotSession(sid) {", web.CLAUDE_CHAT_HTML)

    def test_local_template_recovers_slot_state_after_failed_switch(self):
        self.assertIn("const previousState = _loadSlotState();", web.CLAUDE_CHAT_HTML)
        self.assertIn("if (data.offline) return;", web.CLAUDE_CHAT_HTML)
        self.assertIn("_saveSlotState(previousState);", web.CLAUDE_CHAT_HTML)


if __name__ == "__main__":
    unittest.main()
