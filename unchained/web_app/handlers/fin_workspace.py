"""HTTP handlers for the financial workspace control plane.

Internal API (Docker-internal only; Caddy denies ``/internal/*``):
  POST   /internal/financial-workspace/checkpoints        — create checkpoint (S2S)
  GET    /internal/financial-workspace/checkpoints/{id}    — get checkpoint status
  POST   /internal/financial-workspace/claim               — initiate claim (cookie)
  POST   /internal/financial-workspace/claim/accept        — accept claim (OAuth cb)
  GET    /internal/financial-workspace/claims/{id}         — claim status
  GET    /internal/financial-workspace/workspace           — current user workspace
  GET    /internal/financial-workspace/snapshots           — current user snapshots
  POST   /internal/financial-workspace/effects/process     — process outbox
  POST   /internal/financial-workspace/sweep               — sweep expired checkpoints
  POST   /internal/financial-workspace/runtime/wake        — account runtime wake
  POST   /internal/financial-workspace/runtime/sleep       — account runtime sleep
  GET    /internal/financial-workspace/runtime/status      — account runtime status

Browser routes (proxied by Caddy under ``/fin-terminal-workspace``):
  GET    /auth/claim                    — handoff entry page (no secret in URL)
  POST   /api/claim                     — initiate claim (secret in HttpOnly cookie)
  GET    /api/claims/{claim_id}         — claim status
  GET    /api/workspace                 — current user workspace
  GET    /api/snapshots                 — current user snapshots
  GET    /callback/{provider}           — claim-aware OAuth callback (allowlist)

All internal S2S handlers require the bearer control token
(``Authorization: Bearer <FIN_WORKSPACE_CONTROL_TOKEN>``). The S2S handoff
secret travels only in the gateway-set HttpOnly ``fin-terminal-handoff-secret``
cookie and is read server-side at claim initiation; it never appears in the
browser JS, the POST body, a URL, or a log line. The claim secret is carried
only in an HttpOnly Secure SameSite=Lax parent-domain cookie.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from aiohttp import web

from financial_workspace import (
    FinancialWorkspace,
    FinancialWorkspaceError,
    CheckpointValidationError,
    CheckpointNotFoundError,
    CheckpointStateError,
    ClaimRejectedError,
    ImportConflictError,
    UnauthorizedError,
    is_fin_workspace_enabled,
)
from web_app.core import get_core as _core

log = logging.getLogger(__name__)

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Vary": "Cookie, Authorization",
}

# Control token header name (RFC-compliant Bearer)
_CONTROL_TOKEN_HEADER = "Authorization"

# Claim secret cookie: HttpOnly Secure SameSite=Lax on the parent domain so
# the OAuth callback (same parent domain, any subdomain) can read it. Path=/
# is required for the cookie to be sent on every callback path.
_CLAIM_COOKIE_NAME = "fw_claim_secret"
_CLAIM_NONCE_COOKIE_NAME = "fw_claim_nonce"
_CLAIM_COOKIE_TTL = 3600  # 1 hour (must match claim expiry)

# S2S handoff secret cookie set by the fin-terminal gateway (app repo). The
# control plane reads it SERVER-SIDE from this HttpOnly cookie during claim
# initiation — never from JS, the POST body, or the URL. Host-only: the gateway
# and this control plane share the public terminal host, so a host-only cookie
# with Path=/ is sent on every path of that host, including
# /fin-terminal-workspace/*.
_HANDOFF_COOKIE_NAME = "fin-terminal-handoff-secret"


def _claim_cookie_domain() -> str:
    return os.environ.get("FIN_WORKSPACE_COOKIE_DOMAIN", "").strip()


def _json_response(data, *, status: int = 200, headers: dict | None = None) -> web.Response:
    body = json.dumps(data, separators=(",", ":"), default=str)
    resp_headers = {**_NO_STORE_HEADERS, "Content-Type": "application/json"}
    if headers:
        resp_headers.update(headers)
    return web.Response(status=status, body=body, headers=resp_headers)


def _error_response(message: str, status: int = 400) -> web.Response:
    return _json_response({"error": message}, status=status)


def _extract_handoff_cookie(request: web.Request) -> str:
    """Extract the S2S handoff secret from the gateway-set HttpOnly cookie."""
    return request.cookies.get(_HANDOFF_COOKIE_NAME, "").strip()


def _clear_handoff_cookie(response: web.Response) -> None:
    """Clear the host-only handoff cookie (set by the gateway without Domain)."""
    response.del_cookie(_HANDOFF_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------
def _resolve_fw() -> FinancialWorkspace | None:
    """Resolve the FinancialWorkspace from the running core module."""
    if not is_fin_workspace_enabled():
        return None
    core = _core()
    fw = getattr(core, "_fin_workspace", None)
    if fw is not None:
        return fw
    # Lazy init (fallback for tests/direct invocation; production startup
    # initializes eagerly and fails closed on missing configuration).
    db_path = getattr(core._auth, "db_path", os.environ.get("UNCHAINED_DB_PATH", ""))
    if not db_path:
        return None
    from checkpoint_store import create_checkpoint_store
    store = create_checkpoint_store()
    fw = FinancialWorkspace(db_path, store)
    core._fin_workspace = fw
    return fw


def _verify_control_token(request: web.Request) -> bool:
    """Explicit control token only — never JWT_SECRET or a cookie session."""
    token = getattr(_core(), "_resolve_control_token", None)
    if not callable(token):
        expected = os.environ.get("FIN_WORKSPACE_CONTROL_TOKEN", "").strip()
    else:
        expected = token()
    if not expected:
        return False
    auth = request.headers.get(_CONTROL_TOKEN_HEADER, "")
    if auth.startswith("Bearer "):
        auth = auth[7:]
    return auth == expected


def _extract_claim_cookie(request: web.Request) -> str:
    """Extract the claim secret from the HttpOnly Secure SameSite=Lax cookie."""
    return request.cookies.get(_CLAIM_COOKIE_NAME, "").strip()


def _set_claim_cookie(response: web.Response, secret: str) -> None:
    """Set the parent-domain claim cookie (HttpOnly, Secure, SameSite=Lax)."""
    kwargs: dict = {
        "httponly": True,
        "secure": True,
        "samesite": "Lax",
        "max_age": _CLAIM_COOKIE_TTL,
        "path": "/",
    }
    domain = _claim_cookie_domain()
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(_CLAIM_COOKIE_NAME, secret, **kwargs)


def _clear_claim_cookie(response: web.Response) -> None:
    kwargs: dict = {"path": "/"}
    domain = _claim_cookie_domain()
    if domain:
        kwargs["domain"] = domain
    response.del_cookie(_CLAIM_COOKIE_NAME, **kwargs)


def _set_claim_nonce_cookie(response: web.Response, nonce: str) -> None:
    """Set the same-tab browser nonce cookie (HttpOnly, Secure, SameSite=Lax)."""
    kwargs: dict = {
        "httponly": True,
        "secure": True,
        "samesite": "Lax",
        "max_age": _CLAIM_COOKIE_TTL,
        "path": "/",
    }
    domain = _claim_cookie_domain()
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(_CLAIM_NONCE_COOKIE_NAME, nonce, **kwargs)


def _clear_claim_nonce_cookie(response: web.Response) -> None:
    kwargs: dict = {"path": "/"}
    domain = _claim_cookie_domain()
    if domain:
        kwargs["domain"] = domain
    response.del_cookie(_CLAIM_NONCE_COOKIE_NAME, **kwargs)


# ---------------------------------------------------------------------------
# POST /internal/financial-workspace/checkpoints
# ---------------------------------------------------------------------------
async def handle_fin_workspace_create_checkpoint(request: web.Request) -> web.Response:
    """S2S: create a new checkpoint envelope."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("invalid JSON body", status=400)

    request_id = str(body.get("requestId", "") or "").strip()
    source = body.get("source", {}) or {}
    session_id = str(source.get("sessionId", "") or "").strip()
    worker_id = str(source.get("workerId", "") or "").strip()
    generation = str(source.get("generation", "") or "").strip()
    source_revision = str(source.get("sourceRevision", "") or "").strip()
    checkpoint_raw = body.get("checkpoint")

    # checkpoint must be a JSON object whose serialized bytes are stored
    if isinstance(checkpoint_raw, (dict, list)):
        checkpoint_bytes = json.dumps(checkpoint_raw, separators=(",", ":")).encode("utf-8")
    elif isinstance(checkpoint_raw, str):
        # Already serialized by the caller
        try:
            json.loads(checkpoint_raw)  # validate
            checkpoint_bytes = checkpoint_raw.encode("utf-8")
        except json.JSONDecodeError:
            return _error_response("checkpoint must be valid JSON", status=400)
    else:
        return _error_response("checkpoint must be a JSON object", status=400)

    try:
        result = fw.create_checkpoint(
            request_id=request_id,
            session_id=session_id,
            worker_id=worker_id,
            generation=generation,
            source_revision=source_revision,
            checkpoint=checkpoint_bytes,
        )
    except CheckpointValidationError as e:
        return _error_response(str(e), status=400)
    except FinancialWorkspaceError as e:
        log.error("checkpoint creation failed: %s", e)
        return _error_response(str(e), status=500)

    return _json_response({
        "checkpoint_id": result["checkpoint_id"],
        "expires_at": result["expires_at"],
        "handoff_id": result["handoff_id"],
        "handoff_secret": result["handoff_secret"],
        "auth_url": result["auth_url"],
        "already_exists": result.get("already_exists", False),
        "status": "ready",
    }, status=201 if not result.get("already_exists") else 200)


# ---------------------------------------------------------------------------
# GET /internal/financial-workspace/checkpoints/{checkpoint_id}
# ---------------------------------------------------------------------------
async def handle_fin_workspace_get_checkpoint(request: web.Request) -> web.Response:
    """Get checkpoint status."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)

    checkpoint_id = request.match_info.get("checkpoint_id", "")
    chk = fw.get_checkpoint(checkpoint_id)
    if chk is None:
        return _error_response("checkpoint not found", status=404)

    # Never return handoff secret; it was provided only at creation
    chk.pop("handoff_secret_hash", None)
    chk.pop("handoff_secret", None)

    return _json_response(chk)


# ---------------------------------------------------------------------------
# POST /internal/financial-workspace/claim  (and browser POST /api/claim)
# ---------------------------------------------------------------------------
async def _initiate_claim_impl(request: web.Request) -> web.Response:
    """Initiate a one-time claim for a handoff.

    The S2S handoff secret is read SERVER-SIDE from the gateway-set HttpOnly
    ``fin-terminal-handoff-secret`` cookie — never from JS, the POST body, or
    the URL. The browser only supplies ``handoff_id`` (the same opaque value
    already in the URL), a same-tab ``browser_nonce``, and the provider
    ``audience``.

    On success: rotates the handoff cookie away (cleared), sets the HttpOnly
    parent-domain ``fw_claim_secret`` claim cookie plus the same-tab nonce
    cookie, and returns ``claim_id`` and the OAuth start URL.
    """
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("invalid JSON body", status=400)

    handoff_id = str(body.get("handoff_id", "") or "").strip()
    browser_nonce = str(body.get("browser_nonce", "") or "").strip()
    audience = str(body.get("audience", "") or "").strip().lower()

    if body.get("handoff_secret"):
        # Never accept the handoff secret from JS/body: it is an HttpOnly
        # cookie capability. Reject loudly so a leaky client cannot work.
        return _error_response("handoff_secret must not be sent in the body", status=400)

    handoff_secret = _extract_handoff_cookie(request)
    if not handoff_secret:
        return _error_response("handoff cookie missing", status=401)

    if audience not in ("google", "facebook", "github"):
        return _error_response("audience must be one of: google, facebook, github", status=400)

    try:
        result = fw.initiate_claim(
            handoff_id=handoff_id,
            handoff_secret=handoff_secret,
            browser_nonce=browser_nonce,
            audience=audience,
        )
    except (CheckpointNotFoundError, CheckpointStateError, ClaimRejectedError) as e:
        return _error_response(str(e), status=400)
    except UnauthorizedError as e:
        return _error_response(str(e), status=401)
    except FinancialWorkspaceError as e:
        log.error("claim initiation failed: %s", e)
        return _error_response(str(e), status=500)

    # Set the claim secret cookie (HttpOnly parent-domain, never in URL/logs)
    response = _json_response({
        "claim_id": result["claim_id"],
        "checkpoint_id": result["checkpoint_id"],
        "expires_at": result["expires_at"],
        "oauth_start_url": _claim_oauth_start_url(result["claim_id"], audience),
    }, status=201)
    # Rotate the S2S handoff cookie: it has served its purpose and must not
    # linger past claim initiation.
    _clear_handoff_cookie(response)
    _set_claim_cookie(response, result["claim_secret"])
    if browser_nonce:
        _set_claim_nonce_cookie(response, browser_nonce)
    return response


async def handle_fin_workspace_claim(request: web.Request) -> web.Response:
    """POST /internal/financial-workspace/claim (internal S2S-adjacent)."""
    return await _initiate_claim_impl(request)


async def handle_fin_workspace_browser_claim(request: web.Request) -> web.Response:
    """POST /api/claim — browser claim initiation under /fin-terminal-workspace."""
    return await _initiate_claim_impl(request)


def _claim_oauth_start_url(claim_id: str, audience: str) -> str:
    base = os.environ.get(
        "FIN_TERMINAL_BASE_URL",
        "https://unbrowser.unchainedsky.com/fin-terminal-workspace",
    ).strip().rstrip("/")
    return f"{base}/auth/{audience}/start?claim_id={claim_id}"


# ---------------------------------------------------------------------------
# GET /auth/claim — browser handoff entry page (no secret in URL)
# ---------------------------------------------------------------------------
_CLAIM_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'">
<title>Workspace handoff</title>
<style>
body{font-family:system-ui,sans-serif;background:#0b0f14;color:#e6edf3;
 display:grid;place-items:center;min-height:100vh;margin:0}
.card{max-width:420px;padding:32px;border:1px solid #2d3748;border-radius:12px;
 background:#11161d;text-align:center}
h1{font-size:18px;margin:0 0 8px}
p{color:#9aa7b4;font-size:14px;line-height:1.5;margin:0 0 20px}
button{background:#238636;color:#fff;border:0;padding:10px 18px;border-radius:8px;
 font-size:14px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
</style></head><body>
<div class="card">
<h1>Claim your workspace</h1>
<p>This handoff was issued for this browser session. Choose an account provider
to sign in and import your workspace snapshot.</p>
<div id="providers" style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
<button data-provider="google">Continue with Google</button>
<button data-provider="facebook">Continue with Facebook</button>
<button data-provider="github">Continue with GitHub</button>
</div>
<p id="err" style="display:none;color:#f85149"></p>
</div>
<script>
const handoffId = new URLSearchParams(location.search).get("handoff_id");
if (!handoffId) { document.getElementById("err").style.display = "block";
  document.getElementById("err").textContent = "Missing handoff id."; }
/* The S2S handoff secret is held in the HttpOnly fin-terminal-handoff-secret
   cookie set by the gateway; it never reaches this page's JS. The server
   reads it from the cookie when this POST is processed, then rotates it away. */
document.querySelectorAll("button[data-provider]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const audience = btn.dataset.provider;
    btn.disabled = true;
    try {
      const res = await fetch("../../api/claim", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({handoff_id: handoffId,
          browser_nonce: crypto.randomUUID(), audience})});
      if (!res.ok) throw new Error("claim initiation failed");
      const data = await res.json();
      location.href = data.oauth_start_url;
    } catch (e) {
      document.getElementById("err").style.display = "block";
      document.getElementById("err").textContent = e.message || "Failed to start claim.";
      btn.disabled = false;
    }
  });
});
</script>
</body></html>
"""


async def handle_fin_workspace_auth_claim_page(request: web.Request) -> web.Response:
    """GET /auth/claim — serve the handoff entry page (secret never in URL)."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    return web.Response(
        text=_CLAIM_PAGE,
        content_type="text/html",
        headers={**_NO_STORE_HEADERS},
    )


# ---------------------------------------------------------------------------
# POST /internal/financial-workspace/claim/accept
# ---------------------------------------------------------------------------
async def handle_fin_workspace_claim_accept(request: web.Request) -> web.Response:
    """Accept a claim after OAuth callback.

    The claim_secret comes from the HttpOnly cookie (never from the body/URL).
    Body provides: {claim_id, final_account_user_id, final_account_email,
                     browser_nonce, oauth_state, auth_code_id}
    """
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    claim_secret = _extract_claim_cookie(request)
    if not claim_secret:
        return _error_response("claim cookie missing", status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("invalid JSON body", status=400)

    claim_id = str(body.get("claim_id", "") or "").strip()
    final_account_user_id = str(body.get("final_account_user_id", "") or "").strip()
    final_account_email = str(body.get("final_account_email", "") or "").strip()
    browser_nonce = str(body.get("browser_nonce", "") or "").strip()
    oauth_state = str(body.get("oauth_state", "") or "").strip()
    auth_code_id = str(body.get("auth_code_id", "") or "").strip()

    try:
        result = fw.accept_claim(
            claim_id=claim_id,
            claim_secret=claim_secret,
            final_account_user_id=final_account_user_id,
            final_account_email=final_account_email,
            browser_nonce=browser_nonce,
            oauth_state=oauth_state,
            auth_code_id=auth_code_id,
        )
    except (ClaimRejectedError, ImportConflictError) as e:
        return _error_response(str(e), status=409)
    except UnauthorizedError as e:
        return _error_response(str(e), status=401)
    except FinancialWorkspaceError as e:
        log.error("claim acceptance failed: %s", e)
        return _error_response(str(e), status=500)

    resp_headers = {}
    result_resp = {
        "claim_id": result["claim_id"],
        "checkpoint_id": result["checkpoint_id"],
        "workspace_id": result["workspace_id"],
        "user_id": result["user_id"],
        "account_email": result["account_email"],
        "is_new_workspace": result["is_new_workspace"],
        "snapshot_id": result["snapshot_id"],
        "already_accepted": result.get("already_accepted", False),
    }

    response = _json_response(result_resp)

    # Clear the claim cookie after acceptance
    _clear_claim_cookie(response)

    return response


# ---------------------------------------------------------------------------
# GET /internal/financial-workspace/claims/{claim_id}
# ---------------------------------------------------------------------------
async def handle_fin_workspace_get_claim(request: web.Request) -> web.Response:
    """Get claim status."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)

    claim_id = request.match_info.get("claim_id", "")
    claim = fw.get_claim(claim_id)
    if claim is None:
        return _error_response("claim not found", status=404)
    return _json_response(claim)


# ---------------------------------------------------------------------------
# GET /internal/financial-workspace/workspace
# ---------------------------------------------------------------------------
async def handle_fin_workspace_get_workspace(request: web.Request) -> web.Response:
    """Get the workspace for the currently authenticated user."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return _error_response("authentication required", status=401)

    user_id = str(auth_info.get("user_id", "")).strip()
    if not user_id:
        return _error_response("user identity required", status=401)

    ws = fw.get_workspace_for_user(user_id)
    if ws is None:
        return _error_response("no workspace", status=404)

    return _json_response(ws)


# ---------------------------------------------------------------------------
# GET /internal/financial-workspace/snapshots
# ---------------------------------------------------------------------------
async def handle_fin_workspace_get_snapshots(request: web.Request) -> web.Response:
    """Get snapshots for the currently authenticated user."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return _error_response("authentication required", status=401)

    user_id = str(auth_info.get("user_id", "")).strip()
    if not user_id:
        return _error_response("user identity required", status=401)

    ws = fw.get_workspace_for_user(user_id)
    if ws is None:
        return _error_response("no workspace", status=404)

    snapshots = fw.get_snapshots_for_workspace(ws["workspace_id"])
    return _json_response({"workspace_id": ws["workspace_id"], "snapshots": snapshots})


# ---------------------------------------------------------------------------
# POST /internal/financial-workspace/effects/process
# ---------------------------------------------------------------------------
async def handle_fin_workspace_process_effects(request: web.Request) -> web.Response:
    """Process pending outbox effects (called by scheduler or runtime)."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)

    core = _core()
    ledger = getattr(core, "_credit_ledger", None)

    effects = fw.poll_pending_effects(limit=10)
    results = []

    for effect in effects:
        eid = effect["effect_id"]
        if not fw.mark_effect_processing(eid):
            continue
        try:
            if effect["effect_type"] == "account_grant" and ledger:
                grant_result = fw.process_account_grant_effect(effect["context"], ledger)
                fw.mark_effect_completed(eid)
                results.append({"effect_id": eid, "success": True, "result": grant_result})
            elif effect["effect_type"] in ("workspace_upsert", "snapshot_import"):
                # These are handled inline during claim accept; the effect
                # exists for idempotent replay. Mark completed.
                fw.mark_effect_completed(eid)
                results.append({"effect_id": eid, "success": True, "type": effect["effect_type"]})
            else:
                fw.mark_effect_completed(eid, failed=True, error="unknown effect type")
                results.append({"effect_id": eid, "success": False, "error": "unknown type"})
        except Exception as exc:
            fw.mark_effect_completed(eid, failed=True, error=str(exc))
            results.append({"effect_id": eid, "success": False, "error": str(exc)})

    return _json_response({"processed": len(results), "results": results})


# ---------------------------------------------------------------------------
# POST /internal/financial-workspace/sweep
# ---------------------------------------------------------------------------
async def handle_fin_workspace_sweep(request: web.Request) -> web.Response:
    """Sweep expired checkpoints."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)

    count = fw.sweep_expired()
    return _json_response({"expired_count": count})


# ---------------------------------------------------------------------------
# Browser API routes (proxied under /fin-terminal-workspace, prefix stripped)
# ---------------------------------------------------------------------------

def _browser_auth_user_id(request: web.Request) -> str:
    """Resolve the session-authenticated user for browser API reads."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return ""
    return str(auth_info.get("user_id", "")).strip()


async def handle_fin_workspace_browser_get_claim(request: web.Request) -> web.Response:
    """GET /api/claims/{claim_id} — claim status via the HttpOnly claim cookie."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    claim_secret = _extract_claim_cookie(request)
    if not claim_secret:
        return _error_response("claim cookie missing", status=401)
    claim_id = request.match_info.get("claim_id", "")
    claim = fw.get_claim(claim_id)
    if claim is None:
        return _error_response("claim not found", status=404)
    owned = fw.get_claim_by_secret(claim_secret)
    if owned is None or owned["claim_id"] != claim_id:
        return _error_response("claim cookie does not own this claim", status=403)
    return _json_response(claim)


async def handle_fin_workspace_browser_get_workspace(request: web.Request) -> web.Response:
    """GET /api/workspace — workspace for the session-authenticated user."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    user_id = _browser_auth_user_id(request)
    if not user_id:
        return _error_response("authentication required", status=401)
    ws = fw.get_workspace_for_user(user_id)
    if ws is None:
        return _error_response("no workspace", status=404)
    return _json_response(ws)


async def handle_fin_workspace_browser_get_snapshots(request: web.Request) -> web.Response:
    """GET /api/snapshots — snapshots for the session-authenticated user."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    user_id = _browser_auth_user_id(request)
    if not user_id:
        return _error_response("authentication required", status=401)
    ws = fw.get_workspace_for_user(user_id)
    if ws is None:
        return _error_response("no workspace", status=404)
    snapshots = fw.get_snapshots_for_workspace(ws["workspace_id"])
    return _json_response({"workspace_id": ws["workspace_id"], "snapshots": snapshots})


async def handle_fin_workspace_browser_runtime_status(request: web.Request) -> web.Response:
    """GET /api/runtime/status — account-scoped runtime state."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    user_id = _browser_auth_user_id(request)
    if not user_id:
        return _error_response("authentication required", status=401)
    status = fw.runtime_status(user_id)
    if status is None:
        return _error_response("no workspace", status=404)
    return _json_response(status)


# ---------------------------------------------------------------------------
# Account-scoped runtime control (S2S, control-token protected)
# ---------------------------------------------------------------------------

def _runtime_user_id(request: web.Request) -> str:
    try:
        return request.query.get("user_id", "").strip()
    except Exception:
        return ""


async def handle_fin_workspace_runtime_wake(request: web.Request) -> web.Response:
    """POST /internal/financial-workspace/runtime/wake?user_id=..."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)
    user_id = _runtime_user_id(request)
    reason = request.query.get("reason", "")
    if not user_id:
        return _error_response("user_id required", status=400)
    status = fw.runtime_wake(user_id, reason=reason)
    if status is None:
        return _error_response("no workspace for user", status=404)
    return _json_response(status)


async def handle_fin_workspace_runtime_sleep(request: web.Request) -> web.Response:
    """POST /internal/financial-workspace/runtime/sleep?user_id=..."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)
    user_id = _runtime_user_id(request)
    reason = request.query.get("reason", "")
    if not user_id:
        return _error_response("user_id required", status=400)
    status = fw.runtime_sleep(user_id, reason=reason)
    if status is None:
        return _error_response("no workspace for user", status=404)
    return _json_response(status)


async def handle_fin_workspace_runtime_status(request: web.Request) -> web.Response:
    """GET /internal/financial-workspace/runtime/status?user_id=..."""
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)
    user_id = _runtime_user_id(request)
    if not user_id:
        return _error_response("user_id required", status=400)
    status = fw.runtime_status(user_id)
    if status is None:
        return _error_response("no workspace for user", status=404)
    return _json_response(status)
