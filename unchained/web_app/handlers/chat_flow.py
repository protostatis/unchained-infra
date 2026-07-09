"""Chat status/history/slot handlers extracted from web.py."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from urllib.parse import urlsplit

import httpx
from aiohttp import web

from challenge_detection import detect_challenge
from domain_policy import execution_policy_for_url
from rate_limit import SlidingWindowRateLimiter

from web_app.core import get_core as _core

log = logging.getLogger(__name__)


# Chrome headless launch typically takes 2-5s; 10s gives comfortable margin.
_FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT = 10.0

def _parse_version_tuple(value: str) -> tuple[int, int, int]:
    """Parse a semver-like string into a comparable tuple."""
    try:
        parts = tuple(int(part) for part in str(value or "").strip().split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)
    if len(parts) != 3:
        return (0, 0, 0)
    return parts


def _client_version_status(caps: dict | None) -> dict:
    """Summarize the connected local client package version from auth caps."""
    from agent_package import MIN_VERSION, VERSION

    caps = caps or {}
    local_version = str(caps.get("client_version", "") or "").strip()
    local_t = _parse_version_tuple(local_version) if local_version else (0, 0, 0)
    server_t = _parse_version_tuple(VERSION)
    min_t = _parse_version_tuple(MIN_VERSION)
    return {
        "client_version": local_version,
        "server_version": VERSION,
        "min_client_version": MIN_VERSION,
        "client_update_supported": bool(caps.get("remote_update")),
        "client_outdated": bool(local_version) and local_t < server_t,
        "client_update_required": bool(local_version) and local_t < min_t,
    }


def _normalize_chat_slot(value) -> int | None:
    """Return a valid chat slot number (1-3), or None if absent/invalid."""
    try:
        slot = int(value)
    except (TypeError, ValueError):
        return None
    return slot if slot in (1, 2, 3) else None


_RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION = "0.3.65"
_FIRST_LOOK_SIGNAL_URL_MAX = 500
_FIRST_LOOK_SIGNAL_TEXT_MAX = 6000
_FIRST_LOOK_PREVIEW_WIDTH_DEFAULT = 960
_FIRST_LOOK_PREVIEW_HEIGHT_DEFAULT = 640
_FIRST_LOOK_PUBLIC_RATE_WINDOW_S = 60.0
_FIRST_LOOK_PREFLIGHT_LIMIT = 30
_FIRST_LOOK_SIGNAL_LIMIT = 20
_PUBLIC_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_FIRST_LOOK_PUBLIC_RATE_LIMITER = SlidingWindowRateLimiter()

# --- First-look preview FSM tunables ---
# Protocol version emitted in every event's `v` field.
_FIRST_LOOK_PREVIEW_PROTOCOL_VERSION = 1
# Idle deadline passed to private-core's stream_screencast. Private-core raises
# TimeoutError after this many seconds of no frames, which we catch and handle
# transparently by reconnecting the underlying stream.
_FIRST_LOOK_PREVIEW_STREAM_IDLE_TIMEOUT_S = 120.0
# How many transparent reconnects (on stream_timeout / max_frames / internal
# fault) we allow per client WS before giving up and emitting preview.ended.
# 5 * 120s = up to 10 minutes of logical stream per WS, which comfortably
# covers any guest run.
_FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS = 5
# Backoff between transparent reconnects. Keep small so the preview feels
# continuous; private-core's reattach is cheap.
_FIRST_LOOK_PREVIEW_RECONNECT_BACKOFF_S = 0.5


def _research_desk_install_requires_update(caps: dict | None) -> dict:
    """Return whether the connected client is new enough for bootstrap install."""
    caps = caps or {}
    local_version = str(caps.get("client_version", "") or "").strip()
    local_t = _parse_version_tuple(local_version) if local_version else (0, 0, 0)
    required_t = _parse_version_tuple(_RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION)
    return {
        "client_version": local_version,
        "required_client_version": _RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION,
        "update_supported": bool(caps.get("remote_update")),
        "update_required": (not local_version) or local_t < required_t,
    }


def _normalize_first_look_public_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > _FIRST_LOOK_SIGNAL_URL_MAX:
        raise ValueError("url too long")
    if "://" not in text and _PUBLIC_HOST_RE.match(text):
        text = "https://" + text
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.hostname:
        return ""
    return parsed.geturl()


def _clip_signal_text(value: str, limit: int = _FIRST_LOOK_SIGNAL_TEXT_MAX) -> str:
    return str(value or "").strip()[:limit]


def _request_source_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        source = forwarded.split(",")[0].strip()
        if source:
            return source
    return (request.remote or "unknown").strip() or "unknown"


def _check_first_look_public_rate_limit(
    request: web.Request,
    *,
    bucket: str,
    limit: int,
) -> web.Response | None:
    allowed, retry_after = _FIRST_LOOK_PUBLIC_RATE_LIMITER.allow(
        f"{bucket}:{_request_source_ip(request)}",
        limit,
        _FIRST_LOOK_PUBLIC_RATE_WINDOW_S,
    )
    if allowed:
        return None
    return web.json_response(
        {"error": "Rate limit exceeded", "retry_after": retry_after},
        status=429,
        headers={"Retry-After": str(retry_after)},
    )


def _normalize_first_look_preview_dimension(
    params,
    name: str,
    default: int,
    *,
    min_value: int = 320,
    max_value: int = 1920,
) -> int:
    raw = str(params.get(name, default) or default).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


async def _resolve_first_look_preview_target(
    core,
    guest_auth: dict,
    session_id: str,
    *,
    timeout: float | None = None,
    # 300ms balances responsiveness with CPU overhead during polling.
    poll_interval: float = 0.3,
) -> tuple[str, str]:
    if timeout is None:
        timeout = _FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT
    sid = str(session_id or "").strip()
    if not sid:
        raise web.HTTPBadRequest(text="session_id required")

    # --- Ownership check ---
    # The session_id is formatted as "s-{agent_id}-{suffix}".  Accept the
    # request when EITHER the guest cookie identity owns the session OR the
    # session already exists in _session_tabs (meaning it was created through
    # an authenticated code path such as /web/chat).
    guest_agent = guest_auth.get("agent_id", "")
    prefix = f"s-{guest_agent}-"

    if not core.HEADLESS_AGENT_ID:
        raise web.HTTPServiceUnavailable(text="Shared browser is not configured")
    # The tab may not be provisioned yet (headless Chrome launch takes seconds).
    # Poll until the session appears in _session_tabs.  The ownership check
    # is deferred into the poll loop because the session entry is created
    # asynchronously — rejecting before it exists causes premature 403s that
    # force slow client-side retries.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        tab_id = str(core._session_tabs.get(sid, "") or "").strip()
        if tab_id:
            return core.HEADLESS_AGENT_ID, tab_id
        # If we recognise the session by prefix, keep waiting.
        # If it's an unknown prefix AND not yet in _session_tabs,
        # only reject once we've given it a chance to appear.
        if not sid.startswith(prefix) and loop.time() >= deadline:
            raise web.HTTPForbidden(text="session_id not owned by guest")
        if loop.time() >= deadline:
            raise web.HTTPNotFound(text="No live preview available for this session yet")
        await asyncio.sleep(poll_interval)


async def handle_first_look_preflight(request: web.Request) -> web.Response:
    """GET /web/first-look/preflight — classify a public target before a guest run."""
    limited = _check_first_look_public_rate_limit(
        request,
        bucket="first-look-preflight",
        limit=_FIRST_LOOK_PREFLIGHT_LIMIT,
    )
    if limited is not None:
        return limited
    try:
        url = _normalize_first_look_public_url(request.query.get("url", ""))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not url:
        return web.json_response({"ok": True, "url": "", "policy": None})
    policy = execution_policy_for_url(url)
    return web.json_response({"ok": True, "url": url, "policy": policy.to_dict()})


async def handle_first_look_signal(request: web.Request) -> web.Response:
    """POST /web/first-look/signal — classify run text for challenge hints."""
    limited = _check_first_look_public_rate_limit(
        request,
        bucket="first-look-signal",
        limit=_FIRST_LOOK_SIGNAL_LIMIT,
    )
    if limited is not None:
        return limited
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    try:
        url = _normalize_first_look_public_url(body.get("url", ""))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    text = _clip_signal_text(body.get("text", ""))
    title = _clip_signal_text(body.get("title", ""), limit=256)
    html = _clip_signal_text(body.get("html", ""), limit=6000)
    challenge = detect_challenge(page_text=text, title=title, url=url, html=html)
    payload = {"ok": True, "challenge": challenge.to_dict()}
    if url:
        payload["policy"] = execution_policy_for_url(url).to_dict()
    else:
        payload["policy"] = None
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# First-look preview state machine (server-owned)
# ---------------------------------------------------------------------------
#
# The guest's browser opens a single WebSocket to
# ``/web/first-look/preview/ws`` for the duration of one run. The server
# translates private-core's internal screencast events into a small, explicit
# protocol so the client can stay a dumb renderer.
#
# Event schema (all events carry ``v: 1``):
#
#   {v:1, type:"preview.attached", tab_id, width, height}
#     Sent once, immediately after the WS is accepted and the session tab
#     has been resolved. Tells the client "the server is about to start
#     streaming; clear any stale state and show the live frame area."
#
#   {v:1, type:"preview.frame", mime, data, seq}
#     A paintable frame. ``data`` is a base64-encoded image of the declared
#     mime type. ``seq`` is a monotonic counter scoped to this WS.
#
#   {v:1, type:"preview.reconnecting", attempt}
#     The underlying private-core screencast hit its idle deadline (normal on
#     static pages) or reached its per-connection frame cap. The server is
#     transparently rebuilding it. The client WS is still alive; the client
#     should keep the last-painted frame and optionally show a subtle
#     "reconnecting" indicator. No action required from the client.
#
#   {v:1, type:"preview.ended", reason, retriable, frame_count}
#     Transport-terminal. The WS will close immediately after this event is
#     emitted. ``reason`` is one of:
#         "slow_client"    — backpressure; the client couldn't keep up
#         "max_reconnects" — server gave up after N transparent reconnects
#         "fatal"          — unhandled exception in the underlying stream
#     ``retriable`` tells the client whether it's worth opening a fresh WS
#     if the run is still active. "max_reconnects" is not retriable;
#     "slow_client" and "fatal" are (one-shot retry from the UI).
#
# Design notes:
# - The preview WS owns transport state only. It MUST NOT infer run completion
#   from screencast EOF. Actual run completion comes from the chat SSE `done`
#   event, not this websocket.
# - There is no ``preview.idle`` application-level heartbeat. aiohttp's
#   WebSocketResponse(heartbeat=30) keeps the transport alive; the
#   transparent-reconnect loop below keeps the logical stream alive.
# - Unknown or future event fields are ignored silently by clients; the
#   ``v`` discriminator lets us add breaking changes in a future protocol
#   bump without tripping old clients.
# - This handler is the ONLY component that speaks the public protocol.
#   private-core still speaks its legacy ``frame``/``status`` shape and we
#   translate inside this function. Callers of ``cloud_tools.stream_screencast``
#   elsewhere are unaffected.


async def handle_first_look_preview_ws(request: web.Request) -> web.StreamResponse:
    """GET /web/first-look/preview/ws — server-owned preview state machine.

    Forwards private-core screencast frames to the guest browser as explicit
    protocol events (see the comment block above). The server transparently
    reconnects the underlying screencast when its idle deadline fires, so a
    single client WS represents one *logical* stream for the lifetime of the
    user's run regardless of how many physical reconnects happen inside
    private-core.

    This replaces the previous pass-through handler that leaked private-core's
    ``frame``/``status`` events to the client and relied on the client to
    reconnect on idle-timeout — a gate that never fired once the first frame
    had been painted, causing the preview to freeze after the initial
    navigate on any static page.
    """
    core = _core()
    sid_param = request.query.get("session_id", "")
    log.debug("request received sid=%r tabs=%s", sid_param, dict(core._session_tabs))
    guest_auth, guest_id, _ = core._first_look_guest_auth(request)
    try:
        agent_id, tab_id = await _resolve_first_look_preview_target(
            core,
            guest_auth,
            sid_param,
        )
    except web.HTTPException as exc:
        log.warning("resolve failed: %s %s", exc.status, exc.text)
        denied = web.Response(status=exc.status, text=exc.text)
        core._attach_first_look_guest_cookies(denied, request, guest_id)
        return denied

    # Allow explicit tab_id override for multi-tab auto-follow (ddm --new).
    #
    # Format check: 32-64 char hex (Chrome targetIds are 32 uppercase hex
    # chars). Tightened from {8,64} so a guest can't brute-force 8-char
    # prefixes — at 32 chars the search space is 128 bits which is not
    # economically feasible to scan over WS handshakes.
    #
    # Ownership check: NONE for now. The agent's `ddm --new` flow opens a
    # fresh Chrome tab and emits its targetId in `tool_result.new_tab_id`,
    # but the server-side `core._session_tabs` map is never updated to
    # include that new tab — only the *initial* session tab from
    # `create_session_tab()` lives in `_session_tabs`. So an ownership
    # check that requires `raw_tab in _session_tabs.values()` would 403
    # every legitimate followTab() after a `ddm --new`, breaking
    # multi-tab guest runs entirely.
    #
    # The cross-guest-tab-watch concern (Guest B passes Guest A's known
    # targetId) is mitigated by the 32-char hex entropy: 128 bits is
    # impossible to guess, and the targetId only ever appears in private
    # logs and the guest's own session payloads. Closing this properly
    # requires a per-guest tab tracker (a future PR — see follow-up task
    # #N: "headless agent watchdog + per-guest tab tracking").
    #
    # TODO(unchained-infra#TBD): add per-guest tab tracking + apply a
    # strict ownership check here once the tracker exists.
    raw_tab = request.query.get("tab_id", "")
    if raw_tab:
        if not re.fullmatch(r"[A-Fa-f0-9]{32,64}", raw_tab):
            return web.Response(status=400, text="invalid tab_id format")
        tab_id = raw_tab
    log.debug("resolved agent=%s tab=%s explicit=%s", agent_id, tab_id, bool(raw_tab))

    width = _normalize_first_look_preview_dimension(
        request.query,
        "width",
        _FIRST_LOOK_PREVIEW_WIDTH_DEFAULT,
    )
    height = _normalize_first_look_preview_dimension(
        request.query,
        "height",
        _FIRST_LOOK_PREVIEW_HEIGHT_DEFAULT,
        min_value=240,
    )

    import cloud_tools

    relay_host, relay_port = core._parse_relay()
    ws = web.WebSocketResponse(heartbeat=30)
    core._attach_first_look_guest_cookies(ws, request, guest_id)
    await ws.prepare(request)
    # print() so the line is visible in docker compose logs regardless of
    # the root logger level — the web container runs without a logging
    # handler configured, so log.info goes nowhere.
    print(
        f"[preview-fsm] connected sid={sid_param} "
        f"agent={agent_id} tab={tab_id[:12]}",
        flush=True,
    )

    frame_seq = 0
    reconnect_attempt = 0

    async def emit(event: dict) -> bool:
        """Send one protocol event. Returns False if the WS is no longer writable.

        We swallow ALL connection-gone errors (ConnectionResetError,
        ConnectionAbortedError, BrokenPipeError) and return False so the
        FSM loop's `if not alive: break` path drives a clean teardown.
        Re-raising would propagate out through the handler's `finally`
        and produce a noisy aiohttp traceback in prod logs every time a
        client closes mid-frame, which is the common case.

        We DO re-raise asyncio.CancelledError because that's a cooperative
        cancellation signal from upstream (server shutdown, request
        cleanup) that has to propagate.

        Any other exception (TypeError on a bad payload, etc.) is logged
        once and treated as a closed-WS so the loop unwinds — these
        indicate a real bug that we want visible without crashing the
        request.
        """
        if ws.closed:
            return False
        event.setdefault("v", _FIRST_LOOK_PREVIEW_PROTOCOL_VERSION)
        try:
            await ws.send_json(event)
            return True
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return False
        except Exception as exc:
            print(
                f"[preview-fsm] sid={sid_param} emit failed: {exc!r}",
                flush=True,
            )
            return False

    alive = await emit(
        {
            "type": "preview.attached",
            "tab_id": tab_id,
            "width": width,
            "height": height,
        }
    )

    try:
        while alive:
            # > MAX (not >=). With MAX=N: we run 1 initial stream + N retry
            # streams = N+1 total stream sessions, and the client sees N
            # "preview.reconnecting" events. The (N+1)th stream session is
            # NOT wasted — it's the Nth retry actually being attempted; we
            # need to run it to know whether it succeeds. After it fails
            # the bottom check skips its emit, the loop returns to the top,
            # and the top guard fires `max_reconnects`. Wall-clock cost
            # at default `stream_timeout=120s` is ~(N+1)*120s, not N*120s.
            if reconnect_attempt > _FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS:
                print(
                    f"[preview-fsm] sid={sid_param} ended reason=max_reconnects "
                    f"attempts={reconnect_attempt} frames={frame_seq}",
                    flush=True,
                )
                await emit(
                    {
                        "type": "preview.ended",
                        "reason": "max_reconnects",
                        "retriable": False,
                        "frame_count": frame_seq,
                    }
                )
                break

            # One underlying private-core screencast session. Runs until it
            # emits a terminal status, raises, or the iterator finishes.
            terminal_reason: str | None = None
            retriable_terminal: bool = False
            reconnect_due: bool = False

            try:
                async for event in cloud_tools.stream_screencast(
                    agent_id,
                    tab_id,
                    relay_host=relay_host,
                    relay_port=relay_port,
                    width=width,
                    height=height,
                    # JPEG quality 70: visibly sharper than the previous 30
                    # on both the Chrome push path and the poll fallback.
                    # Bandwidth @1fps x 1440x1080 is ~15 KB/s per guest,
                    # negligible at current concurrency. Paired with the
                    # core-private fix that removed the hard `min(..., 20)`
                    # cap in _poll_screenshot (which was silently clamping
                    # the poll path's quality below what the caller asked
                    # for). Without that core fix this bump would only
                    # affect the Chrome push path.
                    quality=70,
                    image_format="jpeg",
                    every_nth_frame=1,
                    # Bumped 900 → 5000 so the transparent reconnect
                    # triggered by the engine's max_frames status fires
                    # much less often during long guest runs. With the
                    # 1-fps poll floor, 900 frames = 15 min per logical
                    # stream before a (brief) reattach; Chrome push
                    # frames during fast animation can push the count
                    # even higher, so the old 900 cap could fire
                    # mid-page on a single fast-loading e-commerce
                    # site. 5000 frames = ~83 minutes at 1 fps, well
                    # beyond any realistic guest run.
                    max_frames=5000,
                    stream_timeout=_FIRST_LOOK_PREVIEW_STREAM_IDLE_TIMEOUT_S,
                ):
                    if ws.closed:
                        alive = False
                        break

                    evt_type = event.get("type")
                    if evt_type == "frame":
                        frame_seq += 1
                        alive = await emit(
                            {
                                "type": "preview.frame",
                                "mime": event.get("mime", "image/jpeg"),
                                "data": event.get("data", ""),
                                "seq": frame_seq,
                            }
                        )
                        if not alive:
                            break
                        continue

                    if evt_type == "status":
                        # Translate private-core's legacy status reasons into
                        # either a transparent reconnect or a terminal end.
                        # Default to "fatal" (NOT "ended") so the literal
                        # value we emit is always one of the documented
                        # protocol reasons (slow_client|max_reconnects|fatal).
                        # The client switch has no case for "ended" and would
                        # silently swallow it.
                        raw_reason = str(event.get("reason") or "").strip()
                        if not raw_reason:
                            print(
                                f"[preview-fsm] sid={sid_param} status event "
                                f"missing reason; treating as fatal",
                                flush=True,
                            )
                        reason = raw_reason or "fatal"
                        if reason in ("stream_timeout", "max_frames"):
                            reconnect_due = True
                        else:
                            terminal_reason = reason
                            # slow_client is the only status-based reason a
                            # fresh WS might recover from (backpressure may
                            # have cleared by the time the client retries).
                            retriable_terminal = reason == "slow_client"
                        break

                    # Unknown event types from private-core are ignored.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"[preview-fsm] sid={sid_param} underlying stream error: {exc!r}",
                    flush=True,
                )
                # Network / relay / private-core fault. Treat as terminal but
                # retriable so the UI can attempt a fresh WS if the run is
                # still active.
                terminal_reason = "fatal"
                retriable_terminal = True

            if not alive:
                print(
                    f"[preview-fsm] sid={sid_param} client WS closed mid-stream "
                    f"frames={frame_seq}",
                    flush=True,
                )
                break

            if reconnect_due:
                reconnect_attempt += 1
                # > MAX (matches the top-of-loop guard). The (N+1)th
                # reconnect_due fires after we've already run our Nth
                # retry stream session, so we suppress the (N+1)th
                # reconnecting event and let the next iteration's top
                # guard emit max_reconnects instead.
                if reconnect_attempt > _FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS:
                    # Will be caught at the top of the next loop iteration
                    # and turned into a preview.ended(max_reconnects).
                    continue
                print(
                    f"[preview-fsm] sid={sid_param} transparent reconnect "
                    f"attempt={reconnect_attempt} frames_so_far={frame_seq}",
                    flush=True,
                )
                alive = await emit(
                    {
                        "type": "preview.reconnecting",
                        "attempt": reconnect_attempt,
                    }
                )
                if not alive:
                    break
                await asyncio.sleep(_FIRST_LOOK_PREVIEW_RECONNECT_BACKOFF_S)
                continue

            if terminal_reason is not None:
                print(
                    f"[preview-fsm] sid={sid_param} ended reason={terminal_reason} "
                    f"retriable={retriable_terminal} frames={frame_seq}",
                    flush=True,
                )
                await emit(
                    {
                        "type": "preview.ended",
                        "reason": terminal_reason,
                        "retriable": retriable_terminal,
                        "frame_count": frame_seq,
                    }
                )
                break

            # Iterator exhausted with no status and no error. That only tells
            # us the screencast transport ended; it does NOT tell us the run
            # finished. Treat it as a reconnect-worthy transport loss.
            reconnect_attempt += 1
            if reconnect_attempt > _FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS:
                continue
            print(
                f"[preview-fsm] sid={sid_param} iterator exhausted; "
                f"reconnecting attempt={reconnect_attempt} frames_so_far={frame_seq}",
                flush=True,
            )
            alive = await emit(
                {
                    "type": "preview.reconnecting",
                    "attempt": reconnect_attempt,
                }
            )
            if not alive:
                break
            await asyncio.sleep(_FIRST_LOOK_PREVIEW_RECONNECT_BACKOFF_S)
    finally:
        if not ws.closed:
            await ws.close()
        print(
            f"[preview-fsm] sid={sid_param} disconnected frames={frame_seq}",
            flush=True,
        )

    return ws


async def check_relay_agent(agent_id: str) -> bool:
    """Quick check if an agent is connected to the relay via HTTP API."""
    core = _core()
    relay_host, relay_port = core._parse_relay()
    scheme = "https" if relay_port == 443 else "http"
    if relay_port in (443, 80):
        url = f"{scheme}://{relay_host}/api/agents"
    else:
        url = f"{scheme}://{relay_host}:{relay_port}/api/agents"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=3, headers=core._relay_auth_headers())
            if resp.is_success:
                agents = resp.json()
                return any(a.get("agent_id") == agent_id for a in agents)
    except Exception:
        pass

    # Fallback: try WS connect
    import websockets

    try:
        async with websockets.connect(
            core._relay_cdp_url(agent_id, "auto"),
            open_timeout=3,
            additional_headers=core._relay_auth_headers() or None,
        ) as ws:
            try:
                # A connected agent leaves the CDP socket idle until the client sends.
                # Immediate close means the relay accepted the handshake but rejected
                # this agent/auth, so do not count the bridge as online.
                await asyncio.wait_for(ws.recv(), timeout=0.25)
            except asyncio.TimeoutError:
                await ws.close()
                return True
            except websockets.exceptions.ConnectionClosed:
                return False
            await ws.close()
            return True
    except Exception:
        return False


def normalize_bridge_profile(raw: object) -> str:
    """Normalize a relay bridge profile name the same way the local bridge does."""
    profile = str(raw or "").strip() or "default"
    profile = profile.replace(" ", "_").replace(".", "_")
    profile = re.sub(r"[^a-zA-Z0-9_-]", "", profile)[:32]
    return profile or "default"


def bridge_agent_id_for_profile(auth_info: dict, profile: object = "default") -> str:
    """Return the relay agent id for this user's browser bridge profile."""
    base = str(auth_info.get("agent_id", "") or "").strip()
    if not base:
        return ""
    normalized = normalize_bridge_profile(profile)
    return base if normalized == "default" else f"{base}-{normalized}"


def _profile_from_bridge_agent_id(auth_info: dict, agent_id: str) -> str:
    base = str(auth_info.get("agent_id", "") or "").strip()
    aid = str(agent_id or "").strip()
    if not base or aid == base:
        return "default"
    prefix = f"{base}-"
    if aid.startswith(prefix):
        return normalize_bridge_profile(aid[len(prefix):])
    return "default"


def _is_bridge_agent_for_auth(auth_info: dict, agent_id: str) -> bool:
    base = str(auth_info.get("agent_id", "") or "").strip()
    aid = str(agent_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", aid):
        return False
    return bool(base and (aid == base or aid.startswith(f"{base}-")))


async def list_relay_agents_for_auth(auth_info: dict) -> list[dict]:
    """Return relay-connected bridge agents owned by the authenticated user."""
    key = str(auth_info.get("key", "") or "").strip()
    if not key:
        return []
    core = _core()
    relay_host, relay_port = core._parse_relay()
    scheme = "https" if relay_port == 443 else "http"
    if relay_port in (443, 80):
        url = f"{scheme}://{relay_host}/api/agents"
    else:
        url = f"{scheme}://{relay_host}:{relay_port}/api/agents"
    agents = []
    timeout = httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=2.0)
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                )
            if not resp.is_success:
                return []
            agents = resp.json()
            break
        except Exception:
            if attempt == 1:
                return []
            await asyncio.sleep(0.1)
    if not isinstance(agents, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        aid = str(agent.get("agent_id", "") or "").strip()
        if not _is_bridge_agent_for_auth(auth_info, aid) or aid in seen:
            continue
        profile = normalize_bridge_profile(
            agent.get("profile") or _profile_from_bridge_agent_id(auth_info, aid)
        )
        seen.add(aid)
        out.append({"agent_id": aid, "profile": profile, "connected": True})
    out.sort(key=lambda a: (a.get("profile") != "default", a.get("profile", ""), a.get("agent_id", "")))
    return out


async def resolve_bridge_agent(
    auth_info: dict,
    preferred_profile: object | None = None,
    preferred_agent_id: str = "",
) -> dict:
    """Resolve the browser bridge independently from the account-scoped chat agent."""
    core = _core()
    base_agent_id = str(auth_info.get("agent_id", "") or "").strip()
    local_caps = core._chat_agent_caps.get(base_agent_id, {}) if base_agent_id else {}
    explicit_profile = preferred_profile is not None
    raw_profile = preferred_profile
    if raw_profile is None:
        raw_profile = local_caps.get("bridge_profile") or ""
        explicit_profile = bool(raw_profile)
    requested_agent = str(preferred_agent_id or "").strip()

    if requested_agent and _is_bridge_agent_for_auth(auth_info, requested_agent):
        profile = _profile_from_bridge_agent_id(auth_info, requested_agent)
        expected_agent_id = requested_agent
        explicit_profile = True
    else:
        profile = normalize_bridge_profile(raw_profile)
        expected_agent_id = bridge_agent_id_for_profile(auth_info, profile)

    resolution_error = False
    try:
        connected = await core._check_relay_agent(expected_agent_id) if expected_agent_id else False
    except Exception:
        connected = False
        resolution_error = True
    available: list[dict] = []
    if not connected:
        try:
            agents = await core._list_relay_agents_for_auth(auth_info)
        except Exception:
            agents = []
            resolution_error = True
        available = [a for a in agents if _is_bridge_agent_for_auth(auth_info, a.get("agent_id", ""))]
    by_id = {a["agent_id"]: a for a in available if a.get("agent_id")}

    if expected_agent_id in by_id:
        connected = True
        profile = normalize_bridge_profile(by_id[expected_agent_id].get("profile", profile))
    elif not explicit_profile and available:
        default_agent = base_agent_id
        selected = by_id.get(default_agent)
        if selected is None and len(available) == 1:
            selected = available[0]
        if selected is not None:
            expected_agent_id = selected["agent_id"]
            profile = normalize_bridge_profile(selected.get("profile", "default"))
            connected = True

    selection_required = (
        not explicit_profile
        and not connected
        and len(available) > 1
        and expected_agent_id not in by_id
    )
    status_reason = "online" if connected else ("resolution_error" if resolution_error else "offline")
    if selection_required:
        status_reason = "profile_required"
    elif explicit_profile and not connected and profile != "default" and not resolution_error:
        status_reason = "profile_offline"

    return {
        "bridge_agent_id": expected_agent_id,
        "active_bridge_agent_id": expected_agent_id,
        "bridge_profile": profile,
        "active_bridge_profile": profile,
        "bridge_connected": connected,
        "bridge_configured": bool(expected_agent_id),
        "available_bridge_profiles": available,
        "bridge_selection_required": selection_required,
        "bridge_status_reason": status_reason,
    }


def resolve_chat_agent_id(auth_info: dict, model: str) -> str:
    """Return the chat agent_id for the given model + authenticated user."""
    core = _core()
    h = auth_info["key_hash"]
    if model and model.startswith("gemini"):
        return f"gemini-{h}"
    if core._is_claude_sdk_model(model):
        return f"claudesdk-{h}"
    if core._is_codex_sdk_model(model):
        return f"codexsdk-{h}"
    if core._is_codex_cli_model(model):
        # Codex CLI runs on the user's local CLI agent, same lane as Claude CLI.
        return auth_info["agent_id"]
    if core._is_opencode_cli_model(model):
        # OpenCode CLI runs on the user's local CLI agent, same lane as Claude CLI.
        return auth_info["agent_id"]
    return auth_info["agent_id"]  # claude-{hash}


async def agent_request(agent_id: str, msg: dict, timeout: float = 10) -> dict | None:
    """Send a request to the agent WS and wait for a response."""
    core = _core()
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return None
    req_id = uuid.uuid4().hex[:8]
    msg["req_id"] = req_id
    q: asyncio.Queue = asyncio.Queue()
    core._agent_req_queues[req_id] = q
    try:
        await ws.send_json(msg)
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        core._agent_req_queues.pop(req_id, None)


async def _agent_request_after_reconnect(
    agent_id: str, msg: dict, *, timeout: float = 10, retry_delay: float = 1.0
) -> dict | None:
    """Wait through an initial reconnect window without replaying ambiguous requests."""
    core = _core()
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        await asyncio.sleep(retry_delay)
        ws = core._chat_agents.get(agent_id)
        if ws is None or ws.closed:
            return None
    return await agent_request(agent_id, dict(msg), timeout=timeout)


async def handle_chat_status(request: web.Request) -> web.Response:
    """GET /web/chat/status — check if user's agent is connected."""
    core = _core()
    if request.query.get("first_look_guest") == "1":
        guest_auth, guest_id, _ = core._first_look_guest_auth(request)
        gws = core._chat_agents.get(core.TRIAL_AGENT_ID)
        chat_connected = bool(core.TRIAL_AGENT_ID) and gws is not None and not gws.closed
        bridge_connected = False
        if core.HEADLESS_AGENT_ID:
            try:
                bridge_connected = await core._check_relay_agent(core.HEADLESS_AGENT_ID)
            except Exception as e:
                log.warning(
                    "[chat-status] first-look bridge probe failed (agent=%s): %s",
                    core.HEADLESS_AGENT_ID,
                    e,
                )
        connected = chat_connected and bridge_connected
        guest_resp = web.json_response(
            {
                "connected": connected,
                "agent_id": guest_auth.get("agent_id", ""),
                "chat_connected": chat_connected,
                "chat_agent_id": core.TRIAL_AGENT_ID or "",
                "bridge_connected": bridge_connected,
                "bridge_agent_id": core.HEADLESS_AGENT_ID or "",
                "bridge_configured": bool(core.HEADLESS_AGENT_ID),
                "guest": True,
            },
        )
        core._attach_first_look_guest_cookies(guest_resp, request, guest_id)
        return guest_resp
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        bridge_info = await core._resolve_bridge_agent(
            auth_info,
            request.query.get("bridge_profile") if "bridge_profile" in request.query else None,
        )
    except Exception as e:
        log.warning("[chat-status] bridge resolution failed: %s", e)
        fallback_agent = auth_info.get("agent_id", "")
        bridge_info = {
            "bridge_agent_id": fallback_agent,
            "active_bridge_agent_id": fallback_agent,
            "bridge_profile": "default",
            "active_bridge_profile": "default",
            "bridge_connected": False,
            "bridge_configured": bool(fallback_agent),
            "available_bridge_profiles": [],
            "bridge_selection_required": False,
            "bridge_status_reason": "resolution_error",
        }
    bridge_agent_id = bridge_info.get("bridge_agent_id", "")
    agent_id = auth_info.get("agent_id", "")
    ws = core._chat_agents.get(agent_id)
    chat_connected = ws is not None and not ws.closed
    connected = chat_connected
    chat_only = request.query.get("chat_only") == "1"
    bridge_connected = bool(bridge_info.get("bridge_connected"))
    if not connected and agent_id and not chat_only:
        connected = bridge_connected

    model_hint = request.query.get("model", "")
    if core._is_pending_user(auth_info) and model_hint and not core._is_openrouter_model(model_hint):
        return core._pending_limited_response()
    wants_gemini = request.query.get("gemini") == "1"
    wants_codex = (
        request.query.get("codex") == "1"
        or core._is_codex_cli_model(model_hint)
        or core._is_codex_sdk_model(model_hint)
    )
    wants_opencode = request.query.get("opencode") == "1" or core._is_opencode_cli_model(model_hint)
    wants_claude_sdk = core._is_claude_sdk_model(model_hint) or request.query.get("claude_sdk") == "1"

    gemini_connected = False
    if wants_gemini:
        gemini_id = f"gemini-{auth_info['key_hash']}"
        proc = core._gemini_procs.get(gemini_id)
        if not proc or proc.poll() is not None:
            import signup_agent

            user_id = auth_info.get("user_id", "")
            gemini_key = signup_agent.get_provider_key(user_id, "gemini") if user_id else None
            if gemini_key:
                core._spawn_gemini_agent(user_id, auth_info["key"], gemini_key)
        gws = core._chat_agents.get(gemini_id)
        gemini_connected = gws is not None and not gws.closed

    codex_connected = False
    codex_agent_id = ""
    codex_cli_supported = True
    if wants_codex:
        import signup_agent

        user_id = auth_info.get("user_id", "")
        key_hash = auth_info["key_hash"]

        sdk_key = signup_agent.get_provider_key(user_id, "codex-sdk") if user_id else None
        cli_key = signup_agent.get_provider_key(user_id, "codex-cli") if user_id else None
        prefer_cli = core._is_codex_cli_model(model_hint)

        if prefer_cli:
            codex_agent_id = auth_info.get("agent_id", "")
            cws = core._chat_agents.get(codex_agent_id)
            codex_chat_connected = cws is not None and not cws.closed
            codex_connected = codex_chat_connected
            if not codex_connected and codex_agent_id and not chat_only:
                codex_connected = await core._check_relay_agent(codex_agent_id)
            caps = core._chat_agent_caps.get(codex_agent_id, {})
            codex_cli_supported = bool(caps.get("codex_cli"))
            if codex_connected and not codex_cli_supported:
                codex_connected = False
        else:
            codex_key = sdk_key or cli_key
            if codex_key:
                codex_agent_id = f"codexsdk-{key_hash}"
                proc = core._codex_sdk_procs.get(codex_agent_id)
                if not proc or proc.poll() is not None:
                    core._spawn_codex_sdk_agent(user_id, auth_info["key"], codex_key)

        if codex_agent_id and not prefer_cli:
            cws = core._chat_agents.get(codex_agent_id)
            codex_connected = cws is not None and not cws.closed
        if prefer_cli:
            chat_connected = codex_chat_connected
            connected = codex_connected
            agent_id = codex_agent_id

    opencode_connected = False
    opencode_agent_id = ""
    opencode_cli_supported = True
    opencode_models: list[str] = []
    if wants_opencode:
        opencode_agent_id = auth_info.get("agent_id", "")
        ows = core._chat_agents.get(opencode_agent_id)
        opencode_chat_connected = ows is not None and not ows.closed
        opencode_connected = opencode_chat_connected
        if not opencode_connected and opencode_agent_id and not chat_only:
            opencode_connected = await core._check_relay_agent(opencode_agent_id)
        caps = core._chat_agent_caps.get(opencode_agent_id, {})
        opencode_cli_supported = bool(caps.get("opencode_cli")) if caps else True
        raw_models = caps.get("opencode_models") if isinstance(caps, dict) else []
        if isinstance(raw_models, list):
            seen_models = set()
            for raw_model in raw_models:
                model_id = str(raw_model or "").strip()
                if not model_id or model_id in seen_models:
                    continue
                if "/" not in model_id or any(ch.isspace() for ch in model_id):
                    continue
                seen_models.add(model_id)
                opencode_models.append(model_id)
                if len(opencode_models) >= 500:
                    break
        if opencode_connected and not opencode_cli_supported:
            opencode_connected = False
        chat_connected = opencode_chat_connected
        connected = opencode_connected
        agent_id = opencode_agent_id

    claude_sdk_connected = False
    claude_sdk_agent_id = ""
    if wants_claude_sdk:
        import signup_agent

        user_id = auth_info.get("user_id", "")
        key_hash = auth_info["key_hash"]
        claude_key = signup_agent.get_provider_key(user_id, "claude-sdk") if user_id else None
        if claude_key:
            claude_sdk_agent_id = f"claudesdk-{key_hash}"
            proc = core._claude_sdk_procs.get(claude_sdk_agent_id)
            if not proc or proc.poll() is not None:
                core._spawn_claude_sdk_agent(user_id, auth_info["key"], claude_key)
            cws = core._chat_agents.get(claude_sdk_agent_id)
            claude_sdk_connected = cws is not None and not cws.closed

            chat_connected = claude_sdk_connected
            connected = claude_sdk_connected
            agent_id = claude_sdk_agent_id

    mismatch_agent = ""
    if not chat_connected and agent_id:
        user_id = auth_info.get("user_id", "")
        if user_id:
            for other_id, other_uid in core._chat_agent_users.items():
                if other_uid == user_id and other_id != agent_id:
                    other_ws = core._chat_agents.get(other_id)
                    if other_ws and not other_ws.closed:
                        mismatch_agent = other_id
                        break

    local_client_agent_id = auth_info.get("agent_id", "")
    local_client_ws = core._chat_agents.get(local_client_agent_id) if local_client_agent_id else None
    local_client_connected = local_client_ws is not None and not local_client_ws.closed

    resp = {"connected": connected, "agent_id": agent_id}
    resp["chat_connected"] = chat_connected
    resp["chat_agent_id"] = agent_id
    resp["bridge_connected"] = bridge_connected
    resp["bridge_agent_id"] = bridge_agent_id
    resp["active_bridge_agent_id"] = bridge_info.get("active_bridge_agent_id", bridge_agent_id)
    resp["bridge_profile"] = bridge_info.get("bridge_profile", "default")
    resp["active_bridge_profile"] = bridge_info.get("active_bridge_profile", "default")
    resp["available_bridge_profiles"] = bridge_info.get("available_bridge_profiles", [])
    resp["bridge_selection_required"] = bool(bridge_info.get("bridge_selection_required"))
    resp["bridge_status_reason"] = bridge_info.get("bridge_status_reason", "offline")
    resp["bridge_configured"] = bool(bridge_info.get("bridge_configured"))
    resp["client_agent_id"] = local_client_agent_id
    resp["client_connected"] = local_client_connected
    resp.update(_client_version_status(core._chat_agent_caps.get(local_client_agent_id, {})))
    if mismatch_agent:
        resp["mismatch"] = True
        resp["mismatch_agent_id"] = mismatch_agent
    if wants_gemini:
        resp["gemini_agent_id"] = f"gemini-{auth_info['key_hash']}"
        resp["gemini_connected"] = gemini_connected
    if wants_codex:
        resp["codex_agent_id"] = codex_agent_id
        resp["codex_connected"] = codex_connected
        if core._is_codex_cli_model(model_hint):
            resp["codex_cli_supported"] = codex_cli_supported
    if wants_opencode:
        resp["opencode_agent_id"] = opencode_agent_id
        resp["opencode_connected"] = opencode_connected
        resp["opencode_cli_supported"] = opencode_cli_supported
        resp["opencode_models"] = opencode_models
    if wants_claude_sdk:
        resp["claude_sdk_agent_id"] = claude_sdk_agent_id
        resp["claude_sdk_connected"] = claude_sdk_connected
    return web.json_response(resp)


async def handle_chat_update_client(request: web.Request) -> web.Response:
    """POST /web/chat/update-client — ask the local client package to self-update."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    agent_id = str(auth_info.get("agent_id", "") or "").strip()
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return web.json_response({"error": "Your local client is offline."}, status=503)

    caps = core._chat_agent_caps.get(agent_id, {})
    if not bool(caps.get("remote_update")):
        return web.json_response(
            {
                "error": (
                    "This local client package does not support one-click updates yet. "
                    "Run the installer or update script once manually, then retry."
                )
            },
            status=409,
        )
    version_status = _client_version_status(caps)
    if not bool(version_status.get("client_outdated")):
        return web.json_response(
            {
                "error": "Your local client is already current.",
                "client_version": str(version_status.get("client_version") or "").strip(),
                "server_version": str(version_status.get("server_version") or "").strip(),
            },
            status=409,
        )

    resp = await agent_request(agent_id, {"type": "update_client"}, timeout=4)
    if not resp:
        return web.json_response(
            {"error": "Timed out waiting for the local client to start updating."},
            status=504,
        )
    if resp.get("type") == "update_client_ok":
        return web.json_response(
            {
                "ok": True,
                "status": str(resp.get("status") or "updating"),
                "client_version": str(caps.get("client_version") or "").strip(),
            }
        )
    return web.json_response(
        {"error": str(resp.get("error") or "Local client update failed to start.")},
        status=500,
    )


async def handle_chat_install_research_desk(request: web.Request) -> web.Response:
    """POST /web/chat/install-research-desk — ask the local client to install Research Desk."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    agent_id = str(auth_info.get("agent_id", "") or "").strip()
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return web.json_response({"error": "Your local client is offline."}, status=503)

    caps = core._chat_agent_caps.get(agent_id, {})
    if not bool(caps.get("remote_research_desk_install")):
        return web.json_response(
            {
                "error": (
                    "This local client package does not support one-click Research Desk install yet. "
                    "Run the Research Desk install steps manually, then retry."
                )
            },
            status=409,
        )
    install_version = _research_desk_install_requires_update(caps)
    if bool(install_version.get("update_required")):
        client_version = str(install_version.get("client_version") or "unknown").strip() or "unknown"
        required_version = str(install_version.get("required_client_version") or _RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION)
        if bool(install_version.get("update_supported")):
            error = (
                "Your local client is still on {client_version}. Update it to at least {required_version}, "
                "then retry Research Desk install."
            ).format(client_version=client_version, required_version=required_version)
        else:
            error = (
                "Your local client is still on {client_version} and cannot self-update here. "
                "Run the installer or update script until it reaches at least {required_version}, then retry."
            ).format(client_version=client_version, required_version=required_version)
        return web.json_response(
            {
                "error": error,
                "update_required": True,
                "update_supported": bool(install_version.get("update_supported")),
                "client_version": client_version,
                "required_client_version": required_version,
            },
            status=409,
        )

    resp = await agent_request(agent_id, {"type": "install_research_desk"}, timeout=4)
    if not resp:
        return web.json_response(
            {"error": "Timed out waiting for the local client to start the Research Desk install."},
            status=504,
        )
    if resp.get("type") == "install_research_desk_ok":
        return web.json_response(
            {
                "ok": True,
                "status": str(resp.get("status") or "installing"),
                "launcher_prefix": str(resp.get("launcher_prefix") or "python3 -m unchained_pyreplab"),
            }
        )
    return web.json_response(
        {"error": str(resp.get("error") or "Local Research Desk install failed to start.")},
        status=500,
    )


async def handle_chat_history(request: web.Request) -> web.Response:
    """GET /web/chat/history — proxy to agent for local chat history."""
    core = _core()
    guest_mode = False
    guest_id = ""
    if request.query.get("first_look_guest") == "1":
        auth_info, guest_id, _ = core._first_look_guest_auth(request)
        guest_mode = True
    else:
        auth_info = core._authenticate(request)
        if not auth_info:
            return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")
    model = request.query.get("model", "")
    if (guest_mode or core._is_pending_user(auth_info)) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    if guest_mode and not core._is_openrouter_model(model):
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    requested_session_id = request.query.get("session_id", "")
    requested_slot = _normalize_chat_slot(request.query.get("slot"))
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)

    if core._is_openrouter_model(model):
        session_id = core._resolve_trial_session_id(agent_id, requested_session_id)
        msgs, found = core._read_trial_history(session_id)
        payload = {"messages": msgs, "trial": True, "session_id": session_id}
        if not found:
            payload["offline"] = True
        if guest_mode:
            payload["guest"] = True
        history_resp = web.json_response(payload)
        if guest_mode:
            core._attach_first_look_guest_cookies(history_resp, request, guest_id)
        return history_resp

    history_msg = {"type": "get_history", "session_id": requested_session_id}
    if requested_slot is not None:
        history_msg["slot"] = requested_slot
    resp = await core._agent_request(chat_agent_id, history_msg)
    if resp is not None:
        payload = {"messages": resp.get("messages", [])}
        if resp.get("session_id"):
            payload["session_id"] = resp["session_id"]
        return web.json_response(payload)

    session_id = core._resolve_trial_session_id(agent_id, requested_session_id)
    msgs, found = core._read_trial_history(session_id)
    if found:
        return web.json_response({"messages": msgs, "trial": True, "session_id": session_id})
    return web.json_response({"messages": [], "offline": True})


async def handle_chat_new(request: web.Request) -> web.Response:
    """POST /web/chat/new — proxy to agent to advance to next slot."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if bool(body.get("first_look_guest")):
        auth_info, _, _ = core._first_look_guest_auth(request)
    else:
        auth_info = core._authenticate(request)
        if not auth_info:
            return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")

    model = body.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    requested_session_id = body.get("session_id", "")
    requested_slot = _normalize_chat_slot(body.get("slot"))
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    if core._is_openrouter_model(model):
        old_session = core._resolve_trial_session_id(agent_id, requested_session_id)
        new_session = f"s-{agent_id}-{int(core.time.time() * 1000):x}"
        # Migrate the Chrome tab to the new session so the screencast
        # can persist, but always create a fresh session_id so the
        # agent starts with a clean conversation history.
        old_tab = core._session_tabs.pop(old_session, None)
        if old_tab:
            core._session_tabs[new_session] = old_tab
            core._session_agent_map.pop(old_session, None)
        core._delete_trial_session(old_session)
        return web.json_response(
            {
                "ok": True,
                "active_slot": 1,
                "trial": True,
                "session_id": new_session,
            }
        )

    new_chat_msg = {"type": "new_chat"}
    if requested_slot is not None:
        new_chat_msg["slot"] = requested_slot
    resp = await core._agent_request(chat_agent_id, new_chat_msg)
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    result = {"ok": True, "active_slot": resp.get("active_slot", 1)}
    if resp.get("session_id"):
        result["session_id"] = resp["session_id"]
    return web.json_response(result)


async def handle_chat_slots(request: web.Request) -> web.Response:
    """GET /web/chat/slots — get slot info from agent."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")

    model = request.query.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    requested_session_id = request.query.get("session_id", "")
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    if core._is_openrouter_model(model):
        session_id = core._resolve_trial_session_id(agent_id, requested_session_id)
        msgs, _ = core._read_trial_history(session_id)
        preview = ""
        for m in msgs:
            if m.get("role") == "user":
                preview = m.get("content", "")[:40]
                break
        return web.json_response(
            {
                "active_slot": 1,
                "slots": [
                    {"slot": 1, "empty": len(msgs) == 0, "preview": preview},
                    {"slot": 2, "empty": True, "preview": ""},
                    {"slot": 3, "empty": True, "preview": ""},
                ],
                "trial": True,
                "session_id": session_id,
            }
        )

    resp = await core._agent_request(chat_agent_id, {"type": "get_slots"})
    if resp is None:
        return web.json_response(
            {
                "active_slot": 1,
                "slots": [
                    {"slot": 1, "empty": True, "preview": ""},
                    {"slot": 2, "empty": True, "preview": ""},
                    {"slot": 3, "empty": True, "preview": ""},
                ],
                "offline": True,
            }
        )
    return web.json_response(
        {
            "active_slot": resp.get("active_slot", 1),
            "slots": resp.get("slots", []),
        }
    )


async def handle_chat_switch(request: web.Request) -> web.Response:
    """POST /web/chat/switch — switch active slot."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    slot = _normalize_chat_slot(body.get("slot")) or 1
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    if core._is_openrouter_model(model):
        return web.json_response({"ok": True, "active_slot": 1, "trial": True})
    resp = await core._agent_request(chat_agent_id, {"type": "switch_slot", "slot": slot})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    return web.json_response({"ok": True, "active_slot": resp.get("active_slot", slot)})


async def handle_chat_archives(request: web.Request) -> web.Response:
    """GET /web/chat/archives — list archived conversations from agent."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")
    model = request.query.get("model", "")
    chat_agent_id = resolve_chat_agent_id(auth_info, model) if model else agent_id
    resp = await agent_request(chat_agent_id, {"type": "get_archives"})
    if resp is None:
        return web.json_response({"archives": [], "offline": True})
    return web.json_response({"archives": resp.get("archives", [])})


async def handle_chat_restore_archive(request: web.Request) -> web.Response:
    """POST /web/chat/restore-archive — restore an archived conversation."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    archive_id = body.get("archive_id", "")
    requested_slot = _normalize_chat_slot(body.get("slot"))
    if not archive_id:
        return web.json_response({"error": "archive_id required"}, status=400)
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    chat_agent_id = resolve_chat_agent_id(auth_info, model) if model else agent_id
    restore_msg = {"type": "restore_archive", "archive_id": archive_id}
    if requested_slot is not None:
        restore_msg["slot"] = requested_slot
    resp = await _agent_request_after_reconnect(chat_agent_id, restore_msg)
    if resp is None:
        return web.json_response(
            {"error": "Your local client is reconnecting. Wait a few seconds and retry."},
            status=503,
        )
    if resp.get("type") == "restore_archive_error":
        return web.json_response({"error": resp.get("error", "Restore failed")}, status=404)
    payload = {"ok": True, "active_slot": resp.get("active_slot", 1)}
    if resp.get("session_id"):
        payload["session_id"] = resp["session_id"]
    return web.json_response(payload)


async def handle_chat_delete_archive(request: web.Request) -> web.Response:
    """POST /web/chat/delete-archive — delete an archived conversation."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    archive_id = body.get("archive_id", "")
    if not archive_id:
        return web.json_response({"error": "archive_id required"}, status=400)
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    chat_agent_id = resolve_chat_agent_id(auth_info, model) if model else agent_id
    resp = await agent_request(chat_agent_id, {"type": "delete_archive", "archive_id": archive_id})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    if resp.get("type") == "delete_archive_error":
        return web.json_response({"error": "Archive not found or could not be deleted"}, status=404)
    return web.json_response({"ok": True})
