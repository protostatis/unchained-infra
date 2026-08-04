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

Browser routes (proxied by Caddy under ``/fin-terminal-workspace``, dedicated
``/workspace/*`` namespace — never ``/auth/...`` or ``/api/...`` so the claim
OAuth routes cannot shadow the site's login routes):
  GET    /workspace/auth/claim                 — handoff entry page (no secret in URL)
  POST   /workspace/claim                      — initiate claim (secret in HttpOnly cookie)
  GET    /workspace/claims/{claim_id}          — claim status
  GET    /workspace/workspace                  — current user workspace
  GET    /workspace/snapshots                  — current user snapshots
  GET    /workspace/runtime/status             — current user runtime state
  POST   /workspace/oauth/google               — Google GSI id token (claim-bound)
  GET    /workspace/oauth/{provider}/start     — provider OAuth start (allowlist)
  GET    /workspace/oauth/{provider}/callback  — provider OAuth callback (allowlist)
  GET    /workspace/done                       — claim completion page
  GET    /workspace-terminal                   — /fin-terminal/ leg (auth + provider gate)
  GET    /attach/{slug}/{tail:.*}              — per-account runtime proxy (HTTP + WS)

All internal S2S handlers require the bearer control token
(``Authorization: Bearer <FIN_WORKSPACE_CONTROL_TOKEN>``) — including the
internal ``/internal/.../claim`` and ``/internal/.../claim/accept`` variants.
The S2S handoff secret travels only in the gateway-set HttpOnly
``fin-terminal-handoff-secret`` cookie and is read server-side at claim
initiation; it never appears in the browser JS, the POST body, a URL, or a log
line. The claim secret is carried only in an HttpOnly Secure SameSite=Lax
parent-domain cookie.

Browser cookie-auth paths (no control token): ``POST /workspace/claim``,
``GET /workspace/claims/{claim_id}``, ``GET /workspace/workspace``,
``GET /workspace/snapshots``, ``GET /workspace/runtime/status``, and the OAuth
start/callback handlers in ``fin_workspace_auth``. The internal claim/accept
handlers are the S2S contract and reject cookie-only callers without the token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from aiohttp import web
from yarl import URL

from financial_workspace import (
    FinancialWorkspace,
    FinancialWorkspaceRuntimeScheduler,
    FinancialWorkspaceError,
    CheckpointValidationError,
    CheckpointNotFoundError,
    CheckpointStateError,
    ClaimRejectedError,
    ImportConflictError,
    UnauthorizedError,
    is_fin_workspace_enabled,
    runtime_provider_status,
    runtime_provider_validate,
    runtime_provider_wake,
    workspace_runtime_slug,
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
    """Explicit control token only — never JWT_SECRET or a cookie session.

    Constant-time comparison (``hmac.compare_digest``) so timing cannot leak
    token length/prefix. Reads the canonical env-backed resolver, never the
    lazily-imported web runtime, so a missing ``JWT_SECRET`` during tests
    cannot crash token verification.
    """
    from financial_workspace import _resolve_control_token as _token
    expected = _token()
    if not expected:
        return False
    auth = request.headers.get(_CONTROL_TOKEN_HEADER, "")
    if auth.startswith("Bearer "):
        auth = auth[7:]
    return hmac.compare_digest(auth, expected)


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
# POST /internal/financial-workspace/claim  (and browser POST /workspace/claim)
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
    """POST /internal/financial-workspace/claim — internal S2S variant.

    The documented model requires the bearer control token on every
    ``/internal/*`` handler. The browser cookie flow must use the
    ``POST /workspace/claim`` route (no token, handoff cookie) instead.
    """
    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)
    return await _initiate_claim_impl(request)


async def handle_fin_workspace_browser_claim(request: web.Request) -> web.Response:
    """POST /workspace/claim — browser claim initiation under /fin-terminal-workspace."""
    return await _initiate_claim_impl(request)


def _claim_oauth_start_url(claim_id: str, audience: str) -> str:
    base = os.environ.get(
        "FIN_TERMINAL_BASE_URL",
        "https://unbrowser.unchainedsky.com/fin-terminal-workspace",
    ).strip().rstrip("/")
    return f"{base}/workspace/oauth/{audience}/start?claim_id={claim_id}"


# ---------------------------------------------------------------------------
# GET /workspace/auth/claim — browser handoff entry page (no secret in URL)
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
   reads it from the cookie when this POST is processed, then rotates it away.
   This page lives at /workspace/auth/claim; the claim POST endpoint is
   /workspace/claim (one directory up). */
document.querySelectorAll("button[data-provider]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const audience = btn.dataset.provider;
    btn.disabled = true;
    try {
      const res = await fetch("../claim", {method: "POST", headers: {"Content-Type": "application/json"},
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
    """GET /workspace/auth/claim — serve the handoff entry page (secret never in URL)."""
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
    """Accept a claim after OAuth callback (internal S2S variant).

    Requires the bearer control token: the browser OAuth callbacks
    (``/workspace/oauth/{provider}/callback``, ``POST /workspace/oauth/google``) call the core
    accept logic directly with the HttpOnly claim cookie and are the
    documented cookie-auth path.
    """
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)

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


def _resolve_credit_ledger(core) -> "object | None":
    """Resolve (and lazily instantiate) the credit ledger on the core module.

    The ledger is intentionally lazy in production. A missing
    ``core._credit_ledger`` must not cause a legitimate ``account_grant``
    effect to be mislabeled or dropped — instantiate it against the Auth DB
    path exactly like the background outbox loop does.
    """
    ledger = getattr(core, "_credit_ledger", None)
    if ledger is not None:
        return ledger
    auth = getattr(core, "_auth", None)
    db_path = getattr(auth, "db_path", "") if auth is not None else ""
    if not db_path:
        db_path = os.environ.get("UNCHAINED_DB_PATH", "")
    if not db_path:
        return None
    from credit import CreditLedger
    ledger = CreditLedger(db_path=db_path)
    try:
        core._credit_ledger = ledger
    except Exception:
        pass  # read-only core mock in tests; ledger still returned
    return ledger


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

    effects = fw.poll_pending_effects(limit=10)
    results = []

    for effect in effects:
        eid = effect["effect_id"]
        if not fw.mark_effect_processing(eid):
            continue
        try:
            if effect["effect_type"] == "account_grant":
                ledger = _resolve_credit_ledger(core)
                if ledger is None:
                    # The ledger is genuinely unavailable (no DB path). Keep
                    # the effect failed/retryable — never mislabel it unknown
                    # or dead.
                    fw.mark_effect_completed(eid, failed=True, error="credit ledger unavailable")
                    results.append({
                        "effect_id": eid, "success": False,
                        "error": "credit ledger unavailable",
                    })
                    continue
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
    """GET /workspace/claims/{claim_id} — claim status via the HttpOnly claim cookie."""
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
    """GET /workspace/workspace — workspace for the session-authenticated user."""
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
    """GET /workspace/snapshots — snapshots for the session-authenticated user."""
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
    """GET /workspace/runtime/status — account-scoped runtime state."""
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
    """POST /internal/financial-workspace/runtime/sleep?user_id=...

    Durable sleep: the account runtime is stopped ONLY after its current
    authoritative checkpoint has been flushed to the control plane (S2S). If
    the flush fails the runtime stays awake (fail closed — no state loss).
    """
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)
    user_id = _runtime_user_id(request)
    reason = request.query.get("reason", "")
    if not user_id:
        return _error_response("user_id required", status=400)
    result = fw.runtime_sleep_durable(user_id, reason=reason)
    if result.get("error"):
        status = 409 if "flush failed" in str(result.get("error")) else 404
        return _json_response(result, status=status)
    return _json_response(result)


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


async def handle_fin_workspace_runtime_flush(request: web.Request) -> web.Response:
    """POST /internal/financial-workspace/runtime/flush — persist a checkpoint
    flushed back from the account's isolated runtime (S2S, control-token).

    Body: ``{slug, checkpoint}`` — ``slug`` is the account runtime slug the
    provider provisioned; ``user_id`` is derived from the slug server-side so
    the provider only needs to know the opaque slug (the account slug is
    never stored as a user identifier anywhere else).
    """
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)
    if not _verify_control_token(request):
        return _error_response("unauthorized", status=401)
    try:
        body = await request.json()
    except Exception:
        return _error_response("invalid JSON body", status=400)

    from financial_workspace import workspace_runtime_slug

    slug = str(body.get("slug", "") or "").strip()
    checkpoint = body.get("checkpoint")
    if not slug or not isinstance(checkpoint, dict):
        return _error_response("slug and checkpoint required", status=400)

    # Recover the account from the slug (slug is sha256(user_id)[:24]).
    candidate_user_id = ""
    for row in fw._iter_workspace_user_ids():
        if workspace_runtime_slug(row[0]) == slug:
            candidate_user_id = row[0]
            break
    if not candidate_user_id:
        return _error_response("no account for slug", status=404)

    result = fw.import_flushed_checkpoint(candidate_user_id, checkpoint)
    if result is None:
        return _error_response("no workspace for account", status=404)
    return _json_response({"ok": True, "snapshot_id": result["snapshot_id"], "version": result["version"]})


# ---------------------------------------------------------------------------
# Private workspace leg — authenticated /fin-terminal/
# ---------------------------------------------------------------------------
# Caddy maps /fin-terminal/ to this leg only when FIN_TERMINAL_WORKSPACE_ENABLED
# is true: it strips /fin-terminal and rewrites the remainder to /terminal/<rest>
# before proxying to this control plane. That keeps the app runtime image's own
# root-relative surface (/ , /assets/*, /ws, /api/ready) reachable unchanged
# after this handler strips the /terminal marker, while the client's absolute
# /fin-terminal/* asset and /ws URLs round-trip coherently through Caddy.
# The leg NEVER renders the marketing index: it authenticates the session,
# requires an imported workspace, and requires a validated host-side runtime
# provider before waking the account's isolated runtime. Every failure is a
# fail-closed page with no CTA.

_TERMINAL_FAIL_CLOSED_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Workspace unavailable</title>
<style>
body{font-family:system-ui,sans-serif;background:#0b0f14;color:#e6edf3;
display:grid;place-items:center;min-height:100vh;margin:0}
.card{max-width:460px;padding:32px;border:1px solid #2d3748;border-radius:12px;
background:#11161d;text-align:center}
h1{font-size:18px;margin:0 0 8px}
p{color:#9aa7b4;font-size:14px;line-height:1.5;margin:0}
</style></head><body>
<div class="card">
<h1>{title}</h1>
<p>{message}</p>
</div>
</body></html>
"""

# Headers the control plane never forwards to the account runtime: the runtime
# derives its principal and proxy token from the authenticated session ONLY.
_STRIP_RUNTIME_HEADERS = {
    "host", "connection", "upgrade",
    "x-workspace-runtime-token",
    "x-fin-terminal-user", "x-fin-terminal-proxy-token",
    "x-fin-terminal-control-token",
}


def _terminal_fail_closed(title: str, message: str, *, status: int) -> web.Response:
    html = (
        _TERMINAL_FAIL_CLOSED_TEMPLATE
        .replace("{title}", title)
        .replace("{message}", message)
    )
    return web.Response(
        text=html,
        content_type="text/html",
        status=status,
        headers={**_NO_STORE_HEADERS},
    )


def _resolve_runtime_proxy_token() -> str:
    return os.environ.get("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN", "").strip()


def _resolve_runtime_allowed_origins() -> set[str]:
    """Allowed browser origins for the account runtime (the runtime's own
    ``ALLOWED_ORIGINS`` contract). Default: the public unbrowser origin. An
    unset value fails closed (empty allowlist → origins never forwarded)."""
    raw = os.environ.get(
        "FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS",
        "https://unbrowser.unchainedsky.com",
    )
    entries = {entry.strip() for entry in raw.split(",") if entry.strip()}
    return entries


def _validated_browser_origin(request: web.Request) -> str:
    """Return the browser's ``Origin`` header when it is a well-formed HTTP(S)
    origin allowed by ``FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS``; ``""``
    otherwise (missing, ``null``, malformed, or foreign — fail closed).

    The control plane forwards ONLY a validated origin toward the account
    runtime; caller-supplied identity/proxy headers are still stripped and only
    server-owned values are injected."""
    raw = (request.headers.get("Origin", "") or "").strip()
    if not raw or raw == "null":
        return ""
    try:
        parsed = URL(raw)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    try:
        canonical = str(parsed.origin())
    except Exception:
        return ""  # non-absolute (e.g. scheme with no host) — fail closed
    if canonical != raw:
        return ""  # must be a canonical origin (no path/query/trailing slash)
    allowed = _resolve_runtime_allowed_origins()
    return raw if raw in allowed else ""


def _runtime_upstream_headers(slug: str) -> dict[str, str] | None:
    """Build the ONLY headers the control plane ever injects toward the
    account runtime: the shared proxy token and the server-derived principal
    bound to the authenticated account slug. Returns None (fail closed) when
    the proxy token is not configured."""
    proxy_token = _resolve_runtime_proxy_token()
    if not proxy_token or not slug:
        return None
    return {
        "X-Fin-Terminal-Proxy-Token": proxy_token,
        "X-Fin-Terminal-User": f"account:{slug}",
    }


def _resolve_runtime_scheduler(core, fw: FinancialWorkspace) -> FinancialWorkspaceRuntimeScheduler:
    """Lazily resolve the account-runtime idle scheduler (per control-plane
    process). The proxy handlers attach/detach/touch it; the background sweep
    loop in web.py drives ``tick()``."""
    scheduler = getattr(core, "_fin_runtime_scheduler", None)
    if scheduler is None:
        import asyncio as _asyncio
        idle_seconds = max(
            60, int(os.environ.get("FIN_WORKSPACE_RUNTIME_IDLE_SLEEP_SECONDS", "600"))
        )
        scheduler = FinancialWorkspaceRuntimeScheduler(fw, idle_seconds=float(idle_seconds))
        try:
            core._fin_runtime_scheduler = scheduler
        except Exception:
            pass  # read-only core mock in tests
    return scheduler


async def _wake_account_runtime(fw: FinancialWorkspace, user_id: str) -> dict | None:
    """Validate provider + provision the account's isolated runtime.

    Returns the provider status dict, or None when the provider is missing,
    unreachable, or lacks the required capabilities. The control plane never
    touches the Docker socket — the host-side provider owns it.
    """
    if runtime_provider_validate() is None:
        return None
    slug = workspace_runtime_slug(user_id)
    checkpoint = fw.get_workspace_runtime_checkpoint(user_id)
    if checkpoint is None:
        return None
    from financial_workspace import _resolve_control_token
    return runtime_provider_wake(
        slug,
        checkpoint,
        control_token=_resolve_control_token(),
    )


async def handle_fin_workspace_terminal_proxy(request: web.Request) -> web.Response:
    """GET /terminal/{tail:.*} — the authenticated /fin-terminal/ leg.

    Caddy strips /fin-terminal and rewrites to /terminal/<rest>. This handler
    authenticates the session, wakes the account runtime (fail closed when no
    validated provider exists), and proxies EVERY request (HTTP + WebSocket)
    to ``fin-workspace-<slug>:8787/<rest>`` while injecting the server-side
    principal and proxy token — never caller-supplied values.

    Fail-closed contract:
      1. Feature disabled              → 404 (no CTA)
      2. Not authenticated             → 401 (no CTA)
      3. No imported workspace         → 404 (no CTA)
      4. Provider not validated        → 503, explicit reason, NO CTA
      5. Provider validated            → wake + proxy the runtime surface
    """
    fw = _resolve_fw()
    if fw is None:
        return _terminal_fail_closed(
            "Workspace unavailable", "Workspaces are not enabled for this host.", status=404
        )

    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return _terminal_fail_closed(
            "Sign in required",
            "Sign in to open your workspace. It stays private to your account.",
            status=401,
        )
    user_id = str(auth_info.get("user_id", "") or "").strip()
    if not user_id:
        return _terminal_fail_closed("Sign in required", "Sign in to open your workspace.", status=401)

    if fw.get_workspace_for_user(user_id) is None:
        return _terminal_fail_closed(
            "No workspace",
            "This account has not imported a workspace yet.",
            status=404,
        )

    provider_status = await _wake_account_runtime(fw, user_id)
    if provider_status is None:
        return _terminal_fail_closed(
            "Workspace runtime unavailable",
            "The workspace runtime provider is not validated. Workspace is "
            "not being routed to a placeholder — it will open once a "
            "validated runtime provider is provisioned.",
            status=503,
        )

    slug = workspace_runtime_slug(user_id)
    if runtime_provider_status(slug) is None:
        return _terminal_fail_closed(
            "Workspace runtime unavailable",
            "The workspace runtime did not come up. Try again in a moment.",
            status=503,
        )

    tail = str(request.match_info.get("tail", "") or "").strip()
    upstream = f"http://fin-workspace-{slug}:8787/{tail}"

    scheduler = _resolve_runtime_scheduler(core, fw)
    scheduler.touch(user_id)
    if request.headers.get("Upgrade", "").lower() == "websocket":
        scheduler.attach(user_id)
        try:
            return await _proxy_websocket(request, upstream, slug, scheduler, user_id)
        finally:
            scheduler.detach(user_id)
    return await _proxy_http(request, upstream, slug)


async def _proxy_http(request: web.Request, upstream: str, slug: str) -> web.Response:
    """Stream a plain HTTP request to the account runtime, injecting ONLY the
    server-derived principal and proxy token (caller-supplied versions are
    always stripped) and forwarding only a validated browser ``Origin``."""
    injected = _runtime_upstream_headers(slug)
    if injected is None:
        return _terminal_fail_closed(
            "Workspace runtime unavailable",
            "The workspace runtime proxy token is not configured.",
            status=503,
        )
    import aiohttp
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_RUNTIME_HEADERS
    }
    headers.update(injected)
    origin = _validated_browser_origin(request)
    if origin:
        headers["Origin"] = origin
    else:
        # Never forward a caller-supplied Origin the control plane did not
        # validate (same-origin/curl requests simply omit the header).
        headers.pop("Origin", None)
    data = await request.read()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method,
                upstream,
                headers=headers,
                data=data or None,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                body = await resp.read()
                return web.Response(
                    status=resp.status,
                    body=body,
                    headers={
                        "Content-Type": resp.headers.get("Content-Type", "text/html"),
                        "Cache-Control": "no-store",
                    },
                )
    except Exception as exc:
        log.error("[fin-workspace] terminal proxy failed: %s", exc)
        return _terminal_fail_closed(
            "Workspace runtime unreachable",
            "The workspace runtime did not respond. Try again in a moment.",
            status=502,
        )


async def _proxy_websocket(
    request: web.Request,
    upstream: str,
    slug: str,
    scheduler: FinancialWorkspaceRuntimeScheduler,
    user_id: str,
) -> web.Response:
    """Relay a WebSocket between the browser and the account runtime, injecting
    the server-derived principal + proxy token AND forwarding the VALIDATED
    browser ``Origin`` when dialing the runtime (the runtime enforces its own
    ``ALLOWED_ORIGINS`` gate on WebSocket upgrades). The principal is bound to
    the authenticated account/slug — caller-supplied identity headers are
    stripped. A missing or foreign Origin is rejected (fail closed) before any
    dial."""
    injected = _runtime_upstream_headers(slug)
    if injected is None:
        raise web.HTTPBadGateway(text="workspace runtime proxy token not configured")
    origin = _validated_browser_origin(request)
    if not origin:
        log.warning("[fin-workspace] WS proxy rejected missing/foreign Origin for %s", slug)
        raise web.HTTPForbidden(text="origin not allowed")
    headers = dict(injected)
    headers["Origin"] = origin
    import aiohttp
    try:
        ws_local = web.WebSocketResponse(heartbeat=30.0)
        await ws_local.prepare(request)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                upstream,
                headers=headers,
                timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
            ) as ws_upstream:
                async def pump_upstream() -> None:
                    async for msg in ws_upstream:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_local.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await ws_local.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            await ws_local.close()
                            break

                async def pump_local() -> None:
                    async for msg in ws_local:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_upstream.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await ws_upstream.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            await ws_upstream.close()
                            break

                import asyncio as _asyncio
                tasks = [_asyncio.ensure_future(pump_upstream()), _asyncio.ensure_future(pump_local())]
                done, pending = await _asyncio.wait(
                    tasks, return_when=_asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    try:
                        task.result()
                    except Exception:
                        pass
        return ws_local
    except Exception as exc:
        log.error("[fin-workspace] terminal WS proxy failed: %s", exc)
        raise web.HTTPBadGateway(text="workspace runtime unreachable")
