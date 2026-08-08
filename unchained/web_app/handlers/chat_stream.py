"""Chat transport handlers (WS + SSE) extracted from web.py."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid

log = logging.getLogger(__name__)

from aiohttp import web

from chat_event_transport import (
    CHAT_WS_MAX_MESSAGE_BYTES,
    bound_agent_event,
    overlay_event,
)

from web_app.core import get_core as _core
from web_app.handlers.chat_flow import _hosted_repo
from web_state import (
    ChatTurnState,
    profile_session_caller_tag,
    profile_session_guard,
    select_profile_slot_active_tab,
)


def _finish_credit_run(core, session_id: str, status: str = "completed") -> None:
    """Finish a billing run for this session on agent-terminal events.

    Only fires when the control plane has a billing_run_id stored for the
    session. Idempotent — calling it multiple times is safe (already-finished
    runs are skipped). Browser disconnect alone does NOT trigger this path.
    """
    billing = getattr(core, "_session_billing_runs", None)
    if not isinstance(billing, dict):
        return
    run_id = billing.pop(session_id, "")
    if not run_id:
        return
    try:
        from credit import CreditLedger
        ledger = CreditLedger(db_path=core._auth.db_path)
        ledger.finish_run(run_id, status=status)
    except Exception:
        pass


_SCHEDULER_TRIGGER_RE = re.compile(r"^/schedule(?:\s+|$)")

# Local browser-agent turns can legitimately be quiet while the CLI is waiting
# on a long CDP/navigation action. Keep the SSE stream alive long enough to
# match the OpenCode lane's default local subprocess guard instead of ending a
# turn while the local agent is still working in the background.
_CODEX_CLI_SILENCE_TIMEOUT_S = 60
_OPENCODE_CLI_SILENCE_TIMEOUT_S = 300
_FIRST_LOOK_AGENT_SILENCE_TIMEOUT_S = 360
_HOSTED_MAX_ACTIVE_TURNS = max(
    1, int(os.environ.get("HOSTED_MAX_ACTIVE_TURNS", "16"))
)
_HOSTED_MAX_ACTIVE_TURNS_PER_USER = max(
    1, int(os.environ.get("HOSTED_MAX_ACTIVE_TURNS_PER_USER", "3"))
)
_HOSTED_TURN_DEADLINE_S = max(
    30, int(os.environ.get("HOSTED_TURN_DEADLINE_SECONDS", "600"))
)
_HOSTED_MAX_USER_PROMPT_CHARS = max(
    1_000, int(os.environ.get("HOSTED_MAX_USER_PROMPT_CHARS", "20000"))
)

_FIRST_LOOK_SEARCH_REF = "searchagentsky-result"
_FIRST_LOOK_SEARCH_TASK = "search-result"
_FIRST_LOOK_RESULT_ID_RE = re.compile(r"^[a-z0-9]{12}$")
_FIRST_LOOK_TERMINAL_EVENT_TYPES = frozenset({"done", "error", "cancelled"})
_FIRST_LOOK_TERMINAL_OUTCOMES = frozenset(
    {"completed", "error", "cancelled", "client_disconnected"}
)

# Opt-in verbose SSE tracing. Off by default so production logs stay quiet;
# set UNCHAINED_SSE_DEBUG=1 to log every event forwarded to the browser (this
# is what makes the "UI stops updating until refresh" freeze diagnosable).
_SSE_DEBUG = os.environ.get("UNCHAINED_SSE_DEBUG", "") == "1"


def _safe_exc_name(exc: BaseException) -> str:
    """Return a short, safe class name for an exception (no message/PII)."""
    return type(exc).__name__


def _first_look_search_attribution(body: dict) -> dict[str, str]:
    """Return only the exact bounded SearchAgentSky result handoff triad."""
    ref = str(body.get("ref", "") or "").strip()
    task = str(body.get("task", "") or "").strip()
    from_result = str(body.get("from_result", "") or "").strip().lower()
    if (
        ref == _FIRST_LOOK_SEARCH_REF
        and task == _FIRST_LOOK_SEARCH_TASK
        and _FIRST_LOOK_RESULT_ID_RE.fullmatch(from_result)
    ):
        return {
            "ref": _FIRST_LOOK_SEARCH_REF,
            "task": _FIRST_LOOK_SEARCH_TASK,
            "from_result": from_result,
        }
    return {}


def _first_look_run_id(req_id: str) -> str:
    """Build an opaque, bounded correlation ID without retaining request input."""
    return hashlib.sha256(str(req_id or "").encode("utf-8")).hexdigest()[:20]


def _first_look_terminal_outcome(event: dict, req_id: str) -> str:
    """Classify an exact-request terminal agent event, rejecting stale events."""
    if not isinstance(event, dict):
        return ""
    event_type = str(event.get("type", "") or "")
    if event_type not in _FIRST_LOOK_TERMINAL_EVENT_TYPES:
        return ""
    if not req_id or str(event.get("req_id", "") or "") != req_id:
        return ""
    if event_type == "done":
        return "completed"
    return event_type


def _track_first_look_run_event(
    core,
    request: web.Request,
    event: str,
    *,
    run_id: str,
    attribution: dict[str, str],
    status_code: int = 0,
    error_code: str = "",
    outcome: str = "",
    latency_ms: int = 0,
):
    """Persist one privacy-bounded server-owned First Look lifecycle event."""
    meta = {"run_id": run_id}
    if outcome in _FIRST_LOOK_TERMINAL_OUTCOMES:
        meta["outcome"] = outcome
    meta.update(attribution)
    analytics_route = core._analytics_route_from_request(request) or request.path
    return core._track_event(
        request,
        event,
        event_id=f"{event}:{run_id}",
        session_id=core._analytics_session_id_from_request(request),
        page_view_id=core._analytics_page_view_id_from_request(request),
        route="/web/chat",
        route_intended=analytics_route,
        route_effective=analytics_route,
        source="server",
        status_code=status_code,
        error_code=error_code,
        latency_ms=latency_ms,
        meta=meta,
    )


def _turn_registry(core):
    """Return the optional signed-in turn registry without breaking fake cores."""
    registry = getattr(core, "_chat_turns", None)
    return registry if all(hasattr(registry, name) for name in ("get", "start")) else None


def _registry_turn(registry, session_id: str, req_id: str = ""):
    """Look up a retained request while tolerating one-argument fake registries."""
    if not registry:
        return None
    if req_id:
        try:
            return registry.get(session_id, req_id)
        except TypeError:
            pass
    return registry.get(session_id)


def _turn_owned_by(turn, auth_info: dict) -> bool:
    """Use both authenticated identities for a retained turn lookup."""
    owned_by = getattr(turn, "owned_by", None)
    if not callable(owned_by):
        return False
    return bool(owned_by(auth_info.get("user_id", ""), auth_info.get("key_hash", "")))


def _agent_event_matches_turn(core, turn, agent_id: str, ws, req_id: str) -> bool:
    """Accept turn events only from the transport that received the turn."""
    return bool(
        req_id
        and req_id == turn.req_id
        and turn.routing_agent_id == agent_id
        and getattr(turn, "dispatch_ws", None) is ws
    )


def _revoke_turn_grant(core, turn) -> None:
    """Revoke a scheduler grant only after its turn has reached a terminal state."""
    if not getattr(turn, "stream_finished", False):
        return
    grant_id = str(getattr(turn, "scheduler_grant_id", "") or "")
    if grant_id:
        getattr(core, "_scheduler_turn_grants", {}).pop(grant_id, None)


def _revoke_session_scheduler_grants(core, session_id: str) -> None:
    """Revoke all scheduler grants for *session_id* — used in non-registry paths."""
    grants = getattr(core, "_scheduler_turn_grants", None)
    if not isinstance(grants, dict):
        return
    for grant_id, meta in list(grants.items()):
        if isinstance(meta, dict) and str(meta.get("session_id", "")) == str(session_id):
            grants.pop(grant_id, None)


def _publish_turn_event(core, turn, event: dict) -> dict | None:
    """Journal an event before one overlay fan-out; never await subscriber I/O."""
    published = turn.publish(event)
    if published is None:
        return None
    _broadcast_overlay(turn.session_id, published)
    event_type = published.get("type", "")
    if event_type in {"done", "error", "cancelled"}:
        # Agent-terminal event — finish the billing run (once).
        # Browser disconnect alone does NOT reach this path.
        credit_status = "completed" if event_type == "done" else "cancelled"
        _finish_credit_run(core, turn.session_id, status=credit_status)
        for task_attr in ("hosted_deadline_task", "silence_timeout_task"):
            deadline_task = getattr(turn, task_attr, None)
            if (
                deadline_task is not None
                and deadline_task is not asyncio.current_task()
                and not deadline_task.done()
            ):
                deadline_task.cancel()
            setattr(turn, task_attr, None)
    if event_type in {"error", "cancelled"}:
        # Existing consumers expect a final done marker after terminal errors
        # and cancellations. Keep it in the canonical journal so every
        # reconnecting subscriber sees the same end-of-turn sequence. Billing
        # is finalized above first so the primary outcome cannot be overwritten
        # by this compatibility marker.
        _publish_turn_event(
            core,
            turn,
            {"type": "done", "session_id": turn.session_id, "req_id": turn.req_id},
        )
    _revoke_turn_grant(core, turn)
    return published


def _publish_turn_failure(core, turn, message: str) -> None:
    """Finish a started turn that could not be dispatched to its agent."""
    _publish_turn_event(
        core,
        turn,
        {
            "type": "error",
            "session_id": turn.session_id,
            "req_id": turn.req_id,
            "data": message,
        },
    )


def _turn_timeout_task(core, turn, timeout_s: int) -> None:
    """Start a detached local-CLI silence guard independent of SSE connections."""
    if timeout_s <= 0:
        return

    async def watch() -> None:
        while not getattr(turn, "stream_finished", True):
            elapsed = time.time() - float(getattr(turn, "last_event_at", 0.0) or 0.0)
            await asyncio.sleep(max(0.1, timeout_s - elapsed))
            if getattr(turn, "stream_finished", True):
                return
            if time.time() - float(getattr(turn, "last_event_at", 0.0) or 0.0) < timeout_s:
                continue
            _publish_turn_failure(
                core,
                turn,
                "Local CLI did not return a response in time. The provider may be rate-limited or stalled; please retry or switch models.",
            )
            return

    previous = getattr(turn, "silence_timeout_task", None)
    if previous is not None and not previous.done():
        previous.cancel()
    turn.silence_timeout_task = asyncio.create_task(watch())


def _hosted_turn_deadline_task(core, turn, timeout_s: int) -> None:
    """Enforce an absolute hosted-turn deadline independent of SSE clients."""
    if timeout_s <= 0:
        return

    async def watch() -> None:
        await asyncio.sleep(timeout_s)
        if getattr(turn, "stream_finished", True):
            return
        dispatch_ws = getattr(turn, "dispatch_ws", None)
        if dispatch_ws is not None and not getattr(dispatch_ws, "closed", False):
            try:
                await dispatch_ws.send_json({
                    "type": "cancel",
                    "session_id": turn.session_id,
                    "req_id": turn.req_id,
                })
            except Exception:
                pass
        _publish_turn_failure(
            core,
            turn,
            "Hosted inference reached its time limit. Try a smaller task or continue in a new turn.",
        )

    previous = getattr(turn, "hosted_deadline_task", None)
    if previous is not None and not previous.done():
        previous.cancel()
    turn.hosted_deadline_task = asyncio.create_task(watch())


async def _start_registered_turn(
    core,
    registry,
    candidate,
    auth_info: dict,
    *,
    hosted: bool,
):
    """Start a turn with race-safe hosted global/per-user admission limits."""
    if not hosted or not hasattr(registry, "active_for_agent"):
        turn, created, conflict = await registry.start(candidate)
        return turn, created, conflict, ""

    lock = getattr(core, "_hosted_turn_admission_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        core._hosted_turn_admission_lock = lock

    async with lock:
        existing = _registry_turn(registry, candidate.session_id)
        retrying_existing = bool(
            existing
            and getattr(existing, "status", "") in {"active", "cancelling"}
            and getattr(existing, "req_id", "") == candidate.req_id
            and _turn_owned_by(existing, auth_info)
        )
        active = list(registry.active_for_agent(core.TRIAL_AGENT_ID))
        if retrying_existing:
            active = [turn for turn in active if turn is not existing]
        owner_user_id = str(auth_info.get("user_id", "") or "")
        owner_active = [
            turn for turn in active
            if str(getattr(turn, "owner_user_id", "") or "") == owner_user_id
        ]
        if len(owner_active) >= _HOSTED_MAX_ACTIVE_TURNS_PER_USER:
            return candidate, False, False, "user"
        if len(active) >= _HOSTED_MAX_ACTIVE_TURNS:
            return candidate, False, False, "global"
        turn, created, conflict = await registry.start(candidate)
        return turn, created, conflict, ""


async def _write_turn_sse(response: web.StreamResponse, event: dict) -> None:
    """Write one journal item with its monotonic sequence as the SSE id."""
    seq = int(event.get("seq", 0) or 0)
    payload = json.dumps(event, separators=(",", ":"))
    if _SSE_DEBUG:
        _core()._trace(
            "chat.msg.sse_write",
            req_id=str(event.get("req_id") or ""),
            session_id=str(event.get("session_id") or ""),
            type=str(event.get("type") or ""),
            seq=seq,
        )
    try:
        await response.write(f"id: {seq}\ndata: {payload}\n\n".encode())
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        raise
    except Exception as _write_err:
        # A non-socket write failure (closed transport, etc.) drops the stream
        # before `done` reaches the browser — the "UI stops updating until
        # refresh" symptom. Record exactly which event failed so the freeze is
        # diagnosable, then re-raise so the journal loop's finally cleans up.
        _core()._trace(
            "chat.msg.sse_write_failed",
            req_id=str(event.get("req_id") or ""),
            session_id=str(event.get("session_id") or ""),
            type=str(event.get("type") or ""),
            seq=seq,
            error=_safe_exc_name(_write_err),
        )
        raise


async def _stream_turn_journal(
    request: web.Request,
    turn,
    *,
    after: int = 0,
    prepare_response=None,
    observe_event=None,
    observe_disconnect=None,
) -> web.StreamResponse:
    """Replay a turn journal then wait for notifications without owning the turn."""
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
    if callable(prepare_response):
        prepare_response(response)
    await response.prepare(request)
    signal = turn.subscribe()
    last_seq = max(0, after)
    try:
        while True:
            events = turn.events_after(last_seq)
            if events:
                for event in events:
                    await _write_turn_sse(response, event)
                    if callable(observe_event):
                        observe_event(event)
                    last_seq = int(event.get("seq", last_seq) or last_seq)
                if getattr(turn, "stream_finished", False) and last_seq >= turn.last_seq:
                    break
                continue
            if getattr(turn, "stream_finished", False):
                break
            # Clear then re-check so a publish between the first journal read
            # and clear cannot leave a subscriber waiting for a lost signal.
            signal.clear()
            if turn.events_after(last_seq) or getattr(turn, "stream_finished", False):
                continue
            try:
                await asyncio.wait_for(signal.wait(), timeout=15)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        if callable(observe_disconnect):
            observe_disconnect()
    except asyncio.CancelledError:
        if callable(observe_disconnect):
            observe_disconnect()
        raise
    except Exception:
        # aiohttp may expose transport closure with a backend-specific error.
        # Detach only: state, scheduler grant, overlay, and browser tab survive.
        if callable(observe_disconnect):
            observe_disconnect()
    finally:
        turn.unsubscribe(signal)
    return response


def _parse_after_sequence(request: web.Request) -> int | None:
    """Parse a non-negative journal cursor from the replay endpoint."""
    raw = request.query.get("after", request.headers.get("Last-Event-ID", "0"))
    try:
        value = int(str(raw or "0"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


# ---------------------------------------------------------------------------
# Overlay copilot — direct CDP, no bridge/WS/relay involvement
# ---------------------------------------------------------------------------


def _broadcast_overlay(session_id: str, event: dict) -> None:
    """Push an event to the overlay via CDP Runtime.evaluate."""
    event = overlay_event(event)
    core = _core()
    overlay = core._overlay_sessions.get(session_id)
    if not overlay or not overlay.injected:
        if overlay and len(overlay.pending_events) < 50:
            overlay.pending_events.append(event)
        return

    evt_json = json.dumps(event, separators=(",", ":"))
    js = f"(window.__uc_overlay_push && window.__uc_overlay_push({evt_json}))"

    async def _push():
        try:
            import cloud_tools
            relay_host, relay_port = core._parse_relay()
            await cloud_tools.run_js(
                overlay.agent_id, overlay.tab_id, js, relay_host, relay_port, overlay=True)
        except Exception:
            pass
    asyncio.create_task(_push())


def _inject_overlay(core, session_id: str, agent_id: str, tab_id: str,
                     prompt_text: str, user_id: str = "", model: str = "", slot: int | None = None) -> None:
    """Inject the overlay panel via direct CDP and start outbox poller.

    Uses cloud_tools.run_js() — same path as DDM, click, navigate.
    No bridge handlers, no relay HTTP, no chat_agent_cli involvement.
    """
    # "auto" is valid: the bridge resolves it to the agent's current page
    # tab via _get_tab_ws_url with per-channel lease isolation.  Local bridge
    # users share one Chrome so "auto" correctly targets their active tab.
    # Profile/headless sessions always pass concrete tab_ids (prov-...) and
    # never reach this path.  See chrome_bridge._get_tab_ws_url for details.
    if not tab_id:
        return

    try:
        import secrets
        from overlay_js import build_overlay_js, build_overlay_bootstrap_js
        from web_state import OverlaySessionState

        nonce = secrets.token_hex(16)
        overlay_js = build_overlay_js(
            session_id=session_id,
            prompt_text=prompt_text[:500],
            nonce=nonce,
        )
        bootstrap_js = build_overlay_bootstrap_js(
            session_id=session_id,
            prompt_text=prompt_text[:500],
            nonce=nonce,
        )

        overlay = core._overlay_sessions.get(session_id)
        if not overlay:
            overlay = OverlaySessionState(
                session_id=session_id,
                agent_id=agent_id,
                tab_id=tab_id,
                user_id=user_id,
                model=model,
                slot=slot,
            )
            core._overlay_sessions[session_id] = overlay
        else:
            overlay.agent_id = agent_id
            overlay.tab_id = tab_id
            overlay.model = model
            if slot is not None:
                overlay.slot = slot

        async def _inject_and_poll():
            try:
                import cloud_tools
                relay_host, relay_port = core._parse_relay()

                # Inject overlay JS via CDP (overlay=True → separate CDP pool)
                await cloud_tools.run_js(agent_id, tab_id, overlay_js, relay_host, relay_port, overlay=True)
                # Persist across navigations
                await cloud_tools.run_cdp_command(
                    agent_id, tab_id,
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": bootstrap_js},
                    relay_host, relay_port,
                    overlay=True,
                )
                overlay.injected = True

                # Drain buffered events
                for evt in overlay.pending_events:
                    _broadcast_overlay(session_id, evt)
                overlay.pending_events.clear()
                print(f"[overlay] Injected for {session_id} on tab {tab_id[:12]}")

                # Poll outbox for follow-ups (500ms) + re-inject if overlay lost
                while True:
                    await asyncio.sleep(0.5)
                    o = core._overlay_sessions.get(session_id)
                    if not o or not o.injected:
                        break
                    try:
                        # Check if overlay exists + drain outbox in one call
                        raw = await cloud_tools.run_js(
                            agent_id, tab_id,
                            "(function(){if(!document.getElementById('__uc_overlay_host'))return '__REINJECT__';"
                            "var q=window.__uc_overlay_outbox||[];window.__uc_overlay_outbox=[];return JSON.stringify(q)})()",
                            relay_host, relay_port,
                            overlay=True,
                        )
                        if raw == "__REINJECT__":
                            # Overlay lost (navigation cleared DOM) — re-inject
                            await cloud_tools.run_js(agent_id, tab_id, overlay_js, relay_host, relay_port, overlay=True)
                            continue
                        if raw and raw != "[]":
                            from web_app.handlers.overlay_ws import _route_followup
                            msgs = json.loads(raw)
                            for msg in msgs:
                                if msg.get("type") != "user_followup":
                                    continue
                                if msg.get("nonce") != nonce:
                                    continue
                                text = str(msg.get("message", "")).strip()
                                if text and len(text) <= 4000:
                                    await _route_followup(core, session_id, text)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[overlay] Injection failed: {e}")

        # Cancel any previous poll loop for this session to avoid
        # duplicate pollers each opening their own CDP connections.
        if overlay.poll_task and not overlay.poll_task.done():
            overlay.poll_task.cancel()
        overlay.poll_task = asyncio.create_task(_inject_and_poll())
    except Exception as e:
        print(f"[overlay] Setup failed: {e}")


def _normalize_profile_path(raw: object) -> str:
    """Normalize optional profile path from client payloads."""
    return str(raw or "").strip()


def _resolve_profile_intent(
    body: dict,
    current_tab: object,
    remembered_profile_path: object,
    expired_profile: bool = False,
) -> tuple[str, str]:
    """Resolve explicit default, exact profile, preserved profile, or expiry."""
    if "profile_path" in body:
        selected = _normalize_profile_path(body.get("profile_path"))
        return ("profile", selected) if selected else ("default", "")
    if expired_profile:
        return "expired", ""
    remembered = _normalize_profile_path(remembered_profile_path)
    if remembered:
        return "profile", remembered
    if str(current_tab or "").startswith("prov-"):
        return "expired", ""
    return "unchanged", ""


def _discard_response_registration(core, session_id: str) -> None:
    """Remove both halves of a response-queue registration."""
    core._response_queues.pop(session_id, None)
    core._response_req_ids.pop(session_id, None)


def _signal_superseded_response_queue(
    queue: asyncio.Queue,
    *,
    guest_mode: bool,
    session_id: str,
    req_id: str,
) -> None:
    """End an old queue without changing legacy non-guest semantics."""
    if guest_mode:
        terminal_events = (
            {"type": "cancelled", "session_id": session_id, "req_id": req_id},
            {"type": "done", "session_id": session_id, "req_id": req_id},
        )
    else:
        terminal_events = ({"type": "done"},)
    for terminal_event in terminal_events:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(terminal_event)


def _extract_scheduler_turn(message: str, *, allow_trigger: bool = True) -> tuple[bool, str]:
    text = str(message or "").strip()
    if not allow_trigger:
        return False, text
    match = _SCHEDULER_TRIGGER_RE.match(text)
    if not match:
        return False, text
    remainder = text[match.end():].strip()
    if not remainder:
        remainder = "List my scheduled tasks and help me manage them."
    return True, remainder


def _scheduler_trigger_supported(*, guest_mode: bool, is_openrouter: bool) -> bool:
    """Scheduler trigger is supported for authenticated users including hosted openrouter.

    Guest (anonymous) users and non-openrouter paths that don't match a
    user account are still rejected.  Authenticated hosted-openrouter
    turns receive a short-lived scoped grant that lets the shared trial
    worker call back into scheduler endpoints without raw user keys.
    """
    if guest_mode:
        return False
    # Authenticated users on any lane (including hosted openrouter) are allowed
    return True


async def _allowed_profile_paths(core, agent_id: str) -> set[str]:
    """Return server-validated profile paths for this user bridge."""
    import signup_agent

    profiles = signup_agent.list_chrome_profiles()
    if not profiles and agent_id:
        relay_host, relay_port = core._parse_relay()
        profiles = await core.provision_helpers.fetch_relay_profiles(
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
            headers=core._relay_auth_headers(),
        )
    allowed: set[str] = set()
    for profile in profiles:
        path = _normalize_profile_path(profile.get("profile_path") or profile.get("path"))
        if path:
            allowed.add(path)
    return allowed


def _bind_profile_tab(
    core,
    session_id: str,
    cdp_agent_id: str,
    profile_path: str,
    tab_id: str,
) -> None:
    """Atomically replace the server-side target for a profile session."""
    core._session_tabs[session_id] = tab_id
    allowed_tabs = getattr(core, "_session_allowed_tabs", None)
    if allowed_tabs is not None:
        allowed_tabs[session_id] = {tab_id}
    core._session_agent_map[session_id] = cdp_agent_id
    core._session_last_active[session_id] = time.time()
    core._session_profile_paths[session_id] = profile_path
    getattr(core, "_expired_profile_sessions", {}).pop(session_id, None)


async def _live_profile_tab(
    core,
    session_id: str,
    cdp_agent_id: str,
    profile_path: str,
    current_tab: str,
) -> tuple[str, bool]:
    """Return ``(live_tab, may_cleanup_slot)`` for the pinned provision."""
    import cloud_tools

    relay_host, relay_port = core._parse_relay()
    status = await cloud_tools.provision_status(cdp_agent_id, relay_host, relay_port)
    return select_profile_slot_active_tab(
        status,
        session_id=session_id,
        profile_path=profile_path,
        current_tab=current_tab,
    )


def _detach_profile_target(
    core,
    session_id: str,
    cdp_agent_id: str,
    profile_path: str,
) -> None:
    """Drop an unowned stale target while preserving fail-closed intent."""
    core._session_tabs.pop(session_id, None)
    allowed_tabs = getattr(core, "_session_allowed_tabs", None)
    if allowed_tabs is not None:
        allowed_tabs.pop(session_id, None)
    getattr(core, "_overlay_sessions", {}).pop(session_id, None)
    getattr(core, "_chat_preview_generations", {}).pop(session_id, None)
    core._session_profile_paths[session_id] = profile_path
    core._session_agent_map[session_id] = cdp_agent_id


async def _ensure_profile_tab(core, session_id: str, cdp_agent_id: str, profile_path: str) -> str:
    """Ensure a session is pinned to a live browser for the exact profile."""
    async with profile_session_guard(core, session_id):
        current_tab = core._session_tabs.get(session_id, "")
        current_profile = core._session_profile_paths.get(session_id, "")
        pending_close = current_tab and current_tab in getattr(core, "_tabs_pending_close", {})
        may_cleanup_slot = True
        if current_tab and current_profile == profile_path and not pending_close:
            if not str(current_tab).startswith("prov-"):
                may_cleanup_slot = False
            else:
                live_tab, may_cleanup_slot = await _live_profile_tab(
                    core,
                    session_id,
                    cdp_agent_id,
                    profile_path,
                    current_tab,
                )
                if live_tab:
                    _bind_profile_tab(core, session_id, cdp_agent_id, profile_path, live_tab)
                    return live_tab
            print(
                f"[profile] Provision slot expired for session {session_id}; "
                f"relaunching profile={os.path.basename(profile_path)}"
            )

        if current_tab:
            if may_cleanup_slot:
                await core._close_session_tab(
                    session_id,
                    profile_lock_held=True,
                    preserve_profile_path=profile_path,
                    preserve_agent_id=cdp_agent_id,
                )
            else:
                _detach_profile_target(
                    core,
                    session_id,
                    cdp_agent_id,
                    profile_path,
                )

        relay_host, relay_port = core._parse_relay()
        import cloud_tools

        print(f"[profile] Provisioning Chrome for session {session_id} profile={os.path.basename(profile_path)}")
        try:
            launch = await cloud_tools.provision_launch(
                cdp_agent_id,
                profile_path,
                relay_host,
                relay_port,
                caller_tag=profile_session_caller_tag(session_id),
            )
            tab_id = str((launch or {}).get("tab_id", "")).strip()
            from chrome_bridge import _extract_prov_slot

            tab_parts = tab_id.split("-", 2)
            if (
                not _extract_prov_slot(tab_id)
                or len(tab_parts) != 3
                or not tab_parts[2]
            ):
                raise RuntimeError("Profile browser launch did not return a slotted tab_id")
        except Exception:
            # Preserve profile intent after a failed relaunch. A later omitted
            # profile request must retry this profile, never fall through to
            # the default browser.
            core._session_profile_paths[session_id] = profile_path
            core._session_agent_map[session_id] = cdp_agent_id
            core._session_last_active[session_id] = time.time()
            raise

        _bind_profile_tab(core, session_id, cdp_agent_id, profile_path, tab_id)
        return tab_id


async def _clear_profile_tab(core, session_id: str) -> None:
    """Explicitly leave provisioned-profile mode without racing a relaunch."""
    getattr(core, "_expired_profile_sessions", {}).pop(session_id, None)
    current_tab = str(core._session_tabs.get(session_id, "") or "")
    current_profile = core._session_profile_paths.get(session_id, "")
    if not current_profile and not current_tab.startswith("prov-"):
        return
    async with profile_session_guard(core, session_id):
        current_tab = str(core._session_tabs.get(session_id, "") or "")
        current_profile = core._session_profile_paths.get(session_id, "")
        if current_profile or current_tab.startswith("prov-"):
            await core._close_session_tab(session_id, profile_lock_held=True)


_DISCONNECT_GRACE_SECONDS = 30


def _cancel_deferred_cleanup(core, agent_id: str) -> None:
    """Cancel any pending deferred cleanup task for this agent."""
    deferred = getattr(core, "_agent_deferred_cleanup", None)
    task = deferred.get(agent_id) if isinstance(deferred, dict) else None
    if task is not None and isinstance(task, asyncio.Task):
        task.cancel()
        deferred.pop(agent_id, None)


async def _deferred_agent_cleanup(
    core,
    agent_id: str,
    session_tabs: list[tuple[str, str, str, str]],
    active_turn_ids: list[tuple[str, str]],
):
    """Wait a grace period, then clean up if the agent never reconnected.

    Active turns are NOT failed immediately on disconnect.  Instead they stay
    active through the grace window so a reconnect can rebind them.  Only after
    the grace period expires are failures published and tabs closed.
    """
    try:
        await asyncio.sleep(_DISCONNECT_GRACE_SECONDS)
    except asyncio.CancelledError:
        return

    current_ws = core._chat_agents.get(agent_id)
    if current_ws is not None:
        return  # agent reconnected, nothing to clean

    deferred = getattr(core, "_agent_deferred_cleanup", None)
    if isinstance(deferred, dict):
        deferred.pop(agent_id, None)

    # Agent never reconnected — publish failures on all active turns.
    registry = _turn_registry(core)
    for session_id, req_id in active_turn_ids:
        turn = _registry_turn(registry, session_id, req_id) if registry else None
        if turn and not getattr(turn, "stream_finished", False):
            _publish_turn_failure(
                core,
                turn,
                "Local agent disconnected before completing this response. "
                "Please retry after the client reconnects.",
            )

    for sid, expected_tab_id, profile_path, preserve_agent_id in session_tabs:
        await core._close_session_tab(
            sid,
            expected_tab_id=expected_tab_id,
            preserve_profile_path=profile_path,
            preserve_agent_id=preserve_agent_id,
        )

    print(f"[chat] Agent {agent_id} deferred cleanup: "
          f"failed {len(active_turn_ids)} turns, "
          f"closed {len(session_tabs)} session tabs")


async def handle_chat_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for the local chat agent."""
    core = _core()
    ws = web.WebSocketResponse(
        heartbeat=30,
        max_msg_size=CHAT_WS_MAX_MESSAGE_BYTES,
    )
    await ws.prepare(request)

    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except (asyncio.TimeoutError, TypeError):
        await ws.close(code=4001, message=b"auth timeout")
        return ws

    key = auth_msg.get("key", "")
    is_trial_agent = bool(core.TRIAL_AGENT_KEY) and hmac.compare_digest(key, core.TRIAL_AGENT_KEY)
    if not is_trial_agent:
        key_info = core._auth.validate_key(key)
        if not key_info:
            await ws.send_json({"type": "error", "data": "invalid key"})
            await ws.close(code=4003, message=b"invalid key")
            return ws

    if is_trial_agent and core.TRIAL_AGENT_ID:
        agent_id = core.TRIAL_AGENT_ID
    elif auth_msg.get("agent_id"):
        agent_id = auth_msg["agent_id"]
    else:
        agent_id = f"claude-{core._key_hash(key)}"
    caps = auth_msg.get("capabilities", {})
    if not isinstance(caps, dict):
        caps = {}
    await ws.send_json({"type": "auth_ok"})
    old_ws = core._chat_agents.get(agent_id)
    # Always cancel any pending deferred cleanup for this agent on
    # reconnect — even if the old WS was already cleaned up and
    # _chat_agents no longer contains it.
    _cancel_deferred_cleanup(core, agent_id)
    core._chat_agents[agent_id] = ws
    core._chat_agent_caps[agent_id] = caps
    # Migrate dispatch_ws on all of this agent's active turns so they
    # can receive events through the new transport.  This covers both
    # the overlap case (old WS still registered) and the disconnect-
    # first case (old WS already cleaned from _chat_agents).
    registry = _turn_registry(core)
    if registry and hasattr(registry, "active_for_agent"):
        for turn in registry.active_for_agent(agent_id):
            if getattr(turn, "dispatch_ws", None) is not ws:
                turn.dispatch_ws = ws
                logger = getattr(core, "log", None)
                if logger:
                    logger.info(
                        "[chat] agent %s %s — migrated dispatch_ws on turn %s",
                        agent_id,
                        "reconnected" if old_ws is not None and old_ws is not ws else "connected (cleanup after disconnect)",
                        turn.req_id,
                    )
    if not is_trial_agent and key_info:
        core._chat_agent_users[agent_id] = key_info.get("user_id", "")
    print(f"[chat] Agent {agent_id} connected")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                data = bound_agent_event(data, encoded_size=len(msg.data.encode("utf-8")))
                if data.get("malformed_text_event"):
                    log.warning(
                        "[chat] replaced malformed text event session=%s req_id=%s "
                        "agent=%s data_type=%s",
                        str(data.get("session_id", "") or ""),
                        str(data.get("req_id", "") or ""),
                        agent_id,
                        str(data.get("malformed_text_data_type", "") or ""),
                    )

                msg_type = data.get("type", "")

                if msg_type == "openrouter_usage":
                    try:
                        usage_user_id = str(data.get("user_id", "")).strip()
                        usage_sid = str(data.get("session_id", "")).strip()
                        usage_model = str(data.get("model", "")).strip()
                        prompt_tokens = max(0, core._coerce_int(data.get("prompt_tokens"), 0))
                        completion_tokens = max(0, core._coerce_int(data.get("completion_tokens"), 0))
                        total_tokens = max(0, core._coerce_int(data.get("total_tokens"), 0))
                        if total_tokens <= 0:
                            total_tokens = prompt_tokens + completion_tokens
                        direct_cost = core._coerce_float(data.get("cost_usd"), 0.0)
                        estimated_cost = core._coerce_float(data.get("estimated_cost_usd"), 0.0)
                        usage_cost = max(0.0, direct_cost if direct_cost > 0 else estimated_cost)
                        if usage_user_id and (usage_cost > 0 or total_tokens > 0):
                            budget_state = core._track_openrouter_usage_for_user(
                                usage_user_id,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=usage_cost,
                            )
                            core._trace(
                                "openrouter.usage",
                                user_id=usage_user_id,
                                session_id=usage_sid,
                                model=usage_model or "-",
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=f"{usage_cost:.9f}",
                                estimated_cost_usd=f"{estimated_cost:.9f}",
                                spend_usd=f"{core._coerce_float(budget_state.get('spent_usd'), 0.0):.6f}",
                                budget_usd=f"{core._coerce_float(budget_state.get('budget_usd'), 0.0):.6f}",
                                remaining_usd=f"{core._coerce_float(budget_state.get('remaining_usd'), 0.0):.6f}",
                                usage_events=core._coerce_int(budget_state.get("usage_events"), 0),
                                total_user_tokens=core._coerce_int(budget_state.get("total_tokens"), 0),
                                capped=int(bool(budget_state.get("capped"))),
                            )
                    except Exception as e:
                        core.log.warning("[chat] failed to track OpenRouter usage: %s", e)
                    continue

                req_id = data.get("req_id", "")
                sid = data.get("session_id", "")
                registry = _turn_registry(core)
                turn = _registry_turn(registry, sid) if sid else None
                if turn and getattr(turn, "status", "") in {"active", "cancelling"}:
                    # Signed-in resumable turns never infer request ownership
                    # from a session alone. A matching event reaches the journal
                    # before control-response compatibility handling below.
                    if _agent_event_matches_turn(core, turn, agent_id, ws, req_id):
                        _publish_turn_event(core, turn, data)
                        continue
                    if msg_type in {"done", "error", "cancelled"}:
                        logger = getattr(core, "log", None)
                        if logger:
                            logger.warning(
                                "[chat] dropped terminal turn event "
                                "session=%s req_match=%s agent_match=%s "
                                "dispatch_match=%s current_match=%s",
                                sid,
                                req_id == getattr(turn, "req_id", ""),
                                agent_id == getattr(turn, "routing_agent_id", ""),
                                ws is getattr(turn, "dispatch_ws", None),
                                ws is core._chat_agents.get(agent_id),
                            )
                    if not (req_id and (data.get("event_omitted") or msg_type in (
                        "history_response",
                        "new_chat_ok",
                        "new_chat_error",
                        "switch_slot_ok",
                        "slots_response",
                        "update_client_ok",
                        "update_client_error",
                        "archives_response",
                        "restore_archive_ok",
                        "restore_archive_error",
                        "delete_archive_ok",
                        "delete_archive_error",
                    ))):
                        continue
                if req_id and (data.get("event_omitted") or msg_type in (
                    "history_response",
                    "new_chat_ok",
                    "new_chat_error",
                    "switch_slot_ok",
                    "slots_response",
                    "update_client_ok",
                    "update_client_error",
                    "archives_response",
                    "restore_archive_ok",
                    "restore_archive_error",
                    "delete_archive_ok",
                    "delete_archive_error",
                )):
                    rq = core._agent_req_queues.get(req_id)
                    if rq:
                        await rq.put(data)
                    continue

                q = getattr(core, "_response_queues", {}).get(sid)
                if q:
                    expected_rid = getattr(core, "_response_req_ids", {}).get(sid, "")
                    event_rid = data.get("req_id", "")
                    if event_rid and expected_rid and event_rid != expected_rid:
                        continue  # stale event from previous turn
                    await q.put(data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        is_current = core._chat_agents.get(agent_id) is ws
        if is_current:
            del core._chat_agents[agent_id]
            core._chat_agent_caps.pop(agent_id, None)
            core._chat_agent_users.pop(agent_id, None)
        session_tabs: list[tuple[str, str, str, str]] = []
        active_turns = []
        active_turn_closes = []
        closed_sessions = 0
        registry = _turn_registry(core)
        if registry and hasattr(registry, "active_for_transport"):
            active_turns = registry.active_for_transport(agent_id, ws)
        elif is_current and registry and hasattr(registry, "active_for_agent"):
            active_turns = registry.active_for_agent(agent_id)

        # Keep active turns alive through the reconnect grace window.
        # Failures and tab closures are published only after the grace
        # period expires without a reconnect.
        active_turn_ids: list[tuple[str, str]] = []
        for turn in active_turns:
            if getattr(turn, "stream_finished", False):
                continue
            active_turn_ids.append((turn.session_id, turn.req_id))
            closed_sessions += 1
            expected_tab_id = str(core._session_tabs.get(turn.session_id, "") or "")
            profile_path = core._session_profile_paths.get(turn.session_id, "")
            preserve_agent_id = turn.cdp_agent_id if profile_path else ""
            session_tabs.append(
                (turn.session_id, expected_tab_id, profile_path, preserve_agent_id)
            )

        if is_current:
            all_active_turns = (
                registry.active_for_agent(agent_id)
                if registry and hasattr(registry, "active_for_agent")
                else active_turns
            )
            active_turn_sessions = {turn.session_id for turn in all_active_turns}
            agent_sessions = [
                sid
                for sid, aid in list(core._session_agent_map.items())
                if aid == agent_id
            ]
            for sid in agent_sessions:
                if sid in active_turn_sessions:
                    continue
                closed_sessions += 1
                expected_tab_id = str(core._session_tabs.get(sid, "") or "")
                profile_path = core._session_profile_paths.get(sid, "")
                session_tabs.append(
                    (sid, expected_tab_id, profile_path,
                     agent_id if profile_path else "")
                )

        if is_current:
            print(f"[chat] Agent {agent_id} disconnected, "
                  f"deferring {len(active_turn_ids)} turn failures + "
                  f"{len(session_tabs)} session tab closes "
                  f"(grace={_DISCONNECT_GRACE_SECONDS}s)")
        else:
            print(
                f"[chat] Agent {agent_id} stale connection closed "
                f"(superseded by reconnect), cleaning {closed_sessions} session tabs"
            )

        if is_current and (session_tabs or active_turn_ids):
            deferred = getattr(core, "_agent_deferred_cleanup", None)
            if not isinstance(deferred, dict):
                deferred = {}
                core._agent_deferred_cleanup = deferred
            task = asyncio.create_task(
                _deferred_agent_cleanup(core, agent_id, session_tabs,
                                        active_turn_ids)
            )
            deferred[agent_id] = task

        if not is_current and active_turn_ids:
            # Stale transport: publish failures immediately.  The replacement
            # connection already superseded this socket; no grace period is
            # needed because turn events can reach the replacement transport.
            registry = _turn_registry(core)
            for session_id, req_id in active_turn_ids:
                turn = _registry_turn(registry, session_id, req_id) if registry else None
                if turn and not getattr(turn, "stream_finished", False):
                    _publish_turn_failure(
                        core,
                        turn,
                        "Local agent disconnected before completing this response. "
                        "Please retry after the client reconnects.",
                    )

        if not is_current and session_tabs:
            # Stale transport: close tabs immediately (no deferral needed).
            for sid, expected_tab_id, profile_path, preserve_agent_id in session_tabs:
                await core._close_session_tab(
                    sid,
                    expected_tab_id=expected_tab_id,
                    preserve_profile_path=profile_path,
                    preserve_agent_id=preserve_agent_id,
                )

        if not is_current and active_turn_closes:
            await asyncio.gather(*active_turn_closes, return_exceptions=True)

    return ws


async def handle_chat_msg(request: web.Request) -> web.StreamResponse:
    """POST /web/chat — phone sends message, gets SSE stream back."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json body"}, status=400)

    wants_guest_first_look = (
        body.get("first_look_guest") is True and body.get("headless") is True
    )
    auth_info = None if wants_guest_first_look else core._authenticate(request)
    guest_mode = False
    guest_id = ""
    guest_quota_count = 0
    guest_quota_increment = False
    if auth_info is None:
        if not wants_guest_first_look:
            return web.json_response({"error": "Not authenticated"}, status=401)
        auth_info, guest_id, guest_quota_count = core._first_look_guest_auth(request)
        guest_mode = True

    # Guest dispatch IDs are server-owned so a caller cannot reuse an
    # X-Request-ID to merge two runs into one analytics correlation key.
    req_id = uuid.uuid4().hex if guest_mode else core._request_id(request)
    first_look_run_id = _first_look_run_id(req_id) if guest_mode else ""
    first_look_attribution = _first_look_search_attribution(body) if guest_mode else {}
    first_look_accepted = False
    first_look_accepted_at = 0.0
    first_look_terminal = ""

    def reject_first_look(response: web.StreamResponse, reason: str):
        if guest_mode and not first_look_accepted:
            _track_first_look_run_event(
                core,
                request,
                "first_look_run_rejected",
                run_id=first_look_run_id,
                attribution=first_look_attribution,
                status_code=int(getattr(response, "status", 0) or 0),
                error_code=reason,
            )
        return response

    def record_first_look_terminal(outcome: str, *, error_code: str = "") -> bool:
        nonlocal first_look_terminal
        if (
            not guest_mode
            or not first_look_accepted
            or first_look_terminal
            or outcome not in _FIRST_LOOK_TERMINAL_OUTCOMES
        ):
            return False
        first_look_terminal = outcome
        latency_ms = max(0, int((time.monotonic() - first_look_accepted_at) * 1000))
        _track_first_look_run_event(
            core,
            request,
            "first_look_run_terminal",
            run_id=first_look_run_id,
            attribution=first_look_attribution,
            status_code=200,
            error_code=error_code,
            outcome=outcome,
            latency_ms=latency_ms,
        )
        return True

    if guest_mode and not core.HEADLESS_AGENT_ID:
        err = web.json_response({"error": "headless_bridge_not_configured"}, status=503)
        core._attach_first_look_guest_cookies(err, request, guest_id, quota_count=guest_quota_count)
        return reject_first_look(err, "headless_bridge_not_configured")

    raw_message_value = body.get("message", "")
    raw_message = raw_message_value.strip() if isinstance(raw_message_value, str) else ""
    scheduler_armed, message = _extract_scheduler_turn(
        raw_message,
        allow_trigger=bool(body.get("allow_scheduler_trigger", True)),
    )
    agent_id = auth_info.get("agent_id")
    key_hash = auth_info["key_hash"]
    session_id = body.get("session_id", "")
    model = body.get("model", "")
    slot = body.get("slot")
    if (guest_mode or core._is_pending_user(auth_info)) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    guest_requested_model = ""
    if guest_mode:
        guest_requested_model = str(model or "").strip()
    core._trace(
        "chat.msg.in",
        req_id=req_id,
        user_id=auth_info.get("user_id", ""),
        agent_id=agent_id,
        session_id=session_id or "-",
        model=model or "-",
    )

    if not message:
        return reject_first_look(
            web.json_response({"error": "message required"}, status=400),
            "message_required",
        )
    if not agent_id:
        return reject_first_look(
            web.json_response({"error": "agent_id required"}, status=400),
            "agent_id_required",
        )

    is_gemini = model and model.startswith("gemini")
    is_claude_sdk = core._is_claude_sdk_model(model)
    is_codex_sdk = core._is_codex_sdk_model(model)
    is_codex_cli = core._is_codex_cli_model(model)
    is_opencode_cli = core._is_opencode_cli_model(model)
    is_openrouter = core._is_openrouter_model(model)
    if is_openrouter and len(message) > _HOSTED_MAX_USER_PROMPT_CHARS:
        return reject_first_look(
            web.json_response(
                {
                    "error": "hosted_user_prompt_too_large",
                    "message": (
                        "Your message is too long for hosted chat. "
                        f"Keep it under {_HOSTED_MAX_USER_PROMPT_CHARS:,} characters."
                    ),
                },
                status=413,
            ),
            "hosted_user_prompt_too_large",
        )
    if guest_mode and not is_openrouter:
        return reject_first_look(
            web.json_response(
                {"error": "guest_mode_requires_openrouter_model"},
                status=400,
            ),
            "guest_model_invalid",
        )
    if core._is_pending_user(auth_info) and not is_openrouter:
        return core._pending_limited_response()
    if scheduler_armed and not _scheduler_trigger_supported(
        guest_mode=guest_mode,
        is_openrouter=is_openrouter,
    ):
        return reject_first_look(
            web.json_response(
                {
                    "error": "scheduler_trigger_requires_authentication",
                    "message": (
                        "The /schedule trigger requires authentication. "
                        "Signed-out guest/demo users cannot schedule tasks; "
                        "sign in to use /schedule on any lane including the hosted OpenRouter trial."
                    ),
                },
                status=400,
            ),
            "scheduler_unsupported",
        )
    openrouter_forced_model = ""
    openrouter_forced_from_model = ""
    openrouter_forced_notice = ""
    openrouter_budget_state: dict | None = None
    billing_run_id = ""  # opaque billing run ID for trial agent
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    profile_selection_present = "profile_path" in body

    def _session_owned(sid: str) -> bool:
        parts = sid.split("-")
        return len(parts) >= 4 and parts[0] == "s" and parts[2] == key_hash

    if not session_id:
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"
    elif not _session_owned(session_id):
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"
    scheduler_grant_id = ""
    if scheduler_armed and guest_mode:
        scheduler_grant_id = core._mint_scheduler_turn_grant(auth_info.get("user_id", ""), session_id)
    # Browser-hosted turns carry an explicit slot. Background scheduler/API
    # turns intentionally omit it and must not claim or conflict with a UI
    # conversation slot.
    if (
        is_openrouter
        and not guest_mode
        and auth_info.get("user_id")
        and body.get("slot") is not None
    ):
        try:
            repo = _hosted_repo()
            requested_slot_raw = body.get("slot")
            requested_slot = None
            if requested_slot_raw is not None:
                try:
                    candidate = int(requested_slot_raw)
                except (TypeError, ValueError):
                    return web.json_response(
                        {"error": "slot must be an integer from 1 to 3"},
                        status=400,
                    )
                if candidate not in (1, 2, 3):
                    return web.json_response(
                        {"error": "slot must be an integer from 1 to 3"},
                        status=400,
                    )
                requested_slot = candidate
            bound = repo.bind_initial_session(
                auth_info["user_id"],
                session_id,
                slot=requested_slot,
            )
            if not bound:
                return web.json_response(
                    {
                        "error": "hosted_slot_conflict",
                        "message": (
                            "This conversation slot changed on another client. "
                            "Reload the trial page and try again."
                        ),
                    },
                    status=409,
                )
        except (OSError, ValueError) as exc:
            log.exception(
                "[chat] initial slot bind failed for user %s: %s",
                auth_info.get("user_id", ""),
                exc,
            )
            return web.json_response(
                {
                    "error": "hosted_slot_unavailable",
                    "message": "Hosted conversation state is temporarily unavailable.",
                },
                status=503,
            )
    if not guest_mode:
        # Fast-path a refresh/retry before quota and provider checks. The
        # locked start below remains the race-safe authority for two genuinely
        # simultaneous first requests.
        existing_registry = _turn_registry(core)
        existing_turn = _registry_turn(existing_registry, session_id)
        if existing_turn and getattr(existing_turn, "status", "") in {"active", "cancelling"}:
            if existing_turn.req_id == req_id and _turn_owned_by(existing_turn, auth_info):
                return await _stream_turn_journal(request, existing_turn)
            return web.json_response(
                {
                    "error": "chat_turn_active",
                    "message": "A chat turn is already active for this session.",
                    "req_id": existing_turn.req_id if _turn_owned_by(existing_turn, auth_info) else "",
                },
                status=409,
            )
    analytics_route = core._analytics_route_from_request(request) or request.path

    if not guest_mode:
        core._track_event(
            request,
            "chat_message_send",
            session_id=core._analytics_session_id_from_request(request) or session_id,
            page_view_id=core._analytics_page_view_id_from_request(request),
            route="/web/chat",
            route_intended=body.get("route_intended", analytics_route),
            route_effective=body.get("route_effective", analytics_route),
            user_id=auth_info.get("user_id", ""),
            user_type=auth_info.get("user_type", ""),
            source="web",
            meta={
                "model": model or "",
                "headless": bool(body.get("headless", False)),
                "scheduler_armed": scheduler_armed,
                "chat_session_id": session_id,
            },
            status_code=200,
        )

    if is_gemini:
        import signup_agent

        gemini_key = signup_agent.get_provider_key(auth_info["user_id"], "gemini")
        if not gemini_key:
            return web.json_response(
                {"error": "No Gemini API key. Visit /setup to provision one."},
                status=400,
            )
        core._spawn_gemini_agent(auth_info["user_id"], auth_info["key"], gemini_key)
        ws = core._chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Gemini API agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_claude_sdk:
        import signup_agent

        user_id = auth_info["user_id"]
        claude_key = signup_agent.get_provider_key(user_id, "claude-sdk")
        if not claude_key:
            return web.json_response(
                {"error": "No Claude API key. Visit /setup to provision one."},
                status=400,
            )
        core._spawn_claude_sdk_agent(user_id, auth_info["key"], claude_key)
        ws = core._chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            for _wait in range(20):
                await asyncio.sleep(0.5)
                ws = core._chat_agents.get(chat_agent_id)
                if ws is not None and not ws.closed:
                    break
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Claude API agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_codex_sdk:
        import signup_agent

        user_id = auth_info["user_id"]
        primary = "codex-sdk"
        fallback = "codex-cli"
        codex_key = signup_agent.get_provider_key(user_id, primary) or signup_agent.get_provider_key(
            user_id, fallback
        )
        if not codex_key:
            return web.json_response(
                {"error": "No Codex key. Visit /setup to provision one."},
                status=400,
            )
        core._spawn_codex_sdk_agent(user_id, auth_info["key"], codex_key)
        ws = core._chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Codex agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_codex_cli:
        local_agent_id = auth_info["agent_id"]
        ws = core._chat_agents.get(local_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {
                    "error": "Codex CLI requires your local agent connection. Open /app and connect your agent."
                },
                status=503,
            )
        caps = core._chat_agent_caps.get(local_agent_id, {})
        if not bool(caps.get("codex_cli")):
            return web.json_response(
                {
                    "error": "Your local agent does not support Codex CLI yet. Please update/restart your local agent package and try again."
                },
                status=426,
            )
    elif is_opencode_cli:
        local_agent_id = auth_info["agent_id"]
        ws = core._chat_agents.get(local_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {
                    "error": "OpenCode CLI requires your local agent connection. Open /app and connect your agent."
                },
                status=503,
            )
        caps = core._chat_agent_caps.get(local_agent_id, {})
        if not bool(caps.get("opencode_cli")):
            return web.json_response(
                {
                    "error": "Your local agent does not support OpenCode CLI yet. Please update/restart your local agent package and try again."
                },
                status=426,
            )
    elif is_openrouter:
        if not core.TRIAL_AGENT_ID:
            return reject_first_look(
                web.json_response(
                    {"error": "Trial agent is not configured. Please try a Claude model."},
                    status=503,
                ),
                "trial_agent_not_configured",
            )
        if guest_mode:
            # Anonymous First Look runs use the shared free-model lane only.
            # They still receive a zero-dollar accounting run and global
            # admission, but cannot consume paid credit by resetting cookies.
            model = core._OPENROUTER_TRIAL_FALLBACK_MODEL
            from credit import _default_reservation
            if _default_reservation(model) != 0:
                return reject_first_look(
                    web.json_response(
                        {"error": "guest_free_model_not_configured"}, status=503
                    ),
                    "guest_free_model_not_configured",
                )
            if guest_requested_model and guest_requested_model != model:
                openrouter_forced_model = model
                openrouter_forced_from_model = guest_requested_model
                openrouter_forced_notice = (
                    "First Look uses the shared free model. Sign in to use granted credit on paid models."
                )
        user_id = auth_info.get("user_id", "")
        requested_model = (model or core._OPENROUTER_TRIAL_DEFAULT_MODEL).strip()
        model = requested_model
        # --- Model policy enforcement (admins may use any valid OpenRouter ID) ---
        from credit import (
            hosted_model_credit_allows,
            hosted_model_reservation_policy,
            is_hosted_model_allowed_for_identity,
        )
        if not is_hosted_model_allowed_for_identity(
            core,
            model,
            user_id=user_id,
            email=auth_info.get("email", ""),
        ):
            return reject_first_look(
                web.json_response(
                    {
                        "error": (
                            f"Model '{model}' is not available for this account. "
                            "Please select a model from the hosted model list."
                        )
                    },
                    status=400,
                ),
                "model_not_allowed",
            )
        reservation_policy = hosted_model_reservation_policy(
            core,
            requested_model,
            user_id=user_id,
            email=auth_info.get("email", ""),
        )
        if user_id:
            openrouter_budget_state = core._openrouter_budget_state_for_user(user_id)
            # --- Use credit balance as authority for paid model access ---
            # If the user has credit grants, they get paid models even when
            # the legacy openrouter_spend_usd counter is capped.
            credit_authority_ready = False
            credit_allows_requested_model = False
            try:
                from credit import CreditLedger
                credit_ledger = getattr(core, "_credit_ledger", None)
                if credit_ledger is None:
                    credit_ledger = await asyncio.to_thread(
                        CreditLedger, db_path=core._auth.db_path
                    )
                    core._credit_ledger = credit_ledger
                # Ensure trial grant from legacy budget (one-time migration)
                await asyncio.to_thread(
                    credit_ledger.ensure_trial_grant_from_openrouter_budget,
                    user_id,
                    current_spend_usd=openrouter_budget_state.get("spent_usd", 0),
                    budget_usd=openrouter_budget_state.get("budget_usd", 0),
                )
                acct = await asyncio.to_thread(credit_ledger.get_account, user_id)
                if acct:
                    credit_authority_ready = True
                    held = await asyncio.to_thread(
                        credit_ledger.held_reservation_total, acct["account_id"]
                    )
                    available = max(0, int(acct.get("balance_micro_usd", 0)) - held)
                    credit_allows_requested_model = hosted_model_credit_allows(
                        reservation_policy,
                        available,
                    )
            except Exception:
                pass
            # Once migrated, credit availability is authoritative. Legacy
            # float counters are retained only as a compatibility fallback if
            # the credit account could not be read.
            should_force_free_model = (
                (
                    (credit_authority_ready and not credit_allows_requested_model)
                    or (
                        not credit_authority_ready
                        and openrouter_budget_state.get("capped")
                    )
                )
                and not core._is_openrouter_post_cap_allowed_model(requested_model)
            )
            if should_force_free_model and reservation_policy["admin_custom"]:
                if credit_authority_ready:
                    return web.json_response(
                        {
                            "error": (
                                "No hosted credit remains for this custom model. "
                                "Add credit or select a free model."
                            ),
                            "code": "insufficient_hosted_credit",
                        },
                        status=402,
                    )
                return web.json_response(
                    {
                        "error": "Hosted credit is temporarily unavailable. Please retry shortly.",
                        "code": "hosted_credit_unavailable",
                    },
                    status=503,
                )
            if should_force_free_model:
                model = core._OPENROUTER_TRIAL_FALLBACK_MODEL
                openrouter_forced_model = model
                openrouter_forced_from_model = requested_model
                openrouter_forced_notice = (
                    "Available hosted credit is below this model's safety hold. "
                    "Switched to a free model so you can continue."
                )

        ws = core._chat_agents.get(core.TRIAL_AGENT_ID)
        if ws is None or ws.closed:
            return reject_first_look(
                web.json_response(
                    {"error": "Trial agent is not available. Please try a Claude model."},
                    status=503,
                ),
                "trial_agent_unavailable",
            )
    else:
        ws = core._chat_agents.get(agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Your agent is not connected. Download and run the agent package."},
                status=503,
            )

    if body.get("headless", False):
        if guest_mode:
            if guest_quota_count >= core._FIRST_LOOK_GUEST_PROMPT_LIMIT:
                quota_resp = web.json_response(
                    {
                        "error": "demo_quota_exceeded",
                        "demo_prompt_count": guest_quota_count,
                        "demo_prompt_limit": core._FIRST_LOOK_GUEST_PROMPT_LIMIT,
                    },
                    status=429,
                )
                core._attach_first_look_guest_cookies(
                    quota_resp, request, guest_id, quota_count=guest_quota_count,
                )
                return reject_first_look(quota_resp, "quota_exceeded")
            guest_quota_count += 1
            guest_quota_increment = True
        else:
            email = auth_info.get("email", "")
            if email:
                user = core._auth.find_user_by_email(email)
                if not core._is_demo_unlimited(user):
                    count = core._auth.get_demo_count(email)
                    if count >= core._DEMO_PROMPT_LIMIT:
                        return web.json_response(
                            {
                                "error": "demo_quota_exceeded",
                                "demo_prompt_count": count,
                                "demo_prompt_limit": core._DEMO_PROMPT_LIMIT,
                            },
                            status=429,
                        )
                    core._auth.increment_demo_count(email)

    email = auth_info.get("email", "")
    if email:
        user = core._auth.find_user_by_email(email)
        if core._is_rate_limited_user(user):
            result = core._auth.check_and_consume_turn(
                email,
                core._FREE_DAILY_TURN_LIMIT,
                core._FREE_WINDOW_TURN_LIMIT,
                core._FREE_WINDOW_SECONDS,
            )
            if not result["allowed"]:
                resp = {
                    "error": "turn_rate_limit",
                    "daily_remaining": result["daily_remaining"],
                    "window_remaining": result["window_remaining"],
                    "resets_in": result.get("resets_in", 0),
                }
                return web.json_response(resp, status=429)

    turn = None
    registry = _turn_registry(core)
    q: asyncio.Queue | None = None
    if registry:
        turn_auth_info = auth_info
        if guest_mode:
            turn_auth_info = {
                **auth_info,
                "user_id": f"guest:{guest_id}",
            }
        candidate = ChatTurnState(
            owner_user_id=str(turn_auth_info.get("user_id", "") or ""),
            owner_key_hash=str(auth_info.get("key_hash", "") or ""),
            session_id=session_id,
            req_id=req_id,
            chat_agent_id=chat_agent_id,
            routing_agent_id=core.TRIAL_AGENT_ID if is_openrouter else chat_agent_id,
        )
        turn, turn_created, turn_conflict, admission_limit = await _start_registered_turn(
            core,
            registry,
            candidate,
            turn_auth_info,
            hosted=bool(is_openrouter),
        )
        if admission_limit:
            return web.json_response(
                {
                    "error": "hosted_turn_capacity",
                    "scope": admission_limit,
                    "message": (
                        "Too many hosted turns are active for this account."
                        if admission_limit == "user"
                        else "Hosted inference is at capacity. Try again shortly."
                    ),
                },
                status=429,
                headers={"Retry-After": "5"},
            )
        if turn_conflict:
            return web.json_response(
                {
                    "error": "chat_turn_active",
                    "message": "A chat turn is already active for this session.",
                    "req_id": (
                        getattr(turn, "req_id", "")
                        if _turn_owned_by(turn, turn_auth_info)
                        else ""
                    ),
                },
                status=409,
            )
        if not turn_created:
            # A retry with the same browser-generated request ID attaches to
            # its existing journal instead of forwarding a duplicate prompt.
            return await _stream_turn_journal(request, turn)
        if scheduler_armed:
            scheduler_grant_id = core._mint_scheduler_turn_grant(
                auth_info.get("user_id", ""), session_id
            )
            turn.update_routing(scheduler_grant_id=scheduler_grant_id)
    else:
        # Guest and mock-core compatibility retains the old single queue path.
        if scheduler_armed and not scheduler_grant_id:
            scheduler_grant_id = core._mint_scheduler_turn_grant(
                auth_info.get("user_id", ""), session_id
            )
        q = asyncio.Queue(maxsize=8)
        old_q = core._response_queues.get(session_id)
        if old_q is not None:
            # Guest First Look needs correlated cancellation; legacy/mock
            # non-guest paths retain their historical done-only signal.
            _signal_superseded_response_queue(
                old_q,
                guest_mode=guest_mode,
                session_id=session_id,
                req_id=core._response_req_ids.get(session_id, ""),
            )
        core._response_queues[session_id] = q
        core._response_req_ids[session_id] = req_id

    use_headless = body.get("headless", False) and core.HEADLESS_AGENT_ID
    tab_id = core._session_tabs.get(session_id)
    remembered_profile_path = core._session_profile_paths.get(session_id, "")
    profile_intent, effective_profile_path = _resolve_profile_intent(
        body,
        tab_id,
        remembered_profile_path,
        session_id in getattr(core, "_expired_profile_sessions", {}),
    )
    if use_headless and profile_intent == "profile":
        if turn:
            _publish_turn_failure(core, turn, "Profile selection is not supported in headless mode.")
        _discard_response_registration(core, session_id)
        return reject_first_look(
            web.json_response(
                {"error": "Profile selection is not supported in headless mode."},
                status=400,
            ),
            "headless_profile_unsupported",
        )

    bridge_info = {}
    if not use_headless:
        try:
            bridge_info = await core._resolve_bridge_agent(
                auth_info,
                body.get("bridge_profile") if "bridge_profile" in body else None,
            )
        except Exception:
            if turn:
                _publish_turn_failure(core, turn, "Failed to resolve the browser bridge.")
            _discard_response_registration(core, session_id)
            return web.json_response({"error": "Failed to resolve the browser bridge."}, status=502)
    cdp_agent_id = core.HEADLESS_AGENT_ID if use_headless else (bridge_info.get("bridge_agent_id") or agent_id)
    remembered_cdp_agent_id = core._session_agent_map.get(session_id, "")
    if (
        profile_intent == "profile"
        and not profile_selection_present
        and remembered_cdp_agent_id
    ):
        # Omitted profile intent must recover on the bridge that owns the
        # remembered local path, never whichever bridge is currently default.
        cdp_agent_id = remembered_cdp_agent_id

    if profile_intent == "profile":
        if profile_selection_present:
            try:
                allowed_paths = await _allowed_profile_paths(core, cdp_agent_id)
            except Exception:
                if turn:
                    _publish_turn_failure(core, turn, "Failed to validate the selected profile.")
                _discard_response_registration(core, session_id)
                return web.json_response({"error": "Failed to validate the selected profile."}, status=502)
        else:
            allowed_paths = {effective_profile_path}
        if effective_profile_path not in allowed_paths:
            if turn:
                _publish_turn_failure(core, turn, "Selected profile is invalid or unavailable.")
            _discard_response_registration(core, session_id)
            return web.json_response({"error": "Selected profile is invalid or unavailable."}, status=403)
        try:
            tab_id = await _ensure_profile_tab(
                core,
                session_id,
                cdp_agent_id,
                effective_profile_path,
            )
        except Exception as e:
            _discard_response_registration(core, session_id)
            message = (
                "Failed to launch selected profile"
                if profile_selection_present
                else "Failed to restore selected profile"
            )
            if turn:
                _publish_turn_failure(core, turn, f"{message}: {e}")
            return web.json_response({"error": f"{message}: {e}"}, status=502)
    elif profile_intent == "default":
        try:
            await _clear_profile_tab(core, session_id)
        except Exception:
            if turn:
                _publish_turn_failure(core, turn, "Failed to leave the selected browser profile.")
            _discard_response_registration(core, session_id)
            return reject_first_look(
                web.json_response(
                    {"error": "Failed to leave the selected browser profile."},
                    status=502,
                ),
                "profile_clear_failed",
            )
        tab_id = None
    elif profile_intent == "expired":
        if turn:
            _publish_turn_failure(
                core,
                turn,
                "Browser profile session expired. Select a Chrome profile and try again.",
            )
        _discard_response_registration(core, session_id)
        return reject_first_look(
            web.json_response(
                {
                    "error": "Browser profile session expired. Select a Chrome profile and try again.",
                    "code": "profile_session_expired",
                },
                status=409,
            ),
            "profile_session_expired",
        )

    routing_agent_id = core.TRIAL_AGENT_ID if is_openrouter else chat_agent_id

    # --- Credit run creation (at dispatch point, after all pre-flight checks) ---
    billing_user_id = str(auth_info.get("user_id", "") or "")
    if guest_mode:
        billing_user_id = "system:first-look"
    if is_openrouter and billing_user_id and not billing_run_id:
        user_id = billing_user_id
        try:
            from credit import CreditLedger
            credit_ledger = getattr(core, "_credit_ledger", None)
            if credit_ledger is None:
                credit_ledger = await asyncio.to_thread(
                    CreditLedger, db_path=core._auth.db_path
                )
                core._credit_ledger = credit_ledger
            billing_run = await asyncio.to_thread(
                credit_ledger.create_run,
                user_id,
                model=model,
                idempotency_key=f"chat-turn-{req_id}",
            )
            billing_run_id = billing_run.get("run_id", "")
            if billing_run_id:
                billing_runs = getattr(core, "_session_billing_runs", None)
                if billing_runs is None:
                    billing_runs = {}
                    core._session_billing_runs = billing_runs
                billing_runs[session_id] = billing_run_id
        except Exception as e:
            # Ledger/grant failure for authenticated user is terminal —
            # do not silently dispatch without a billing run.
            log.warning("[chat] credit run creation failed for user %s: %s", user_id, e)
            if turn:
                _publish_turn_failure(core, turn, "Credit system unavailable")
            _discard_response_registration(core, session_id)
            return web.json_response(
                {"error": "Credit system unavailable. Please try again shortly."},
                status=503,
            )

    try:
        ws_msg = {
            "type": "user_message",
            "session_id": session_id,
            "agent_id": cdp_agent_id,
            "message": message,
            "req_id": req_id,
        }
        if slot is not None:
            ws_msg["slot"] = slot
        if scheduler_armed:
            ws_msg["scheduler_armed"] = True
            ws_msg["scheduler_grant_id"] = scheduler_grant_id
        if model:
            ws_msg["model"] = model
        if is_openrouter and auth_info.get("user_id"):
            ws_msg["user_id"] = auth_info["user_id"]
        if billing_run_id:
            ws_msg["billing_run_id"] = billing_run_id
        if is_gemini:
            core._gemini_last_active[chat_agent_id] = time.time()
        elif is_claude_sdk:
            core._claude_sdk_last_active[chat_agent_id] = time.time()
        elif is_codex_sdk:
            core._codex_sdk_last_active[chat_agent_id] = time.time()
        elif is_codex_cli:
            core._session_last_active[session_id] = time.time()
        elif is_opencode_cli:
            core._session_last_active[session_id] = time.time()
        if not tab_id and use_headless:
            tab_id = await core._ensure_session_tab(session_id, cdp_agent_id)
        if tab_id:
            ws_msg["tab_id"] = tab_id
        if cdp_agent_id:
            core._session_agent_map[session_id] = cdp_agent_id
        core._session_last_active[session_id] = time.time()
        if turn:
            turn.update_routing(
                chat_agent_id=chat_agent_id,
                routing_agent_id=routing_agent_id,
                dispatch_ws=ws,
                cdp_agent_id=cdp_agent_id,
                tab_id=tab_id or "",
            )
        # Record routing before forwarding so a concurrent reconnect or agent
        # disconnect can resolve this exact turn without a response queue.
        core._session_agents[session_id] = routing_agent_id
        await ws.send_json(ws_msg)
        if turn and is_openrouter:
            _hosted_turn_deadline_task(core, turn, _HOSTED_TURN_DEADLINE_S)
        if guest_mode:
            first_look_accepted = True
            first_look_accepted_at = time.monotonic()
            _track_first_look_run_event(
                core,
                request,
                "first_look_run_accepted",
                run_id=first_look_run_id,
                attribution=first_look_attribution,
                status_code=200,
            )
        core._trace(
            "chat.msg.forwarded",
            req_id=req_id,
            session_id=session_id,
            chat_agent_id=chat_agent_id,
            cdp_agent_id=cdp_agent_id,
        )
    except Exception:
        if turn:
            _publish_turn_failure(core, turn, "Failed to reach chat agent")
        elif scheduler_grant_id:
            core._scheduler_turn_grants.pop(scheduler_grant_id, None)
        core._trace(
            "chat.msg.forward_error",
            req_id=req_id,
            session_id=session_id,
            chat_agent_id=chat_agent_id,
        )
        _discard_response_registration(core, session_id)
        dispatch_error = web.json_response(
            {"error": "Failed to reach chat agent"}, status=502
        )
        if guest_mode and first_look_accepted:
            record_first_look_terminal("error", error_code="server_post_dispatch_error")
            return dispatch_error
        return reject_first_look(dispatch_error, "agent_dispatch_failed")

    core._session_agents[session_id] = routing_agent_id

    # --- Inject overlay copilot into the task browser ---
    overlay_tab = tab_id or "auto"
    if not guest_mode and not is_openrouter:
        _inject_overlay(core, session_id, cdp_agent_id, overlay_tab, message,
                        user_id=auth_info.get("user_id", ""), model=model, slot=slot)

    if turn:
        if openrouter_forced_model:
            forced_evt = {
                "type": "model_forced",
                "reason": "openrouter_budget_limit",
                "model": openrouter_forced_model,
                "allowed_models": list(core._OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS),
            }
            if openrouter_forced_from_model:
                forced_evt["requested_model"] = openrouter_forced_from_model
            if openrouter_budget_state:
                forced_evt["budget"] = openrouter_budget_state
            _publish_turn_event(core, turn, forced_evt)
        if openrouter_forced_notice:
            _publish_turn_event(
                core,
                turn,
                {"type": "text", "data": openrouter_forced_notice},
            )
        if is_opencode_cli:
            _turn_timeout_task(core, turn, _OPENCODE_CLI_SILENCE_TIMEOUT_S)
        elif is_codex_cli:
            _turn_timeout_task(core, turn, _CODEX_CLI_SILENCE_TIMEOUT_S)
        stream_kwargs = {}
        if guest_mode:
            def prepare_guest_response(response):
                core._attach_first_look_guest_cookies(
                    response,
                    request,
                    guest_id,
                    quota_count=(
                        guest_quota_count if guest_quota_increment else None
                    ),
                )

            def observe_guest_event(event):
                outcome = _first_look_terminal_outcome(event, req_id)
                if outcome:
                    record_first_look_terminal(
                        outcome,
                        error_code="agent_error" if outcome == "error" else "",
                    )

            def observe_guest_disconnect():
                record_first_look_terminal(
                    "client_disconnected", error_code="client_disconnected"
                )

            stream_kwargs = {
                "prepare_response": prepare_guest_response,
                "observe_event": observe_guest_event,
                "observe_disconnect": observe_guest_disconnect,
            }
        return await _stream_turn_journal(request, turn, **stream_kwargs)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    if guest_mode:
        core._attach_first_look_guest_cookies(
            resp,
            request,
            guest_id,
            quota_count=guest_quota_count if guest_quota_increment else None,
        )
    stream_completed = False
    last_stream_event_at = time.time()
    first_look_last_agent_event_at = time.monotonic()
    local_cli_silence_timeout_s = 0
    if is_opencode_cli:
        local_cli_silence_timeout_s = _OPENCODE_CLI_SILENCE_TIMEOUT_S
    elif is_codex_cli:
        local_cli_silence_timeout_s = _CODEX_CLI_SILENCE_TIMEOUT_S
    try:
        await resp.prepare(request)
        if openrouter_forced_model:
            forced_evt = {
                "type": "model_forced",
                "reason": "openrouter_budget_limit",
                "model": openrouter_forced_model,
                "allowed_models": list(core._OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS),
            }
            if openrouter_forced_from_model:
                forced_evt["requested_model"] = openrouter_forced_from_model
            if openrouter_budget_state:
                forced_evt["budget"] = openrouter_budget_state
            await resp.write(f"data: {json.dumps(forced_evt)}\n\n".encode())
        if openrouter_forced_notice:
            await resp.write(
                f"data: {json.dumps({'type': 'text', 'data': openrouter_forced_notice})}\n\n".encode()
            )

        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                first_look_failure_code = ""
                if guest_mode:
                    current_ws = core._chat_agents.get(routing_agent_id)
                    if current_ws is not ws or getattr(ws, "closed", False):
                        first_look_failure_code = "agent_disconnected"
                    elif (
                        time.monotonic() - first_look_last_agent_event_at
                        >= _FIRST_LOOK_AGENT_SILENCE_TIMEOUT_S
                    ):
                        first_look_failure_code = "agent_timeout"

                if first_look_failure_code:
                    record_first_look_terminal(
                        "error", error_code=first_look_failure_code
                    )
                    evt = {
                        "type": "error",
                        "data": (
                            "The shared browser agent disconnected. Please retry."
                            if first_look_failure_code == "agent_disconnected"
                            else "The shared browser run timed out. Please retry."
                        ),
                        "session_id": session_id,
                        "req_id": req_id,
                    }
                elif not use_headless and not is_openrouter and not guest_mode:
                    current_ws = core._chat_agents.get(chat_agent_id)
                    if current_ws is not ws or getattr(ws, "closed", False):
                        evt = {
                            "type": "error",
                            "data": "Local agent disconnected before completing this response. Please retry after the client reconnects.",
                            "session_id": session_id,
                        }
                        if req_id:
                            evt["req_id"] = req_id
                        await resp.write(f"data: {json.dumps(evt)}\n\n".encode())
                        done_evt = {"type": "done", "session_id": session_id}
                        if req_id:
                            done_evt["req_id"] = req_id
                        await resp.write(f"data: {json.dumps(done_evt)}\n\n".encode())
                        stream_completed = True
                        break
                if first_look_failure_code:
                    pass
                elif local_cli_silence_timeout_s and time.time() - last_stream_event_at >= local_cli_silence_timeout_s:
                    evt = {
                        "type": "error",
                        "data": "Local CLI did not return a response in time. The provider may be rate-limited or stalled; please retry or switch models.",
                        "session_id": session_id,
                    }
                    if req_id:
                        evt["req_id"] = req_id
                    await resp.write(f"data: {json.dumps(evt)}\n\n".encode())
                    done_evt = {"type": "done", "session_id": session_id}
                    if req_id:
                        done_evt["req_id"] = req_id
                    await resp.write(f"data: {json.dumps(done_evt)}\n\n".encode())
                    stream_completed = True
                    break
                else:
                    try:
                        await resp.write(b": keepalive\n\n")
                    except (ConnectionResetError, BrokenPipeError):
                        record_first_look_terminal(
                            "client_disconnected", error_code="client_disconnected"
                        )
                        break
                    except Exception:
                        record_first_look_terminal("error", error_code="server_stream_error")
                        break
                    continue

            last_stream_event_at = time.time()
            if guest_mode:
                first_look_last_agent_event_at = time.monotonic()

            event_type = str(evt.get("type", "") or "")
            event_outcome = _first_look_terminal_outcome(evt, req_id)
            if guest_mode and event_type in _FIRST_LOOK_TERMINAL_EVENT_TYPES:
                # Stale and uncorrelated terminal events must neither end this
                # run nor reach its client stream.
                if not event_outcome:
                    continue
                record_first_look_terminal(
                    event_outcome,
                    error_code="agent_error" if event_outcome == "error" else "",
                )

            sse = f"data: {json.dumps(evt)}\n\n"
            try:
                await resp.write(sse.encode())
            except (ConnectionResetError, BrokenPipeError):
                record_first_look_terminal(
                    "client_disconnected", error_code="client_disconnected"
                )
                break
            except Exception:
                record_first_look_terminal("error", error_code="server_stream_error")
                break

            # Broadcast to overlay copilot subscribers
            _broadcast_overlay(session_id, evt)

            if evt.get("type") == "error":
                _finish_credit_run(core, session_id, status="cancelled")
                done_evt = {"type": "done", "session_id": session_id}
                if req_id:
                    done_evt["req_id"] = req_id
                try:
                    await resp.write(f"data: {json.dumps(done_evt)}\n\n".encode())
                except (ConnectionResetError, BrokenPipeError):
                    pass
                except Exception:
                    pass
                stream_completed = True
                break

            if evt.get("type") == "done":
                _finish_credit_run(core, session_id, status="completed")
                stream_completed = True
                break
    except (ConnectionResetError, BrokenPipeError):
        record_first_look_terminal(
            "client_disconnected", error_code="client_disconnected"
        )
    except asyncio.CancelledError:
        record_first_look_terminal(
            "client_disconnected", error_code="handler_cancelled"
        )
        raise
    except Exception:
        record_first_look_terminal("error", error_code="server_stream_error")
        raise
    finally:
        # Only clean up if we still own the queue (a new turn may have replaced it)
        if core._response_queues.get(session_id) is q:
            core._response_queues.pop(session_id, None)
            core._response_req_ids.pop(session_id, None)
            # Keep session_agents alive if overlay is active
            overlay = core._overlay_sessions.get(session_id)
            has_overlay = overlay and overlay.injected
            if not has_overlay:
                core._session_agents.pop(session_id, None)
        # A disconnected SSE client is not a turn cancellation. Preserve the
        # overlay/tab and leave a scheduler grant valid until the legacy turn
        # reaches its terminal stream marker.
        if scheduler_grant_id and stream_completed:
            core._scheduler_turn_grants.pop(scheduler_grant_id, None)
        core._trace(
            "chat.msg.stream_end",
            req_id=req_id,
            session_id=session_id,
            stream_completed=stream_completed,
        )

    return resp


async def handle_chat_active(request: web.Request) -> web.Response:
    """GET /web/chat/active — return sequenced events and current action state."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    session_id = str(request.query.get("session_id", "") or "").strip()
    registry = _turn_registry(core)
    turn = _registry_turn(registry, session_id) if session_id else None
    if (
        not turn
        or not _turn_owned_by(turn, auth_info)
        or getattr(turn, "status", "") not in {"active", "cancelling"}
    ):
        return web.json_response({"active": False}, status=404)
    return web.json_response(
        turn.snapshot(include_events=True),
        headers={"Cache-Control": "no-store"},
    )


async def handle_chat_events(request: web.Request) -> web.StreamResponse:
    """GET /web/chat/events — replay and follow an SSE event-stream journal."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    session_id = str(request.query.get("session_id", "") or "").strip()
    req_id = str(request.query.get("req_id", "") or "").strip()
    after = _parse_after_sequence(request)
    if not session_id or not req_id or after is None:
        return web.json_response({"error": "session_id, req_id, and valid after are required"}, status=400)
    registry = _turn_registry(core)
    turn = _registry_turn(registry, session_id, req_id)
    if (
        not turn
        or not _turn_owned_by(turn, auth_info)
        or str(getattr(turn, "req_id", "")) != req_id
    ):
        return web.json_response({"active": False}, status=404)
    return await _stream_turn_journal(request, turn, after=after)


async def handle_chat_cancel(request: web.Request) -> web.Response:
    """POST /web/chat/cancel — cancel an active chat session."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        body = {}
    wants_guest_first_look = bool(body.get("first_look_guest"))
    auth_info = None if wants_guest_first_look else core._authenticate(request)
    guest_mode = False
    guest_id = ""
    if auth_info is None:
        if not wants_guest_first_look:
            return web.json_response({"error": "Not authenticated"}, status=401)
        auth_info, guest_id, _ = core._first_look_guest_auth(request)
        guest_mode = True
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)

    agent_id = auth_info.get("agent_id", "")
    if guest_mode and not session_id.startswith(f"s-{agent_id}-"):
        denied = web.json_response({"error": "session_id not owned by guest"}, status=403)
        core._attach_first_look_guest_cookies(denied, request, guest_id)
        return denied

    registry = _turn_registry(core)
    turn = _registry_turn(registry, session_id)
    if turn:
        turn_auth_info = auth_info
        if guest_mode:
            turn_auth_info = {**auth_info, "user_id": f"guest:{guest_id}"}
        if not _turn_owned_by(turn, turn_auth_info):
            if guest_mode:
                denied = web.json_response(
                    {"error": "session_id not owned by guest"}, status=403
                )
                core._attach_first_look_guest_cookies(denied, request, guest_id)
                return denied
            else:
                return web.json_response({"error": "session_id not owned by user"}, status=403)
        requested_req_id = str(body.get("req_id", "") or "").strip()
        if requested_req_id and requested_req_id != turn.req_id:
            return web.json_response(
                {
                    "error": "chat_turn_mismatch",
                    "message": "The requested turn is not active for this session.",
                    "req_id": turn.req_id,
                },
                status=409,
            )
        if getattr(turn, "stream_finished", False):
            response = web.json_response(
                {"ok": True, "status": turn.status, "req_id": turn.req_id}
            )
            if guest_mode:
                core._attach_first_look_guest_cookies(response, request, guest_id)
            return response

        turn.mark_cancelling()
        routing_agent_id = turn.routing_agent_id or core._session_agents.get(
            session_id, agent_id
        )
        ws = core._chat_agents.get(routing_agent_id)
        if ws and not ws.closed:
            try:
                await ws.send_json(
                    {"type": "cancel", "session_id": session_id, "req_id": turn.req_id}
                )
            except Exception:
                # The canonical cancellation below still releases the turn
                # for reconnecting tabs when an agent has just vanished.
                pass
        _publish_turn_event(
            core,
            turn,
            {"type": "cancelled", "session_id": session_id, "req_id": turn.req_id},
        )
        response = web.json_response(
            {"ok": True, "status": turn.status, "req_id": turn.req_id}
        )
        if guest_mode:
            core._attach_first_look_guest_cookies(response, request, guest_id)
        return response

    default_agent = core.TRIAL_AGENT_ID if guest_mode else agent_id
    routing_agent_id = core._session_agents.get(session_id, default_agent)

    # Capture queue ref before awaiting WS send (queue may be replaced by a new turn)
    cancel_q = core._response_queues.get(session_id)
    cancel_rid = core._response_req_ids.get(session_id, "")

    ws = core._chat_agents.get(routing_agent_id)
    if ws and not ws.closed:
        await ws.send_json({"type": "cancel", "session_id": session_id})

    # Inject cancelled+done into the SSE queue so the client sees a clean end
    if cancel_q is not None:
        cancelled_evt = {"type": "cancelled", "session_id": session_id}
        done_evt = {"type": "done", "session_id": session_id}
        if cancel_rid:
            cancelled_evt["req_id"] = cancel_rid
            done_evt["req_id"] = cancel_rid
        await cancel_q.put(cancelled_evt)
        await cancel_q.put(done_evt)
    # Revoke scheduler grants regardless of queue state — cancel must
    # always invalidate any active grant for this session.
    _revoke_session_scheduler_grants(core, session_id)
    ok = web.json_response({"ok": True})
    if guest_mode:
        core._attach_first_look_guest_cookies(ok, request, guest_id)
    return ok

    if ws and not ws.closed:
        ok = web.json_response({"ok": True})
        if guest_mode:
            core._attach_first_look_guest_cookies(ok, request, guest_id)
        return ok
    err = web.json_response({"error": "Agent not connected"}, status=503)
    if guest_mode:
        core._attach_first_look_guest_cookies(err, request, guest_id)
    return err
