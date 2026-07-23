"""hosted_conversations.py — Server-owned trial conversation repository.

Provides slot state, archive, restore, and delete operations for the hosted
OpenRouter trial lane. Each user's active 3-slot state and conversation archives
are persisted atomically under a structured directory tree keyed by hashed
user identity. The module preserves compatibility with the existing
``/data/sessions`` format used by ``chat_agent_openrouter.py`` for active
message payloads.

Archive metadata (preview, slot, timestamps) is stored alongside the
full message snapshot in the archive directory. Slot state is a tiny JSON
blob that remains atomic: a corrupt or partial write never replaces the
current state.

All public methods accept an explicit ``data_dir`` parameter so tests can
target a temp directory without touching the real ``/data`` volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid

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


class HostedConversationRepo:
    """Persistent, server-authoritative trial conversation slot + archive store."""

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

    def read_session_messages(
        self, session_id: str, *, sessions_dir: str | None = None
    ) -> tuple[list[dict], bool]:
        """Read active session messages. Returns (messages, found).

        Dual-read: tries the validated path first, then falls back to
        the legacy sanitized path so historically-stored files remain
        readable.
        """
        sd = sessions_dir or self._sessions_dir
        # Try validated path first; fall back to legacy for historic files.
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
        for candidate in candidates:
            try:
                with open(candidate) as f:
                    data = json.load(f)
                msgs = data.get("messages")
                if isinstance(msgs, list):
                    return msgs, True
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
        return [], False

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

    def set_slot_state(self, user_id: str, state: dict) -> None:
        """Persist slot state atomically via temp-file + rename."""
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
        and return the archive ID. Returns None if the session file does
        not exist or is empty.

        The original session file is NOT removed by this method — callers
        are responsible for cleaning it up after a successful archive.
        """
        msgs, found = self.read_session_messages(
            session_id, sessions_dir=sessions_dir
        )
        if not found or not msgs:
            return None
        self._ensure_user_dirs(user_id)
        # Auto-extract preview from first user message if none given.
        if not preview:
            for m in msgs:
                if m.get("role") == "user":
                    preview = re.sub(r"\s+", " ", str(m.get("content", "")).strip())[:80]
                    break
        archive_id = f"{int(time.time() * 1000)}.{uuid.uuid4().hex[:12]}"
        payload = {
            "archive_id": archive_id,
            "session_id": session_id,
            "slot": slot,
            "preview": preview[:200],
            "message_count": len(msgs),
            "archived_at": time.time(),
            "messages": msgs,
        }
        path = self._archive_path(user_id, archive_id)
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
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
        sessions_dir: str | None = None,
        agent_id: str = "trial-agent",
    ) -> tuple[str | None, int, list[dict]]:
        """Restore an archive into *target_slot* (or the slot stored in the archive).

        *agent_id* determines the session-id prefix so the restored session is
        owned by the correct authenticated identity.  Callers MUST pass the
        authenticated account's agent_id from auth_info; the default
        ``"trial-agent"`` is only appropriate for the legacy shared trial lane.

        Returns ``(new_session_id, slot, messages)``. The restored messages are
        written to a fresh session file. If the archive does not exist, returns
        ``(None, 0, [])``.
        """
        self._ensure_user_dirs(user_id)
        path = self._archive_path(user_id, archive_id)
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None, 0, []
        msgs = data.get("messages", [])
        if not isinstance(msgs, list):
            msgs = []
        slot = target_slot or data.get("slot") or 1
        if slot not in (1, 2, 3):
            slot = 1
        preview = str(data.get("preview", ""))[:200]
        # Write restored messages to a fresh session file with an
        # agent-scoped session_id so ownership checks pass.
        new_sid = self.new_session_id(user_id, agent_id)
        sid_path = self.session_path(new_sid, sessions_dir=sessions_dir)
        tmp = f"{sid_path}.{uuid.uuid4().hex}.tmp"
        try:
            os.makedirs(os.path.dirname(sid_path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"messages": msgs}, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, sid_path)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
        # Update slot state to point to the new session.
        state = self.get_slot_state(user_id)
        state["slots"][str(slot)] = new_sid
        if preview:
            state["previews"][str(slot)] = preview
        state["active_slot"] = slot
        self.set_slot_state(user_id, state)
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
