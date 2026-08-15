"""hosted_conversations.py — Server-owned trial conversation repository.

Provides slot state, archive, restore, and delete operations for the hosted
OpenRouter trial lane. Each user's active 3-slot state and conversation archives
are persisted atomically under a structured directory tree keyed by hashed
user identity. The module preserves compatibility with the existing
``/data/sessions`` format used by ``chat_agent_openrouter.py`` for active
message payloads.

Archive metadata (preview, slot, timestamps), bounded provider resume context,
and the full user-visible transcript are stored together in the archive
directory. Slot state is a tiny JSON blob that remains atomic: a corrupt or
partial write never replaces the current state.

All public methods accept an explicit ``data_dir`` parameter so tests can
target a temp directory without touching the real ``/data`` volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager

from conversation_transcript import (
    SESSION_SCHEMA_VERSION,
    has_supported_session_schema,
    project_visible_messages,
    validate_visible_transcript,
    visible_transcript_from_payload,
)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - production and CI are POSIX
    _fcntl = None

_ARCHIVE_DIR_NAME = "archives"
_SLOT_STATE_FILE = "slot-state.json"
_DEFAULT_DATA_DIR = os.environ.get(
    "UNCHAINED_HOSTED_DATA_DIR",
    "/data/hosted-conversations",
)
_DEFAULT_SESSIONS_DIR = os.environ.get(
    "UNCHAINED_SESSIONS_DIR",
    os.path.join(
        os.environ.get("UNCHAINED_DATA_DIR",
                       os.path.expanduser("~/.unchained")),
        "sessions",
    ) if os.environ.get("UNCHAINED_DATA_DIR") else "/data/sessions",
)

_SLOT_STATE_CURRENT_VERSION = 1
_SLOT_COUNT = 3


class ArchivePreservationError(RuntimeError):
    """An existing session could not be safely preserved before replacement."""


class ArchiveRestoreConflictError(RuntimeError):
    """The target slot changed after its active session was preserved."""


class HostedConversationRepo:
    """Persistent, server-authoritative trial conversation slot + archive store."""

    # A fixed set of process-local locks prevents same-process races without
    # retaining one lock forever for every user. A per-user advisory file lock
    # below extends the critical section across web processes on POSIX hosts.
    _slot_thread_locks = tuple(threading.RLock() for _ in range(64))

    def __init__(
        self,
        *,
        data_dir: str | None = None,
        sessions_dir: str | None = None,
    ) -> None:
        self._data_dir = (data_dir or _DEFAULT_DATA_DIR).rstrip("/\\")
        self._sessions_dir = (sessions_dir or _DEFAULT_SESSIONS_DIR).rstrip("/\\")
        os.makedirs(self._data_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_user_key(user_id: str) -> str:
        """Derive a safe directory name from an opaque user identity string."""
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id is required")
        return hashlib.sha256(user_id.encode()).hexdigest()[:32]

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self._data_dir, self._safe_user_key(user_id))

    def _archives_dir(self, user_id: str) -> str:
        return os.path.join(self._user_dir(user_id), _ARCHIVE_DIR_NAME)

    def _slot_state_path(self, user_id: str) -> str:
        return os.path.join(self._user_dir(user_id), _SLOT_STATE_FILE)

    def _ensure_user_dirs(self, user_id: str) -> None:
        os.makedirs(self._user_dir(user_id), exist_ok=True)
        os.makedirs(self._archives_dir(user_id), exist_ok=True)

    @contextmanager
    def _slot_state_lock(self, user_id: str):
        """Serialize slot-state read/modify/write operations for one user."""
        self._ensure_user_dirs(user_id)
        user_key = self._safe_user_key(user_id)
        thread_lock = self._slot_thread_locks[
            int(user_key[:8], 16) % len(self._slot_thread_locks)
        ]
        lock_path = os.path.join(self._user_dir(user_id), ".slot-state.lock")
        with thread_lock:
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(lock_fd, "a+") as lock_file:
                if _fcntl is not None:
                    _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if _fcntl is not None:
                        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # Session ID helpers (pass-through to existing /data/sessions format)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_session_id(session_id: str) -> None:
        """Reject session IDs that contain path traversal or dangerous characters.

        Must be called before any write path that accepts an externally-
        supplied session_id. Generated IDs from new_session_id() are
        always safe and skip this check.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        # Reject path traversal attempts — no silent stripping.
        if ".." in session_id:
            raise ValueError("session_id contains path traversal (..)")
        if "/" in session_id or "\\" in session_id:
            raise ValueError("session_id contains directory separator")
        # Reject null bytes and control characters.
        if "\0" in session_id:
            raise ValueError("session_id contains null byte")
        # Allow only printable ASCII-safe chars: alphanumeric, dashes, underscores,
        # dots, colons, and a limited set of extras. No whitespace or shell-
        # special characters.
        if not re.fullmatch(r"[A-Za-z0-9._:\-]{1,200}", session_id):
            raise ValueError("session_id contains disallowed characters")

    @staticmethod
    def make_session_path(session_id: str, *, sessions_dir: str = "/data/sessions") -> str:
        """Build a session file path without needing an instance.

        Always requires *sessions_dir* for safety (no implicit defaults
        from the repo). Tests use this directly.

        This method validates the session_id before constructing the path
        so that path traversal attempts are rejected rather than silently
        stripped. For reading historically stored files that were written
        before validation was added, use :meth:`make_session_path_legacy`.
        """
        HostedConversationRepo.validate_session_id(session_id)
        safe_id = session_id.replace(" ", "_")
        return os.path.join(sessions_dir.rstrip("/\\"), f"{safe_id}.json")

    @staticmethod
    def make_session_path_legacy(
        session_id: str, *, sessions_dir: str = "/data/sessions"
    ) -> str:
        """Build a session file path with pre-validation sanitization.

        Use this ONLY for reading files that may have been written before
        the validate-first policy was introduced. New code must use
        :meth:`make_session_path` instead.

        The sanitization mirrors the historical behavior: slashes and
        ``..`` are replaced, not rejected.
        """
        safe_id = session_id.replace("/", "_").replace("..", "").replace(" ", "_")
        return os.path.join(sessions_dir.rstrip("/\\"), f"{safe_id}.json")

    def session_path(self, session_id: str, *, sessions_dir: str | None = None) -> str:
        """Path to the active session file (instance version).

        Uses the instance-configured *sessions_dir* by default.
        """
        return HostedConversationRepo.make_session_path(
            session_id,
            sessions_dir=sessions_dir or self._sessions_dir,
        )

    def _session_read_candidates(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> list[str]:
        """Return current and legacy paths in the order they should be read."""
        sd = sessions_dir or self._sessions_dir
        try:
            path = HostedConversationRepo.make_session_path(session_id, sessions_dir=sd)
        except ValueError:
            path = None
        legacy = HostedConversationRepo.make_session_path_legacy(
            session_id, sessions_dir=sd
        )
        candidates = []
        if path is not None:
            candidates.append(path)
        if legacy != path:
            candidates.append(legacy)
        return candidates

    @staticmethod
    def _visible_messages(messages: object) -> list[dict]:
        """Return the strict legacy projection used by unversioned records."""
        return project_visible_messages(messages)

    def read_session_payload(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> tuple[dict, bool]:
        """Read a session's bounded resume history and display transcript.

        ``messages`` remains the legacy provider-context contract.  The
        optional ``transcript`` field is a complete user-visible record that
        must not be capped along with model context.  Legacy files derive it
        from ``messages`` so they stay readable.
        """
        # Try validated path first; fall back to legacy for historic files.
        for candidate in self._session_read_candidates(
            session_id, sessions_dir=sessions_dir
        ):
            try:
                with open(candidate) as f:
                    data = json.load(f)
                if not has_supported_session_schema(data):
                    continue
                msgs = data.get("messages")
                if isinstance(msgs, list):
                    return {
                        "messages": msgs,
                        "transcript": visible_transcript_from_payload(data),
                    }, True
            except (AttributeError, FileNotFoundError, json.JSONDecodeError, OSError):
                continue
        return {}, False

    def _read_archive_session_payload(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> tuple[dict, bool]:
        """Read a source session strictly before a destructive transition.

        Missing sessions and valid sessions without visible history are safe
        no-ops. Existing malformed, unreadable, or future-schema files must
        stop the transition rather than being deleted without an archive.
        """
        for candidate in self._session_read_candidates(
            session_id, sessions_dir=sessions_dir
        ):
            try:
                with open(candidate) as f:
                    data = json.load(f)
            except FileNotFoundError:
                continue
            except (json.JSONDecodeError, OSError, UnicodeError) as exc:
                raise ArchivePreservationError(
                    "could not read existing session for archival"
                ) from exc
            if not isinstance(data, dict) or not has_supported_session_schema(data):
                raise ArchivePreservationError(
                    "existing session has an unsupported or invalid schema"
                )
            messages = data.get("messages")
            if not isinstance(messages, list):
                raise ArchivePreservationError(
                    "existing session has invalid provider context"
                )
            if (
                data.get("schema_version") == SESSION_SCHEMA_VERSION
                and validate_visible_transcript(data.get("transcript")) is None
            ):
                raise ArchivePreservationError(
                    "existing session has an invalid visible transcript"
                )
            return {
                "messages": messages,
                "transcript": visible_transcript_from_payload(data),
            }, True
        return {}, False

    def read_session_messages(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> tuple[list[dict], bool]:
        """Read the bounded provider/resume history from an active session."""
        payload, found = self.read_session_payload(
            session_id, sessions_dir=sessions_dir
        )
        return payload.get("messages", []), found

    def read_session_transcript(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> tuple[list[dict], bool]:
        """Read the complete user-visible transcript from an active session."""
        payload, found = self.read_session_payload(
            session_id, sessions_dir=sessions_dir
        )
        return payload.get("transcript", []), found

    def session_file_exists(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> bool:
        sd = sessions_dir or self._sessions_dir
        try:
            path = HostedConversationRepo.make_session_path(session_id, sessions_dir=sd)
        except ValueError:
            path = None
        if path and os.path.isfile(path):
            return True
        legacy = HostedConversationRepo.make_session_path_legacy(
            session_id, sessions_dir=sd
        )
        if legacy != path and os.path.isfile(legacy):
            return True
        return False

    # ------------------------------------------------------------------
    # Slot state
    # ------------------------------------------------------------------

    def _default_slot_state(self) -> dict:
        return {
            "version": _SLOT_STATE_CURRENT_VERSION,
            "active_slot": 1,
            "slots": {"1": "", "2": "", "3": ""},
            "previews": {"1": "", "2": "", "3": ""},
        }

    def get_slot_state(self, user_id: str) -> dict:
        """Return the authoritative 3-slot state for *user_id*."""
        path = self._slot_state_path(user_id)
        try:
            with open(path) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self._default_slot_state()
        if not isinstance(state, dict) or state.get("version") != _SLOT_STATE_CURRENT_VERSION:
            return self._default_slot_state()
        for key in ("slots", "previews"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        state.setdefault("active_slot", 1)
        return state

    def _write_slot_state_unlocked(self, user_id: str, state: dict) -> None:
        """Persist slot state atomically; caller must hold _slot_state_lock."""
        self._ensure_user_dirs(user_id)
        if not isinstance(state, dict):
            raise ValueError("state must be a dict")
        state.setdefault("version", _SLOT_STATE_CURRENT_VERSION)
        state.setdefault("active_slot", 1)
        for key in ("slots", "previews"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        path = self._slot_state_path(user_id)
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    def set_slot_state(self, user_id: str, state: dict) -> None:
        """Persist slot state atomically under the per-user mutation lock."""
        with self._slot_state_lock(user_id):
            self._write_slot_state_unlocked(user_id, state)

    # ------------------------------------------------------------------
    # Initial session binding (first-turn slot assignment)
    # ------------------------------------------------------------------

    def bind_initial_session(
        self,
        user_id: str,
        session_id: str,
        *,
        slot: int | None = None,
    ) -> bool:
        """Atomically bind *session_id* to the first empty slot for *user_id*.

        If *slot* is given (1-3), attempts to bind that specific slot.
        Otherwise uses the current ``active_slot``.
        Does NOT overwrite a slot that already holds a different session.
        A slot that already points to the same *session_id* is considered
        already bound and returns ``True``.

        Returns ``True`` if the session was bound (or already bound).
        Returns ``False`` if the target slot is occupied by another session.

        Uses a per-user process + advisory file lock so concurrent requests,
        including requests handled by different web processes, serialize and
        exactly one wins the slot.

        Raises ``ValueError`` for invalid slot or missing user_id.
        Raises ``OSError`` only for filesystem-level failures the caller
        must handle (disk full, permission denied).
        """
        self._safe_user_key(user_id)
        self.validate_session_id(session_id)
        target = slot
        if target is not None and target not in (1, 2, 3):
            raise ValueError(f"slot must be 1-3, got {target}")

        with self._slot_state_lock(user_id):
            state = self.get_slot_state(user_id)
            slots = state["slots"]

            # A session is authoritative in at most one slot. Replays without
            # an explicit slot follow the existing binding instead of copying
            # the same conversation into whichever slot is currently active.
            existing_slot = next(
                (
                    int(slot_key)
                    for slot_key, bound_session in slots.items()
                    if bound_session == session_id and slot_key in {"1", "2", "3"}
                ),
                None,
            )
            if existing_slot is not None:
                if target is not None and target != existing_slot:
                    return False
                if state.get("active_slot") != existing_slot:
                    state["active_slot"] = existing_slot
                    self._write_slot_state_unlocked(user_id, state)
                return True

            if target is None:
                target = state.get("active_slot", 1)
            if target not in (1, 2, 3):
                target = 1
            slot_key = str(target)
            existing = slots.get(slot_key, "")
            if existing:
                # Slot is occupied by a different session — do not overwrite.
                return False
            # Bind the new session.
            slots[slot_key] = session_id
            state["active_slot"] = target
            self._write_slot_state_unlocked(user_id, state)
            return True

    def set_active_slot(self, user_id: str, slot: int) -> None:
        """Set the active slot without losing concurrent slot assignments."""
        if slot not in (1, 2, 3):
            raise ValueError(f"slot must be 1-3, got {slot}")
        with self._slot_state_lock(user_id):
            state = self.get_slot_state(user_id)
            state["active_slot"] = slot
            self._write_slot_state_unlocked(user_id, state)

    def replace_slot_session(
        self,
        user_id: str,
        slot: int,
        expected_session_id: str,
        new_session_id: str,
        *,
        preview: str = "",
        allow_empty: bool = False,
    ) -> bool:
        """Compare-and-set a slot session while preserving concurrent updates.

        ``allow_empty`` lets a new-chat transition claim an unbound slot, while
        still rejecting a session installed by a competing transition.
        """
        if slot not in (1, 2, 3):
            raise ValueError(f"slot must be 1-3, got {slot}")
        self.validate_session_id(expected_session_id)
        self.validate_session_id(new_session_id)
        with self._slot_state_lock(user_id):
            state = self.get_slot_state(user_id)
            slot_key = str(slot)
            current_session_id = state["slots"].get(slot_key, "")
            source_bound_elsewhere = any(
                key != slot_key and bound_session == expected_session_id
                for key, bound_session in state["slots"].items()
            )
            if current_session_id == new_session_id:
                return not source_bound_elsewhere
            if current_session_id != expected_session_id and not (
                allow_empty and not current_session_id
            ):
                return False
            if allow_empty and not current_session_id and source_bound_elsewhere:
                return False
            state["slots"][slot_key] = new_session_id
            state["previews"][slot_key] = str(preview)[:200]
            self._write_slot_state_unlocked(user_id, state)
            return True

    # ------------------------------------------------------------------
    # Session ID generation (derived, opaque, replay-stable)
    # ------------------------------------------------------------------

    def new_session_id(self, user_id: str, agent_id: str, request_id: str | None = None) -> str:
        """Generate a high-entropy session ID scoped to user+agent.

        When *request_id* is given the ID is replay-stable so retried
        new-chat reservations produce the same destination session.
        """
        self._ensure_user_dirs(user_id)
        if request_id:
            digest = hashlib.sha256(
                f"{user_id}\0{agent_id}\0{request_id}".encode()
            ).hexdigest()[:24]
        else:
            digest = uuid.uuid4().hex[:24]
        return f"s-{agent_id}-{digest}"

    # ------------------------------------------------------------------
    # Archives
    # ------------------------------------------------------------------

    def _archive_path(self, user_id: str, archive_id: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9._-]{8,80}", archive_id):
            raise ValueError("invalid archive_id")
        return os.path.join(self._archives_dir(user_id), f"{archive_id}.json")

    def archive_restore_slot(
        self,
        user_id: str,
        archive_id: str,
        target_slot: int | None = None,
    ) -> int | None:
        """Return an archive's safe restore slot when its payload is valid."""
        data = self._read_restore_archive_payload(user_id, archive_id)
        if data is None:
            return None
        slot = target_slot or data.get("slot") or 1
        return slot if slot in (1, 2, 3) else 1

    def _read_restore_archive_payload(
        self, user_id: str, archive_id: str
    ) -> dict | None:
        """Read an archive only when its display and resume data are valid."""
        self._ensure_user_dirs(user_id)
        try:
            with open(self._archive_path(user_id, archive_id)) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if (
            not isinstance(data, dict)
            or not has_supported_session_schema(data)
            or not isinstance(data.get("messages"), list)
        ):
            return None
        if (
            data.get("schema_version") == SESSION_SCHEMA_VERSION
            and validate_visible_transcript(data.get("transcript")) is None
        ):
            return None
        return data

    def archive_session(
        self,
        user_id: str,
        session_id: str,
        *,
        slot: int | None = None,
        preview: str = "",
        sessions_dir: str | None = None,
    ) -> str | None:
        """Read the current session payload, persist an archive snapshot,
        and return the archive ID. Returns None only when the session is
        confirmed absent or has no visible history.

        The original session file is NOT removed by this method — callers
        are responsible for cleaning it up after a successful archive.
        """
        session, found = self._read_archive_session_payload(
            session_id, sessions_dir=sessions_dir
        )
        msgs = session.get("messages", [])
        transcript = session.get("transcript", [])
        if not found or not transcript:
            return None
        tmp = ""
        try:
            self._ensure_user_dirs(user_id)
            # Auto-extract preview from first user message if none given.
            if not preview:
                for m in transcript:
                    if m.get("role") == "user":
                        preview = re.sub(r"\s+", " ", str(m.get("content", "")).strip())[:80]
                        break
            archive_id = f"{int(time.time() * 1000)}.{uuid.uuid4().hex[:12]}"
            payload = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "archive_id": archive_id,
                "session_id": session_id,
                "slot": slot,
                "preview": preview[:200],
                "message_count": len(transcript),
                "archived_at": time.time(),
                "messages": msgs,
                "transcript": transcript,
            }
            path = self._archive_path(user_id, archive_id)
            tmp = f"{path}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as exc:
            raise ArchivePreservationError(
                "could not persist session archive"
            ) from exc
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except FileNotFoundError:
                    pass
        return archive_id

    def list_archives(self, user_id: str, *, limit: int = 100) -> list[dict]:
        """Return archive summaries sorted newest-first."""
        self._ensure_user_dirs(user_id)
        result: list[dict] = []
        archives_dir = self._archives_dir(user_id)
        try:
            entries = sorted(os.scandir(archives_dir), key=lambda e: e.name, reverse=True)
        except FileNotFoundError:
            return []
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            archive_id = entry.name[:-5]  # strip ".json"
            try:
                with open(entry.path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            result.append({
                "id": archive_id,
                "archive_id": archive_id,
                "preview": str(data.get("preview", ""))[:200],
                "message_count": data.get("message_count", 0),
                "slot": data.get("slot"),
                "archived_at": data.get("archived_at", 0),
                "session_id": data.get("session_id", ""),
            })
            if len(result) >= limit:
                break
        return result

    def restore_archive(
        self,
        user_id: str,
        archive_id: str,
        *,
        target_slot: int | None = None,
        expected_session_id: str | None = None,
        sessions_dir: str | None = None,
        agent_id: str = "trial-agent",
    ) -> tuple[str | None, int, list[dict]]:
        """Restore an archive into *target_slot* (or the slot stored in the archive).

        *agent_id* determines the session-id prefix so the restored session is
        owned by the correct authenticated identity.  Callers MUST pass the
        authenticated account's agent_id from auth_info; the default
        ``"trial-agent"`` is only appropriate for the legacy shared trial lane.

        ``expected_session_id`` makes a replacement conditional on the target
        slot still pointing at that session; a mismatch raises
        :class:`ArchiveRestoreConflictError` without writing a replacement.

        Returns ``(new_session_id, slot, resume_messages)``. The bounded
        provider resume context and full display transcript are written to a
        fresh session file. If the archive does not exist, returns
        ``(None, 0, [])``.
        """
        data = self._read_restore_archive_payload(user_id, archive_id)
        if data is None:
            return None, 0, []
        msgs = data["messages"]
        transcript = visible_transcript_from_payload(data)
        slot = target_slot or data.get("slot") or 1
        if slot not in (1, 2, 3):
            slot = 1
        preview = str(data.get("preview", ""))[:200]
        with self._slot_state_lock(user_id):
            state = self.get_slot_state(user_id)
            slot_key = str(slot)
            if (
                expected_session_id is not None
                and state["slots"].get(slot_key, "") != expected_session_id
            ):
                raise ArchiveRestoreConflictError(
                    "target slot changed before restore could commit"
                )
            # Write restored messages to a fresh session file with an
            # agent-scoped session_id so ownership checks pass.
            new_sid = self.new_session_id(user_id, agent_id)
            sid_path = self.session_path(new_sid, sessions_dir=sessions_dir)
            tmp = f"{sid_path}.{uuid.uuid4().hex}.tmp"
            try:
                os.makedirs(os.path.dirname(sid_path), exist_ok=True)
                with open(tmp, "w") as f:
                    json.dump(
                        {
                            "schema_version": SESSION_SCHEMA_VERSION,
                            "messages": msgs,
                            "transcript": transcript,
                        },
                        f,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, sid_path)
            finally:
                try:
                    os.remove(tmp)
                except FileNotFoundError:
                    pass
            # Update slot state to point to the new session.
            state["slots"][slot_key] = new_sid
            if preview:
                state["previews"][slot_key] = preview
            state["active_slot"] = slot
            self._write_slot_state_unlocked(user_id, state)
        return new_sid, slot, msgs

    def delete_archive(self, user_id: str, archive_id: str) -> bool:
        """Delete an archive. Returns True if it existed and was removed."""
        self._ensure_user_dirs(user_id)
        path = self._archive_path(user_id, archive_id)
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Ownership checks
    # ------------------------------------------------------------------

    def archive_owned_by(self, user_id: str, archive_id: str) -> bool:
        """Check that *archive_id* belongs to *user_id*."""
        self._ensure_user_dirs(user_id)
        return os.path.isfile(self._archive_path(user_id, archive_id))
