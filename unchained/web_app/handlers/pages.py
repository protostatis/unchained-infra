"""Page handlers extracted from web.py."""

from __future__ import annotations

from aiohttp import web


from web_app.core import get_core as _core


async def handle_install_page(request: web.Request) -> web.Response:
    """Serve the one-click installer onboarding page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.INSTALL_ONBOARD_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


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
    """Serve the Codex chat HTML page (per-user provisioned key)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_CODEX_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


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
    """Serve the headless first-look chat HTML page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.HEADLESS_DEMO_HTML, core.GOOGLE_CLIENT_ID)
    resp = web.Response(text=html, content_type="text/html")
    _, guest_id, quota_count = core._first_look_guest_auth(request)
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
    html = core.inject_google_client_id(core.CLAUDE_CHAT_HTML, core.GOOGLE_CLIENT_ID)
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
