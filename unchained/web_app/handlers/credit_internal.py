"""Internal credit endpoints for the hosted trial worker.

Service-authenticated endpoints that the trial agent (chat_agent_openrouter.py)
calls to reserve credit before OpenRouter API calls and settle/release afterward.

Uses a narrowly-scoped CREDIT_SERVICE_TOKEN (fallback: TRIAL_AGENT_KEY).
Never reads PRIVATE_CORE_TOKEN or RELAY_SHARED_TOKEN.
"""

from __future__ import annotations

import hmac
import os

from aiohttp import web

from credit import (
    CreditLedger,
    HOSTED_MODEL_CATALOG,
    InsufficientBalanceError,
    RunNotActiveError,
    _default_reservation,
    _micro_to_usd,
    credit_service_token,
    is_hosted_model_allowed as _is_hosted_model_allowed,
)

from web_app.core import get_core as _core


def _validate_service_auth(request: web.Request) -> bool:
    """Check that the request's Bearer token matches the credit service token."""
    expected = credit_service_token()
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    return hmac.compare_digest(token, expected)


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _get_ledger() -> CreditLedger:
    """Return the process-level cached ledger (avoids re-running DDL)."""
    core = _core()
    db_path = core._auth.db_path
    ledger = getattr(core, "_credit_ledger", None)
    if ledger is None or getattr(ledger, "db_path", "") != db_path:
        ledger = CreditLedger(db_path=db_path)
        core._credit_ledger = ledger
    return ledger


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_credit_balance(request: web.Request) -> web.Response:
    """GET /web/credit/balance?user_id=... — get balance for a user.

    Requires service auth. Returns account state in micro-USD and USD.
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    user_id = request.query.get("user_id", "").strip()
    if not user_id:
        return _json_error(400, "user_id query param required")

    ledger = _get_ledger()
    account = ledger.get_account(user_id)
    if not account:
        return web.json_response({
            "user_id": user_id,
            "balance_micro_usd": 0,
            "balance_usd": 0.0,
            "total_granted_micro_usd": 0,
            "total_spent_micro_usd": 0,
        })

    held = ledger.held_reservation_total(account["account_id"])
    available = account["balance_micro_usd"] - held

    return web.json_response({
        "account_id": account["account_id"],
        "user_id": user_id,
        "balance_micro_usd": account["balance_micro_usd"],
        "balance_usd": account["balance_usd"],
        "held_micro_usd": held,
        "available_micro_usd": max(0, available),
        "available_usd": _micro_to_usd(max(0, available)),
        "total_granted_micro_usd": account["total_granted_micro_usd"],
        "total_spent_micro_usd": account["total_spent_micro_usd"],
    })


async def handle_credit_history(request: web.Request) -> web.Response:
    """GET /web/credit/history?user_id=...&limit=100 — get ledger entries.

    Requires service auth.
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    user_id = request.query.get("user_id", "").strip()
    if not user_id:
        return _json_error(400, "user_id query param required")

    try:
        limit = min(500, max(1, int(request.query.get("limit", "100"))))
    except ValueError:
        limit = 100

    ledger = _get_ledger()
    entries = ledger.get_ledger_for_user(user_id, limit=limit)

    return web.json_response({"user_id": user_id, "entries": entries})


async def handle_credit_reserve(request: web.Request) -> web.Response:
    """POST /web/credit/reserve — reserve credit for an upcoming provider call.

    JSON body:
        {
            "run_id": "run-...",
            "model": "google/gemini-flash-lite",
            "idempotency_key": "optional-stable-key"
        }

    Returns {call_id, reserved_micro_usd, ...}
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body")

    run_id = str(body.get("run_id", "")).strip()
    model = str(body.get("model", "")).strip()
    idempotency_key = str(body.get("idempotency_key", "")).strip()

    if not run_id:
        return _json_error(400, "run_id required")
    if not idempotency_key:
        return _json_error(400, "idempotency_key required")

    # Model allowlist check
    if not _is_hosted_model_allowed(model):
        return _json_error(400, f"Model '{model}' is not in the hosted allowlist")

    reservation = _default_reservation(model)

    ledger = _get_ledger()
    try:
        result = ledger.reserve_call(
            run_id=run_id,
            model=model,
            reservation_micro_usd=reservation,
            idempotency_key=idempotency_key,
        )
        return web.json_response(result)
    except InsufficientBalanceError as e:
        return _json_error(402, str(e))
    except (RunNotActiveError, ValueError) as e:
        return _json_error(400, str(e))


async def handle_credit_settle(request: web.Request) -> web.Response:
    """POST /web/credit/settle — settle a reserved call with actual usage.

    JSON body:
        {
            "call_id": "call-...",
            "actual_cost_micro_usd": 7,
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300
        }
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body")

    call_id = str(body.get("call_id", "")).strip()
    if not call_id:
        return _json_error(400, "call_id required")

    actual_cost = max(0, int(body.get("actual_cost_micro_usd", 0) or 0))
    prompt_tokens = max(0, int(body.get("prompt_tokens", 0) or 0))
    completion_tokens = max(0, int(body.get("completion_tokens", 0) or 0))
    total_tokens = max(0, int(body.get("total_tokens", 0) or 0))
    cost_absent = bool(body.get("cost_absent", False))
    provider_cost_micro_usd = max(0, int(body.get("provider_cost_micro_usd", 0) or 0))

    ledger = _get_ledger()
    try:
        result = ledger.settle_call(
            call_id=call_id,
            actual_cost_micro_usd=actual_cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_absent=cost_absent,
            provider_cost_micro_usd=provider_cost_micro_usd,
        )
        return web.json_response(result)
    except ValueError as e:
        return _json_error(400, str(e))


async def handle_credit_release(request: web.Request) -> web.Response:
    """POST /web/credit/release — release a held reservation.

    JSON body: {"call_id": "call-..."}
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body")

    call_id = str(body.get("call_id", "")).strip()
    if not call_id:
        return _json_error(400, "call_id required")

    ledger = _get_ledger()
    try:
        result = ledger.release_call(call_id)
        return web.json_response(result)
    except ValueError as e:
        return _json_error(400, str(e))


async def handle_credit_create_run(request: web.Request) -> web.Response:
    """POST /web/credit/run/create — create an inference run.

    JSON body:
        {
            "user_id": "u-abc123",
            "model": "google/gemini-flash-lite",
            "idempotency_key": "chat-turn-..."
        }

    Returns {run_id, ...}
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body")

    user_id = str(body.get("user_id", "")).strip()
    model = str(body.get("model", "")).strip()
    idempotency_key = str(body.get("idempotency_key", "")).strip()

    if not user_id:
        return _json_error(400, "user_id required")
    if not idempotency_key:
        return _json_error(400, "idempotency_key required")

    # Model allowlist check
    if model and not _is_hosted_model_allowed(model):
        return _json_error(400, f"Model '{model}' is not in the hosted allowlist")

    ledger = _get_ledger()
    result = ledger.create_run(
        user_id=user_id,
        model=model,
        idempotency_key=idempotency_key,
    )
    return web.json_response(result)


async def handle_credit_finish_run(request: web.Request) -> web.Response:
    """POST /web/credit/run/finish — finish an inference run.

    JSON body:
        {
            "run_id": "run-...",
            "status": "completed"  # or "cancelled", "exhausted"
        }
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body")

    run_id = str(body.get("run_id", "")).strip()
    status = str(body.get("status", "completed")).strip()

    if not run_id:
        return _json_error(400, "run_id required")
    if status not in ("completed", "cancelled", "exhausted"):
        return _json_error(400, f"Invalid finish status: {status}")

    ledger = _get_ledger()
    try:
        result = ledger.finish_run(run_id, status=status)
        return web.json_response(result)
    except Exception as e:
        return _json_error(400, str(e))


async def handle_credit_model_catalog(request: web.Request) -> web.Response:
    """GET /web/credit/model-catalog — list hosted model catalog.

    Requires service auth.
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    catalog = {}
    for model, reservation in HOSTED_MODEL_CATALOG.items():
        catalog[model] = {
            "reservation_micro_usd": reservation,
            "reservation_usd": _micro_to_usd(reservation),
        }
    return web.json_response({
        "models": catalog,
        "default_reservation_micro_usd": _default_reservation("unknown"),
        "default_reservation_usd": _micro_to_usd(_default_reservation("unknown")),
    })
