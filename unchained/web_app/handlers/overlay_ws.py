"""Overlay copilot WebSocket handler.

The overlay JS running inside the agent-controlled browser tab connects
here to receive real-time chat events and (in Phase 2) send follow-up
prompts back to the agent.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import json
import os
import time

from aiohttp import web

from web_app.core import get_core as _core


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

_TOKEN_TTL = 3600  # 1 hour


def _require_secret(secret: str = "") -> str:
    """Resolve the HMAC secret, raising if absent."""
    secret = secret or os.environ.get("JWT_SECRET", "")
    if not secret:
        raise RuntimeError("JWT_SECRET is not configured — overlay tokens cannot be signed")
    return secret


def mint_overlay_token(session_id: str, user_id: str, secret: str = "") -> str:
    """Create a short-lived HMAC-signed token scoped to one session."""
    secret = _require_secret(secret)
    payload = json.dumps({
        "session_id": session_id,
        "user_id": user_id,
        "exp": time.time() + _TOKEN_TTL,
    }, separators=(",", ":"))
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = json.dumps({"p": payload, "s": sig}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def validate_overlay_token(token: str, secret: str = "") -> dict | None:
    """Validate and decode an overlay token. Returns {session_id, user_id} or None."""
    secret = secret or os.environ.get("JWT_SECRET", "")
    if not secret:
        return None  # refuse to validate without a signing key
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        obj = json.loads(raw)
        payload = obj["p"]
        sig = obj["s"]
    except Exception:
        return None
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if time.time() > data.get("exp", 0):
        return None
    return {"session_id": data["session_id"], "user_id": data["user_id"]}


# ---------------------------------------------------------------------------
# Broadcast helper (called from chat_stream.py)
# ---------------------------------------------------------------------------

# Per-session ring buffer for events sent before any overlay WS connects.
# Replayed to new subscribers on connect so they don't miss early events.
_replay_buffers: dict[str, collections.deque] = {}
_REPLAY_BUFFER_MAX = 50


def broadcast_to_overlay(session_id: str, event: dict) -> None:
    """Push an event to all overlay WS subscribers for a session.

    If no subscribers exist yet, events are buffered (up to 50) so the
    overlay can replay them when it connects.
    """
    core = _core()
    subscribers = core._overlay_subscribers.get(session_id)
    if not subscribers:
        # No overlay connected yet — buffer for replay
        buf = _replay_buffers.get(session_id)
        if buf is None:
            buf = collections.deque(maxlen=_REPLAY_BUFFER_MAX)
            _replay_buffers[session_id] = buf
        buf.append(event)
        return
    for q in subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # skip event for slow client, don't evict


def _drain_replay_buffer(session_id: str, q: asyncio.Queue) -> None:
    """Replay buffered events to a newly connected overlay subscriber."""
    buf = _replay_buffers.pop(session_id, None)
    if not buf:
        return
    for event in buf:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            break


# ---------------------------------------------------------------------------
# Follow-up routing
# ---------------------------------------------------------------------------

async def _route_followup(core, session_id: str, message: str) -> None:
    """Route a follow-up message from the overlay into the agent WS.

    Sends the message to the agent and sets up a response queue.
    Events are broadcast to overlay subscribers via the existing
    chat_stream WS handler (agent responses go to response_queues,
    which chat_stream broadcasts). This function does NOT block
    waiting for the response — it fires and returns immediately.
    """
    import uuid
    agent_id = core._session_agents.get(session_id)
    if not agent_id:
        print(f"[overlay] follow-up dropped — no agent for session {session_id}")
        return
    agent_ws = core._chat_agents.get(agent_id)
    if agent_ws is None or agent_ws.closed:
        print(f"[overlay] follow-up dropped — agent {agent_id} not connected")
        return

    req_id = f"overlay-{uuid.uuid4().hex[:8]}"
    core._response_req_ids[session_id] = req_id

    ws_msg = {
        "type": "user_message",
        "session_id": session_id,
        "message": message,
        "req_id": req_id,
    }
    tab_id = core._session_tabs.get(session_id)
    if tab_id:
        ws_msg["tab_id"] = tab_id

    try:
        await agent_ws.send_json(ws_msg)
        print(f"[overlay] follow-up routed to agent {agent_id}: {message[:60]}")
    except Exception as e:
        print(f"[overlay] follow-up send failed: {e}")
        return

    # Re-inject overlay on the agent's current tab (may have changed)
    try:
        from web_app.handlers.chat_stream import _maybe_inject_overlay
        _maybe_inject_overlay(core, session_id, agent_id, "auto", message)
    except Exception:
        pass

    # Agent responses arrive on the response_queues[session_id] via
    # the chat WS handler. If no SSE consumer is active (parent browser
    # closed), we need a background task to drain and broadcast them.
    async def _drain_responses():
        q = core._response_queues.get(session_id)
        if q is None:
            q = asyncio.Queue()
            core._response_queues[session_id] = q
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=120)
                except asyncio.TimeoutError:
                    break
                broadcast_to_overlay(session_id, evt)
                if evt.get("type") == "error":
                    break
                if evt.get("type") == "done":
                    break  # turn done, but don't clean up — stay ready
        except Exception:
            pass

    asyncio.create_task(_drain_responses())


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def handle_overlay_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /overlay/ws -- overlay copilot WebSocket."""
    core = _core()
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # --- Auth handshake ---
    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close(code=4001, message=b"auth timeout or malformed")
        return ws

    token_str = auth_msg.get("token", "")
    token_data = validate_overlay_token(token_str)
    if not token_data:
        await ws.send_json({"type": "error", "data": "invalid or expired overlay token"})
        await ws.close(code=4003, message=b"invalid token")
        return ws

    session_id = token_data["session_id"]
    await ws.send_json({"type": "auth_ok", "session_id": session_id})

    # --- Subscribe to session events (cap at 5 per session) ---
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    subs = core._overlay_subscribers.setdefault(session_id, [])
    if len(subs) >= 5:
        await ws.send_json({"type": "error", "data": "too many overlay connections for this session"})
        await ws.close(code=4008, message=b"subscriber limit")
        return ws
    subs.append(q)
    # Replay any events that were broadcast before this WS connected
    _drain_replay_buffer(session_id, q)

    async def _writer():
        """Forward events from the queue to the overlay WS."""
        try:
            while not ws.closed:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    # aiohttp heartbeat=30 handles WS ping/pong; just loop
                    continue
                try:
                    await ws.send_json(evt)
                except Exception:
                    break
                # Don't break on "done" — it's a turn completion, not
                # session end. The overlay stays alive for follow-ups.
                if evt.get("type") == "error":
                    break
        except asyncio.CancelledError:
            pass

    writer_task = asyncio.create_task(_writer())

    # --- Read incoming messages from the overlay ---
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type", "")
                if msg_type == "user_followup":
                    followup_text = str(data.get("message", "")).strip()
                    if followup_text:
                        await _route_followup(core, session_id, followup_text)
                # Other message types can be added here
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        writer_task.cancel()
        try:
            await writer_task
        except asyncio.CancelledError:
            pass
        # Remove our queue from the subscriber list
        try:
            subs = core._overlay_subscribers.get(session_id, [])
            subs[:] = [s for s in subs if s is not q]
            if not subs:
                core._overlay_subscribers.pop(session_id, None)
        except Exception:
            pass

    return ws
