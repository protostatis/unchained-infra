"""Tests for local chat archive restore and archive ID handling."""

from __future__ import annotations

import asyncio
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
from conversation_transcript import SESSION_SCHEMA_VERSION


class TestChatAgentCliArchives(unittest.IsolatedAsyncioTestCase):
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
                "opencode_session": {},
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
                        "opencode_session": {
                            "chat_session_id": restored_sid,
                            "session_id": "opencode-restored",
                        },
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
        self.assertEqual(mod.opencode_sessions.get(restored_sid), "opencode-restored")
        self.assertNotIn(current_sid, mod.claude_sessions)

        previews = {entry["preview"] for entry in mod._list_archives(limit=10)}
        self.assertIn("restored chat", previews)
        self.assertIn("current chat", previews)

    def test_sync_active_slot_targets_new_chat_clear(self):
        mod = self._load_module()
        mod._save_meta({"active_slot": 1})
        mod._save_chat({"messages": [{"role": "user", "content": "slot one"}]}, 1)
        mod._save_chat({"messages": [{"role": "user", "content": "slot two"}]}, 2)

        current = mod._sync_active_slot(mod._normalize_slot("2"), "test")
        mod._clear_slot(current)

        self.assertEqual(current, 2)
        self.assertEqual(mod._active_slot(), 2)
        self.assertEqual(mod._load_chat(2)["messages"], [])
        self.assertEqual(mod._load_chat(1)["messages"][0]["content"], "slot one")

    def test_invalid_slot_does_not_sync_active_slot(self):
        mod = self._load_module()
        mod._save_meta({"active_slot": 1})

        self.assertIsNone(mod._normalize_slot("abc"))
        current = mod._sync_active_slot(mod._normalize_slot("abc"), "test")

        self.assertEqual(current, 1)
        self.assertEqual(mod._active_slot(), 1)

    def test_restore_archive_into_explicit_slot_does_not_use_active_slot(self):
        mod = self._load_module()
        mod._save_meta({"active_slot": 1})
        mod._save_chat({"messages": [{"role": "user", "content": "active slot"}]}, 1)
        mod._save_chat({"messages": [{"role": "user", "content": "target slot"}]}, 2)
        os.makedirs(mod.ARCHIVE_DIR, exist_ok=True)
        with open(os.path.join(mod.ARCHIVE_DIR, "arc.json"), "w") as f:
            json.dump(
                {
                    "slot_data": {
                        "messages": [{"role": "user", "content": "restored into two"}],
                        "claude_session": {},
                        "codex_session": {},
                        "opencode_session": {},
                    },
                    "archived_at": 123,
                    "slot": 1,
                    "preview": "restored into two",
                    "message_count": 1,
                },
                f,
            )

        slot_data, _ = mod._restore_archive_into_slot("arc", 2)

        self.assertIsNotNone(slot_data)
        self.assertEqual(mod._load_chat(1)["messages"][0]["content"], "active slot")
        self.assertEqual(mod._load_chat(2)["messages"][0]["content"], "restored into two")

    async def test_new_chat_cancellation_stops_task_and_process_before_cleanup(self):
        mod = self._load_module()
        sid = "s-agent-active"

        async def running_turn():
            await asyncio.Event().wait()

        class FakeProcess:
            pid = 4242
            returncode = None

            async def wait(self):
                return self.returncode

        task = asyncio.create_task(running_turn())
        await asyncio.sleep(0)
        proc = FakeProcess()
        mod.active_tasks[sid] = task
        mod.active_procs[sid] = proc

        def kill_process(target):
            target.returncode = -9

        with patch.object(mod, "_kill_process", side_effect=kill_process) as kill:
            stopped = await mod._cancel_active_session_work(sid, timeout=0.2)

        self.assertTrue(stopped)
        self.assertTrue(task.cancelled())
        kill.assert_called_once_with(proc)
        self.assertNotIn(sid, mod.active_tasks)
        self.assertNotIn(sid, mod.active_procs)

    async def test_old_task_callback_cannot_remove_newer_turn_state(self):
        mod = self._load_module()
        sid = "s-agent-race"
        old_task = asyncio.create_task(asyncio.sleep(0))
        new_task = asyncio.create_task(asyncio.sleep(0))
        new_proc = object()
        mod.active_tasks[sid] = new_task
        mod.active_procs[sid] = new_proc

        mod._finish_active_task(sid, old_task)

        self.assertIs(mod.active_tasks[sid], new_task)
        self.assertIs(mod.active_procs[sid], new_proc)
        await old_task
        await new_task

    async def test_new_chat_cancellation_timeout_keeps_active_state(self):
        mod = self._load_module()
        sid = "s-agent-stuck"
        release = asyncio.Event()

        async def stubborn_turn():
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = asyncio.create_task(stubborn_turn())
        await asyncio.sleep(0)
        mod.active_tasks[sid] = task

        stopped = await mod._cancel_active_session_work(sid, timeout=0.01)

        self.assertFalse(stopped)
        self.assertIs(mod.active_tasks[sid], task)
        release.set()
        await asyncio.wait_for(task, timeout=0.2)
        mod.active_tasks.clear()


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
            _is_opencode_cli_model=lambda model: False,
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

        request = SimpleNamespace(query={"model": "claude-sonnet-4-6", "session_id": "s-agent-current", "slot": "2"})
        response = await handle_chat_history(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["session_id"], "s-agent-restored")
        self.assertEqual(data["messages"][0]["content"], "restored chat")
        core._agent_request.assert_awaited_once_with(
            "claude-abc12345",
            {"type": "get_history", "session_id": "s-agent-current", "slot": 2},
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_new_passes_valid_slot_to_agent(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new

        core = self._core_stub()
        core._agent_request.return_value = {"active_slot": 3, "session_id": "s-agent-next"}
        mock_core.return_value = core

        request = SimpleNamespace(
            json=AsyncMock(return_value={"model": "claude-sonnet-4-6", "session_id": "s-agent-current", "slot": "3"})
        )
        response = await handle_chat_new(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["active_slot"], 3)
        core._agent_request.assert_awaited_once_with(
            "claude-abc12345",
            {"type": "new_chat", "session_id": "s-agent-current", "slot": 3},
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_new_does_not_acknowledge_agent_cancellation_failure(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new

        core = self._core_stub()
        core._agent_request.return_value = {
            "ok": False,
            "error": "Could not stop the active task; chat was not cleared",
        }
        mock_core.return_value = core
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "claude-sonnet-4-6",
                    "session_id": "s-agent-current",
                    "slot": "3",
                }
            )
        )

        response = await handle_chat_new(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 503)
        self.assertIn("not cleared", data["error"])

    @patch("web_app.handlers.chat_flow._core")
    async def test_chat_history_omits_invalid_slot(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_history

        core = self._core_stub()
        core._agent_request.return_value = {"messages": []}
        mock_core.return_value = core

        request = SimpleNamespace(query={"model": "claude-sonnet-4-6", "session_id": "s-agent-current", "slot": "abc"})
        response = await handle_chat_history(request)

        self.assertEqual(response.status, 200)
        core._agent_request.assert_awaited_once_with(
            "claude-abc12345",
            {"type": "get_history", "session_id": "s-agent-current"},
        )

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
            json=AsyncMock(return_value={"archive_id": "arc", "model": "claude-sonnet-4-6", "slot": "2"})
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["session_id"], "s-agent-restored")
        self.assertTrue(data["ok"])
        mock_agent_request.assert_awaited_once_with(
            "claude-abc12345",
            {"type": "restore_archive", "archive_id": "arc", "slot": 2},
            timeout=10,
        )

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

    def test_local_template_sends_slot_for_history_new_and_restore(self):
        self.assertIn("slot: activeSlot,", web.CLAUDE_CHAT_HTML)
        self.assertIn("archive_id: id, model: currentModel(), slot: activeSlot", web.CLAUDE_CHAT_HTML)

    def test_local_template_recovers_slot_state_after_failed_switch(self):
        self.assertIn("const previousState = _loadSlotState();", web.CLAUDE_CHAT_HTML)
        self.assertIn("if (data.offline) return;", web.CLAUDE_CHAT_HTML)
        self.assertIn("_saveSlotState(previousState);", web.CLAUDE_CHAT_HTML)


class TestHostedTranscriptHistory(unittest.TestCase):
    def test_history_prefers_full_transcript_over_capped_provider_context(self):
        """Reloaded /workspace history must retain the original prompt."""
        session_id = "s-trial-agent-transcript-history"
        transcript = []
        for index in range(20):
            transcript.extend([
                {"role": "user", "content": f"original prompt {index}"},
                {"role": "assistant", "content": f"answer {index}"},
            ])
        with tempfile.TemporaryDirectory() as tempdir:
            path = os.path.join(tempdir, f"{session_id}.json")
            with open(path, "w") as f:
                json.dump(
                    {
                        "schema_version": SESSION_SCHEMA_VERSION,
                        "messages": transcript[-8:],
                        "transcript": transcript,
                    },
                    f,
                )
            with patch.object(web, "_trial_session_path", return_value=path):
                messages, found = web._read_trial_history(session_id)

        self.assertTrue(found)
        self.assertEqual(messages, transcript)
        self.assertEqual(messages[0]["content"], "original prompt 0")


if __name__ == "__main__":
    unittest.main()
