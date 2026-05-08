"""Page handlers extracted from web.py."""

from __future__ import annotations

import json

from aiohttp import web


from web_app.core import get_core as _core
from web_app.research_desk_page import build_research_desk_html as _build_research_desk_html
from web_app.templates import FIRST_LOOK_PREVIEW_HTML


async def handle_install_page(request: web.Request) -> web.Response:
    """Serve the one-click installer onboarding page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.INSTALL_ONBOARD_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_cli_page(request: web.Request) -> web.Response:
    """Serve the CLI install guide page."""
    core = _core()
    core._track_page_view(request)
    from web_app.templates import CLI_INSTALL_HTML
    return web.Response(
        text=CLI_INSTALL_HTML,
        content_type="text/html",
        charset="utf-8",
        headers={
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )


async def handle_mcp_page(request: web.Request) -> web.Response:
    """Serve the MCP install/setup page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.MCP_PAGE_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_mcp_guide_page(request: web.Request) -> web.Response:
    """Serve markdown-rendered MCP setup + route-plan docs."""
    core = _core()
    core._track_page_view(request)
    return web.Response(text=core._build_mcp_guide_html(), content_type="text/html")


def _build_first_look_preview_html(*, prompt_limit: int, remaining: int) -> str:
    html = FIRST_LOOK_PREVIEW_HTML
    html = html.replace("__FIRST_LOOK_GUEST_LIMIT__", str(max(1, int(prompt_limit))))
    html = html.replace("__FIRST_LOOK_GUEST_REMAINING__", str(max(0, int(remaining))))
    return html


async def handle_research_desk_page(request: web.Request) -> web.Response:
    """Serve the hosted launch page for the local Research Desk app."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    return web.Response(
        text=_build_research_desk_html(authenticated=bool(auth_info)),
        content_type="text/html",
    )


async def handle_tab_page(request: web.Request) -> web.Response:
    """Serve the lightweight branded default tab page."""
    del request
    core = _core()
    return web.Response(
        text=core.BRANDED_TAB_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def handle_trial_page(request: web.Request) -> web.Response:
    """Serve the trial chat HTML page (OpenRouter models)."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.TRIAL_CHAT_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_gemini_page(request: web.Request) -> web.Response:
    """Serve the Gemini SDK chat HTML page (per-user provisioned key)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_GEMINI_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_codex_page(request: web.Request) -> web.Response:
    """Redirect legacy Codex CLI chat route to the shared local page."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    core._track_redirect(request, "/local?provider=codex-cli", reason="legacy_route_alias")
    raise web.HTTPFound("/local?provider=codex-cli")


async def handle_chat_claude_page(request: web.Request) -> web.Response:
    """Serve the Claude SDK chat HTML page (per-user provisioned key)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_CLAUDE_SDK_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_case_study_zillow(request: web.Request) -> web.Response:
    """Serve the Zillow rental relisting case study page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.CASE_STUDY_ZILLOW_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_apartment(request: web.Request) -> web.Response:
    """Serve apartment hunting use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_APARTMENT_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_flights(request: web.Request) -> web.Response:
    """Serve flight comparison use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_FLIGHTS_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_competitor(request: web.Request) -> web.Response:
    """Serve competitor monitoring use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_COMPETITOR_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_price_tracking(request: web.Request) -> web.Response:
    """Serve price tracking use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_PRICE_TRACKING_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_published_result(request: web.Request) -> web.Response:
    """Serve a published result page (public, no auth)."""
    import re as _re
    import time as _time
    from html import escape
    from published_results import get_result
    core = _core()
    slug = request.match_info.get("slug", "")
    # Validate slug format
    if not slug or not _re.fullmatch(r"[a-z0-9-]{1,120}", slug):
        raise web.HTTPNotFound()
    result = get_result(slug)
    if not result:
        raise web.HTTPNotFound()
    core._track_page_view(request)
    query = result["query"]
    result_text = result["result_text"]
    # Escape for HTML meta attributes
    title_text = escape(query[:80])
    desc_html = escape(result_text[:200]).replace("\n", " ")
    # Escape for JSON-LD (must be valid JSON string content + escape </ for script safety)
    title_json = json.dumps(query[:80])[1:-1].replace("</", r"<\/")
    desc_json = json.dumps(result_text[:200].replace("\n", " "))[1:-1].replace("</", r"<\/")
    created = _time.strftime("%Y-%m-%d", _time.gmtime(result["created_at"]))
    # Single-pass substitution to prevent double-replacement if result_html
    # contains placeholder strings like __RESULT_TITLE__
    import re as _re
    _subs = {
        "__RESULT_TITLE__": title_text,
        "__RESULT_TITLE_JSON__": title_json,
        "__RESULT_DESC__": desc_html,
        "__RESULT_DESC_JSON__": desc_json,
        "__RESULT_SLUG__": slug,
        "__RESULT_HTML__": result["result_html"],
        "__RESULT_DATE__": created,
        "__RESULT_VIEWS__": str(result["view_count"]),
        "__CONTACT_EMAIL__": core.CONTACT_EMAIL,
    }
    _pattern = _re.compile("|".join(_re.escape(k) for k in _subs))
    html = _pattern.sub(lambda m: _subs[m.group(0)], core.PUBLISHED_RESULT_HTML)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_publish_result(request: web.Request) -> web.Response:
    """POST /web/publish-result — publish a session as a shareable page."""
    import re as _re
    from published_results import publish_result
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    body = await request.json()
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)
    # Validate session_id format to prevent path traversal
    if not _re.fullmatch(r"[a-zA-Z0-9_.-]{8,80}", session_id):
        return web.json_response({"error": "Invalid session_id"}, status=400)
    # Load session data
    session_path = core._trial_session_path(session_id)
    try:
        with open(session_path) as f:
            session_data = json.load(f)
    except FileNotFoundError:
        return web.json_response({"error": "Session not found"}, status=404)
    user_id = auth_info.get("email", auth_info.get("key_hash", ""))
    # Offload to thread — publish_result does sync SQLite + sync HTTP (PII guard)
    import asyncio
    slug = await asyncio.to_thread(
        publish_result, session_data, user_id=user_id, session_id=session_id
    )
    if not slug:
        return web.json_response(
            {"error": "No publishable content in session"}, status=400
        )
    return web.json_response({
        "slug": slug,
        "url": f"https://unchainedsky.com/r/{slug}",
        "status": "pending",
    })


async def handle_pending_results(request: web.Request) -> web.Response:
    """GET /web/publish/pending — list results awaiting approval."""
    from published_results import list_pending
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    results = list_pending(limit=100)
    return web.json_response({"pending": results})


async def handle_approve_result(request: web.Request) -> web.Response:
    """POST /web/publish/approve — approve one or more pending results."""
    from published_results import approve_result, bulk_approve
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    body = await request.json()
    slugs = body.get("slugs", [])
    slug = body.get("slug", "")
    if slug:
        ok = approve_result(slug)
        return web.json_response({"approved": ok, "slug": slug})
    if slugs:
        count = bulk_approve(slugs)
        return web.json_response({"approved_count": count, "slugs": slugs})
    return web.json_response({"error": "slug or slugs required"}, status=400)


async def handle_reject_result(request: web.Request) -> web.Response:
    """POST /web/publish/reject — reject one or more pending results."""
    from published_results import reject_result, bulk_reject
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    body = await request.json()
    reason = body.get("reason", "")
    slugs = body.get("slugs", [])
    slug = body.get("slug", "")
    if slug:
        ok = reject_result(slug, reason)
        return web.json_response({"rejected": ok, "slug": slug})
    if slugs:
        count = bulk_reject(slugs, reason)
        return web.json_response({"rejected_count": count, "slugs": slugs})
    return web.json_response({"error": "slug or slugs required"}, status=400)


async def handle_privacy_page(request: web.Request) -> web.Response:
    """Serve public privacy policy page (required for OAuth provider submissions)."""
    core = _core()
    core._track_page_view(request)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Privacy Policy | Unchained</title>
</head>
<body style="margin:0;padding:32px 18px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b1020;color:#e6ecff;line-height:1.6">
  <main style="max-width:880px;margin:0 auto">
    <h1 style="margin:0 0 6px;font-size:32px">Privacy Policy</h1>
    <p style="margin:0 0 20px;color:#a8b3cf">Last updated: March 6, 2026</p>
    <p>Unchained provides browser automation and chat tooling. This policy describes how we collect, use, and protect information when you use our services.</p>
    <h2>Information We Collect</h2>
    <ul>
      <li>Account information you provide during sign-in (name, email, avatar from Google/Facebook when authorized).</li>
      <li>Session and usage data needed to operate chat, authentication, and rate limits.</li>
      <li>Operational logs and analytics events for reliability, abuse prevention, and product improvement.</li>
    </ul>
    <h2>How We Use Information</h2>
    <ul>
      <li>Authenticate users and secure accounts.</li>
      <li>Provide and improve product functionality.</li>
      <li>Detect abuse, enforce limits, and maintain service reliability.</li>
    </ul>
    <h2>Data Sharing</h2>
    <p>We do not sell personal data. We share data only with service providers required to run the platform (for example hosting, email delivery, and model providers), subject to contractual safeguards.</p>
    <h2>Data Retention</h2>
    <p>We retain account and operational data for as long as needed to provide the service, meet legal requirements, resolve disputes, and enforce agreements.</p>
    <h2>Your Rights</h2>
    <p>You can request account/data deletion via <a href="/data-deletion" style="color:#7dd3fc">/data-deletion</a>.</p>
    <h2>Contact</h2>
    <p>Questions about this policy: <a href="mailto:{core.CONTACT_EMAIL}" style="color:#7dd3fc">{core.CONTACT_EMAIL}</a></p>
  </main>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_data_deletion_page(request: web.Request) -> web.Response:
    """Serve public user-data deletion instructions page for OAuth compliance."""
    core = _core()
    core._track_page_view(request)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>User Data Deletion | Unchained</title>
</head>
<body style="margin:0;padding:32px 18px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b1020;color:#e6ecff;line-height:1.6">
  <main style="max-width:880px;margin:0 auto">
    <h1 style="margin:0 0 6px;font-size:32px">User Data Deletion</h1>
    <p style="margin:0 0 20px;color:#a8b3cf">Last updated: March 6, 2026</p>
    <p>If you want your Unchained account and related personal data deleted, send a request from your account email to:</p>
    <p><a href="mailto:{core.CONTACT_EMAIL}" style="color:#7dd3fc">{core.CONTACT_EMAIL}</a></p>
    <h2>What to Include</h2>
    <ul>
      <li>Your account email address.</li>
      <li>Subject line: <code>Data Deletion Request</code>.</li>
    </ul>
    <h2>What Happens Next</h2>
    <ul>
      <li>We verify account ownership.</li>
      <li>We delete or anonymize eligible personal data from active systems.</li>
      <li>We may retain limited records where required for legal/security obligations.</li>
    </ul>
    <p>For policy details, see <a href="/privacy" style="color:#7dd3fc">/privacy</a>.</p>
  </main>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_first_look_page(request: web.Request) -> web.Response:
    """Serve the first-look page (now uses the preview template)."""
    core = _core()
    ref = request.query.get('ref', '')[:64]
    core._track_page_view(request, meta={'ref': ref} if ref else None)
    _, guest_id, quota_count = core._first_look_guest_auth(request)
    html = _build_first_look_preview_html(
        prompt_limit=core._FIRST_LOOK_GUEST_PROMPT_LIMIT,
        remaining=max(0, core._FIRST_LOOK_GUEST_PROMPT_LIMIT - quota_count),
    )
    resp = web.Response(text=html, content_type="text/html")
    core._attach_first_look_guest_cookies(resp, request, guest_id, quota_count=quota_count)
    return resp


async def handle_first_look_preview_page(request: web.Request) -> web.Response:
    """Serve the guest-safe preview route for first-look review."""
    core = _core()
    ref = request.query.get("ref", "")[:64]
    meta = {"route": "first-look-preview"}
    if ref:
        meta["ref"] = ref
    core._track_page_view(request, meta=meta)
    _, guest_id, quota_count = core._first_look_guest_auth(request)
    html = _build_first_look_preview_html(
        prompt_limit=core._FIRST_LOOK_GUEST_PROMPT_LIMIT,
        remaining=max(0, core._FIRST_LOOK_GUEST_PROMPT_LIMIT - quota_count),
    )
    resp = web.Response(text=html, content_type="text/html")
    core._attach_first_look_guest_cookies(resp, request, guest_id, quota_count=quota_count)
    return resp


async def handle_demo_page(request: web.Request) -> web.Response:
    """Redirect /demo to /first-look for backward compatibility."""
    core = _core()
    core._track_redirect(request, "/first-look", reason="legacy_route_alias")
    raise web.HTTPFound("/first-look")


async def handle_local_page(request: web.Request) -> web.Response:
    """Serve the local agent chat HTML page (Claude CLI + Codex CLI)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    provider = request.query.get("provider", "").strip().lower()
    html_source = core.CHAT_CODEX_HTML if provider == "codex-cli" else core.CLAUDE_CHAT_HTML
    html = core.inject_google_client_id(html_source, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_claude_page(request: web.Request) -> web.Response:
    """Redirect /app to /local for backward compatibility."""
    core = _core()
    core._track_page_view(request)
    core._track_redirect(request, "/local", reason="legacy_route_alias")
    raise web.HTTPFound("/local")


async def handle_chat_redirect(request: web.Request) -> web.Response:
    """Redirect /chat to /local for backward compatibility."""
    core = _core()
    core._track_redirect(request, "/local", reason="legacy_route_alias")
    raise web.HTTPFound("/local")
