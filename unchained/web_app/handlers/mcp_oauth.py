"""Consent screen for MCP connectors that sign in with OAuth.

Google, Anthropic and OpenAI run the connector from their own servers, so the
MCP endpoint has to be an OAuth authorization server.  The MCP process owns the
protocol endpoints; this module owns the one step that needs the website's
login session: showing the user which app is asking, which browser it will
drive, and letting them approve or deny it.

The browser never receives a token here.  Approving writes a grant plus a
single-use ticket, and bounces back to the MCP server, which mints the
authorization code and redirects to the client's pre-validated callback.

Everything the user sees about the client is either stored from registration or
escaped; nothing fetched from the client is rendered as HTML.
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

from aiohttp import web

from mcp_identity import agent_label
from mcp_oauth_policy import client_callback_status
from web_app.core import get_core as _core

_CSRF_COOKIE = "uc_oauth_csrf"
_CSRF_MAX_AGE = 600


_STORE: list = []


def _store():
    """Lazy store; it also initialises the tables the consent flow needs."""
    if not _STORE:
        from mcp_oauth_store import OAuthStore

        _STORE.append(OAuthStore())
    return _STORE[0]


def _issuer() -> str:
    return os.environ.get("MCP_OAUTH_ISSUER", "https://unchainedsky.com").strip().rstrip("/")


def _escape(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=_PAGE.format(title=_escape(title), body=body),
        content_type="text/html",
        status=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _signed_in(request: web.Request):
    """Return the session-backed user, or ``None``.

    Only a real browser session may approve a connector.  A Bearer API key must
    never be upgradable into a delegated grant.
    """
    core = _core()
    try:
        info = core._authenticate(request)
    except Exception:
        return None
    if not info or info.get("auth_method") != "session":
        return None
    if info.get("status") in ("rejected", "pending"):
        return None
    return info


def _login_page(request: web.Request) -> web.Response:
    """Reuse the site's Google Sign-In page; it reloads on success."""
    from web_app.handlers.auth_admin import _AUTH_LOGIN_PAGE

    core = _core()
    client_id = getattr(core, "GOOGLE_CLIENT_ID", "") or ""
    if not client_id:
        return _page(
            "Sign in",
            "<p class='muted'>Sign-in is not configured on this deployment. "
            "Use the local dev sign-in, then reopen this link.</p>",
        )
    page = _AUTH_LOGIN_PAGE.replace("__GOOGLE_CLIENT_ID__", _escape(client_id))
    return web.Response(
        text=page,
        content_type="text/html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _agent_options(user_id: str) -> list[dict]:
    """Browsers this user may hand to a connector.

    Live agents come from the relay and carry the profile name the user sees in
    their agent list; keys that are not currently connected are still offered so
    a connector can be set up before the machine is online.
    """
    from mcp_oauth_store import agent_id_for_key

    core = _core()
    keys = core._auth.get_keys_for_user(user_id)
    options: dict[str, dict] = {}

    for agent in _live_agents(core, keys):
        agent_id = str(agent.get("agent_id") or "")
        key = _key_for_agent(agent_id, keys)
        if not key:
            continue
        options[agent_id] = {
            "agent_id": agent_id,
            "profile": str(agent.get("profile") or "default"),
            "key": key,
            "connected": True,
        }

    for key in keys:
        agent_id = agent_id_for_key(key)
        options.setdefault(
            agent_id,
            {"agent_id": agent_id, "profile": "default", "key": key, "connected": False},
        )

    return sorted(options.values(), key=lambda o: (not o["connected"], o["profile"]))


def _live_agents(core, keys: list[str]) -> list[dict]:
    """Best-effort relay query; consent must still render when it fails."""
    if not keys:
        return []
    import httpx

    relay = os.environ.get("RELAY_INTERNAL_URL", "ws://relay:8765")
    parsed = urlparse(relay)
    host = parsed.hostname or "relay"
    port = parsed.port or 8765
    scheme = "https" if port == 443 else "http"
    port_part = "" if port in (443, 80) else f":{port}"
    url = f"{scheme}://{host}{port_part}/api/agents"
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {keys[0]}"})
            if not resp.is_success:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _key_for_agent(agent_id: str, keys: list[str]) -> str:
    for key in keys:
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:8]
        if agent_id.startswith(f"claude-{key_hash}") or agent_id.startswith(f"headless-{key_hash}"):
            return key
    return ""


# -- GET /oauth/consent ----------------------------------------------------


async def handle_mcp_oauth_consent(request: web.Request) -> web.Response:
    if not _oauth_enabled():
        return web.Response(text="OAuth is not enabled.", status=404)

    request_id = (request.query.get("request") or "").strip()
    if not request_id:
        return _page(
            "Invalid link",
            "<p class='muted'>This link is missing its request id.</p>",
            status=400,
        )

    store = _store()
    pending = store.pending_request(request_id)
    if pending is None:
        return _page(
            "Link expired",
            "<p class='muted'>This authorization request expired or was already used. "
            "Return to your assistant and try connecting again.</p>",
            status=400,
        )

    user = _signed_in(request)
    if user is None:
        return _login_page(request)

    client = store.get_client(pending["client_id"])
    if client is None:
        return _page(
            "App no longer available",
            "<p class='muted'>This app is no longer registered. Ask your assistant "
            "to connect again.</p>",
            status=400,
        )
    status = client_callback_status(client, store.list_policies())
    if status == "blocked":
        return _page(
            "Callback address not approved",
            "<p class='muted'>This app wants to send you back to an address Unchained "
            "does not approve for automatic registration. Nothing was granted. "
            "If this is unexpected, contact the operator before trying again.</p>",
            status=403,
        )

    options = _agent_options(user["user_id"])
    if not options:
        return _page(
            "Connect a browser first",
            "<p class='muted'>No browser is connected to this account yet. Install the "
            "Unchained agent and connect a Chrome profile, then retry from your assistant.</p>",
        )

    csrf = secrets.token_urlsafe(24)
    callback = pending["redirect_uri"]
    callback_host = urlparse(callback).netloc

    rows = []
    for opt in options:
        state = "" if opt["connected"] else " <span class='muted'>(not connected right now)</span>"
        rows.append(
            "<label class='agent'>"
            f"<input type='radio' name='agent_id' value='{_escape(opt['agent_id'])}'"
            f" required>"
            f"<span><strong>{_escape(opt['profile'])}</strong>"
            f"<code class='subtle'>{_escape(opt['agent_id'])}</code>{state}</span>"
            "</label>"
        )

    policy_label = (
        "Callback registered by an Unchained administrator."
        if status == "manual"
        else "Callback matches Unchained's allowed destinations."
    )

    body = f"""
<h1>Connect {_escape(client.get('client_name') or 'an app')}</h1>
<p class='muted'>
  <strong>{_escape(client.get('client_name') or client.get('client_id') or 'An app')}</strong>
  wants to control one browser in your Unchained account.
  After you approve, it will be sent back to <strong>{_escape(callback_host)}</strong>.
</p>
<p class='muted small'>
  The app name above was supplied by the app. {_escape(policy_label)}
</p>
<p class='muted small'>
  Callback: <code class='wrap'>{_escape(callback)}</code>
</p>
{"" if status == "manual" else "<p class='note warn'>Unverified connector. Its name was supplied by the app, and this callback does not tell Unchained which account will receive access. Only continue if you started this connection yourself in the app.</p>"}
<p class='note'>
  While connected it can read pages, click, type, run script and read cookies in
  the browser you choose below. You can disconnect it any time from
  <a href="/oauth/connections">Connected apps</a>.
</p>
<form method="post" action="/oauth/consent">
  <input type="hidden" name="request" value="{_escape(request_id)}">
  <input type="hidden" name="csrf" value="{_escape(csrf)}">
  <fieldset>
    <legend>Choose the browser it may drive</legend>
    {''.join(rows)}
  </fieldset>
  <div class='actions'>
    <button type="submit" name="decision" value="approve" class='primary'>Allow</button>
    <button type="submit" name="decision" value="deny">Deny</button>
  </div>
</form>
"""
    response = _page(f"Connect {client.get('client_name') or 'app'}", body)
    _set_cookie(response, request, csrf)
    return response


# -- POST /oauth/consent ---------------------------------------------------


async def handle_mcp_oauth_consent_post(request: web.Request) -> web.Response:
    if not _oauth_enabled():
        return web.Response(text="OAuth is not enabled.", status=404)

    user = _signed_in(request)
    if user is None:
        return web.Response(text="Sign in required.", status=401)

    form = await request.post()
    request_id = str(form.get("request") or "").strip()
    decision = str(form.get("decision") or "").strip()
    agent_id = str(form.get("agent_id") or "").strip()
    csrf = str(form.get("csrf") or "").strip()

    if not _csrf_ok(request, csrf):
        return _page(
            "Session expired",
            "<p class='muted'>This page expired. Return to your assistant and try again.</p>",
            status=400,
        )

    store = _store()
    pending = store.pending_request(request_id)
    if pending is None:
        return _page(
            "Link expired",
            "<p class='muted'>This authorization request expired or was already used.</p>",
            status=400,
        )

    client = store.get_client(pending["client_id"])
    if client is None or client_callback_status(
        client, store.list_policies()
    ) == "blocked":
        return _page(
            "App no longer approved",
            "<p class='muted'>This app's callback address is no longer approved. "
            "Nothing was granted.</p>",
            status=403,
        )

    if decision != "approve":
        store.deny_request(request_id, user["user_id"])
        return _redirect_denied(pending)

    options = {opt["agent_id"]: opt for opt in _agent_options(user["user_id"])}
    chosen = options.get(agent_id)
    if chosen is None:
        return _page(
            "Unknown browser",
            "<p class='muted'>That browser is not available on this account. Try again.</p>",
            status=400,
        )

    from mcp_oauth_store import fingerprint_key

    grant_id, ticket = store.approve_request(
        request_id=request_id,
        user_id=user["user_id"],
        agent_id=chosen["agent_id"],
        key_hash=hashlib.sha256(chosen["key"].encode()).hexdigest()[:8],
        key_fingerprint=fingerprint_key(chosen["key"]),
        scopes=pending["scopes"],
        resource=pending["resource"],
    )

    url = f"{_issuer()}/oauth/complete?{urlencode({'ticket': ticket})}"
    response = web.HTTPFound(url)
    _clear_cookie(response)
    return response


# -- connected apps --------------------------------------------------------


async def handle_mcp_oauth_connections(request: web.Request) -> web.Response:
    if not _oauth_enabled():
        return web.Response(text="OAuth is not enabled.", status=404)
    user = _signed_in(request)
    if user is None:
        return _login_page(request)

    grants = _store().list_grants_for_user(user["user_id"])
    csrf = secrets.token_urlsafe(24)
    if not grants:
        body = "<h1>Connected apps</h1><p class='muted'>No app is connected.</p>"
    else:
        rows = []
        for grant in grants:
            when = time.strftime("%Y-%m-%d", time.gmtime(grant["created_at"]))
            rows.append(
                "<div class='grant'>"
                f"<div><strong>{_escape(grant['client_name'])}</strong>"
                f"<code class='subtle'>{_escape(agent_label(grant['agent_id']))}</code>"
                f"<span class='muted'>connected {_escape(when)}</span></div>"
                "<form method='post' action='/oauth/connections/revoke'>"
                f"<input type='hidden' name='grant' value='{_escape(grant['id'])}'>"
                f"<input type='hidden' name='csrf' value='{_escape(csrf)}'>"
                "<button type='submit'>Disconnect</button>"
                "</form></div>"
            )
        body = "<h1>Connected apps</h1>" + "".join(rows)
    response = _page("Connected apps", body)
    _set_cookie(response, request, csrf)
    return response


async def handle_mcp_oauth_connections_revoke(request: web.Request) -> web.Response:
    if not _oauth_enabled():
        return web.Response(text="OAuth is not enabled.", status=404)
    user = _signed_in(request)
    if user is None:
        return web.Response(text="Sign in required.", status=401)

    form = await request.post()
    grant_id = str(form.get("grant") or "").strip()
    csrf = str(form.get("csrf") or "").strip()
    if not _csrf_ok(request, csrf):
        return _page(
            "Session expired",
            "<p class='muted'>This page expired. Open Connected apps again.</p>",
            status=400,
        )
    if grant_id:
        _store().revoke_grant(grant_id, user_id=user["user_id"])
    response = web.HTTPFound("/oauth/connections")
    _clear_cookie(response)
    return response


# -- GET /oauth/error ------------------------------------------------------


async def handle_mcp_oauth_error(request: web.Request) -> web.Response:
    """Issuer-owned error landing page.

    Used instead of redirecting to a callback that is no longer approved, so an
    attacker cannot turn our error path into a code-delivery mechanism.
    """
    reason = (request.query.get("reason") or "").strip()
    messages = {
        "client_not_approved": (
            "This app's callback address is not approved, so nothing was granted. "
            "Ask the assistant to try again later, or contact the operator."
        ),
    }
    text = messages.get(reason, "This connection could not be completed.")
    return _page("Connection not allowed", f"<p class='muted'>{_escape(text)}</p>", status=400)


# -- helpers ---------------------------------------------------------------


def _oauth_enabled() -> bool:
    return os.environ.get("MCP_OAUTH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _redirect_denied(pending: dict) -> web.Response:
    params = {"error": "access_denied"}
    if pending.get("state"):
        params["state"] = pending["state"]
    sep = "&" if "?" in pending["redirect_uri"] else "?"
    return web.HTTPFound(f"{pending['redirect_uri']}{sep}{urlencode(params)}")


def _set_cookie(response: web.Response, request: web.Request, value: str) -> None:
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").strip().lower()
    secure = request.secure or forwarded == "https"
    response.set_cookie(
        _CSRF_COOKIE,
        value,
        max_age=_CSRF_MAX_AGE,
        path="/oauth",
        httponly=True,
        samesite="Lax",
        secure=secure,
    )


def _clear_cookie(response: web.Response) -> None:
    response.del_cookie(_CSRF_COOKIE, path="/oauth")


def _csrf_ok(request: web.Request, value: str) -> bool:
    if not value:
        return False
    cookie = request.cookies.get(_CSRF_COOKIE) or ""
    return bool(cookie) and secrets.compare_digest(cookie, value)


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Unchained</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{min-height:100vh;background:#0d1117;color:#c9d1d9;
       font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex;justify-content:center;padding:3rem 1rem}}
  main{{width:100%;max-width:560px}}
  h1{{font-size:1.3rem;margin-bottom:.75rem}}
  p{{font-size:.9rem;line-height:1.5;margin-bottom:1rem}}
  .muted{{color:#8b949e}}
  .small{{font-size:.8rem}}
  .subtle{{color:#6e7681;font-size:.7rem}}
  .wrap{{white-space:pre-wrap;word-break:break-all;font-size:.72rem}}
  a{{color:#58a6ff}}
  .note{{font-size:.8rem;color:#8b949e;border-left:2px solid #30363d;padding-left:.75rem}}
  .note.warn{{color:#d29922;border-left-color:#d29922}}
  code{{font-family:ui-monospace,SFMono-Regular,monospace;font-size:.78rem;color:#8b949e;
        display:block;margin-top:.2rem}}
  fieldset{{border:1px solid #30363d;border-radius:10px;padding:1rem;margin:1rem 0}}
  legend{{font-size:.8rem;color:#8b949e;padding:0 .4rem}}
  .agent{{display:flex;gap:.6rem;align-items:flex-start;padding:.55rem 0;cursor:pointer}}
  .agent strong{{font-size:.9rem;font-weight:600}}
  .actions{{display:flex;gap:.75rem;margin-top:1rem}}
  button{{font:inherit;font-size:.9rem;padding:.55rem 1.1rem;border-radius:8px;cursor:pointer;
          border:1px solid #30363d;background:#161b22;color:#c9d1d9}}
  button.primary{{background:#238636;border-color:#2ea043;color:#fff}}
  .grant{{display:flex;justify-content:space-between;align-items:center;gap:1rem;
          border:1px solid #30363d;border-radius:10px;padding:1rem;margin-bottom:.75rem}}
  .grant span.muted{{display:block;font-size:.78rem;margin-top:.25rem}}
</style>
</head>
<body><main>{body}</main></body>
</html>
"""
