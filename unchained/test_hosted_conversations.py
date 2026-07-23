"""Comprehensive tests for hosted conversation repository and trial integrations."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from hosted_conversations import HostedConversationRepo


def _json_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _hold_slot_state_lock(
    data_dir: str,
    sessions_dir: str,
    user_id: str,
    acquired,
    release,
) -> None:
    repo = HostedConversationRepo(data_dir=data_dir, sessions_dir=sessions_dir)
    with repo._slot_state_lock(user_id):
        acquired.set()
        if not release.wait(10):
            raise RuntimeError("timed out waiting to release slot-state lock")


def _bind_slot_in_process(
    data_dir: str,
    sessions_dir: str,
    user_id: str,
    session_id: str,
    started,
    results,
) -> None:
    repo = HostedConversationRepo(data_dir=data_dir, sessions_dir=sessions_dir)
    started.set()
    results.put((session_id, repo.bind_initial_session(user_id, session_id, slot=1)))


class TestHostedConversationRepo(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temp.name) / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.repo = HostedConversationRepo(
            data_dir=self.temp.name, sessions_dir=str(self.session_dir)
        )

    def tearDown(self):
        self.temp.cleanup()

    def _write_session(self, session_id: str, messages: list[dict]) -> str:
        path = self.repo.session_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)
        return path

    # --- Slot state ---

    def test_default_slot_state(self):
        state = self.repo.get_slot_state("u-test-1")
        self.assertEqual(state["active_slot"], 1)
        self.assertEqual(state["slots"]["1"], "")
        self.assertEqual(state["slots"]["2"], "")
        self.assertEqual(state["slots"]["3"], "")

    def test_slot_state_persistence(self):
        state = self.repo.get_slot_state("u-test-2")
        state["active_slot"] = 2
        state["slots"]["2"] = "s-agent-slot2"
        state["previews"]["2"] = "Hello world"
        self.repo.set_slot_state("u-test-2", state)

        reloaded = self.repo.get_slot_state("u-test-2")
        self.assertEqual(reloaded["active_slot"], 2)
        self.assertEqual(reloaded["slots"]["2"], "s-agent-slot2")
        self.assertEqual(reloaded["previews"]["2"], "Hello world")

    def test_slot_state_user_isolation(self):
        self.repo.set_slot_state(
            "u-a",
            {"active_slot": 1, "slots": {"1": "s-a", "2": "", "3": ""}},
        )
        self.repo.set_slot_state(
            "u-b",
            {"active_slot": 3, "slots": {"1": "", "2": "s-b", "3": ""}},
        )
        self.assertEqual(self.repo.get_slot_state("u-a")["slots"]["1"], "s-a")
        self.assertEqual(self.repo.get_slot_state("u-b")["slots"]["2"], "s-b")
        self.assertNotEqual(
            self.repo._safe_user_key("u-a"),
            self.repo._safe_user_key("u-b"),
        )

    # --- Session IDs ---

    def test_new_session_id_is_stable_with_request_id(self):
        sid1 = self.repo.new_session_id("u-1", "trial-agent", request_id="req-1")
        sid2 = self.repo.new_session_id("u-1", "trial-agent", request_id="req-1")
        self.assertEqual(sid1, sid2)
        self.assertTrue(sid1.startswith("s-trial-agent-"))
        self.assertEqual(len(sid1.split("-")[-1]), 24)

    def test_new_session_id_differs_without_request_id(self):
        sid1 = self.repo.new_session_id("u-1", "trial-agent")
        sid2 = self.repo.new_session_id("u-1", "trial-agent")
        self.assertNotEqual(sid1, sid2)

    def test_new_session_id_is_user_scoped(self):
        sid_a = self.repo.new_session_id("u-a", "trial-agent", request_id="req-1")
        sid_b = self.repo.new_session_id("u-b", "trial-agent", request_id="req-1")
        self.assertNotEqual(sid_a, sid_b)

    # --- Archives ---

    def test_archive_session_creates_archive_and_returns_id(self):
        sid = "s-trial-agent-test-archive-1"
        self._write_session(sid, [
            {"role": "user", "content": "Find flights to Tokyo"},
            {"role": "assistant", "content": "Here are the results..."},
        ])
        archive_id = self.repo.archive_session(
            "u-test", sid, slot=1, preview="Find flights",
        )
        self.assertIsNotNone(archive_id)
        archives = self.repo.list_archives("u-test")
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["archive_id"], archive_id)
        self.assertEqual(archives[0]["message_count"], 2)
        self.assertEqual(archives[0]["slot"], 1)

    def test_archive_session_returns_none_for_missing_session(self):
        archive_id = self.repo.archive_session(
            "u-test", "s-nonexistent", sessions_dir=str(self.session_dir),
        )
        self.assertIsNone(archive_id)

    def test_archive_session_returns_none_for_empty_session(self):
        sid = "s-trial-agent-empty"
        self._write_session(sid, [])
        archive_id = self.repo.archive_session(
            "u-test", sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNone(archive_id)

    def test_list_archives_oldest_first_and_respects_limit(self):
        for i in range(5):
            sid = f"s-trial-agent-arc-{i}"
            self._write_session(sid, [{"role": "user", "content": f"msg {i}"}])
            self.repo.archive_session(
                "u-limit", sid, sessions_dir=str(self.session_dir),
            )
        archives = self.repo.list_archives("u-limit", limit=3)
        self.assertEqual(len(archives), 3)
        # Sorted newest-first by archive_id (timestamp-based)
        self.assertGreater(archives[0]["archived_at"], archives[-1]["archived_at"])

    def test_restore_archive_returns_session_and_messages(self):
        sid = "s-trial-agent-archive-src"
        self._write_session(sid, [
            {"role": "user", "content": "Research Python"},
            {"role": "assistant", "content": "Python was created..."},
        ])
        archive_id = self.repo.archive_session(
            "u-restore", sid, slot=2, preview="Research Python",
            sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        # Restore with a specific agent_id so the new session is scoped
        # to the authenticated account (not the shared trial-agent lane).
        new_sid, slot, msgs = self.repo.restore_archive(
            "u-restore", archive_id, target_slot=3,
            sessions_dir=str(self.session_dir),
            agent_id="claude-testhash",
        )
        self.assertIsNotNone(new_sid)
        self.assertTrue(new_sid.startswith("s-claude-testhash-"))
        self.assertEqual(slot, 3)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["content"], "Research Python")

        # Verify restored session file exists
        restored_path = self.repo.session_path(
            new_sid, sessions_dir=str(self.session_dir)
        )
        self.assertTrue(os.path.isfile(restored_path))

        # Verify slot state updated
        state = self.repo.get_slot_state("u-restore")
        self.assertEqual(state["slots"]["3"], new_sid)
        self.assertEqual(state["active_slot"], 3)

    def test_restore_archive_uses_archive_slot_when_no_target(self):
        sid = "s-trial-agent-archive-noslot"
        self._write_session(sid, [{"role": "user", "content": "Hi"}])
        archive_id = self.repo.archive_session(
            "u-noslot", sid, slot=2, sessions_dir=str(self.session_dir),
        )
        # Default agent_id="trial-agent" — test the legacy shared lane path.
        _, slot, _ = self.repo.restore_archive(
            "u-noslot", archive_id, sessions_dir=str(self.session_dir),
        )
        self.assertEqual(slot, 2)

    def test_restore_nonexistent_archive_returns_none(self):
        new_sid, slot, msgs = self.repo.restore_archive(
            "u-test", "nonexistent-id", sessions_dir=str(self.session_dir),
        )
        self.assertIsNone(new_sid)
        self.assertEqual(slot, 0)
        self.assertEqual(msgs, [])

    def test_delete_archive(self):
        sid = "s-trial-agent-del"
        self._write_session(sid, [{"role": "user", "content": "to delete"}])
        archive_id = self.repo.archive_session(
            "u-test", sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)
        self.assertTrue(self.repo.archive_owned_by("u-test", archive_id))

        self.assertTrue(self.repo.delete_archive("u-test", archive_id))
        self.assertFalse(self.repo.archive_owned_by("u-test", archive_id))
        self.assertFalse(self.repo.delete_archive("u-test", archive_id))

    def test_delete_nonexistent_archive_returns_false(self):
        self.assertFalse(self.repo.delete_archive("u-test", "nonexistent"))

    # --- Ownership / cross-user denial ---

    def test_cross_user_list_isolation(self):
        sid_a = "s-trial-agent-xa"
        self._write_session(sid_a, [{"role": "user", "content": "User A chat"}])
        self.repo.archive_session(
            "user-a", sid_a, preview="User A chat",
        )

        sid_b = "s-trial-agent-xb"
        self._write_session(sid_b, [{"role": "user", "content": "User B chat"}])
        self.repo.archive_session(
            "user-b", sid_b, preview="User B chat",
        )

        self.assertEqual(len(self.repo.list_archives("user-a")), 1)
        self.assertEqual(len(self.repo.list_archives("user-b")), 1)
        self.assertNotEqual(
            self.repo.list_archives("user-a")[0]["preview"],
            self.repo.list_archives("user-b")[0]["preview"],
        )

    def test_cross_user_cannot_restore_foreign_archive(self):
        sid = "s-trial-agent-foreign"
        self._write_session(sid, [{"role": "user", "content": "Owner's chat"}])
        archive_id = self.repo.archive_session(
            "owner", sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        new_sid, _, _ = self.repo.restore_archive(
            "attacker", archive_id, sessions_dir=str(self.session_dir),
        )
        self.assertIsNone(new_sid)

    def test_cross_user_cannot_delete_foreign_archive(self):
        sid = "s-trial-agent-foreign-del"
        self._write_session(sid, [{"role": "user", "content": "Owner's chat"}])
        archive_id = self.repo.archive_session(
            "owner", sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)
        self.assertFalse(self.repo.delete_archive("attacker", archive_id))
        self.assertTrue(self.repo.archive_owned_by("owner", archive_id))

    # --- Path traversal safety ---

    def test_archive_id_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            self.repo._archive_path("u-test", "../etc/passwd")

        with self.assertRaises(ValueError):
            self.repo._archive_path("u-test", "valid/../../etc")

    def test_session_path_rejects_traversal(self):
        """Path traversal session IDs are rejected, not silently stripped."""
        with self.assertRaises(ValueError) as ctx:
            self.repo.session_path("s-agent/../etc/passwd")
        self.assertIn("path traversal", str(ctx.exception).lower())

        with self.assertRaises(ValueError) as ctx:
            self.repo.session_path("/etc/passwd")
        self.assertIn("separator", str(ctx.exception).lower())

    def test_session_path_rejects_null_byte(self):
        with self.assertRaises(ValueError):
            self.repo.session_path("s-agent\0test")

    def test_session_path_accepts_valid_ids(self):
        """Well-formed session IDs construct the expected file path."""
        path = self.repo.session_path("s-trial-agent-abc123def456")
        self.assertIn("s-trial-agent-abc123def456.json", os.path.basename(path))

    def test_legacy_path_still_sanitizes_for_dual_read(self):
        """make_session_path_legacy preserves old sanitization for reading
        historically-stored files that were written before validation."""
        path = HostedConversationRepo.make_session_path_legacy(
            "s-agent/../etc/passwd", sessions_dir=str(self.session_dir)
        )
        self.assertNotIn("..", os.path.basename(path))
        self.assertNotIn("/", os.path.basename(path))
        read_path = os.path.join(os.path.dirname(path), os.path.basename(path))
        # The historic sanitized file can still be read via read_session_messages
        os.makedirs(os.path.dirname(read_path), exist_ok=True)
        with open(read_path, "w") as f:
            json.dump({"messages": [{"role": "user", "content": "legacy"}]}, f)
        msgs, found = self.repo.read_session_messages(
            "s-agent/../etc/passwd", sessions_dir=str(self.session_dir)
        )
        self.assertTrue(found)
        self.assertEqual(msgs[0]["content"], "legacy")

    def test_invalid_archive_id_raises(self):
        with self.assertRaises(ValueError):
            self.repo._archive_path("u-test", "x" * 200)

    # --- Atomic writes ---

    def test_slot_state_atomic_write_never_leaves_tmp(self):
        state = {"active_slot": 3, "slots": {"1": "", "2": "", "3": "s-final"}}
        self.repo.set_slot_state("u-atomic", state)
        # No .tmp files should remain
        tmp_files = list(
            Path(self.repo._user_dir("u-atomic")).glob("*.tmp")
        )
        self.assertEqual(len(tmp_files), 0)
        reloaded = self.repo.get_slot_state("u-atomic")
        self.assertEqual(reloaded["slots"]["3"], "s-final")

    @unittest.skipUnless(os.name == "posix", "advisory slot locks require POSIX")
    def test_initial_slot_binding_serializes_across_processes(self):
        ctx = multiprocessing.get_context("spawn")
        acquired = ctx.Event()
        release = ctx.Event()
        started_a = ctx.Event()
        started_b = ctx.Event()
        results = ctx.Queue()
        user_id = "u-cross-process"
        holder = ctx.Process(
            target=_hold_slot_state_lock,
            args=(self.temp.name, str(self.session_dir), user_id, acquired, release),
        )
        binders = [
            ctx.Process(
                target=_bind_slot_in_process,
                args=(
                    self.temp.name,
                    str(self.session_dir),
                    user_id,
                    session_id,
                    started,
                    results,
                ),
            )
            for session_id, started in (
                ("s-process-a", started_a),
                ("s-process-b", started_b),
            )
        ]
        try:
            holder.start()
            self.assertTrue(acquired.wait(10), "holder did not acquire file lock")
            for process in binders:
                process.start()
            self.assertTrue(started_a.wait(10), "first binder did not start")
            self.assertTrue(started_b.wait(10), "second binder did not start")
            with self.assertRaises(queue.Empty):
                results.get(timeout=0.25)

            release.set()
            outcomes = [results.get(timeout=10), results.get(timeout=10)]
            self.assertEqual(sum(1 for _, won in outcomes if won), 1)
            winner = next(session_id for session_id, won in outcomes if won)
            self.assertEqual(
                self.repo.get_slot_state(user_id)["slots"]["1"], winner
            )
        finally:
            release.set()
            for process in (holder, *binders):
                if process.pid is not None:
                    process.join(timeout=10)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
            results.close()
            results.join_thread()

    def test_archive_atomic_write(self):
        sid = "s-trial-agent-atomic"
        self._write_session(sid, [{"role": "user", "content": "atomic test"}])
        archive_id = self.repo.archive_session(
            "u-atomic-arc", sid, sessions_dir=str(self.session_dir),
        )
        tmp_files = list(
            Path(self.repo._archives_dir("u-atomic-arc")).glob("*.tmp")
        )
        self.assertEqual(len(tmp_files), 0)

    # --- Idempotency ---

    def test_archive_same_session_twice_produces_different_ids(self):
        sid = "s-trial-agent-idem"
        self._write_session(sid, [{"role": "user", "content": "idempotent"}])
        arc1 = self.repo.archive_session(
            "u-idem", sid, sessions_dir=str(self.session_dir),
        )
        arc2 = self.repo.archive_session(
            "u-idem", sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(arc1)
        self.assertIsNotNone(arc2)
        self.assertNotEqual(arc1, arc2)
        self.assertEqual(len(self.repo.list_archives("u-idem")), 2)

    def test_slot_state_get_set_is_idempotent(self):
        state = self.repo.get_slot_state("u-idem-state")
        state["slots"]["1"] = "s-idem-1"
        self.repo.set_slot_state("u-idem-state", state)
        self.repo.set_slot_state("u-idem-state", state)
        self.assertEqual(
            self.repo.get_slot_state("u-idem-state")["slots"]["1"],
            "s-idem-1",
        )


class TestTrialNewChatArchives(unittest.IsolatedAsyncioTestCase):
    """Verify that committed new-chat transitions archive the old session."""

    def setUp(self):
        from web_app.handlers import chat_flow

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()
        chat_flow._guest_new_chat_requests.clear()
        chat_flow._guest_new_chat_sources.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_status_dir = chat_flow._TRIAL_NEW_CHAT_STATUS_DIR
        chat_flow._TRIAL_NEW_CHAT_STATUS_DIR = os.path.join(
            self.temp_dir.name, "transition-status"
        )
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        # Reset the global singleton to pick up the new data dir
        chat_flow._HOSTED_REPO = None

    def tearDown(self):
        from web_app.handlers import chat_flow

        chat_flow._TRIAL_NEW_CHAT_STATUS_DIR = self.original_status_dir
        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    def _core(self, user_id: str = "u-test-001"):
        return SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": user_id,
                "agent_id": "trial-agent",
                "key_hash": "abc12345",
                "key": "uc_live_test",
                "email": "test@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested
            if requested and requested.startswith(f"s-{agent_id}")
            else f"s-{agent_id}",
            _read_trial_history=lambda session_id: (
                [],
                os.path.isfile(
                    HostedConversationRepo.make_session_path(
                        session_id,
                        sessions_dir=str(self.session_dir),
                    )
                ),
            ),
            _delete_trial_session=lambda session_id: os.remove(
                HostedConversationRepo.make_session_path(
                    session_id,
                    sessions_dir=str(self.session_dir),
                )
            )
            if os.path.isfile(
                HostedConversationRepo.make_session_path(
                    session_id,
                    sessions_dir=str(self.session_dir),
                )
            )
            else None,
            _trial_session_path=lambda session_id: HostedConversationRepo.make_session_path(
                session_id, sessions_dir=str(self.session_dir)
            ),
            _session_tabs={},
            _session_agent_map={},
            _session_allowed_tabs={},
            _session_profile_paths={},
            _session_last_active={},
            _chat_preview_generations={},
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            JWT_SECRET="test-jwt-secret",
            _attach_first_look_guest_cookies=Mock(),
        )

    def _write_session(self, session_id: str, messages: list[dict]):
        path = HostedConversationRepo.make_session_path(
            session_id, sessions_dir=str(self.session_dir)
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)

    def _request(self, request_id: str, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "session_id": session_id,
                    "slot": 1,
                }
            )
        )

    def _ack_request(
        self,
        request_id: str,
        old_session: str,
        new_session: str,
        commit_token: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "commit_token": commit_token,
                    "previous_session_id": old_session,
                    "session_id": new_session,
                    "slot": 1,
                }
            )
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_new_chat_ack_archives_old_session(self, mock_core):
        """After ack, old session messages appear in the hosted archive."""
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        old_session = "s-trial-agent-old-session"
        user_id = "u-test-archive"
        core = self._core(user_id)
        core._session_tabs[old_session] = "active-tab"
        mock_core.return_value = core

        # Write session file with real messages
        self._write_session(old_session, [
            {"role": "user", "content": "Hello, find me flights"},
            {"role": "assistant", "content": "Let me search..."},
        ])

        request_id = "request-00000000000000A1"
        reservation = await handle_chat_new(
            self._request(request_id, old_session)
        )
        self.assertEqual(reservation.status, 200)
        data = json.loads(reservation.body.decode())

        # Ack commits the transition
        ack = await handle_chat_new_ack(
            self._ack_request(
                request_id,
                old_session,
                data["session_id"],
                data["commit_token"],
            )
        )
        self.assertEqual(ack.status, 200)

        # Old session file should be deleted
        old_path = HostedConversationRepo.make_session_path(
            old_session, sessions_dir=str(self.session_dir)
        )
        self.assertFalse(os.path.isfile(old_path))

        # Archive should exist in hosted repo
        archives = self.repo().list_archives(user_id)
        self.assertEqual(len(archives), 1)
        self.assertIn("Hello", archives[0]["preview"])
        self.assertEqual(archives[0]["message_count"], 2)

    @patch("web_app.handlers.chat_flow._core")
    async def test_new_chat_never_deletes_history(self, mock_core):
        """The old session messages are preserved as an archive."""
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        old_session = "s-trial-agent-preserve"
        user_id = "u-test-preserve"
        core = self._core(user_id)
        mock_core.return_value = core

        self._write_session(old_session, [
            {"role": "user", "content": "Valuable research"},
        ])

        request_id = "request-00000000000000A2"
        reservation = await handle_chat_new(
            self._request(request_id, old_session)
        )
        data = json.loads(reservation.body.decode())

        await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )

        # Verify archive exists
        archives = self.repo().list_archives(user_id)
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["preview"], "Valuable research")

    @patch("web_app.handlers.chat_flow._core")
    async def test_empty_session_is_not_archived(self, mock_core):
        """A session with no messages should not produce an archive."""
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        old_session = "s-trial-agent-empty-session"
        user_id = "u-test-empty-arc"
        core = self._core(user_id)
        mock_core.return_value = core

        # Session file that exists but has empty messages (trial agent may
        # create the file before any messages are saved).
        self._write_session(old_session, [])

        request_id = "request-00000000000000A3"
        reservation = await handle_chat_new(
            self._request(request_id, old_session)
        )
        data = json.loads(reservation.body.decode())

        await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )

        archives = self.repo().list_archives(user_id)
        self.assertEqual(len(archives), 0)

    @patch("web_app.handlers.chat_flow._core")
    async def test_missing_session_file_during_archive_is_safe(self, mock_core):
        """If old session file was already cleaned up, archive is a no-op."""
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        old_session = "s-trial-agent-already-gone"
        user_id = "u-test-gone"
        core = self._core(user_id)
        mock_core.return_value = core

        request_id = "request-00000000000000A4"
        reservation = await handle_chat_new(
            self._request(request_id, old_session)
        )
        data = json.loads(reservation.body.decode())

        await handle_chat_new_ack(
            self._ack_request(
                request_id, old_session, data["session_id"], data["commit_token"]
            )
        )

        archives = self.repo().list_archives(user_id)
        self.assertEqual(len(archives), 0)

    def repo(self):
        from web_app.handlers.chat_flow import _hosted_repo

        return _hosted_repo()


class TestTrialArchiveHandlers(unittest.IsolatedAsyncioTestCase):
    """Verify archive list/restore/delete handlers for OpenRouter lane."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()

        from web_app.handlers import chat_flow

        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        chat_flow._HOSTED_REPO = None
        self.repo = HostedConversationRepo(
            data_dir=str(self.hosted_data_dir),
            sessions_dir=str(self.session_dir),
        )

    def tearDown(self):
        from web_app.handlers import chat_flow

        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    def _write_session(self, session_id: str, messages: list[dict]):
        path = self.repo.session_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)

    def _core(self, user_id: str = "u-test-hdlr"):
        return SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": user_id,
                "agent_id": "trial-agent",
                "key_hash": "abc12345",
                "key": "uc_live_test",
                "email": "test@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _pending_limited_response=lambda: SimpleNamespace(status=403),
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_archives_list_uses_hosted_repo(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_archives

        user_id = "u-test-archives"
        sid = "s-trial-agent-test-arc"
        self._write_session(sid, [{"role": "user", "content": "Archive me"}])
        self.repo.archive_session(
            user_id, sid, slot=1, sessions_dir=str(self.session_dir),
        )

        core = self._core(user_id)
        mock_core.return_value = core
        request = SimpleNamespace(query={"model": "google/gemini-3.1-flash-lite"})
        response = await handle_chat_archives(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data.get("trial"))
        self.assertEqual(len(data["archives"]), 1)
        self.assertEqual(data["archives"][0]["preview"], "Archive me")

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_restore_archive_uses_hosted_repo(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        user_id = "u-test-restore"
        sid = "s-trial-agent-restore-src"
        self._write_session(sid, [
            {"role": "user", "content": "Restore me"},
            {"role": "assistant", "content": "OK"},
        ])
        archive_id = self.repo.archive_session(
            user_id, sid, slot=2, preview="Restore me",
            sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        core = self._core(user_id)
        mock_core.return_value = core
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "archive_id": archive_id,
                    "slot": 2,
                }
            )
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data.get("trial"))
        self.assertIsNotNone(data.get("session_id"))
        self.assertTrue(data["session_id"].startswith("s-trial-agent-"))
        self.assertEqual(data["active_slot"], 2)

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_restore_foreign_archive_returns_404(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        owner_id = "u-test-owner"
        sid = "s-trial-agent-owned"
        self._write_session(sid, [{"role": "user", "content": "My archive"}])
        archive_id = self.repo.archive_session(
            owner_id, sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        core = self._core("u-test-attacker")
        mock_core.return_value = core
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "archive_id": archive_id,
                }
            )
        )
        response = await handle_chat_restore_archive(request)
        self.assertEqual(response.status, 404)

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_delete_archive_uses_hosted_repo(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_delete_archive

        user_id = "u-test-delete"
        sid = "s-trial-agent-delete-me"
        self._write_session(sid, [{"role": "user", "content": "Delete me"}])
        archive_id = self.repo.archive_session(
            user_id, sid, sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        core = self._core(user_id)
        mock_core.return_value = core
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "archive_id": archive_id,
                }
            )
        )
        response = await handle_chat_delete_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data.get("trial"))
        self.assertFalse(self.repo.archive_owned_by(user_id, archive_id))

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_archive_request_does_not_rpc_local_agent(self, mock_core):
        """OpenRouter archive handlers must not fall through to agent RPC."""
        from web_app.handlers.chat_flow import handle_chat_archives

        core = self._core()
        mock_core.return_value = core
        request = SimpleNamespace(query={"model": "google/gemini-3.1-flash-lite"})
        response = await handle_chat_archives(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data.get("trial"))
        self.assertEqual(data["archives"], [])


class TestTrialSlotsServerAuthority(unittest.IsolatedAsyncioTestCase):
    """Verify server slot state is authoritative for trial users."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()

        from web_app.handlers import chat_flow

        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        chat_flow._HOSTED_REPO = None
        self.repo = HostedConversationRepo(
            data_dir=str(self.hosted_data_dir),
            sessions_dir=str(self.session_dir),
        )

    def tearDown(self):
        from web_app.handlers import chat_flow

        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    def _write_session(self, session_id: str, messages: list[dict]):
        path = self.repo.session_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)

    def _core(self, user_id: str = "u-test-slots"):
        return SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": user_id,
                "agent_id": "trial-agent",
                "key_hash": "abc12345",
                "key": "uc_live_test",
                "email": "test@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _pending_limited_response=lambda: SimpleNamespace(status=403),
            _agent_request=AsyncMock(return_value={"active_slot": 1, "slots": []}),
            time=SimpleNamespace(time=lambda: 1234.5),
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_slots_reflect_persisted_state(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_slots

        user_id = "u-test-slots"
        sid1 = self.repo.new_session_id(user_id, "trial-agent")
        sid2 = self.repo.new_session_id(user_id, "trial-agent")
        self._write_session(sid1, [{"role": "user", "content": "Slot 1 chat"}])
        self._write_session(sid2, [{"role": "user", "content": "Slot 2 chat"}])

        state = self.repo.get_slot_state(user_id)
        state["slots"]["1"] = sid1
        state["slots"]["2"] = sid2
        state["active_slot"] = 2
        self.repo.set_slot_state(user_id, state)

        core = self._core(user_id)
        mock_core.return_value = core
        request = SimpleNamespace(
            query={"model": "google/gemini-3.1-flash-lite"}
        )
        response = await handle_chat_slots(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["trial"])
        self.assertEqual(data["active_slot"], 2)
        slots_by_id = {s["slot"]: s for s in data["slots"]}
        self.assertEqual(slots_by_id[1]["preview"], "Slot 1 chat")
        self.assertEqual(slots_by_id[2]["preview"], "Slot 2 chat")
        self.assertFalse(slots_by_id[1]["empty"])
        self.assertFalse(slots_by_id[2]["empty"])
        self.assertTrue(slots_by_id[3]["empty"])

    @patch("web_app.handlers.chat_flow._core")
    async def test_switch_persists_to_server(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_switch

        user_id = "u-test-switch-srv"
        core = self._core(user_id)
        mock_core.return_value = core
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "slot": 3,
                }
            )
        )
        response = await handle_chat_switch(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["trial"])
        self.assertEqual(data["active_slot"], 3)

        state = self.repo.get_slot_state(user_id)
        self.assertEqual(state["active_slot"], 3)


class TestTrialDualReadCompat(unittest.TestCase):
    """Verify dual-read compatibility between server slot state and localStorage."""

    def test_session_file_format_is_backward_compatible(self):
        """The /data/sessions format used by the repo matches
        chat_agent_openrouter.py expectations."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = HostedConversationRepo(data_dir=td)
            sessions_dir = os.path.join(td, "sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            sid = "s-trial-agent-test-compat"
            path = repo.session_path(sid, sessions_dir=sessions_dir)
            messages = [
                {"role": "system", "content": "You are an agent"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump({"messages": messages}, f)

            msgs, found = repo.read_session_messages(
                sid, sessions_dir=sessions_dir
            )
            self.assertTrue(found)
            self.assertEqual(len(msgs), 3)
            self.assertEqual(msgs[0]["role"], "system")
            self.assertEqual(msgs[1]["role"], "user")

    def test_session_path_sanitization_matches_chat_agent(self):
        """Rejects dangerous chars; legacy path used for dual-read compat."""
        with tempfile.TemporaryDirectory() as td:
            repo = HostedConversationRepo(
                data_dir=os.path.join(td, "hosted"),
                sessions_dir=os.path.join(td, "sessions"),
            )
            # Dangerous session IDs are rejected by validate_session_id.
            with self.assertRaises(ValueError):
                repo.session_path("s-trial-agent/test..with spaces")
            # Clean IDs are accepted.
            path = repo.session_path("s-trial-agent-test")
            self.assertIn("s-trial-agent-test.json", os.path.basename(path))
            # Legacy path still sanitizes for backward compat.
            legacy = HostedConversationRepo.make_session_path_legacy(
                "s-trial-agent/test..with spaces",
                sessions_dir=os.path.join(td, "sessions"),
            )
            self.assertNotIn("/", os.path.basename(legacy))
            self.assertNotIn("..", os.path.basename(legacy))
            self.assertNotIn(" ", os.path.basename(legacy))


class TestSlotSessionIdField(unittest.TestCase):
    """Verify that handle_chat_slots returns session_id per slot and
    at top level so JS sync can consume it authoritatively."""

    def test_slot_object_contains_session_id(self):
        """Each slot dict MUST carry its session_id for client-side sync."""
        from web_app.templates import TRIAL_CHAT_HTML

        # The JS function syncTrialSlotStateFromServer reads sv.session_id
        self.assertIn("sv.session_id", TRIAL_CHAT_HTML)
        self.assertIn("data.active_slot", TRIAL_CHAT_HTML)
        # Also check that top-level session_id is used
        self.assertIn("data.session_id", TRIAL_CHAT_HTML)


class TestTrialNewChatSlotUpdate(unittest.IsolatedAsyncioTestCase):
    """After ACK, the server slot mapping must point to the new session."""

    def setUp(self):
        from web_app.handlers import chat_flow

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()
        chat_flow._guest_new_chat_requests.clear()
        chat_flow._guest_new_chat_sources.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_status_dir = chat_flow._TRIAL_NEW_CHAT_STATUS_DIR
        chat_flow._TRIAL_NEW_CHAT_STATUS_DIR = os.path.join(
            self.temp_dir.name, "transition-status"
        )
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        chat_flow._HOSTED_REPO = None

    def tearDown(self):
        from web_app.handlers import chat_flow

        chat_flow._TRIAL_NEW_CHAT_STATUS_DIR = self.original_status_dir
        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    def _write_session(self, session_id: str, messages: list[dict]):
        path = HostedConversationRepo.make_session_path(
            session_id, sessions_dir=str(self.session_dir)
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)

    def _make_read_trial_history(self):
        """Return a _read_trial_history that actually reads from the test sessions dir."""
        sessions_dir = str(self.session_dir)

        def reader(session_id):
            path = HostedConversationRepo.make_session_path(
                session_id, sessions_dir=sessions_dir
            )
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get("messages", []), True
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return [], False
        return reader

    def _core(self, user_id: str = "u-test-slot-update"):
        sessions_dir = str(self.session_dir)

        def read_history(session_id):
            path = HostedConversationRepo.make_session_path(
                session_id, sessions_dir=sessions_dir
            )
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get("messages", []), True
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return [], False

        def delete_session(session_id):
            path = HostedConversationRepo.make_session_path(
                session_id, sessions_dir=sessions_dir
            )
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        def session_path(session_id):
            return HostedConversationRepo.make_session_path(
                session_id, sessions_dir=sessions_dir
            )

        return SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": user_id,
                "agent_id": "trial-agent",
                "key_hash": "abc12345",
                "key": "uc_live_test",
                "email": "test@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested
            if requested and requested.startswith(f"s-{agent_id}")
            else f"s-{agent_id}",
            _read_trial_history=read_history,
            _delete_trial_session=delete_session,
            _trial_session_path=session_path,
            _session_tabs={},
            _session_agent_map={},
            _session_allowed_tabs={},
            _session_profile_paths={},
            _session_last_active={},
            _chat_preview_generations={},
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            JWT_SECRET="test-jwt-secret",
            _attach_first_look_guest_cookies=Mock(),
        )

    def _repo(self):
        from web_app.handlers.chat_flow import _hosted_repo
        return _hosted_repo()

    @patch("web_app.handlers.chat_flow._core")
    async def test_slot_maps_to_new_session_after_ack(self, mock_core):
        """Slot state must point to the new session_id after ACK arrives."""
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        old_session = "s-trial-agent-old-slot1"
        user_id = "u-test-slot-ack"
        core = self._core(user_id)
        # Pre-seed slot state so old_session occupies slot 1.
        state = self._repo().get_slot_state(user_id)
        state["slots"]["1"] = old_session
        state["active_slot"] = 1
        self._repo().set_slot_state(user_id, state)
        mock_core.return_value = core
        self._write_session(old_session, [
            {"role": "user", "content": "Old slot chat"},
        ])

        request_id = "request-0000000000000B01"
        reservation = await handle_chat_new(
            SimpleNamespace(
                json=AsyncMock(return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "session_id": old_session,
                    "slot": 1,
                })
            )
        )
        data = json.loads(reservation.body.decode())
        new_session = data["session_id"]
        self.assertNotEqual(new_session, old_session)

        await handle_chat_new_ack(
            SimpleNamespace(
                json=AsyncMock(return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "commit_token": data["commit_token"],
                    "previous_session_id": old_session,
                    "session_id": new_session,
                    "slot": 1,
                })
            )
        )

        # Slot 1 must now point to the new session.
        updated = self._repo().get_slot_state(user_id)
        self.assertEqual(updated["slots"]["1"], new_session)

        # History by slot 1 should find the new session (empty, new chat).
        self.assertFalse(
            os.path.isfile(
                HostedConversationRepo.make_session_path(
                    old_session, sessions_dir=str(self.session_dir)
                )
            )
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_slot_idempotent_recovery_preserves_mapping(self, mock_core):
        """Double-ack must leave the correct slot mapping intact."""
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        old_session = "s-trial-agent-slot-idem"
        user_id = "u-test-slot-idem"
        core = self._core(user_id)
        state = self._repo().get_slot_state(user_id)
        state["slots"]["2"] = old_session
        state["active_slot"] = 2
        self._repo().set_slot_state(user_id, state)
        mock_core.return_value = core
        self._write_session(old_session, [
            {"role": "user", "content": "Slot 2 chat"},
        ])

        request_id = "request-0000000000000B02"
        reservation = await handle_chat_new(
            SimpleNamespace(
                json=AsyncMock(return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "session_id": old_session,
                    "slot": 2,
                })
            )
        )
        data = json.loads(reservation.body.decode())
        new_session = data["session_id"]

        ack_body = {
            "model": "google/gemini-3.1-flash-lite",
            "request_id": request_id,
            "commit_token": data["commit_token"],
            "previous_session_id": old_session,
            "session_id": new_session,
            "slot": 2,
        }
        first = await handle_chat_new_ack(
            SimpleNamespace(json=AsyncMock(return_value=ack_body))
        )
        self.assertEqual(first.status, 200)

        second = await handle_chat_new_ack(
            SimpleNamespace(json=AsyncMock(return_value=ack_body))
        )
        self.assertEqual(second.status, 200)

        updated = self._repo().get_slot_state(user_id)
        self.assertEqual(updated["slots"]["2"], new_session)

    @patch("web_app.handlers.chat_flow._core")
    async def test_cross_browser_sync_slots_after_ack(self, mock_core):
        """A second browser calling slots after an ACK sees the new session."""
        from web_app.handlers.chat_flow import (
            handle_chat_new, handle_chat_new_ack, handle_chat_slots,
        )

        old_session = "s-trial-agent-cross-sync"
        user_id = "u-test-cross-sync"
        core = self._core(user_id)
        state = self._repo().get_slot_state(user_id)
        state["slots"]["1"] = old_session
        state["active_slot"] = 1
        self._repo().set_slot_state(user_id, state)
        mock_core.return_value = core
        self._write_session(old_session, [
            {"role": "user", "content": "Cross-browser chat"},
        ])

        request_id = "request-0000000000000B03"
        reservation = await handle_chat_new(
            SimpleNamespace(
                json=AsyncMock(return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "session_id": old_session,
                    "slot": 1,
                })
            )
        )
        data = json.loads(reservation.body.decode())
        new_session = data["session_id"]

        await handle_chat_new_ack(
            SimpleNamespace(
                json=AsyncMock(return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "commit_token": data["commit_token"],
                    "previous_session_id": old_session,
                    "session_id": new_session,
                    "slot": 1,
                })
            )
        )

        # Simulate a second browser calling slots.
        slots_resp = await handle_chat_slots(
            SimpleNamespace(query={"model": "google/gemini-3.1-flash-lite"})
        )
        slots_data = json.loads(slots_resp.body.decode())
        self.assertEqual(slots_data["active_slot"], 1)
        self.assertEqual(slots_data["session_id"], new_session)
        slot1 = next(s for s in slots_data["slots"] if s["slot"] == 1)
        self.assertEqual(slot1["session_id"], new_session)
        self.assertTrue(slot1["empty"])  # new chat = empty

    @patch("web_app.handlers.chat_flow._core")
    async def test_history_defaults_to_hosted_active_slot(self, mock_core):
        """When no slot is requested, history uses the hosted active_slot."""
        from web_app.handlers.chat_flow import handle_chat_history

        user_id = "u-test-hist-default"
        sid2 = self._repo().new_session_id(user_id, "trial-agent")
        self._write_session(sid2, [
            {"role": "user", "content": "Active slot 2 chat"},
        ])
        state = self._repo().get_slot_state(user_id)
        state["slots"]["2"] = sid2
        state["active_slot"] = 2
        self._repo().set_slot_state(user_id, state)

        core = self._core(user_id)
        mock_core.return_value = core

        # Request history without specifying a slot.
        resp = await handle_chat_history(
            SimpleNamespace(query={"model": "google/gemini-3.1-flash-lite"})
        )
        data = json.loads(resp.body.decode())
        self.assertEqual(data["session_id"], sid2)
        self.assertEqual(data["messages"][0]["content"], "Active slot 2 chat")

    @patch("web_app.handlers.chat_flow._core")
    async def test_guest_slots_fallback_has_session_id(self, mock_core):
        """Guest slots must still include session_id per slot + top-level."""
        from web_app.handlers.chat_flow import handle_chat_slots

        core = SimpleNamespace(
            _authenticate=lambda request: None,  # unauthenticated
            _first_look_guest_auth=lambda request: (
                {"agent_id": "guest-abcd"}, "gid", 0,
            ),
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested
            if requested else f"s-{agent_id}",
            _read_trial_history=lambda session_id: ([], False),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _pending_limited_response=lambda: SimpleNamespace(status=403),
            _agent_request=AsyncMock(),
            time=SimpleNamespace(time=lambda: 1234.5),
            _attach_first_look_guest_cookies=Mock(),
        )
        mock_core.return_value = core

        resp = await handle_chat_slots(
            SimpleNamespace(
                query={
                    "model": "google/gemini-3.1-flash-lite",
                    "first_look_guest": "1",
                }
            )
        )
        data = json.loads(resp.body.decode())
        # Top-level session_id must be present.
        self.assertIn("session_id", data)
        self.assertTrue(data["session_id"].startswith("s-guest-abcd"))
        # Each slot must have session_id.
        for s in data["slots"]:
            self.assertIn("session_id", s)
        self.assertTrue(data["trial"])


class TestTrialNoGuestFallback(unittest.IsolatedAsyncioTestCase):
    """OpenRouter guest path must not RPC TrialAgent for get_slots."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()

        from web_app.handlers import chat_flow
        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        chat_flow._HOSTED_REPO = None

    def tearDown(self):
        from web_app.handlers import chat_flow
        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    @patch("web_app.handlers.chat_flow._core")
    async def test_guest_get_slots_never_contacts_trial_agent(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_slots

        agent_rpc_called = []

        async def agent_rpc(*args, **kwargs):
            agent_rpc_called.append(True)
            return {"active_slot": 1, "slots": []}

        core = SimpleNamespace(
            _authenticate=lambda request: None,
            _first_look_guest_auth=lambda request: (
                {"agent_id": "guest-xyz"}, "gid", 0,
            ),
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested
            if requested else f"s-{agent_id}",
            _read_trial_history=lambda session_id: (
                [{"role": "user", "content": "guest task"}], True
            ),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _pending_limited_response=lambda: SimpleNamespace(status=403),
            _agent_request=agent_rpc,
            _attach_first_look_guest_cookies=Mock(),
            time=SimpleNamespace(time=lambda: 1234.5),
        )
        mock_core.return_value = core

        resp = await handle_chat_slots(
            SimpleNamespace(
                query={
                    "model": "google/gemini-3.1-flash-lite",
                    "first_look_guest": "1",
                }
            )
        )
        data = json.loads(resp.body.decode())
        self.assertTrue(data["trial"])
        self.assertFalse(agent_rpc_called, "guest slots must not RPC trial agent")


class TestRestoreSessionOwnedCompatibility(unittest.IsolatedAsyncioTestCase):
    """Verify that a restored session passes _session_owned on the next send.

    Regression: restore_archive() generated ``s-trial-agent-...`` regardless
    of the authenticated user's agent_id, so handle_chat_msg's
    ``_session_owned`` check rejected it and replaced the session.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()

        from web_app.handlers import chat_flow
        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        chat_flow._HOSTED_REPO = None
        self.repo = HostedConversationRepo(
            data_dir=str(self.hosted_data_dir),
            sessions_dir=str(self.session_dir),
        )

    def tearDown(self):
        from web_app.handlers import chat_flow
        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    def _write_session(self, session_id: str, messages: list[dict]):
        path = self.repo.session_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)

    @patch("web_app.handlers.chat_flow._core")
    async def test_restore_then_send_preserves_session(self, mock_core):
        """After restore, the session_id is owned by the authenticated account.

        An auto-registered user (agent_id=claude-<keyhash>) restores an
        archive. The new session must use the correct agent_id prefix so
        that handle_chat_msg's _session_owned() check passes and the
        session is NOT replaced on the next send.
        """
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        user_id = "u-test-restore-owned"
        key_hash = "abc12345"
        agent_id = f"claude-{key_hash}"

        # Create an archive owned by this user.
        sid = "s-claude-abc12345-restore-src"
        self._write_session(sid, [
            {"role": "user", "content": "Restore me please"},
            {"role": "assistant", "content": "Done"},
        ])
        archive_id = self.repo.archive_session(
            user_id, sid, slot=1, preview="Restore me",
            sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        # Wire up the mock core with auto-registered user identity.
        core = SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": user_id,
                "agent_id": agent_id,
                "key_hash": key_hash,
                "key": "uc_live_test",
                "email": "test@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: agent_id,
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _pending_limited_response=lambda: SimpleNamespace(status=403),
        )
        mock_core.return_value = core

        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "archive_id": archive_id,
                    "slot": 1,
                }
            )
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        restored_sid = data["session_id"]

        # The restored session MUST be scoped to the authenticated agent.
        self.assertTrue(
            restored_sid.startswith(f"s-{agent_id}-"),
            f"restored session {restored_sid!r} should start with s-{agent_id}-",
        )

        # Simulate handle_chat_msg's _session_owned check.
        parts = restored_sid.split("-")
        self.assertEqual(parts[0], "s")
        self.assertEqual(parts[2], key_hash,
                         f"_session_owned check: parts[2]={parts[2]!r} must == key_hash={key_hash!r}")

        # Verify the restored session file actually exists and is readable.
        msgs, found = self.repo.read_session_messages(restored_sid)
        self.assertTrue(found)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["content"], "Restore me please")

        # Slot state must point to the new session.
        state = self.repo.get_slot_state(user_id)
        self.assertEqual(state["slots"]["1"], restored_sid)
        self.assertEqual(state["active_slot"], 1)


class TestRestoreCleansOrphanSession(unittest.IsolatedAsyncioTestCase):
    """On restore, the currently-occupied target slot session file is archived
    exactly once, then removed so it doesn't leave an unbounded orphan."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hosted_data_dir = Path(self.temp_dir.name) / "hosted-conversations"
        self.hosted_data_dir.mkdir()
        self.session_dir = Path(self.temp_dir.name) / "sessions"
        self.session_dir.mkdir()

        from web_app.handlers import chat_flow
        self.original_data_dir = chat_flow._HOSTED_REPO_DATA_DIR
        self.original_sessions_dir = chat_flow._HOSTED_SESSIONS_DIR
        chat_flow._HOSTED_REPO_DATA_DIR = str(self.hosted_data_dir)
        chat_flow._HOSTED_SESSIONS_DIR = str(self.session_dir)
        chat_flow._HOSTED_REPO = None
        self.repo = HostedConversationRepo(
            data_dir=str(self.hosted_data_dir),
            sessions_dir=str(self.session_dir),
        )

    def tearDown(self):
        from web_app.handlers import chat_flow
        chat_flow._HOSTED_REPO_DATA_DIR = self.original_data_dir
        chat_flow._HOSTED_SESSIONS_DIR = self.original_sessions_dir
        chat_flow._HOSTED_REPO = None
        self.temp_dir.cleanup()

    def _write_session(self, session_id: str, messages: list[dict]):
        path = self.repo.session_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"messages": messages}, f)

    @patch("web_app.handlers.chat_flow._core")
    async def test_restore_cleans_orphan_active_file(self, mock_core):
        """Active session file is removed after being archived; source archive stays."""
        from web_app.handlers.chat_flow import handle_chat_restore_archive

        user_id = "u-test-orphan"
        agent_id = "claude-testhash"

        # Create a source archive.
        sid_src = "s-claude-testhash-src"
        self._write_session(sid_src, [
            {"role": "user", "content": "Restored content"},
        ])
        archive_id = self.repo.archive_session(
            user_id, sid_src, slot=1, preview="Restored",
            sessions_dir=str(self.session_dir),
        )
        self.assertIsNotNone(archive_id)

        # Create an active session occupying slot 1 (the target).
        sid_active = "s-claude-testhash-active"
        self._write_session(sid_active, [
            {"role": "user", "content": "Active content to be archived"},
        ])
        self.repo.get_slot_state(user_id)  # ensure user_dir exists
        state = self.repo.get_slot_state(user_id)
        state["slots"]["1"] = sid_active
        state["active_slot"] = 1
        self.repo.set_slot_state(user_id, state)

        # Verify active session file exists.
        active_path = self.repo.session_path(
            sid_active, sessions_dir=str(self.session_dir)
        )
        self.assertTrue(os.path.isfile(active_path))

        # Verify source archive exists.
        archive_path = self.repo._archive_path(user_id, archive_id)
        self.assertTrue(os.path.isfile(archive_path))

        core = SimpleNamespace(
            _authenticate=lambda request: {
                "user_id": user_id,
                "agent_id": agent_id,
                "key_hash": "testhash",
                "key": "uc_live_test",
                "email": "test@example.com",
            },
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: agent_id,
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _pending_limited_response=lambda: SimpleNamespace(status=403),
        )
        mock_core.return_value = core

        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "archive_id": archive_id,
                    "slot": 1,
                }
            )
        )
        response = await handle_chat_restore_archive(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])

        # The active session file must be removed (orphan cleaned).
        self.assertFalse(
            os.path.isfile(active_path),
            "active session file should be removed after archive+restore",
        )

        # The source archive file must still exist (not deleted).
        self.assertTrue(
            os.path.isfile(archive_path),
            "source archive must not be deleted",
        )

        # The active session must have been archived (appears in list).
        archives = self.repo.list_archives(user_id)
        previews = [a["preview"] for a in archives]
        self.assertIn("Active content to be archived", previews)

        # Slot must now point to the restored session.
        state = self.repo.get_slot_state(user_id)
        self.assertEqual(state["slots"]["1"], data["session_id"])


if __name__ == "__main__":
    unittest.main()
