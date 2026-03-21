"""Mutable runtime state for the web server.

This centralizes chat/session/process dictionaries so `web.py` can be split
incrementally without changing behavior.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from _thread import LockType
from dataclasses import dataclass, field
from typing import TextIO


@dataclass
class OverlaySessionState:
    """All overlay copilot state for one chat session.

    Single source of truth — replaces the scattered dicts from v1.
    """
    session_id: str
    agent_id: str
    tab_id: str  # concrete tab ID, never "auto"
    user_id: str
    slot: int | None = None
    token: str = ""
    subscriber: asyncio.Queue | None = None  # at most ONE active WS
    injected: bool = False
    bootstrap_id: str = ""  # Page.addScriptToEvaluateOnNewDocument identifier
    pending_events: list = field(default_factory=list)  # buffered before WS connects (max 50)


@dataclass
class ChatRuntimeState:
    chat_agents: dict[str, object] = field(default_factory=dict)
    response_queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    response_req_ids: dict[str, str] = field(default_factory=dict)
    session_agents: dict[str, str] = field(default_factory=dict)
    agent_req_queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    session_tabs: dict[str, str] = field(default_factory=dict)
    session_profile_paths: dict[str, str] = field(default_factory=dict)
    session_last_active: dict[str, float] = field(default_factory=dict)
    session_agent_map: dict[str, str] = field(default_factory=dict)
    stale_tab_task: asyncio.Task | None = None
    tabs_pending_close: dict[str, tuple[str, int]] = field(default_factory=dict)
    gemini_procs: dict[str, subprocess.Popen] = field(default_factory=dict)
    gemini_log_fhs: dict[str, TextIO] = field(default_factory=dict)
    gemini_last_active: dict[str, float] = field(default_factory=dict)
    gemini_spawn_lock: LockType = field(default_factory=threading.Lock)
    gemini_cleanup_task: asyncio.Task | None = None
    scheduler_turn_grants: dict[str, dict[str, object]] = field(default_factory=dict)
    pending_provision: dict[str, tuple[str, str, float]] = field(default_factory=dict)
    provision_cooldowns: dict[str, float] = field(default_factory=dict)
    # Overlay copilot v2 — single source of truth per session
    overlay_sessions: dict[str, OverlaySessionState] = field(default_factory=dict)
