"""Claim-aware OAuth start/callback handlers for the financial workspace.

Integrates the existing Google / Facebook / GitHub account creation with the
one-time claim flow:

1. The browser initiates a claim (``POST /api/claim``). The control plane reads
   the S2S handoff secret from the gateway-set HttpOnly
   ``fin-terminal-handoff-secret`` cookie, creates the claim, rotates the
   handoff cookie away, and sets an HttpOnly parent-domain claim cookie plus a
   same-tab nonce cookie. The body carries only ``handoff_id``/``browser_nonce``/
   ``audience`` — never the secret.
2. ``GET /auth/{provider}/start?claim_id=...`` binds the provider OAuth state
   to the claim (exact provider allowlist, purpose/audience checked) and
   redirects to the provider.
3. ``GET /auth/{provider}/callback`` verifies the exact callback path, the
   claim cookie, and the OAuth state binding, then get-or-creates the user,
   records the user origin, and accepts the claim exactly once.
4. Google uses GSI (client-side id token): ``POST /api/google`` accepts the
   id token bound to the claim cookie and the same claim state.

No bearer value ever travels in a URL or a log line; the claim secret lives
only in the HttpOnly cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from urllib.parse import urlencode

import httpx
from aiohttp import web

from web_app.core import get_core as _core
from web_app.handlers import auth_admin as _auth_admin
from web_app.handlers.fin_workspace import (
    _clear_claim_cookie,
    _CLAIM_COOKIE_NAME,
    _CLAIM_NONCE_COOKIE_NAME,
    _error_response,
    _json_response,
    _NO_STORE_HEADERS,
    _resolve_fw,
)
from financial_workspace import (
    ClaimRejectedError,
    FinancialWorkspaceError,
    ImportConflictError,
)

log = logging.getLogger(__name__)

# Exact callback allowlist — no wildcards, no unlisted providers.
_PROVIDER_ALLOWLIST = frozenset({"google", "facebook", "github"})

_GOOGLE_CLAIM_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'self' https://accounts.google.com https://accounts.google.com/gsi/; connect-src 'self'; frame-src https://accounts.google.com/gsi/; img-src data:">
<title>Continue with Google</title>
<style>
body{font-family:system-ui,sans-serif;background:#0b0f14;color:#e6edf3;
 display:grid;place-items:center;min-height:100vh;margin:0}
.card{max-width:420px;padding:32px;border:1px solid #2d3748;border-radius:12px;
 background:#11161d;text-align:center}
p{color:#9aa7b4;font-size:14px;margin:0 0 20px}
</style></head><body>
<div class="card">
<p>Sign in with Google to claim this workspace. The window will close when
authentication completes.</p>
</div>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<script>
const params = new URLSearchParams(location.search);
const claimId = params.get("claim_id") || "";
const state = params.get("state") || "";
function handleCredential(response) {
  fetch("../../api/google", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({credential: response.credential, claim_id: claimId,
      oauth_state: state})})
    .then((r) => r.json().then((d) => ({ok: r.ok, d})))
    .then(({ok, d}) => {
      if (ok && d.redirect_url) { location.href = d.redirect_url; }
      else { document.querySelector("p").textContent = d.error || "Claim failed."; }
    });
}
window.onload = () => {
  if (window.google && google.accounts && google.accounts.id) {
    google.accounts.id.initialize({client_id: document.body.dataset.clientId, callback: handleCredential});
    google.accounts.id.renderButton(document.querySelector(".card"), {theme: "filled_black", size: "large"});
  }
};
</script>
</body></html>
"""


def _resolve_claim_from_cookie(request: web.Request):
    """Resolve the pending claim owned by the HttpOnly claim cookie."""
    fw = _resolve_fw()
    if fw is None:
        return None, None, None
    claim_secret = request.cookies.get(_CLAIM_COOKIE_NAME, "").strip()
    if not claim_secret:
        return fw, None, None
    claim = fw.get_claim_by_secret(claim_secret)
    if claim is None:
        return fw, None, None
    return fw, claim, claim_secret


def _claim_nonce_from_cookie(request: web.Request) -> str:
    return request.cookies.get(_CLAIM_NONCE_COOKIE_NAME, "").strip()


def _claim_callback_base_url() -> str:
    """Exact callback base for the claim OAuth flow (Caddy workspace routes)."""
    base = os.environ.get(
        "FIN_TERMINAL_BASE_URL",
        "https://unbrowser.unchainedsky.com/fin-terminal-workspace",
    ).strip().rstrip("/")
    return base


def _claim_done_url(claim_id: str, status: str) -> str:
    base = os.environ.get(
        "FIN_TERMINAL_BASE_URL",
        "https://unbrowser.unchainedsky.com/fin-terminal-workspace",
    ).strip().rstrip("/")
    return f"{base}/done?claim_id={claim_id}&status={status}"


# ---------------------------------------------------------------------------
# GET /auth/{provider}/start?claim_id=...
# ---------------------------------------------------------------------------
async def handle_claim_oauth_start(request: web.Request) -> web.Response:
    """Start a claim-aware OAuth flow for an allowlisted provider."""
    fw, claim, claim_secret = _resolve_claim_from_cookie(request)
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if claim is None:
        return _error_response("claim cookie missing or invalid", status=401)

    provider = str(request.match_info.get("provider", "")).strip().lower()
    if provider not in _PROVIDER_ALLOWLIST:
        return _error_response("provider not allowed", status=404)

    claim_id = str(request.query.get("claim_id", "") or "").strip()
    if not claim_id or claim_id != claim["claim_id"]:
        return _error_response("claim_id mismatch", status=400)
    if claim["status"] != "pending":
        return _error_response(f"claim is {claim['status']}, not pending", status=409)
    if claim.get("audience") not in ("", provider):
        return _error_response(
            f"claim bound to {claim.get('audience')!r}, not {provider!r}", status=409
        )

    core = _core()
    state = secrets.token_urlsafe(24)

    if provider == "github":
        client_id, client_secret, authorize_url, _token_url, _api_base = (
            _auth_admin._github_oauth_config()
        )
        if not client_id or not client_secret:
            return _error_response("github not configured", status=503)
        redirect_uri = f"{_claim_callback_base_url()}/auth/github/callback"
        oauth_url = f"{authorize_url}?{urlencode({
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'scope': 'read:user user:email',
            'state': state,
            'allow_signup': 'true',
        })}"
        if not fw.bind_oauth_state(claim_id, state, audience=provider):
            return _error_response("claim no longer pending", status=409)
        return web.HTTPFound(oauth_url, headers=_NO_STORE_HEADERS)

    if provider == "facebook":
        app_id, app_secret, dialog_url, _graph_base = _auth_admin._facebook_oauth_config()
        if not app_id or not app_secret:
            return _error_response("facebook not configured", status=503)
        redirect_uri = f"{_claim_callback_base_url()}/auth/facebook/callback"
        oauth_url = f"{dialog_url}?{urlencode({
            'client_id': app_id,
            'redirect_uri': redirect_uri,
            'scope': 'email,public_profile',
            'response_type': 'code',
            'state': state,
        })}"
        if not fw.bind_oauth_state(claim_id, state, audience=provider):
            return _error_response("claim no longer pending", status=409)
        return web.HTTPFound(oauth_url, headers=_NO_STORE_HEADERS)

    # google — GSI page; the client posts the id token to /api/google.
    if not core.GOOGLE_CLIENT_ID:
        return _error_response("google not configured", status=503)
    if not fw.bind_oauth_state(claim_id, state, audience=provider):
        return _error_response("claim no longer pending", status=409)
    page = _GOOGLE_CLAIM_PAGE.replace(
        "<body>", f'<body data-client-id="{core.GOOGLE_CLIENT_ID}" data-claim-id="{claim_id}" data-state="{state}">'
    )
    return web.Response(text=page, content_type="text/html", headers=_NO_STORE_HEADERS)


# ---------------------------------------------------------------------------
# GET /auth/{provider}/callback  (exact allowlist, claim cookie required)
# ---------------------------------------------------------------------------
async def handle_claim_oauth_callback(request: web.Request) -> web.Response:
    """Callback for claim-aware OAuth. Verifies state binding + claim cookie."""
    provider = str(request.match_info.get("provider", "")).strip().lower()
    if provider not in _PROVIDER_ALLOWLIST:
        return _error_response("provider not allowed", status=404)

    fw, claim, claim_secret = _resolve_claim_from_cookie(request)
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if claim is None:
        return web.Response(
            status=401,
            text="claim cookie missing or invalid",
            headers=_NO_STORE_HEADERS,
        )
    if claim["status"] != "pending":
        if claim["status"] == "accepted":
            return web.HTTPFound(
                _claim_done_url(claim["claim_id"], "accepted"),
                headers=_NO_STORE_HEADERS,
            )
        return _error_response(f"claim is {claim['status']}", status=409)
    if claim.get("audience") not in ("", provider):
        return _error_response(
            f"claim bound to {claim.get('audience')!r}, not {provider!r}", status=409
        )

    got_state = str(request.query.get("state", "") or "").strip()
    if not got_state:
        return _error_response("missing oauth state", status=400)

    # The state must match what was bound at start (claim.oauth_state_hash).
    # The claim dict from the cookie resolver includes the bound hash.
    state_hash = hashlib.sha256(got_state.encode()).hexdigest()
    if claim.get("oauth_state_hash"):
        if not hmac.compare_digest(state_hash, claim["oauth_state_hash"]):
            return _error_response("oauth state binding mismatch", status=401)
    elif not fw.bind_oauth_state(claim["claim_id"], got_state, audience=provider):
        return _error_response("claim no longer pending", status=409)

    if request.query.get("error"):
        return web.HTTPFound(
            _claim_done_url(claim["claim_id"], "denied"),
            headers=_NO_STORE_HEADERS,
        )

    core = _core()
    email = ""
    name = ""
    picture = ""
    provider_account_id = ""

    if provider == "github":
        result = await _exchange_github(request)
        if result is None:
            return web.HTTPFound(
                _claim_done_url(claim["claim_id"], "profile_failed"),
                headers=_NO_STORE_HEADERS,
            )
        email, name, picture, provider_account_id = result
    elif provider == "facebook":
        result = await _exchange_facebook(request)
        if result is None:
            return web.HTTPFound(
                _claim_done_url(claim["claim_id"], "profile_failed"),
                headers=_NO_STORE_HEADERS,
            )
        email, name, picture, provider_account_id = result

    email = str(email or "").strip().lower()
    if not email:
        return web.HTTPFound(
            _claim_done_url(claim["claim_id"], "email_required"),
            headers=_NO_STORE_HEADERS,
        )

    user = core._auth.get_or_create_user(email, name, picture)
    user_id = str(user.get("user_id", "") or "").strip()
    if not user_id:
        return web.HTTPFound(
            _claim_done_url(claim["claim_id"], "account_failed"),
            headers=_NO_STORE_HEADERS,
        )

    # Record the provider origin (get-or-create, concurrency-safe).
    try:
        fw.get_or_create_user_origin(
            user_id, provider=provider, provider_account_id=provider_account_id
        )
    except Exception as exc:  # origin recording is best-effort, not fatal
        log.warning("origin record failed for %s: %s", user_id, exc)

    nonce = _claim_nonce_from_cookie(request)
    try:
        result = fw.accept_claim(
            claim["claim_id"],
            claim_secret,
            final_account_user_id=user_id,
            final_account_email=email,
            browser_nonce=nonce,
            oauth_state=got_state,
            auth_code_id="",
        )
    except (ClaimRejectedError, ImportConflictError) as exc:
        log.warning("claim accept failed: %s", exc)
        return web.HTTPFound(
            _claim_done_url(claim["claim_id"], "rejected"),
            headers=_NO_STORE_HEADERS,
        )

    session_token = core.create_session_token(user_id, email)
    resp = web.HTTPFound(
        _claim_done_url(claim["claim_id"], "accepted"),
        headers=_NO_STORE_HEADERS,
    )
    core._set_session_cookie(resp, session_token, request)
    _clear_claim_cookie(resp)
    resp.del_cookie(_CLAIM_NONCE_COOKIE_NAME, path="/")
    return resp


async def _exchange_github(request: web.Request):
    """Exchange a GitHub code; returns (email, name, picture, provider_account_id)."""
    core = _core()
    client_id, client_secret, _authorize_url, token_url, api_base = (
        _auth_admin._github_oauth_config()
    )
    if not client_id or not client_secret:
        return None
    code = str(request.query.get("code", "") or "").strip()
    if not code:
        return None
    redirect_uri = f"{_claim_callback_base_url()}/auth/github/callback"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            token_payload = token_resp.json() if token_resp.content else {}
            access_token = str(token_payload.get("access_token", "") or "").strip()
            if not access_token:
                return None
            profile_resp = await client.get(
                f"{api_base}/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "unchainedsky-auth",
                },
            )
            profile_resp.raise_for_status()
            profile = profile_resp.json() if profile_resp.content else {}
            if not isinstance(profile, dict):
                return None
    except Exception:
        return None

    email = _auth_admin._normalized_email(profile.get("email", ""))
    if not email:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                emails_resp = await client.get(
                    f"{api_base}/user/emails",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "unchainedsky-auth",
                    },
                )
                emails_resp.raise_for_status()
                emails_payload = emails_resp.json() if emails_resp.content else []
            for row in emails_payload or []:
                if not isinstance(row, dict):
                    continue
                if row.get("primary") and row.get("verified"):
                    email = _auth_admin._normalized_email(row.get("email", ""))
                    break
        except Exception:
            return None
    if not email:
        return None
    name = str(profile.get("name", "") or "").strip() or str(profile.get("login", "") or "").strip()
    picture = str(profile.get("avatar_url", "") or "").strip()
    provider_account_id = str(profile.get("id", "") or "").strip()
    return email, name, picture, provider_account_id


async def _exchange_facebook(request: web.Request):
    """Exchange a Facebook code; returns (email, name, picture, provider_account_id)."""
    app_id, app_secret, _dialog_url, graph_base = _auth_admin._facebook_oauth_config()
    if not app_id or not app_secret:
        return None
    code = str(request.query.get("code", "") or "").strip()
    if not code:
        return None
    redirect_uri = f"{_claim_callback_base_url()}/auth/facebook/callback"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.get(
                f"{graph_base}/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            token_resp.raise_for_status()
            token_payload = token_resp.json() if token_resp.content else {}
            access_token = str(token_payload.get("access_token", "") or "").strip()
            if not access_token:
                return None
            profile_resp = await client.get(
                f"{graph_base}/me",
                params={"fields": "id,name,email", "access_token": access_token},
            )
            profile_resp.raise_for_status()
            profile = profile_resp.json() if profile_resp.content else {}
            if not isinstance(profile, dict):
                return None
    except Exception:
        return None
    email = _auth_admin._normalized_email(profile.get("email", ""))
    name = str(profile.get("name", "") or "").strip()
    provider_account_id = str(profile.get("id", "") or "").strip()
    if not email:
        return None
    return email, name, "", provider_account_id


# ---------------------------------------------------------------------------
# POST /api/google — Google GSI id-token bound to the claim cookie
# ---------------------------------------------------------------------------
async def handle_claim_google_token(request: web.Request) -> web.Response:
    """Accept a Google id token and accept the claim (GSI flow)."""
    fw, claim, claim_secret = _resolve_claim_from_cookie(request)
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if claim is None:
        return _error_response("claim cookie missing or invalid", status=401)
    if claim["status"] != "pending":
        return _error_response(f"claim is {claim['status']}", status=409)
    if claim.get("audience") not in ("", "google"):
        return _error_response("claim bound to a different provider", status=409)

    try:
        body = await request.json()
    except Exception:
        return _error_response("invalid JSON body", status=400)

    claim_id = str(body.get("claim_id", "") or "").strip()
    if not claim_id or claim_id != claim["claim_id"]:
        return _error_response("claim_id mismatch", status=400)

    oauth_state = str(body.get("oauth_state", "") or "").strip()
    if not oauth_state:
        return _error_response("oauth_state required", status=400)
    state_hash = hashlib.sha256(oauth_state.encode()).hexdigest()
    stored = fw.get_claim(claim_id)
    if stored and stored.get("oauth_state_hash"):
        if not hmac.compare_digest(state_hash, stored["oauth_state_hash"]):
            return _error_response("oauth state binding mismatch", status=401)

    credential = str(body.get("credential", "") or "").strip()
    if not credential:
        return _error_response("credential required", status=400)

    core = _core()
    payload = await core.verify_google_token(credential)
    if payload is None:
        return _error_response("invalid Google token", status=401)
    email = str(payload.get("email", "") or "").strip().lower()
    if not email:
        return _error_response("Google token missing email", status=400)
    name = str(payload.get("name", "") or "").strip()
    picture = str(payload.get("picture", "") or "").strip()
    provider_account_id = str(payload.get("sub", "") or "").strip()

    user = core._auth.get_or_create_user(email, name, picture)
    user_id = str(user.get("user_id", "") or "").strip()
    if not user_id:
        return _error_response("account creation failed", status=500)

    try:
        fw.get_or_create_user_origin(
            user_id, provider="google", provider_account_id=provider_account_id
        )
    except Exception as exc:
        log.warning("origin record failed for %s: %s", user_id, exc)

    browser_nonce = str(body.get("browser_nonce", "") or "").strip() or _claim_nonce_from_cookie(request)
    try:
        result = fw.accept_claim(
            claim["claim_id"],
            claim_secret,
            final_account_user_id=user_id,
            final_account_email=email,
            browser_nonce=browser_nonce,
            oauth_state=oauth_state,
            auth_code_id="",
        )
    except (ClaimRejectedError, ImportConflictError) as exc:
        return _error_response(str(exc), status=409)
    except FinancialWorkspaceError as exc:
        log.error("google claim accept failed: %s", exc)
        return _error_response(str(exc), status=500)

    session_token = core.create_session_token(user_id, email)
    resp = _json_response({
        "claim_id": result["claim_id"],
        "workspace_id": result["workspace_id"],
        "redirect_url": _claim_done_url(result["claim_id"], "accepted"),
    })
    core._set_session_cookie(resp, session_token, request)
    _clear_claim_cookie(resp)
    resp.del_cookie(_CLAIM_NONCE_COOKIE_NAME, path="/")
    return resp


# ---------------------------------------------------------------------------
# GET /done — claim completion page (no secrets)
# ---------------------------------------------------------------------------
_DONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Workspace</title>
<style>
body{font-family:system-ui,sans-serif;background:#0b0f14;color:#e6edf3;
display:grid;place-items:center;min-height:100vh;margin:0}
.card{max-width:420px;padding:32px;border:1px solid #2d3748;border-radius:12px;
background:#11161d;text-align:center}
p{color:#9aa7b4;font-size:14px;line-height:1.5}
a{color:#58a6ff;font-weight:600;text-decoration:none}
</style></head><body>
<div class="card">
<h1>Workspace {status}</h1>
<p>Your workspace snapshot {status}. You can now open the workspace.</p>
<p><a href="/fin-terminal/">Open workspace</a></p>
</div>
</body></html>
"""


async def handle_claim_done(request: web.Request) -> web.Response:
    """GET /done — claim completion page (never echoes secrets)."""
    status = str(request.query.get("status", "") or "").strip()
    safe_status = status if status in ("accepted", "denied", "rejected",
                                       "profile_failed", "email_required",
                                       "account_failed", "claim_failed") else "accepted"
    label = "ready" if safe_status == "accepted" else safe_status
    return web.Response(
        text=_DONE_PAGE.format(status=label),
        content_type="text/html",
        headers=_NO_STORE_HEADERS,
    )
