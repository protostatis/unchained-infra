"""Overlay copilot — follow-up routing.

Events are pushed and follow-ups polled via direct CDP in chat_stream.py.
This module only handles follow-up routing to the agent and the HTTP
endpoint for bridge-originated follow-ups (legacy, kept for compat).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from aiohttp import web

from web_app.core import get_core as _core


async def _route_followup(core, session_id: str, message: str) -> None:
    """Route a follow-up from the overlay through the normal chat path."""
    overlay = core._overlay_sessions.get(session_id)
    if not overlay:
        print(f"[overlay] follow-up dropped — no overlay state for {session_id}")
        return

    agent_id = core._session_agents.get(session_id) or overlay.agent_id
    agent_ws = core._chat_agents.get(agent_id)
    if agent_ws is None or agent_ws.closed:
        print(f"[overlay] follow-up dropped — agent {agent_id} not connected")
        return

    req_id = f"overlay-{uuid.uuid4().hex[:8]}"

    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    core._response_queues[session_id] = q
    core._response_req_ids[session_id] = req_id
    core._session_last_active[session_id] = time.time()

    ws_msg = {
        "type": "user_message",
        "session_id": session_id,
        "agent_id": overlay.agent_id,
        "message": message,
        "req_id": req_id,
    }
    if overlay.model:
        ws_msg["model"] = overlay.model
    if overlay.tab_id:
        ws_msg["tab_id"] = overlay.tab_id
    if overlay.slot is not None:
        ws_msg["slot"] = overlay.slot

    try:
        await agent_ws.send_json(ws_msg)
        print(f"[overlay] follow-up routed to {agent_id}: {message[:60]}")
    except Exception as e:
        print(f"[overlay] follow-up send failed: {e}")
        core._response_queues.pop(session_id, None)
        return

    # Drain responses and push to overlay via CDP
    async def _drain():
        try:
            from web_app.handlers.chat_stream import _broadcast_overlay
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=120)
                except asyncio.TimeoutError:
                    break
                _broadcast_overlay(session_id, evt)
                if evt.get("type") in ("done", "error"):
                    break
        except Exception:
            pass

    asyncio.create_task(_drain())


async def handle_overlay_followup(request: web.Request) -> web.Response:
    """POST /web/overlay-followup — receive follow-up from relay."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    session_id = str(body.get("session_id", "")).strip()
    message = str(body.get("message", "")).strip()
    if not session_id or not message or len(message) > 4000:
        return web.json_response({"error": "invalid"}, status=400)

    await _route_followup(core, session_id, message)
    return web.json_response({"ok": True})
