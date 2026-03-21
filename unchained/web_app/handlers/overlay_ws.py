"""Overlay copilot WebSocket handler.

The overlay JS running inside the agent-controlled browser tab connects
here to receive real-time chat events and (in Phase 2) send follow-up
prompts back to the agent.
"""

from __future__ import annotations

import asyncio
import base64
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


def mint_overlay_token(session_id: str, user_id: str, secret: str = "") -> str:
    """Create a short-lived HMAC-signed token scoped to one session."""
    secret = secret or os.environ.get("JWT_SECRET", "")
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

def broadcast_to_overlay(session_id: str, event: dict) -> None:
    """Push an event to all overlay WS subscribers for a session.

    Safe to call from sync or async context — Queue.put_nowait is used.
    """
    core = _core()
    subscribers = core._overlay_subscribers.get(session_id)
    if not subscribers:
        return
    dead: list[int] = []
    for i, q in enumerate(subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(i)
    # Remove dead queues (reverse order to preserve indices)
    for i in reversed(dead):
        subscribers.pop(i)


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
    except (asyncio.TimeoutError, TypeError):
        await ws.close(code=4001, message=b"auth timeout")
        return ws

    token_str = auth_msg.get("token", "")
    token_data = validate_overlay_token(token_str)
    if not token_data:
        await ws.send_json({"type": "error", "data": "invalid or expired overlay token"})
        await ws.close(code=4003, message=b"invalid token")
        return ws

    session_id = token_data["session_id"]
    await ws.send_json({"type": "auth_ok", "session_id": session_id})

    # --- Subscribe to session events ---
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    subs = core._overlay_subscribers.setdefault(session_id, [])
    subs.append(q)

    async def _writer():
        """Forward events from the queue to the overlay WS."""
        try:
            while not ws.closed:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    try:
                        await ws.send_str(": keepalive\n")
                    except Exception:
                        break
                    continue
                try:
                    await ws.send_json(evt)
                except Exception:
                    break
                if evt.get("type") in ("done", "error"):
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
                    # Phase 2: route follow-up back into chat system
                    print(f"[overlay] follow-up from session {session_id}: {str(data.get('message', ''))[:80]}")
                # Other message types can be added here
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        writer_task.cancel()
        # Remove our queue from the subscriber list
        try:
            subs = core._overlay_subscribers.get(session_id, [])
            subs[:] = [s for s in subs if s is not q]
            if not subs:
                core._overlay_subscribers.pop(session_id, None)
        except Exception:
            pass

    return ws
