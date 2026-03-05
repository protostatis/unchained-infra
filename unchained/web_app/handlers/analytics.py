"""Analytics event ingest and admin funnel handlers."""

from __future__ import annotations

from aiohttp import web


def _core():
    import web as core

    return core


def _is_admin(request: web.Request) -> bool:
    from web_app.handlers.auth_admin import is_admin

    return bool(is_admin(request))


async def handle_analytics_event(request: web.Request) -> web.Response:
    """POST /web/analytics/event — lightweight unauthenticated client events."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    payload, error = core._coerce_analytics_event_payload(body, request)
    if error:
        return web.json_response({"error": error}, status=400)
    core._track_event(
        request,
        payload["event"],
        event_id=payload["event_id"],
        session_id=payload["session_id"],
        page_view_id=payload["page_view_id"],
        route=payload["route"],
        route_intended=payload["route_intended"],
        route_effective=payload["route_effective"],
        gate_type=payload["gate_type"],
        cta_id=payload["cta_id"],
        error_code=payload["error_code"],
        source=payload["source"],
        latency_ms=payload["latency_ms"],
        meta=payload["meta"],
        status_code=204,
        dedupe_ttl_s=0.75,
    )
    return web.json_response({"ok": True})


async def handle_analytics_events(request: web.Request) -> web.Response:
    """POST /web/analytics/events — batched lightweight unauthenticated events."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if isinstance(body, list):
        events = body
    elif isinstance(body, dict):
        events = body.get("events", [])
    else:
        events = []
    if not isinstance(events, list):
        return web.json_response({"error": "events must be an array"}, status=400)
    if len(events) > 100:
        return web.json_response({"error": "events length max is 100"}, status=400)

    accepted = 0
    rejected = 0
    for raw in events:
        payload, error = core._coerce_analytics_event_payload(raw, request)
        if error:
            rejected += 1
            continue
        ok = core._track_event(
            request,
            payload["event"],
            event_id=payload["event_id"],
            session_id=payload["session_id"],
            page_view_id=payload["page_view_id"],
            route=payload["route"],
            route_intended=payload["route_intended"],
            route_effective=payload["route_effective"],
            gate_type=payload["gate_type"],
            cta_id=payload["cta_id"],
            error_code=payload["error_code"],
            source=payload["source"],
            latency_ms=payload["latency_ms"],
            meta=payload["meta"],
            status_code=204,
            dedupe_ttl_s=0.75,
        )
        if ok:
            accepted += 1
    return web.json_response({"ok": True, "received": len(events), "accepted": accepted, "rejected": rejected})


async def handle_admin_analytics_funnel(request: web.Request) -> web.Response:
    """GET /admin/analytics/funnel — analytics funnel metrics."""
    core = _core()
    if not _is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    raw_days = str(request.query.get("days", "7")).strip() or "7"
    funnel = str(request.query.get("funnel", "auth_inline_gsi")).strip() or "auth_inline_gsi"
    try:
        days = max(1, min(90, int(raw_days)))
    except ValueError:
        return web.json_response({"error": "days must be an integer"}, status=400)
    try:
        payload = core._analytics.funnel_report(funnel=funnel, days=days)
    except Exception as e:
        core.log.warning("[analytics] funnel query failed: %s", e)
        return web.json_response({"error": "Failed to build funnel"}, status=500)
    return web.json_response(payload)

