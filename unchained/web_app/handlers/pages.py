"""Page handlers extracted from web.py."""

from __future__ import annotations

from aiohttp import web


from web_app.core import get_core as _core


async def handle_install_page(request: web.Request) -> web.Response:
    """Serve the one-click installer onboarding page."""
    core = _core()
    del request
    return web.Response(text=core.INSTALL_ONBOARD_HTML, content_type="text/html")


async def handle_trial_page(request: web.Request) -> web.Response:
    """Serve the trial chat HTML page (OpenRouter models)."""
    core = _core()
    del request
    html = core.inject_google_client_id(core.TRIAL_CHAT_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_gemini_page(request: web.Request) -> web.Response:
    """Serve the Gemini SDK chat HTML page (per-user provisioned key)."""
    core = _core()
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_GEMINI_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_codex_page(request: web.Request) -> web.Response:
    """Serve the Codex chat HTML page (per-user provisioned key)."""
    core = _core()
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = core.CHAT_CODEX_HTML.replace("__GOOGLE_CLIENT_ID__", core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_claude_page(request: web.Request) -> web.Response:
    """Serve the Claude SDK chat HTML page (per-user provisioned key)."""
    core = _core()
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = core.CHAT_CLAUDE_SDK_HTML.replace("__GOOGLE_CLIENT_ID__", core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_case_study_zillow(request: web.Request) -> web.Response:
    """Serve the Zillow rental relisting case study page (public, no auth)."""
    core = _core()
    del request
    html = core.CASE_STUDY_ZILLOW_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    return web.Response(text=html, content_type="text/html")


async def handle_demo_page(request: web.Request) -> web.Response:
    """Serve the headless demo chat HTML page."""
    core = _core()
    del request
    html = core.inject_google_client_id(core.HEADLESS_DEMO_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_local_page(request: web.Request) -> web.Response:
    """Serve the local agent chat HTML page (Claude CLI + Codex CLI)."""
    core = _core()
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CLAUDE_CHAT_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_claude_page(request: web.Request) -> web.Response:
    """Redirect /app to /local for backward compatibility."""
    del request
    raise web.HTTPFound("/local")


async def handle_chat_redirect(request: web.Request) -> web.Response:
    """Redirect /chat to /local for backward compatibility."""
    del request
    raise web.HTTPFound("/local")
