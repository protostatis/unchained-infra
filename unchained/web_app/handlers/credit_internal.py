"""Internal credit endpoints for the hosted trial worker.

Service-authenticated endpoints that the trial agent (chat_agent_openrouter.py)
calls to reserve credit before OpenRouter API calls, persist submission, and
settle/release afterward. Production exposes these only on the Docker-internal
web listener; Caddy rejects the ``/internal/*`` namespace.

Uses the mandatory, narrowly-scoped HOSTED_AGENT_SERVICE_TOKEN. Never falls
back to a user-, relay-, trial-agent-, or private-core credential.
"""

from __future__ import annotations

import asyncio
import hmac

from aiohttp import web

from credit import (
    CreditLedger,
    InsufficientBalanceError,
    RunNotActiveError,
    _PROVIDER_MODEL_PREFIXES,
    credit_service_token,
    hosted_model_reservation_policy,
    is_hosted_model_allowed_for_identity,
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


async def _json_object(request: web.Request) -> tuple[dict | None, web.Response | None]:
    try:
        body = await request.json()
    except Exception:
        return None, _json_error(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return None, _json_error(400, "JSON body must be an object")
    return body, None


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


async def handle_credit_reserve(request: web.Request) -> web.Response:
    """POST /internal/credit/reserve — reserve an upcoming provider attempt.

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

    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error

    run_id = str(body.get("run_id", "")).strip()
    model = str(body.get("model", "")).strip()
    idempotency_key = str(body.get("idempotency_key", "")).strip()

    if not run_id:
        return _json_error(400, "run_id required")
    if not idempotency_key:
        return _json_error(400, "idempotency_key required")

    core = _core()
    ledger = _get_ledger()
    run = await asyncio.to_thread(ledger.get_run, run_id)
    run_user_id = str((run or {}).get("user_id", "") or "").strip()
    if not is_hosted_model_allowed_for_identity(
        core,
        model,
        user_id=run_user_id,
    ):
        return _json_error(400, f"Model '{model}' is not available for this account")

    reservation_policy = hosted_model_reservation_policy(
        core,
        model,
        user_id=run_user_id,
    )

    try:
        result = await asyncio.to_thread(
            ledger.reserve_call,
            run_id,
            model=model,
            reservation_micro_usd=reservation_policy["reservation_micro_usd"],
            idempotency_key=idempotency_key,
            cap_reservation_to_available=reservation_policy["cap_to_available"],
        )
        return web.json_response(result)
    except InsufficientBalanceError as e:
        return _json_error(402, str(e))
    except (RunNotActiveError, ValueError) as e:
        return _json_error(400, str(e))


async def handle_credit_mark_submitted(request: web.Request) -> web.Response:
    """POST /internal/credit/submitted — persist provider-boundary crossing."""
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error
    call_id = str(body.get("call_id", "")).strip()
    if not call_id:
        return _json_error(400, "call_id required")

    try:
        result = await asyncio.to_thread(_get_ledger().mark_call_submitted, call_id)
        return web.json_response(result)
    except ValueError as e:
        return _json_error(400, str(e))


async def handle_credit_settle(request: web.Request) -> web.Response:
    """POST /internal/credit/settle — settle a submitted call with actual usage.

    JSON body:
        {
            "call_id": "call-...",
            "actual_cost_micro_usd": 7,
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "prompt_cache_hit_tokens": 90,
            "prompt_cache_miss_tokens": 10
        }
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error

    call_id = str(body.get("call_id", "")).strip()
    if not call_id:
        return _json_error(400, "call_id required")

    try:
        actual_cost = max(0, int(body.get("actual_cost_micro_usd", 0) or 0))
        prompt_tokens = max(0, int(body.get("prompt_tokens", 0) or 0))
        completion_tokens = max(0, int(body.get("completion_tokens", 0) or 0))
        total_tokens = max(0, int(body.get("total_tokens", 0) or 0))
        provider_cost_micro_usd = max(
            0, int(body.get("provider_cost_micro_usd", 0) or 0)
        )
        prompt_cache_hit_tokens = max(
            0, int(body.get("prompt_cache_hit_tokens", 0) or 0)
        )
        prompt_cache_miss_tokens = max(
            0, int(body.get("prompt_cache_miss_tokens", 0) or 0)
        )
    except (TypeError, ValueError):
        return _json_error(400, "cost and token fields must be integers")
    cost_absent = bool(body.get("cost_absent", False))

    ledger = _get_ledger()
    try:
        result = await asyncio.to_thread(
            ledger.settle_call,
            call_id,
            actual_cost_micro_usd=actual_cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_absent=cost_absent,
            provider_cost_micro_usd=provider_cost_micro_usd,
            prompt_cache_hit_tokens=prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=prompt_cache_miss_tokens,
        )
        return web.json_response(result)
    except ValueError as e:
        return _json_error(400, str(e))


async def handle_credit_release(request: web.Request) -> web.Response:
    """POST /internal/credit/release — release a pre-submit reservation.

    JSON body: {"call_id": "call-..."}
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error

    call_id = str(body.get("call_id", "")).strip()
    if not call_id:
        return _json_error(400, "call_id required")

    ledger = _get_ledger()
    try:
        result = await asyncio.to_thread(ledger.release_call, call_id)
        return web.json_response(result)
    except ValueError as e:
        return _json_error(400, str(e))


async def handle_credit_provider_balance(request: web.Request) -> web.Response:
    """POST /internal/credit/provider-balance — store provider balance snapshots.

    Called periodically by the hosted worker (which holds the provider key).
    The reconciliation job later compares realized balance deltas against
    ledger-estimated spend to detect pricing drift.

    JSON body:
        {
            "snapshots": [
                {"provider": "deepseek", "currency": "USD",
                 "total_balance": "11.32", "is_available": true,
                 "snapshot_at": 1720000000.0}
            ]
        }
    """
    if not _validate_service_auth(request):
        return _json_error(401, "Service auth required")

    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error

    snapshots = body.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return _json_error(400, "snapshots array required")

    ledger = _get_ledger()
    stored = 0
    for raw in snapshots:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider", "")).strip()
        currency = str(raw.get("currency", "")).strip()
        total_balance_raw = raw.get("total_balance")
        if not provider or not currency:
            continue
        # Only accept known providers (reconciliation scopes by this set);
        # a typo'd/garbled provider would otherwise pollute the snapshots table.
        if provider not in _PROVIDER_MODEL_PREFIXES:
            continue
        try:
            total_balance = float(total_balance_raw)
        except (TypeError, ValueError):
            continue
        try:
            await asyncio.to_thread(
                ledger.record_provider_balance_snapshot,
                provider=provider,
                currency=currency,
                total_balance=total_balance,
                is_available=bool(raw.get("is_available", False)),
                snapshot_at=float(raw.get("snapshot_at") or 0) or None,
            )
            stored += 1
        except Exception as e:
            # A dropped snapshot silently widens the reconciliation gap; log it
            # so operators see the balance verification is degrading.
            import logging as _logging
            _logging.getLogger("credit_internal").warning(
                "provider balance snapshot store failed (provider=%s currency=%s): %s",
                raw.get("provider"), raw.get("currency"), e,
            )
    if stored <= 0:
        return _json_error(400, "no valid snapshots stored")
    return web.json_response({"stored": stored})
