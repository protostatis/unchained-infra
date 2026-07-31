"""Docker-internal authorization endpoint for the financial terminal."""

from __future__ import annotations

import hashlib

from aiohttp import web

from web_app.core import get_core as _core


_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Vary": "Cookie, Authorization",
}


def _terminal_principal(user_id: str) -> str:
    """Return a stable, opaque principal accepted by the terminal proxy."""
    digest = hashlib.sha256(f"fin-terminal:{user_id}".encode()).hexdigest()[:32]
    return f"ft-{digest}"


async def handle_fin_terminal_auth(request: web.Request) -> web.Response:
    """Authorize an approved admin/allowlisted user for Caddy forward_auth."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.Response(status=401, headers=_NO_STORE_HEADERS)

    email = str(auth_info.get("email", "")).strip().lower()
    user_id = str(auth_info.get("user_id", "")).strip()
    allowed_emails = {
        str(value).strip().lower()
        for value in getattr(core, "FIN_TERMINAL_ALLOWED_EMAILS", ())
        if str(value).strip()
    }
    if (
        auth_info.get("status") != "approved"
        or not user_id
        or not email
        or email not in allowed_emails
    ):
        return web.Response(status=403, headers=_NO_STORE_HEADERS)

    return web.Response(
        status=204,
        headers={
            **_NO_STORE_HEADERS,
            "X-Fin-Terminal-User": _terminal_principal(user_id),
        },
    )
