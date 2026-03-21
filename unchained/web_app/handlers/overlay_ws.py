"""Overlay copilot WebSocket handler (v2).

Thin WS handler — the overlay JS connects here to receive events
and send follow-ups. All state lives in OverlaySessionState on core.
Follow-ups are routed through the normal chat message path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

# Strong references to background tasks to prevent GC
_background_tasks: set = set()
import hmac
import json
import os
import time
import uuid

from aiohttp import web

from web_app.core import get_core as _core


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

_TOKEN_TTL = 3600  # 1 hour


def _require_secret(secret: str = "") -> str:
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
        return None
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
# Broadcast (called from chat_stream SSE loop)
# ---------------------------------------------------------------------------

def broadcast_to_overlay(session_id: str, event: dict) -> None:
    """Push an event to the overlay via CDP Runtime.evaluate.

    Calls window.__uc_overlay_push(evt) in the browser tab.
    No WebSocket or network connection needed — bypasses all CSP.
    """
    core = _core()
    overlay = core._overlay_sessions.get(session_id)
    if not overlay or not overlay.injected:
        # Buffer events before overlay is injected
        if overlay and len(overlay.pending_events) < 50:
            overlay.pending_events.append(event)
        return

    import json as _json
    evt_json = _json.dumps(event, separators=(",", ":"))
    js = f"(window.__uc_overlay_push && window.__uc_overlay_push({evt_json}))"

    async def _push():
        try:
            import cloud_tools
            relay_host, relay_port = core._parse_relay()
            await cloud_tools.run_js(
                overlay.agent_id, overlay.tab_id, js,
                relay_host, relay_port,
            )
        except Exception:
            pass  # best effort — overlay may have navigated

    asyncio.create_task(_push())


# ---------------------------------------------------------------------------
# Follow-up routing (through normal chat path)
# ---------------------------------------------------------------------------

async def _route_followup(core, session_id: str, message: str) -> None:
    """Route a follow-up from the overlay through the normal chat message path."""
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

    # Create response queue BEFORE sending so no events are lost
    q: asyncio.Queue = asyncio.Queue()
    core._response_queues[session_id] = q
    core._response_req_ids[session_id] = req_id

    ws_msg = {
        "type": "user_message",
        "session_id": session_id,
        "agent_id": overlay.agent_id,  # CDP agent for correct profile
        "message": message,
        "req_id": req_id,
    }
    if overlay.tab_id:
        ws_msg["tab_id"] = overlay.tab_id
    if overlay.slot is not None:
        ws_msg["slot"] = overlay.slot

    # Keep the provisioned tab alive — refresh last-active timestamp
    core._session_last_active[session_id] = time.time()

    try:
        await agent_ws.send_json(ws_msg)
        print(f"[overlay] follow-up routed to {agent_id}: {message[:60]}")
    except Exception as e:
        print(f"[overlay] follow-up send failed: {e}")
        core._response_queues.pop(session_id, None)
        return

    # Drain responses in background — broadcast to overlay subscriber
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
# WebSocket handler
# ---------------------------------------------------------------------------

async def handle_overlay_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /overlay/ws — overlay copilot WebSocket (v2)."""
    core = _core()
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # --- Auth ---
    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close(code=4001, message=b"auth timeout")
        return ws

    token_data = validate_overlay_token(auth_msg.get("token", ""))
    if not token_data:
        await ws.send_json({"type": "error", "data": "invalid or expired token"})
        await ws.close(code=4003, message=b"invalid token")
        return ws

    session_id = token_data["session_id"]
    overlay = core._overlay_sessions.get(session_id)

    # Create overlay state if it doesn't exist yet (first connect)
    if not overlay:
        from web_state import OverlaySessionState
        overlay = OverlaySessionState(
            session_id=session_id,
            agent_id="",
            tab_id="",
            user_id=token_data.get("user_id", ""),
            token=auth_msg.get("token", ""),
        )
        core._overlay_sessions[session_id] = overlay

    await ws.send_json({"type": "auth_ok"})

    # --- Subscribe: replace any previous subscriber ---
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    old_q = overlay.subscriber
    if old_q is not None:
        try:
            old_q.put_nowait({"type": "done"})
        except asyncio.QueueFull:
            pass
    overlay.subscriber = q

    # Drain any events buffered before this WS connected
    for evt in overlay.pending_events:
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            break
    overlay.pending_events.clear()

    # --- Writer: forward events from queue to WS ---
    async def _writer():
        try:
            while not ws.closed:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    continue
                try:
                    await ws.send_json(evt)
                except Exception:
                    break
                if evt.get("type") == "error":
                    break
        except asyncio.CancelledError:
            pass

    writer_task = asyncio.create_task(_writer())

    # --- Reader: handle incoming messages ---
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "user_followup":
                    text = str(data.get("message", "")).strip()
                    if text and len(text) <= 4000:
                        await _route_followup(core, session_id, text)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        writer_task.cancel()
        try:
            await writer_task
        except asyncio.CancelledError:
            pass
        # Track whether THIS path cleared the subscriber
        cleared = overlay.subscriber is q
        if cleared:
            overlay.subscriber = None

        # Deferred tab cleanup: only if WE cleared the subscriber
        # AND no active SSE stream exists.
        if cleared and not core._response_queues.get(session_id):
            async def _deferred_cleanup():
                try:
                    await core._close_session_tab(session_id)
                except Exception as e:
                    print(f"[overlay] deferred tab cleanup failed for {session_id}: {e}")
                finally:
                    core._overlay_sessions.pop(session_id, None)
            task = asyncio.create_task(_deferred_cleanup())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return ws
