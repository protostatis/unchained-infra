"""Mutable runtime state for the web server.

This centralizes chat/session/process dictionaries so `web.py` can be split
incrementally without changing behavior.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import hashlib
import json
import subprocess
import threading
import time
from _thread import LockType
from dataclasses import dataclass, field
from typing import Deque, TextIO


@dataclass
class OverlaySessionState:
    """Overlay copilot state for one chat session (v3 — bridge-based).

    The bridge handles injection and event pushing locally via CDP.
    The server only tracks session state for routing.
    """
    session_id: str
    agent_id: str
    tab_id: str  # concrete tab ID or "auto" (bridge resolves to active tab)
    user_id: str
    model: str = ""
    slot: int | None = None
    injected: bool = False
    pending_events: list = field(default_factory=list)  # buffered before bridge injection
    poll_task: object = None  # asyncio.Task for the overlay poll loop


@dataclass
class ProfileSessionLockState:
    """Reference-counted lock for one profile-session lifecycle."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def profile_session_caller_tag(session_id: str) -> str:
    """Return a stable bounded owner tag safe for bridge query strings."""
    digest = hashlib.sha256(str(session_id).encode()).hexdigest()[:24]
    return f"chat-{digest}"


@asynccontextmanager
async def profile_session_guard(core, session_id: str):
    """Serialize profile mutations and remove unused lock entries safely."""
    locks = getattr(core, "_session_profile_locks", None)
    if locks is None:
        locks = {}
        core._session_profile_locks = locks
    state = locks.get(session_id)
    if state is None:
        state = ProfileSessionLockState()
        locks[session_id] = state
    state.users += 1
    try:
        async with state.lock:
            yield
    finally:
        state.users -= 1
        current = locks.get(session_id)
        tab_id = str(getattr(core, "_session_tabs", {}).get(session_id, "") or "")
        profile_path = getattr(core, "_session_profile_paths", {}).get(session_id, "")
        if (
            current is state
            and state.users == 0
            and not profile_path
            and not tab_id.startswith("prov-")
        ):
            locks.pop(session_id, None)


_CHAT_TURN_JOURNAL_LIMIT = 200
_CHAT_TURN_REPLAY_EVENT_BYTES = 12 * 1024
_CHAT_TURN_REPLAY_TEXT_BYTES = 64 * 1024
_CHAT_TURN_RETENTION_SECONDS = 5 * 60
_CHAT_TURN_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})


def _chat_turn_replay_event(event: dict) -> dict:
    """Return a bounded event safe to retain and replay to reconnecting tabs."""
    event_type = str(event.get("type", "") or "")[:80]
    name = str(event.get("name", "") or "")[:160]
    is_image = (
        bool(event.get("is_screenshot"))
        or "screenshot" in event_type.lower()
        or "screenshot" in name.lower()
        or event_type.startswith("live_preview")
    )
    replay: dict = {}
    for key, value in event.items():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_text == "seq" or any(
            marker in key_lower for marker in ("screenshot", "base64", "image_data")
        ):
            continue
        if key_text == "data" and is_image:
            replay[key_text] = ""
            replay["replay_body_omitted"] = True
            continue
        if isinstance(value, str):
            replay[key_text] = value[:_CHAT_TURN_REPLAY_TEXT_BYTES]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            replay[key_text] = value
        elif isinstance(value, (list, dict)):
            # Keep small structured fields (for example tool input) but never
            # retain an arbitrary response body that can bloat reconnects.
            try:
                encoded = json.dumps(value, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                continue
            if len(encoded.encode("utf-8")) <= _CHAT_TURN_REPLAY_TEXT_BYTES:
                replay[key_text] = value

    replay["type"] = event_type or "event"
    try:
        encoded = json.dumps(replay, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = ""
    if len(encoded.encode("utf-8")) <= _CHAT_TURN_REPLAY_EVENT_BYTES:
        return replay
    # Preserve routing and terminal information when a tool result still has a
    # large structured body after the field-level limits above.
    minimal = {
        key: replay[key]
        for key in ("type", "name", "session_id", "req_id", "error", "message")
        if key in replay
    }
    minimal["replay_body_omitted"] = True
    return minimal


@dataclass
class ChatTurnState:
    """One authenticated or server-identified turn retained across SSE changes.

    This process-local journal supports page refreshes, not web-server restart
    recovery. It deliberately excludes screenshot and oversized replay bodies.
    """

    owner_user_id: str
    owner_key_hash: str
    session_id: str
    req_id: str
    chat_agent_id: str = ""
    routing_agent_id: str = ""
    dispatch_ws: object | None = field(default=None, repr=False)
    cdp_agent_id: str = ""
    tab_id: str = ""
    scheduler_grant_id: str = ""
    hosted_deadline_task: object | None = field(default=None, repr=False)
    silence_timeout_task: object | None = field(default=None, repr=False)
    status: str = "active"
    phase: str = "planning"
    current_action: dict = field(default_factory=lambda: {"type": "planning"})
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    terminal_at: float = 0.0
    last_seq: int = 0
    next_step_id: int = 0
    open_step_ids: Deque[str] = field(default_factory=deque, repr=False)
    journal: Deque[dict] = field(
        default_factory=lambda: deque(maxlen=_CHAT_TURN_JOURNAL_LIMIT)
    )
    subscribers: set[asyncio.Event] = field(default_factory=set, repr=False)
    stream_finished: bool = False

    @property
    def first_seq(self) -> int:
        return int(self.journal[0].get("seq", 0)) if self.journal else 0

    @property
    def terminal(self) -> bool:
        return self.status in _CHAT_TURN_TERMINAL_STATUSES

    def owned_by(self, user_id: str, key_hash: str) -> bool:
        """Require both stable authenticated identities when reconnecting."""
        return bool(
            self.owner_user_id
            and self.owner_key_hash
            and self.owner_user_id == str(user_id or "")
            and self.owner_key_hash == str(key_hash or "")
        )

    def update_routing(
        self,
        *,
        chat_agent_id: str = "",
        routing_agent_id: str = "",
        dispatch_ws: object | None = None,
        cdp_agent_id: str = "",
        tab_id: str | None = None,
        scheduler_grant_id: str | None = None,
    ) -> None:
        """Record the dispatch target before the agent receives the turn."""
        if chat_agent_id:
            self.chat_agent_id = chat_agent_id
        if routing_agent_id:
            self.routing_agent_id = routing_agent_id
        if dispatch_ws is not None:
            self.dispatch_ws = dispatch_ws
        if cdp_agent_id:
            self.cdp_agent_id = cdp_agent_id
        if tab_id is not None:
            self.tab_id = tab_id
        if scheduler_grant_id is not None:
            self.scheduler_grant_id = scheduler_grant_id
        self.updated_at = time.time()

    def mark_cancelling(self) -> bool:
        """Transition an active turn without making its journal terminal yet."""
        if self.status != "active":
            return False
        self.status = "cancelling"
        self.current_action = {"type": "cancelling"}
        self.updated_at = time.time()
        self._notify_subscribers()
        return True

    def publish(self, event: dict) -> dict | None:
        """Append one event and wake subscribers without awaiting their I/O."""
        event = dict(event)
        event_type = str(event.get("type", "") or "")
        if self.stream_finished and event_type in {"done", "cancelled", "error"}:
            return None
        if self.status == "cancelled" and event_type in {"cancelled", "error"}:
            return None
        if self.status == "error" and event_type == "error":
            return None

        if event_type == "tool_start":
            self.next_step_id += 1
            event.setdefault("step_id", f"step-{self.next_step_id}")
            self.open_step_ids.append(str(event["step_id"]))
        elif event_type == "tool_result":
            if "step_id" not in event and self.open_step_ids:
                event["step_id"] = self.open_step_ids.popleft()
            elif event.get("step_id"):
                try:
                    self.open_step_ids.remove(str(event["step_id"]))
                except ValueError:
                    pass

        replay = _chat_turn_replay_event(event)
        replay["session_id"] = self.session_id
        replay["req_id"] = self.req_id
        self.last_seq += 1
        replay["seq"] = self.last_seq
        now = time.time()
        replay["published_at"] = now
        self.journal.append(replay)
        self.updated_at = now
        self.last_event_at = now

        if event_type in {"tool_start", "tool_result"}:
            self.phase = "browsing"
            self.current_action = {
                key: replay[key]
                for key in ("type", "step_id", "name", "input", "data", "error")
                if key in replay
            }
        elif event_type == "text":
            self.phase = "writing"
            self.current_action = {
                key: replay[key]
                for key in ("type", "data", "message")
                if key in replay
            }
        elif event_type == "cancelled":
            self.status = "cancelled"
            self.terminal_at = now
            self.current_action = {"type": "cancelled"}
        elif event_type == "error":
            self.status = "error"
            self.terminal_at = now
            self.current_action = {
                key: replay[key] for key in ("type", "data", "error") if key in replay
            }
        elif event_type == "done":
            if self.status not in {"cancelled", "error"}:
                self.status = "done"
            self.terminal_at = now
            self.stream_finished = True
            self.current_action = {"type": "done"}

        self._notify_subscribers()
        return replay

    def events_after(self, seq: int) -> list[dict]:
        return [event for event in self.journal if int(event.get("seq", 0)) > seq]

    def subscribe(self) -> asyncio.Event:
        signal = asyncio.Event()
        self.subscribers.add(signal)
        return signal

    def unsubscribe(self, signal: asyncio.Event) -> None:
        self.subscribers.discard(signal)

    def snapshot(self, *, include_events: bool = True) -> dict:
        payload = {
            "active": not self.terminal,
            "session_id": self.session_id,
            "req_id": self.req_id,
            "status": self.status,
            "phase": self.phase,
            "current_action": self.current_action,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "routing": {
                "chat_agent_id": self.chat_agent_id,
                "agent_id": self.routing_agent_id,
                "cdp_agent_id": self.cdp_agent_id,
                "tab_id": self.tab_id,
            },
            "scheduler_grant_id": self.scheduler_grant_id,
        }
        if include_events:
            payload["events"] = list(self.journal)
        return payload

    def _notify_subscribers(self) -> None:
        for signal in tuple(self.subscribers):
            signal.set()


@dataclass
class ChatTurnRegistry:
    """Process-local chat-turn registry with atomic per-session starts."""

    # ``turns`` is the current state per session for start/cancel/routing.
    # ``turns_by_request`` keeps terminal journals replayable by their exact
    # request ID when a later turn has already become the current session turn.
    turns: dict[str, ChatTurnState] = field(default_factory=dict)
    turns_by_request: dict[tuple[str, str], ChatTurnState] = field(default_factory=dict)
    start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    retention_seconds: float = _CHAT_TURN_RETENTION_SECONDS

    async def start(self, candidate: ChatTurnState) -> tuple[ChatTurnState, bool, bool]:
        """Return ``(turn, created, conflict)`` for one session's next turn."""
        async with self.start_lock:
            self.prune()
            current = self.turns.get(candidate.session_id)
            if current and current.status in {"active", "cancelling"}:
                if current.req_id == candidate.req_id and current.owned_by(
                    candidate.owner_user_id, candidate.owner_key_hash
                ):
                    return current, False, False
                return current, False, True
            existing = self.turns_by_request.get((candidate.session_id, candidate.req_id))
            if existing and existing.owned_by(
                candidate.owner_user_id, candidate.owner_key_hash
            ):
                return existing, False, False
            self.turns[candidate.session_id] = candidate
            self.turns_by_request[(candidate.session_id, candidate.req_id)] = candidate
            return candidate, True, False

    def get(self, session_id: str, req_id: str = "") -> ChatTurnState | None:
        self.prune()
        if req_id:
            return self.turns_by_request.get((session_id, req_id))
        return self.turns.get(session_id)

    def matching_active(self, session_id: str, req_id: str) -> ChatTurnState | None:
        turn = self.get(session_id)
        if turn and turn.status in {"active", "cancelling"} and turn.req_id == req_id:
            return turn
        return None

    def active_for_agent(self, agent_id: str) -> list[ChatTurnState]:
        self.prune()
        return [
            turn
            for turn in self.turns.values()
            if turn.status in {"active", "cancelling"}
            and turn.routing_agent_id == agent_id
        ]

    def active_for_transport(self, agent_id: str, dispatch_ws: object) -> list[ChatTurnState]:
        """Return active turns dispatched through one exact agent connection."""
        return [
            turn
            for turn in self.active_for_agent(agent_id)
            if turn.dispatch_ws is dispatch_ws
        ]

    def prune(self) -> None:
        now = time.time()
        for key, turn in list(self.turns_by_request.items()):
            if turn.terminal_at and now - turn.terminal_at > self.retention_seconds:
                self.turns_by_request.pop(key, None)
                if self.turns.get(turn.session_id) is turn:
                    self.turns.pop(turn.session_id, None)


@dataclass
class ChatRuntimeState:
    chat_agents: dict[str, object] = field(default_factory=dict)
    response_queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    response_req_ids: dict[str, str] = field(default_factory=dict)
    # Browser turns use this journal registry. Response queues remain only for
    # legacy paths and lightweight test doubles that omit a registry.
    chat_turns: ChatTurnRegistry = field(default_factory=ChatTurnRegistry)
    session_agents: dict[str, str] = field(default_factory=dict)
    agent_req_queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    session_tabs: dict[str, str] = field(default_factory=dict)
    # Server-authorized targets for each authenticated chat session. The
    # active target remains in session_tabs for backward compatibility.
    session_allowed_tabs: dict[str, set[str]] = field(default_factory=dict)
    session_profile_paths: dict[str, str] = field(default_factory=dict)
    # Fail-closed tombstones for profile sessions evicted by lifecycle cleanup.
    expired_profile_sessions: dict[str, float] = field(default_factory=dict)
    # Serializes profile liveness checks and relaunches for one chat session.
    session_profile_locks: dict[str, ProfileSessionLockState] = field(default_factory=dict)
    session_last_active: dict[str, float] = field(default_factory=dict)
    session_agent_map: dict[str, str] = field(default_factory=dict)
    # Serializes CDP operations that target the same source document. Semantic
    # capture/actions and /web/cmd share these locks so their evaluations do
    # not interleave on one tab.
    source_operation_locks: dict[tuple[str, str], asyncio.Lock] = field(
        default_factory=dict
    )
    # The newest authenticated preview owns the action channel for a chat.
    chat_preview_generations: dict[str, int] = field(default_factory=dict)
    stale_tab_task: asyncio.Task | None = None
    tabs_pending_close: dict[str, tuple[str, int]] = field(default_factory=dict)
    tabs_pending_close_caller_tags: dict[str, str] = field(default_factory=dict)
    gemini_procs: dict[str, subprocess.Popen] = field(default_factory=dict)
    gemini_log_fhs: dict[str, TextIO] = field(default_factory=dict)
    gemini_last_active: dict[str, float] = field(default_factory=dict)
    gemini_spawn_lock: LockType = field(default_factory=threading.Lock)
    gemini_cleanup_task: asyncio.Task | None = None
    headless_watchdog_task: asyncio.Task | None = None
    scheduler_turn_grants: dict[str, dict[str, object]] = field(default_factory=dict)
    pending_provision: dict[str, tuple[str, str, float]] = field(default_factory=dict)
    provision_cooldowns: dict[str, float] = field(default_factory=dict)
    # Overlay copilot v2 — single source of truth per session
    overlay_sessions: dict[str, OverlaySessionState] = field(default_factory=dict)
