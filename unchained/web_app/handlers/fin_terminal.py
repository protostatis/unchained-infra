"""Docker-internal authorization endpoint for the financial terminal."""

from __future__ import annotations

import hashlib
import os
from urllib.parse import urlencode

from aiohttp import web

from web_app.core import get_core as _core


_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Vary": "Cookie, Authorization",
}

# Public prefix that Caddy strips before the browser-auth subrequest (see the
# @fin_terminal_browser handler in the Caddyfile). The forwarded subrequest only
# carries the suffix, so re-attach the prefix to recover the requested path.
_TERMINAL_PUBLIC_PREFIX = "/fin-terminal-browser"
_TERMINAL_PUBLIC_BASE = "/fin-terminal-browser/"

# Signed-out browser navigations are sent here instead of a bare 401. The
# sign-in surface lives on the primary host (not the Unbrowser subdomain) and
# shares the `.unchainedsky.com` session cookie, so the terminal works once the
# user is returned to it after signing in.
_SIGNIN_PATH = "/local"
_SIGNIN_BASE_URL = (
    os.environ.get("FIN_TERMINAL_SIGNIN_URL", "").strip()
    or "https://unchainedsky.com"
).rstrip("/")


def _terminal_principal(user_id: str) -> str:
    """Return a stable, opaque principal accepted by the terminal proxy."""
    digest = hashlib.sha256(f"fin-terminal:{user_id}".encode()).hexdigest()[:32]
    return f"ft-{digest}"


def _header(request, name: str) -> str:
    """Read a single forwarded header without assuming an aiohttp request."""
    headers = getattr(request, "headers", None) or {}
    try:
        return str(headers.get(name, "") or "").strip()
    except AttributeError:
        return ""


def _has_control_char(value: str) -> bool:
    """Return True if ``value`` contains an ASCII control byte.

    Caddy strips only the public prefix, so the forwarded URI is otherwise
    attacker-influenced. The value is always urlencoded before it is placed in
    the ``Location`` header, but reject control bytes at this layer so an
    unencoded reuse of the recovered path can never fold a header or smuggle a
    delimiter.
    """
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _approved_user_id(auth_info) -> str:
    """Return the stable user ID for an approved account, else an empty string."""
    if not auth_info:
        return ""
    user_id = str(auth_info.get("user_id", "")).strip()
    if auth_info.get("status") != "approved" or not user_id:
        return ""
    return user_id


def _principal_response(user_id: str) -> web.Response:
    """Authorize an account and return the opaque principal for Caddy."""
    return web.Response(
        status=204,
        headers={
            **_NO_STORE_HEADERS,
            "X-Fin-Terminal-User": _terminal_principal(user_id),
        },
    )


def _auth_response(request: web.Request, *, allowlisted: bool) -> web.Response:
    """Authorize an account and return the opaque principal for Caddy."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.Response(status=401, headers=_NO_STORE_HEADERS)

    user_id = _approved_user_id(auth_info)
    if not user_id:
        return web.Response(status=403, headers=_NO_STORE_HEADERS)

    if allowlisted:
        email = str(auth_info.get("email", "")).strip().lower()
        allowed_emails = {
            str(value).strip().lower()
            for value in getattr(core, "FIN_TERMINAL_ALLOWED_EMAILS", ())
            if str(value).strip()
        }
        if not email or email not in allowed_emails:
            return web.Response(status=403, headers=_NO_STORE_HEADERS)

    return _principal_response(user_id)


def _forwarded_terminal_path(request) -> str:
    """Recover the public terminal path from the forwarded auth subrequest.

    Caddy strips ``/fin-terminal-browser`` before ``forward_auth`` runs, so the
    forwarded URI is the suffix only. Re-attach the prefix (and fall back to the
    terminal root) so the post-signin return lands on the requested path.
    """
    uri = _header(request, "X-Forwarded-Uri")
    if (
        not uri.startswith("/")
        or uri.startswith("//")
        or "\\" in uri
        or _has_control_char(uri)
    ):
        return _TERMINAL_PUBLIC_BASE
    if not uri.startswith(_TERMINAL_PUBLIC_PREFIX):
        uri = _TERMINAL_PUBLIC_PREFIX + uri
    return uri


def _is_html_navigation(request) -> bool:
    """Return True for a top-level browser document navigation.

    Only navigations are redirected to sign-in. API, asset, and non-GET probes
    keep their explicit ``401`` so non-browser callers (including the deployment
    smoke's ``curl`` probe, which sends ``Accept: */*``) are unaffected.

    ``Accept`` and ``Sec-Fetch-Mode`` are caller-declared, so the ``401``
    contract holds only for callers that do not present browser-navigation
    headers: a ``GET`` that declares navigation gets the redirect. That is safe
    because the terminal host re-authenticates on the redirected request, and a
    caller that follows the redirect still has to sign in.
    """
    method = _header(request, "X-Forwarded-Method") or str(
        getattr(request, "method", "") or "GET"
    )
    if method.upper() != "GET":
        return False
    if "text/html" in _header(request, "Accept").lower():
        return True
    return _header(request, "Sec-Fetch-Mode").lower() == "navigate"


def _signin_redirect(request) -> web.Response:
    """Return a 302 to the site sign-in page with a post-signin return target."""
    location = (
        f"{_SIGNIN_BASE_URL}{_SIGNIN_PATH}"
        f"?{urlencode({'next': _forwarded_terminal_path(request)})}"
    )
    return web.Response(
        status=302,
        headers={**_NO_STORE_HEADERS, "Location": location},
    )


async def handle_fin_terminal_auth(request: web.Request) -> web.Response:
    """Authorize an approved admin/allowlisted user for the Pi terminal."""
    return _auth_response(request, allowlisted=True)


async def handle_fin_terminal_browser_auth(request: web.Request) -> web.Response:
    """Authorize any approved signed-in UnchainedSky account for the browser terminal.

    Signed-out document navigations are redirected to the site sign-in page,
    returning to the requested terminal path, so the user is told to sign in
    instead of seeing a bare error page. Non-navigation requests keep the
    explicit ``401`` contract used by the deployment smoke.
    """
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        if _is_html_navigation(request):
            return _signin_redirect(request)
        return web.Response(status=401, headers=_NO_STORE_HEADERS)

    user_id = _approved_user_id(auth_info)
    if not user_id:
        return web.Response(status=403, headers=_NO_STORE_HEADERS)

    return _principal_response(user_id)
