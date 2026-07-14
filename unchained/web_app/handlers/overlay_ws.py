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
from web_state import ChatTurnState


def _turn_registry(core):
    """Return the optional signed-in turn registry without breaking fake cores."""
    try:
        if "_chat_turns" not in vars(core):
            return None
    except TypeError:
        return None
    registry = getattr(core, "_chat_turns", None)
    return registry if all(hasattr(registry, name) for name in ("get", "start")) else None


def _retained_turn(registry, session_id: str):
    """Look up the current retained turn while tolerating simple fake registries."""
    if not registry:
        return None
    return registry.get(session_id)


def _route_result(ok: bool, code: str, message: str, status: int) -> dict:
    return {"ok": ok, "code": code, "message": message, "status": status}


def _route_failure(
    session_id: str,
    code: str,
    message: str,
    status: int,
    *,
    notify_overlay: bool,
) -> dict:
    if notify_overlay:
        from web_app.handlers.chat_stream import _broadcast_overlay

        _broadcast_overlay(
            session_id,
            {"type": "error", "code": code, "data": message},
        )
    return _route_result(False, code, message, status)


async def _route_followup(
    core,
    session_id: str,
    message: str,
    *,
    notify_overlay: bool = True,
) -> dict:
    """Route a follow-up without replacing an in-flight browser turn."""
    overlay = core._overlay_sessions.get(session_id)
    if not overlay:
        print(f"[overlay] follow-up dropped — no overlay state for {session_id}")
        return _route_failure(
            session_id,
            "overlay_not_found",
            "Follow-up was not sent because the overlay session is no longer available.",
            404,
            notify_overlay=notify_overlay,
        )

    agent_id = core._session_agents.get(session_id) or overlay.agent_id
    req_id = f"overlay-{uuid.uuid4().hex[:8]}"
    registry = _turn_registry(core)
    if registry:
        prior_turn = _retained_turn(registry, session_id)
        if prior_turn and getattr(prior_turn, "status", "") in {"active", "cancelling"}:
            print(f"[overlay] follow-up dropped — turn active for {session_id}")
            return _route_failure(
                session_id,
                "turn_active",
                "Follow-up was not sent because another turn is still active.",
                409,
                notify_overlay=notify_overlay,
            )
        if not prior_turn:
            # The overlay alone has a user ID but not the key hash required to
            # bind a signed-in turn safely. Unknown retained state must not be
            # turned into an unowned resumable request.
            print(f"[overlay] follow-up dropped — no retained turn owner for {session_id}")
            return _route_failure(
                session_id,
                "turn_owner_missing",
                "Follow-up was not sent because session ownership could not be verified.",
                409,
                notify_overlay=notify_overlay,
            )

        owner_user_id = str(getattr(prior_turn, "owner_user_id", "") or "")
        owner_key_hash = str(getattr(prior_turn, "owner_key_hash", "") or "")
        if not owner_user_id or not owner_key_hash:
            print(f"[overlay] follow-up dropped — retained turn owner incomplete for {session_id}")
            return _route_failure(
                session_id,
                "turn_owner_incomplete",
                "Follow-up was not sent because session ownership is incomplete.",
                409,
                notify_overlay=notify_overlay,
            )

        turn, created, conflict = await registry.start(
            ChatTurnState(
                owner_user_id=owner_user_id,
                owner_key_hash=owner_key_hash,
                session_id=session_id,
                req_id=req_id,
                chat_agent_id=str(getattr(prior_turn, "chat_agent_id", "") or agent_id),
                routing_agent_id=agent_id,
                cdp_agent_id=overlay.agent_id,
                tab_id=overlay.tab_id,
            )
        )
        if conflict or not created:
            print(f"[overlay] follow-up dropped — turn race for {session_id}")
            return _route_failure(
                session_id,
                "turn_conflict",
                "Follow-up was not sent because another turn started first.",
                409,
                notify_overlay=notify_overlay,
            )

        agent_ws = core._chat_agents.get(agent_id)
        if agent_ws is None or agent_ws.closed:
            from web_app.handlers.chat_stream import _publish_turn_failure

            failure_message = "Follow-up was not sent because the chat agent is unavailable."
            _publish_turn_failure(core, turn, failure_message)
            print(f"[overlay] follow-up agent unavailable for {agent_id}")
            return _route_failure(
                session_id,
                "agent_unavailable",
                failure_message,
                503,
                notify_overlay=False,
            )

        turn.update_routing(
            chat_agent_id=str(getattr(prior_turn, "chat_agent_id", "") or agent_id),
            routing_agent_id=agent_id,
            dispatch_ws=agent_ws,
            cdp_agent_id=overlay.agent_id,
            tab_id=overlay.tab_id,
        )
        core._session_agents[session_id] = agent_id
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
            from web_app.handlers.chat_stream import _publish_turn_failure

            failure_message = "Follow-up was not sent because the chat agent could not be reached."
            _publish_turn_failure(core, turn, failure_message)
            print(f"[overlay] follow-up send failed: {e}")
            return _route_failure(
                session_id,
                "agent_send_failed",
                failure_message,
                502,
                notify_overlay=False,
            )
        return _route_result(True, "followup_routed", "Overlay follow-up sent.", 200)

    # Legacy fake-core and guest-compatible behavior. This remains queue based
    # because it has no signed-in owner identity or canonical journal.
    agent_ws = core._chat_agents.get(agent_id)
    if agent_ws is None or agent_ws.closed:
        print(f"[overlay] follow-up dropped — agent {agent_id} not connected")
        return _route_failure(
            session_id,
            "agent_unavailable",
            "Follow-up was not sent because the chat agent is unavailable.",
            503,
            notify_overlay=notify_overlay,
        )

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
        return _route_failure(
            session_id,
            "agent_send_failed",
            "Follow-up was not sent because the chat agent could not be reached.",
            502,
            notify_overlay=notify_overlay,
        )

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
    return _route_result(True, "followup_routed", "Overlay follow-up sent.", 200)


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

    result = await _route_followup(
        core,
        session_id,
        message,
        notify_overlay=False,
    )
    return web.json_response(result, status=result["status"])
