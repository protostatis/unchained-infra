"""HTTP handlers for the financial workspace internal API.

All handlers require either:
  - A bearer control token (``Authorization: Bearer <FIN_WORKSPACE_CONTROL_TOKEN>``)
  - Or a session-authenticated request (cookie-based)

Endpoints:
  POST   /internal/financial-workspace/checkpoints        — create checkpoint (S2S)
  GET    /internal/financial-workspace/checkpoints/{id}    — get checkpoint status
  POST   /internal/financial-workspace/claim               — initiate claim (cookie-based)
  POST   /internal/financial-workspace/claim/accept        — accept claim (OAuth callback)
  GET    /internal/financial-workspace/claims/{id}         — claim status
  GET    /internal/financial-workspace/workspace           — current user workspace
  GET    /internal/financial-workspace/snapshots           — current user snapshots
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

_CLAIM_COOKIE_NAME = "__Host-fw-claim-secret"
_CLAIM_COOKIE_TTL = 3600  # 1 hour (must match claim expiry)


def _json_response(data, *, status: int = 200, headers: dict | None = None) -> web.Response:
    body = json.dumps(data, separators=(",", ":"), default=str)
    resp_headers = {**_NO_STORE_HEADERS, "Content-Type": "application/json"}
    if headers:
        resp_headers.update(headers)
    return web.Response(status=status, body=body, headers=resp_headers)


def _error_response(message: str, status: int = 400) -> web.Response:
    return _json_response({"error": message}, status=status)


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
    # Lazy init
    db_path = getattr(core._auth, "db_path", os.environ.get("UNCHAINED_DB_PATH", ""))
    if not db_path:
        return None
    from checkpoint_store import create_checkpoint_store
    store = create_checkpoint_store()
    fw = FinancialWorkspace(db_path, store)
    core._fin_workspace = fw
    return fw


def _verify_control_token(request: web.Request) -> bool:
    token = getattr(_core(), "_resolve_control_token", None)
    if not callable(token):
        fin_token = os.environ.get("FIN_WORKSPACE_CONTROL_TOKEN", "").strip()
        jwt_secret = os.environ.get("JWT_SECRET", "").strip()
        expected = fin_token or jwt_secret
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
# POST /internal/financial-workspace/claim
# ---------------------------------------------------------------------------
async def handle_fin_workspace_claim(request: web.Request) -> web.Response:
    """Initiate a one-time claim for a handoff.

    Expects: {handoff_id, handoff_secret, browser_nonce}
    Returns: sets HttpOnly cookie with claim_secret, returns claim_id.
    """
    fw = _resolve_fw()
    if fw is None:
        return _error_response("financial workspace disabled", status=503)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("invalid JSON body", status=400)

    handoff_id = str(body.get("handoff_id", "") or "").strip()
    handoff_secret = str(body.get("handoff_secret", "") or "").strip()
    browser_nonce = str(body.get("browser_nonce", "") or "").strip()

    try:
        result = fw.initiate_claim(
            handoff_id=handoff_id,
            handoff_secret=handoff_secret,
            browser_nonce=browser_nonce,
        )
    except (CheckpointNotFoundError, CheckpointStateError, ClaimRejectedError) as e:
        return _error_response(str(e), status=400)
    except UnauthorizedError as e:
        return _error_response(str(e), status=401)
    except FinancialWorkspaceError as e:
        log.error("claim initiation failed: %s", e)
        return _error_response(str(e), status=500)

    # Set the claim secret cookie
    response = _json_response({
        "claim_id": result["claim_id"],
        "checkpoint_id": result["checkpoint_id"],
        "expires_at": result["expires_at"],
    })
    response.set_cookie(
        _CLAIM_COOKIE_NAME,
        result["claim_secret"],
        httponly=True,
        secure=True,
        samesite="Lax",
        max_age=_CLAIM_COOKIE_TTL,
        path="/internal/financial-workspace/claim",
    )
    return response


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
    response.del_cookie(_CLAIM_COOKIE_NAME, path="/internal/financial-workspace/claim")

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
