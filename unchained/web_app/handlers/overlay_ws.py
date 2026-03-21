"""Overlay copilot server-side helpers (v3 — bridge-based).

The overlay panel is injected and managed by chrome_bridge locally.
The server only:
1. Sends overlay_inject to the bridge on chat turn start
2. Sends overlay_event for each chat event (bridge pushes via CDP)
3. Receives overlay_followup from bridge (via relay HTTP forward)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from aiohttp import web

from web_app.core import get_core as _core


# ---------------------------------------------------------------------------
# Broadcast (called from chat_stream SSE loop)
# ---------------------------------------------------------------------------

def broadcast_to_overlay(session_id: str, event: dict) -> None:
    """Send an event to the overlay via the bridge's agent WS."""
    core = _core()
    overlay = core._overlay_sessions.get(session_id)
    if not overlay or not overlay.injected:
        if overlay and len(overlay.pending_events) < 50:
            overlay.pending_events.append(event)
        return

    agent_ws = core._chat_agents.get(overlay.agent_id)
    if not agent_ws or agent_ws.closed:
        return

    async def _push():
        try:
            await agent_ws.send_json({
                "type": "overlay_event",
                "session_id": session_id,
                "event": event,
            })
        except Exception:
            pass

    asyncio.create_task(_push())


# ---------------------------------------------------------------------------
# Follow-up routing
# ---------------------------------------------------------------------------

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

    q: asyncio.Queue = asyncio.Queue()
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

    # Drain responses — broadcast to overlay via bridge
    async def _drain():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=120)
                except asyncio.TimeoutError:
                    break
                broadcast_to_overlay(session_id, evt)
                if evt.get("type") in ("done", "error"):
                    break
        except Exception:
            pass

    asyncio.create_task(_drain())


# ---------------------------------------------------------------------------
# HTTP endpoint for bridge follow-ups (relay forwards these)
# ---------------------------------------------------------------------------

async def handle_overlay_followup(request: web.Request) -> web.Response:
    """POST /web/overlay-followup — receive follow-up from bridge via relay."""
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
