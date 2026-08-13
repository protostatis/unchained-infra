"""Credit / inference accounting module for hosted OpenRouter inference.

Stores integer micro-USD. Provides atomic, idempotent operations around
credit accounts, immutable ledger entries, inference runs, per-call reservations,
and provider usage records.

Uses SQLite WAL, foreign_keys, busy_timeout, short BEGIN IMMEDIATE transactions.
Every mutation helper reads AND writes within the same BEGIN IMMEDIATE connection
to avoid TOCTOU (the old pattern called ``ensure_account`` or ``get_account``
through a separate connection leak, creating a stale-read window).

Stale-run sweep: ``sweep_stale_runs(ttl_seconds)`` releases reservations that
were never submitted, conservatively captures submitted reservations, and
marks active runs as ``exhausted`` when they exceed the TTL.

Usage:
    from credit import CreditLedger

    ledger = CreditLedger(db_path="/path/to/auth.db")
    run = ledger.create_run(user_id="u-abc123",
                            idempotency_key="chat-turn-...")
    call = ledger.reserve_call(run_id=run["run_id"], model="google/...",
                               idempotency_key="or-call-...")
    # ... make the API call ...
    ledger.settle_call(call["call_id"], actual_cost_micro_usd=7)
    ledger.finish_run(run["run_id"])
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
import uuid as _uuid_module
from contextlib import contextmanager

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------
MICRO_USD_PER_USD = 1_000_000


def _usd_to_micro(usd: float) -> int:
    """Convert a USD float to integer micro-USD (rounded 6 decimal places)."""
    return max(0, round(usd * MICRO_USD_PER_USD))


def _micro_to_usd(micro: int) -> float:
    """Convert integer micro-USD back to a USD float."""
    return round(micro / MICRO_USD_PER_USD, 6)


def _now_ts() -> float:
    return time.time()


def _uuid_hex() -> str:
    return _uuid_module.uuid4().hex


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _configure_connection(conn: sqlite3.Connection) -> None:
    # Set the lock wait before any pragma that may need a database lock. WAL is
    # enabled once during schema initialization rather than on every request.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# Service token for credit internal endpoints
# ---------------------------------------------------------------------------

def credit_service_token() -> str:
    """Narrowly-scoped token for credit internal endpoints.

    The hosted callback credential is intentionally mandatory and separate
    from the trial-agent WebSocket key. Never reads PRIVATE_CORE_TOKEN,
    RELAY_SHARED_TOKEN, CREDIT_SERVICE_TOKEN, or TRIAL_AGENT_KEY.
    """
    return os.environ.get("HOSTED_AGENT_SERVICE_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# Immutable, time-versioned DeepSeek pricing schedule
# ---------------------------------------------------------------------------
# DeepSeek's published per-model rates, stored as integer micro-USD per million
# tokens to avoid float drift. Pricing is time-versioned: each successful
# request is priced by its local provider-submission timestamp (captured by the
# worker immediately after the submitted callback), never by a response field
# or a manual env flip. Unknown models keep their conservative fallback.
#
# Legacy schedule — in effect until the time-of-use change below:
#   Flash: hit $0.0028, miss $0.14, output $0.28
#   Pro:   hit $0.003625, miss $0.435, output $0.87
#
# New schedule — effective exactly 2026-08-16T16:00:00Z (unix 1786896000):
#   Peak windows recur daily in UTC: [01:00, 04:00) and [06:00, 10:00).
#   All other hours are off-peak.
#   Flash off $0.007 / $0.22 / $0.66,  peak $0.014 / $0.44 / $1.32
#   Pro   off $0.022 / $0.66 / $1.98, peak $0.044 / $1.32 / $3.96

DEEPSEEK_PRICING_EFFECTIVE_TIMESTAMP = 1_786_896_000  # 2026-08-16T16:00:00Z
DEEPSEEK_PRICING_SCHEDULE_VERSION = "2026-08-16T16:00:00Z"
DEEPSEEK_PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

# (input_cache_hit, input_cache_miss, output) integer micro-USD per million
# tokens.
DEEPSEEK_LEGACY_RATES_MICRO_USD_PER_MILLION: dict[str, tuple[int, int, int]] = {
    "deepseek-v4-flash": (2_800, 140_000, 280_000),
    "deepseek-v4-pro": (3_625, 435_000, 870_000),
}
DEEPSEEK_OFFPEAK_RATES_MICRO_USD_PER_MILLION: dict[str, tuple[int, int, int]] = {
    "deepseek-v4-flash": (7_000, 220_000, 660_000),
    "deepseek-v4-pro": (22_000, 660_000, 1_980_000),
}
DEEPSEEK_PEAK_RATES_MICRO_USD_PER_MILLION: dict[str, tuple[int, int, int]] = {
    "deepseek-v4-flash": (14_000, 440_000, 1_320_000),
    "deepseek-v4-pro": (44_000, 1_320_000, 3_960_000),
}

# Defensive ceiling for provider-reported per-call token fields. This is far
# above the hosted request envelope while keeping every token/rate product and
# persisted SQLite INTEGER safely bounded if an authenticated worker is buggy
# or compromised.
MAX_PROVIDER_USAGE_TOKENS_PER_FIELD = 1_000_000_000


def validate_provider_usage_tokens(**token_fields: int) -> None:
    """Reject negative or implausibly large provider usage token counts."""
    for field, raw_value in token_fields.items():
        try:
            value = int(raw_value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
        if value < 0 or value > MAX_PROVIDER_USAGE_TOKENS_PER_FIELD:
            raise ValueError(
                f"{field} must be between 0 and "
                f"{MAX_PROVIDER_USAGE_TOKENS_PER_FIELD}"
            )


def deepseek_pricing_for_timestamp(model: str, ts: float) -> dict | None:
    """Return the immutable time-versioned DeepSeek price for *model* at *ts*.

    ``ts`` is the local provider-submission timestamp (unix seconds). Returns a
    dict carrying the schedule version, tier (``legacy``/``peak``/``offpeak``),
    the pricing basis timestamp, and the applied hit/miss/output rates as
    integer micro-USD per million tokens. Unknown models return ``None`` so
    callers keep their conservative fallback behavior.
    """
    m = str(model or "").strip()
    if m not in DEEPSEEK_LEGACY_RATES_MICRO_USD_PER_MILLION:
        return None
    basis = float(ts)
    if basis < DEEPSEEK_PRICING_EFFECTIVE_TIMESTAMP:
        tier = "legacy"
        hit, miss, output = DEEPSEEK_LEGACY_RATES_MICRO_USD_PER_MILLION[m]
    else:
        hour = time.gmtime(basis).tm_hour
        if any(lo <= hour < hi for lo, hi in DEEPSEEK_PEAK_WINDOWS_UTC):
            tier = "peak"
            hit, miss, output = DEEPSEEK_PEAK_RATES_MICRO_USD_PER_MILLION[m]
        else:
            tier = "offpeak"
            hit, miss, output = DEEPSEEK_OFFPEAK_RATES_MICRO_USD_PER_MILLION[m]
    return {
        "model": m,
        "schedule_version": DEEPSEEK_PRICING_SCHEDULE_VERSION,
        "tier": tier,
        "pricing_basis_ts": basis,
        "input_cache_hit_micro_usd_per_million": hit,
        "input_cache_miss_micro_usd_per_million": miss,
        "output_micro_usd_per_million": output,
    }


def normalize_deepseek_tokens(
    prompt_tokens: int,
    prompt_cache_hit_tokens: int,
    prompt_cache_miss_tokens: int,
    completion_tokens: int,
    total_tokens: int = 0,
) -> tuple[int, int, int, int, int]:
    """Apply the conservative DeepSeek token-normalization rules.

    Shared by the worker estimator and the control-plane settlement
    recomputation so both derive identical token breakdowns. Returns
    ``(prompt, hit, miss, completion, total)``:
    - absent cache breakdown → all prompt tokens billed as cache miss;
    - hit+miss is never allowed to exceed prompt tokens;
    - any unaccounted remainder is billed as cache miss.
    """
    validate_provider_usage_tokens(
        prompt_tokens=prompt_tokens,
        prompt_cache_hit_tokens=prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=prompt_cache_miss_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    prompt = max(0, int(prompt_tokens or 0))
    hit = max(0, int(prompt_cache_hit_tokens or 0))
    miss = max(0, int(prompt_cache_miss_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    if hit <= 0 and miss <= 0:
        miss = prompt
    if hit + miss > prompt:
        hit = min(hit, prompt)
        miss = min(miss, prompt)
        if hit + miss > prompt:
            excess = hit + miss - prompt
            hit = max(0, hit - excess)
    if hit + miss < prompt:
        miss += prompt - (hit + miss)
    total = max(0, int(total_tokens or 0))
    if total <= 0:
        total = prompt + completion
    return prompt, hit, miss, completion, total


def round_cost_micro(numerator: int) -> int:
    """Round a token×rate numerator to nearest integer micro-USD (half-up)."""
    return (max(0, numerator) + 500_000) // 1_000_000


def deepseek_cost_for_tokens(
    model: str,
    basis_ts: float,
    prompt_tokens: int,
    prompt_cache_hit_tokens: int,
    prompt_cache_miss_tokens: int,
    completion_tokens: int,
    total_tokens: int = 0,
) -> dict:
    """Authoritative DeepSeek cost estimate from a token breakdown.

    Normalizes tokens with the shared conservative rules, looks up the
    immutable time-of-use schedule for *model* at *basis_ts*, and computes
    integer micro-USD with deterministic half-up rounding. Returns a dict with
    the normalized tokens, ``cost_micro_usd``, ``cost_present`` (known-priced
    model with nonzero usage), and the pricing audit metadata (``None`` when
    the model is not in the schedule).
    """
    pricing = deepseek_pricing_for_timestamp(model, basis_ts)
    prompt, hit, miss, completion, total = normalize_deepseek_tokens(
        prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
        completion_tokens, total_tokens,
    )
    has_usage = bool(hit or miss or completion)
    cost_micro = 0
    if pricing is not None and has_usage:
        numerator = (
            hit * pricing["input_cache_hit_micro_usd_per_million"]
            + miss * pricing["input_cache_miss_micro_usd_per_million"]
            + completion * pricing["output_micro_usd_per_million"]
        )
        cost_micro = round_cost_micro(numerator)
    return {
        "pricing": pricing,
        "cost_micro_usd": cost_micro,
        "cost_present": pricing is not None and has_usage,
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _validate_pricing_against_authoritative(
    authoritative: dict,
    schedule_version: str,
    tier: str,
    basis_ts: float | None,
    hit_rate: int,
    miss_rate: int,
    out_rate: int,
) -> None:
    """Validate worker pricing metadata against the authoritative schedule.

    ``authoritative`` is the schedule derived from the reservation's recorded
    ``submitted_at``. Absent metadata is allowed (an old worker) and is simply
    not validated; any supplied metadata must match the authoritative version,
    tier, basis timestamp (within a tiny tolerance), and rates, otherwise
    ``ValueError`` is raised.
    """
    sv = str(schedule_version or "").strip()
    t = str(tier or "").strip()
    has_any = bool(sv or t or basis_ts is not None or hit_rate or miss_rate or out_rate)
    if not has_any:
        return

    if sv != authoritative["schedule_version"]:
        raise ValueError("pricing schedule version is not recognized")
    if t != authoritative["tier"]:
        raise ValueError("pricing tier does not match the authoritative submitted_at")
    try:
        basis = float(basis_ts)
    except (TypeError, ValueError) as exc:
        raise ValueError("pricing basis timestamp must be numeric") from exc
    if not math.isfinite(basis):
        raise ValueError("pricing basis timestamp must be finite")
    if abs(basis - authoritative["pricing_basis_ts"]) > 1.0:
        raise ValueError("pricing basis timestamp does not match reservation submitted_at")
    try:
        supplied = (int(hit_rate or 0), int(miss_rate or 0), int(out_rate or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("pricing rates must be integers") from exc
    expected_rates = (
        authoritative["input_cache_hit_micro_usd_per_million"],
        authoritative["input_cache_miss_micro_usd_per_million"],
        authoritative["output_micro_usd_per_million"],
    )
    if supplied != expected_rates:
        raise ValueError("pricing rates do not match the authoritative schedule")


def _validate_pricing_audit_metadata(
    model: str,
    schedule_version: str,
    tier: str,
    basis_ts: float | None,
    hit_rate: int,
    miss_rate: int,
    out_rate: int,
) -> tuple[str | None, str | None, float | None, int | None, int | None, int | None]:
    """Validate and normalize worker-supplied time-of-use pricing metadata.

    The worker is not fully trusted, so its claims are reconciled against the
    immutable schedule for *model* at *basis_ts*. Returns the sanitized fields
    (all ``None`` when no pricing metadata was supplied — non-DeepSeek or
    unpriced calls), or raises ``ValueError`` (surfaced as HTTP 400) when
    metadata is present but inconsistent with the schedule.
    """
    sv = str(schedule_version or "").strip()
    t = str(tier or "").strip()
    has_any = bool(
        sv or t or basis_ts is not None or hit_rate or miss_rate or out_rate
    )
    if not has_any:
        return None, None, None, None, None, None

    if sv != DEEPSEEK_PRICING_SCHEDULE_VERSION:
        raise ValueError("pricing schedule version is not recognized")
    if t not in ("legacy", "peak", "offpeak"):
        raise ValueError("pricing tier must be one of legacy/peak/offpeak")
    try:
        basis = float(basis_ts)
    except (TypeError, ValueError) as exc:
        raise ValueError("pricing basis timestamp must be numeric") from exc
    if not math.isfinite(basis) or basis < 0:
        raise ValueError("pricing basis timestamp must be finite and nonnegative")

    expected = deepseek_pricing_for_timestamp(model, basis)
    if expected is None:
        raise ValueError("pricing metadata supplied for an unpriced model")
    if expected["tier"] != t:
        raise ValueError("pricing tier does not match the schedule for this timestamp")
    try:
        supplied = (int(hit_rate or 0), int(miss_rate or 0), int(out_rate or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("pricing rates must be integers") from exc
    expected_rates = (
        expected["input_cache_hit_micro_usd_per_million"],
        expected["input_cache_miss_micro_usd_per_million"],
        expected["output_micro_usd_per_million"],
    )
    if supplied != expected_rates:
        raise ValueError("pricing rates do not match the schedule for this timestamp")
    return (
        expected["schedule_version"],
        expected["tier"],
        basis,
        expected_rates[0],
        expected_rates[1],
        expected_rates[2],
    )


# ---------------------------------------------------------------------------
# Model catalog / allowlist
# ---------------------------------------------------------------------------

# Operational pricing/usage certification reviewed 2026-08-08 against the
# published OpenRouter rates. It assumes a 425k input-token envelope (the 400k
# serialized-message budget plus provider/tool-schema headroom), 4,096 output
# tokens, and a 25% margin. This deliberately certifies the fixed catalog only;
# it is not a universal character-to-token proof for arbitrary custom models.
#
# Rates are micro-USD per million tokens. Where a model has input-size tiers,
# the fixture uses the highest tier reachable by the certified envelope. Update
# this fixture and re-run its regression test whenever catalog pricing changes.
HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS = 400_000
HOSTED_HOLD_CERTIFIED_INPUT_TOKENS = 425_000
HOSTED_HOLD_CERTIFIED_OUTPUT_TOKENS = 4_096
HOSTED_HOLD_CERTIFIED_MARGIN_NUMERATOR = 125
HOSTED_HOLD_CERTIFIED_MARGIN_DENOMINATOR = 100
HOSTED_HOLD_CERTIFIED_RATES_MICRO_USD_PER_MILLION_TOKENS: dict[str, tuple[int, int]] = {
    "google/gemini-3.1-flash-lite": (250_000, 1_500_000),
    "google/gemini-3.5-flash-lite": (300_000, 2_500_000),
    "google/gemini-2.5-flash-lite": (100_000, 400_000),
    "google/gemini-2.5-flash": (300_000, 2_500_000),
    # >=200k input-token tier
    "google/gemini-2.5-pro": (2_500_000, 15_000_000),
    "google/gemini-3-flash-preview": (500_000, 3_000_000),
    # >=256k input-token tier
    "qwen/qwen3.6-plus": (1_300_000, 3_900_000),
    "qwen/qwen3.5-flash-02-23": (65_000, 260_000),
    # DeepSeek direct — cache-miss input tier and output rate use the NEW
    # time-of-use PEAK worst-case schedule so the fixed hold covers the most
    # expensive hour of the day. Flash hold $0.25 and Pro hold $1.00 remain
    # certified sufficient against these peak rates.
    "deepseek-v4-flash": (440_000, 1_320_000),   # peak miss $0.44 / output $1.32
    "deepseek-v4-pro": (1_320_000, 3_960_000),   # peak miss $1.32 / output $3.96
}


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _certified_min_reservation_micro_usd(
    input_rate_micro_usd_per_million: int,
    output_rate_micro_usd_per_million: int,
) -> int:
    usage_numerator = (
        HOSTED_HOLD_CERTIFIED_INPUT_TOKENS * input_rate_micro_usd_per_million
        + HOSTED_HOLD_CERTIFIED_OUTPUT_TOKENS * output_rate_micro_usd_per_million
    )
    return _ceil_div(
        usage_numerator * HOSTED_HOLD_CERTIFIED_MARGIN_NUMERATOR,
        MICRO_USD_PER_USD * HOSTED_HOLD_CERTIFIED_MARGIN_DENOMINATOR,
    )


HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD: dict[str, int] = {
    model: _certified_min_reservation_micro_usd(*rates)
    for model, rates in HOSTED_HOLD_CERTIFIED_RATES_MICRO_USD_PER_MILLION_TOKENS.items()
}

HOSTED_MODEL_CATALOG: dict[str, int] = {
    # Paid models — fixed holds certified for the context budget above.
    "google/gemini-3.1-flash-lite": 150_000,
    "google/gemini-3.5-flash-lite": 200_000,
    "google/gemini-2.5-flash-lite": 100_000,
    "google/gemini-2.5-flash": 250_000,
    "google/gemini-2.5-pro": 1_500_000,
    "google/gemini-3-flash-preview": 300_000,
    "qwen/qwen3.6-plus": 750_000,
    "qwen/qwen3.5-flash-02-23": 250_000,
    # DeepSeek direct API — paid hosted models. Conservative per-attempt holds
    # (the worker settles actual cost estimated from cache-aware token usage).
    "deepseek-v4-flash": 250_000,   # $0.25 hold
    "deepseek-v4-pro": 1_000_000,   # $1.00 hold (reasoning model, 3x output price)
    # Free models — zero hold (no reservation needed)
    "google/gemma-3-27b-it:free": 0,
    "nvidia/nemotron-3-nano-30b-a3b:free": 0,
    "poolside/laguna-xs-2.1:free": 0,
    "meta-llama/llama-3.3-70b-instruct:free": 0,
    "deepseek/deepseek-chat-v3-0324:free": 0,
    "deepseek/deepseek-chat:free": 0,
    "deepseek/deepseek-r1:free": 0,
    "nvidia/nemotron-3-super-120b-a12b:free": 0,
}


def validate_hosted_context_budget(max_internal_context_chars: int) -> None:
    """Fail closed when the worker budget exceeds reviewed catalog holds."""
    try:
        budget = int(max_internal_context_chars)
    except (TypeError, ValueError) as exc:
        raise ValueError("hosted internal context budget must be an integer") from exc
    if budget > HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS:
        raise ValueError(
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS exceeds the credit-hold "
            f"certification of {HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS}"
        )

    paid_catalog = {
        model for model, hold in HOSTED_MODEL_CATALOG.items() if int(hold) > 0
    }
    certified_models = set(HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD)
    if paid_catalog != certified_models:
        missing = sorted(paid_catalog - certified_models)
        stale = sorted(certified_models - paid_catalog)
        raise RuntimeError(
            "hosted credit certification does not match the paid catalog "
            f"(missing={missing}, stale={stale})"
        )

    underfunded = {
        model: (HOSTED_MODEL_CATALOG[model], minimum)
        for model, minimum in HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD.items()
        if HOSTED_MODEL_CATALOG[model] < minimum
    }
    if underfunded:
        raise RuntimeError(
            "hosted credit holds are below their certified minimums: "
            f"{underfunded}"
        )

HOSTED_MODEL_POLICY_SETTING_KEY = "hosted_openrouter_models"
HOSTED_MODEL_POLICY_VERSION = 1
HOSTED_MODEL_POLICY_MAX_MODELS = 64
HOSTED_MODEL_POLICY_MAX_ID_LENGTH = 200
HOSTED_FREE_MODEL_DEFAULTS: tuple[str, ...] = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "poolside/laguna-xs-2.1:free",
)
HOSTED_USER_MODEL_DEFAULTS: tuple[str, ...] = (
    "deepseek-v4-flash",
    "google/gemini-3.1-flash-lite",
    "qwen/qwen3.6-plus",
    "qwen/qwen3.5-flash-02-23",
    "google/gemini-3-flash-preview",
    "deepseek-v4-pro",
    *HOSTED_FREE_MODEL_DEFAULTS,
)
_MODEL_ID_EXTRA_CHARS = frozenset("._-/:+@")
_NON_OPENROUTER_MODEL_PREFIXES = (
    "claude-sdk:",
    "codex-cli:",
    "codex-sdk:",
    "opencode-cli:",
)

_DEFAULT_CONSERVATIVE_RESERVATION_MICRO_USD: int = max(
    1, int(os.environ.get("CREDIT_DEFAULT_RESERVATION_MICRO_USD", "1000000"))
)  # default $1.00 for explicitly allowlisted models without catalog pricing

# Stale-run sweep TTL (seconds). Active runs older than this are expired.
STALE_RUN_TTL_SECONDS: int = max(
    60, int(os.environ.get("CREDIT_STALE_RUN_TTL_SECONDS", "7200"))
)  # default 2 hours


def _default_reservation(model: str) -> int:
    m = (model or "").strip()
    if not m:
        return _DEFAULT_CONSERVATIVE_RESERVATION_MICRO_USD
    catalog_val = HOSTED_MODEL_CATALOG.get(m)
    if catalog_val is not None:
        return catalog_val  # 0 for :free models, cents for paid
    return _DEFAULT_CONSERVATIVE_RESERVATION_MICRO_USD


def is_openrouter_model_id(model: str) -> bool:
    """Return whether *model* is a bounded, syntactically safe OpenRouter ID."""
    value = str(model or "").strip()
    if (
        not value
        or not value.isascii()
        or len(value) > HOSTED_MODEL_POLICY_MAX_ID_LENGTH
    ):
        return False
    if value.startswith(_NON_OPENROUTER_MODEL_PREFIXES):
        return False
    parts = value.split("/")
    if len(parts) < 2 or any(not part for part in parts):
        return False
    return all(ch.isalnum() or ch in _MODEL_ID_EXTRA_CHARS for ch in value)


_DEEPSEEK_MODEL_PREFIX = "deepseek-"


def is_deepseek_model_id(model: str) -> bool:
    """Return whether *model* is a bounded, syntactically safe DeepSeek direct ID.

    DeepSeek model IDs are slash-free (e.g. ``deepseek-v4-flash``,
    ``deepseek-v4-pro``) and route through the DeepSeek direct API rather than
    OpenRouter.
    """
    value = str(model or "").strip()
    if (
        not value
        or not value.isascii()
        or len(value) > HOSTED_MODEL_POLICY_MAX_ID_LENGTH
        or not value.startswith(_DEEPSEEK_MODEL_PREFIX)
    ):
        return False
    rest = value[len(_DEEPSEEK_MODEL_PREFIX):]
    return bool(rest) and all(ch.isalnum() or ch in "._-" for ch in rest)


def is_hosted_model_id(model: str) -> bool:
    """Return whether *model* is a valid hosted (OpenRouter or DeepSeek) ID."""
    return is_openrouter_model_id(model) or is_deepseek_model_id(model)


# Provider → model-ID prefix used to scope metering/reconciliation rows.
_PROVIDER_MODEL_PREFIXES: dict[str, str] = {
    "deepseek": "deepseek-",
}


def _provider_model_scope(provider: str) -> tuple[str | None, list]:
    """Return (SQL LIKE pattern, params) scoping usage rows to a provider.

    Providers with a deterministic model-ID prefix (e.g. ``deepseek-``) get a
    LIKE filter so reconciliation never mixes providers; unknown providers get
    ``(None, [])`` meaning "all rows".
    """
    prefix = _PROVIDER_MODEL_PREFIXES.get(str(provider or "").strip())
    if prefix:
        return f"{prefix}%", []
    return None, []


def normalize_hosted_model_ids(
    models,
    *,
    required_models=(),
    reject_duplicates: bool = True,
) -> tuple[str, ...]:
    """Validate and normalize an ordered hosted-model policy list."""
    if not isinstance(models, (list, tuple)):
        raise ValueError("models must be an array")
    if not models:
        raise ValueError("at least one model is required")
    if len(models) > HOSTED_MODEL_POLICY_MAX_MODELS:
        raise ValueError(
            f"at most {HOSTED_MODEL_POLICY_MAX_MODELS} models are allowed"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in models:
        model = str(raw or "").strip()
        if not is_hosted_model_id(model):
            raise ValueError(f"invalid hosted model ID: {model or '<empty>'}")
        if model in seen:
            if reject_duplicates:
                raise ValueError(f"duplicate model ID: {model}")
            continue
        seen.add(model)
        normalized.append(model)

    required = []
    for raw in required_models:
        model = str(raw or "").strip()
        if model and model not in required:
            required.append(model)
    invalid_required = [model for model in required if not is_hosted_model_id(model)]
    if invalid_required:
        raise ValueError(
            "invalid required hosted model ID: " + ", ".join(invalid_required)
        )
    missing = [model for model in required if model not in seen]
    if missing:
        raise ValueError("model list must include required models: " + ", ".join(missing))
    return tuple(normalized)


def _runtime_hosted_model_requirements(core) -> tuple[str, tuple[str, ...], str]:
    post_cap = tuple(
        str(model or "").strip()
        for model in getattr(
            core,
            "_OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS",
            HOSTED_FREE_MODEL_DEFAULTS,
        )
        if str(model or "").strip()
    )
    if not post_cap:
        post_cap = HOSTED_FREE_MODEL_DEFAULTS
    # The authenticated /workspace default is decoupled from the guest/trial
    # lane: `_OPENROUTER_TRIAL_DEFAULT_MODEL` (a free OpenRouter model) remains
    # the no-model fallback for guest/pending traffic in chat_stream/chat_flow,
    # while HOSTED_DEFAULT_MODEL is the canonical paid-lane default. When unset
    # it falls back to HOSTED_USER_MODEL_DEFAULTS[0] (deepseek-v4-flash) so
    # local runs match production.
    default_model = str(
        os.environ.get("HOSTED_DEFAULT_MODEL", "")
        or HOSTED_USER_MODEL_DEFAULTS[0]
    ).strip()
    fallback_model = str(
        getattr(core, "_OPENROUTER_TRIAL_FALLBACK_MODEL", post_cap[0]) or post_cap[0]
    ).strip()
    return default_model, post_cap, fallback_model


def effective_hosted_model_policy(core) -> dict:
    """Load the ordered non-admin model policy, falling back safely on errors."""
    default_model, post_cap, fallback_model = _runtime_hosted_model_requirements(core)
    required = tuple(dict.fromkeys((default_model, fallback_model, *post_cap)))
    built_in = tuple(dict.fromkeys((*HOSTED_USER_MODEL_DEFAULTS, *required)))
    built_in = normalize_hosted_model_ids(
        built_in,
        required_models=required,
        reject_duplicates=False,
    )

    configured = False
    models = built_in
    updated_at = None
    updated_by = ""
    auth_store = getattr(core, "_auth", None)
    try:
        record_getter = getattr(auth_store, "get_app_setting_record", None)
        if callable(record_getter):
            record = record_getter(HOSTED_MODEL_POLICY_SETTING_KEY)
        else:
            value_getter = getattr(auth_store, "get_app_setting", None)
            value = value_getter(HOSTED_MODEL_POLICY_SETTING_KEY) if callable(value_getter) else None
            record = {"value": value} if value is not None else None
        if record is not None:
            value = record.get("value")
            if not isinstance(value, dict) or value.get("version") != HOSTED_MODEL_POLICY_VERSION:
                raise ValueError("unsupported hosted model policy format")
            models = normalize_hosted_model_ids(
                value.get("models"),
                required_models=required,
            )
            configured = True
            updated_at = record.get("updated_at")
            updated_by = str(record.get("updated_by", "") or "")
    except Exception as exc:
        log.error("Invalid hosted model policy; using built-in defaults: %s", exc)

    return {
        "version": HOSTED_MODEL_POLICY_VERSION,
        "models": list(models),
        "default_model": default_model,
        "fallback_model": fallback_model,
        "post_cap_models": list(post_cap),
        "required_models": list(required),
        "configured": configured,
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


def is_hosted_admin_identity(
    core,
    *,
    user_id: str = "",
    email: str = "",
) -> bool:
    """Resolve hosted admin status from trusted server-side identity data."""
    resolved_email = str(email or "").strip().lower()
    if not resolved_email and user_id:
        finder = getattr(getattr(core, "_auth", None), "find_user_by_id", None)
        if callable(finder):
            user = finder(str(user_id).strip())
            if user:
                resolved_email = str(user.get("email", "") or "").strip().lower()
    admin_emails = {
        str(candidate or "").strip().lower()
        for candidate in getattr(core, "ADMIN_EMAILS", ())
        if str(candidate or "").strip()
    }
    return bool(resolved_email and resolved_email in admin_emails)


def hosted_model_reservation_policy(
    core,
    model: str,
    *,
    user_id: str = "",
    email: str = "",
) -> dict:
    """Return the nominal hold and whether it may use all remaining admin credit.

    Unknown models keep the conservative default hold for non-admin users. For
    a trusted admin identity, the authoritative ledger may atomically cap that
    nominal hold to the account's available balance. This avoids silently
    replacing an explicitly selected custom model when slightly less than the
    conservative hold remains, while still preventing concurrent overspend.
    """
    value = str(model or "").strip()
    nominal = _default_reservation(value)
    admin_custom = bool(
        value
        and value not in HOSTED_MODEL_CATALOG
        and is_hosted_admin_identity(core, user_id=user_id, email=email)
    )
    return {
        "reservation_micro_usd": nominal,
        "cap_to_available": admin_custom,
        "admin_custom": admin_custom,
    }


def hosted_model_credit_allows(reservation_policy: dict, available_micro_usd: int) -> bool:
    """Apply the same preflight rule used by the authoritative reservation."""
    available = max(0, int(available_micro_usd or 0))
    if reservation_policy.get("cap_to_available"):
        return available > 0
    return available >= max(
        0, int(reservation_policy.get("reservation_micro_usd", 0) or 0)
    )


def is_hosted_model_allowed_for_identity(
    core,
    model: str,
    *,
    user_id: str = "",
    email: str = "",
) -> bool:
    """Apply current admin/non-admin hosted model availability policy."""
    value = str(model or "").strip()
    if not is_hosted_model_id(value):
        return False
    if is_hosted_admin_identity(core, user_id=user_id, email=email):
        return True
    policy = effective_hosted_model_policy(core)
    return value in set(policy["models"])


def is_hosted_model_allowed(
    model: str,
    *,
    admin_allowlist: set[str] | None = None,
    allow_slash_models: bool | None = None,
) -> bool:
    m = (model or "").strip()
    if not m:
        return False
    if m in HOSTED_MODEL_CATALOG:
        return True
    effective_allowlist = _ADMIN_ALLOWLIST if admin_allowlist is None else admin_allowlist
    if effective_allowlist and m in effective_allowlist:
        return True
    # The argument remains available for explicit, in-process policy tests, but
    # production configuration cannot wildcard every provider/model. Unknown
    # hosted models must be named in CREDIT_ADMIN_ALLOWLIST and receive the
    # conservative $1 default hold.
    if allow_slash_models is True and is_hosted_model_id(m):
        return True
    return False


_ADMIN_ALLOWLIST_ENV = os.environ.get("CREDIT_ADMIN_ALLOWLIST", "").strip()
_ADMIN_ALLOWLIST: set[str] = set(
    m.strip() for m in _ADMIN_ALLOWLIST_ENV.split(",") if m.strip()
)
# ---------------------------------------------------------------------------
# Exceptions (defined before CreditLedger so class-level constants can
# reference them)
# ---------------------------------------------------------------------------

class InsufficientBalanceError(Exception):
    """Not enough available credit (balance minus held reservations)."""
    pass


class RunNotActiveError(Exception):
    """Run is not in active state."""
    pass


# ---------------------------------------------------------------------------
# CreditLedger
# ---------------------------------------------------------------------------

class CreditLedger:
    """Atomic credit accounting backed by SQLite.

    **Ledger invariant:** The cached ``balance_micro_usd`` in
    ``credit_accounts`` equals the sum of ``amount_micro_usd`` for all
    monetary ledger entries (``grant``, ``reversal``, ``settlement``,
    and ``fee``).  ``reservation`` and ``release`` entries record holds
    without changing the cached balance — the hold against available
    funds is computed from ``credit_call_reservations.status='held'``.

    ``CreditLedger`` implements the context-manager protocol
    (``__enter__`` / ``__exit__``) so callers can integrate with ``with``
    blocks.  A process-level instance cache lets callers reuse one
    ledger without re-running DDL for every internal provider call.
    """

    _instances: dict[str, CreditLedger] = {}
    _instances_lock = threading.Lock()

    def __new__(cls, db_path: str):
        with cls._instances_lock:
            if db_path in cls._instances:
                return cls._instances[db_path]
            instance = super().__new__(cls)
            instance._init_lock = threading.Lock()
            cls._instances[db_path] = instance
            return instance

    def __init__(self, db_path: str):
        # __init__ may be called again on a cached instance — only
        # initialise schema if this path hasn't been seen before.
        with self._init_lock:
            if getattr(self, "_schema_initialised", False):
                return
            self.db_path = db_path
            self._init_schema()
            self._schema_initialised = True

    def __enter__(self) -> CreditLedger:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Connections are short-lived (per-op); nothing to close here.
        # The context manager exists so tests can use ``with`` blocks
        # and callers can integrate with existing context-manager patterns.
        return

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        _configure_connection(conn)
        try:
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            # WAL persists in the database. Enabling it after busy_timeout is
            # configured avoids immediate lock failures during process races.
            conn.execute("PRAGMA journal_mode=WAL")
            _begin_immediate(conn)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_accounts (
                    account_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    balance_micro_usd INTEGER NOT NULL DEFAULT 0,
                    total_granted_micro_usd INTEGER NOT NULL DEFAULT 0,
                    total_spent_micro_usd INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (balance_micro_usd >= 0)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_ledger (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    entry_type TEXT NOT NULL CHECK (
                        entry_type IN ('grant', 'reversal', 'reservation',
                                       'settlement', 'release', 'fee')
                    ),
                    amount_micro_usd INTEGER NOT NULL,
                    balance_after_micro_usd INTEGER NOT NULL,
                    run_id TEXT,
                    call_id TEXT,
                    model TEXT,
                    metadata_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES credit_accounts(account_id),
                    UNIQUE (account_id, idempotency_key)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_runs (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (
                        status IN ('active', 'completed', 'cancelled', 'exhausted')
                    ),
                    reserved_micro_usd INTEGER NOT NULL DEFAULT 0,
                    settled_micro_usd INTEGER NOT NULL DEFAULT 0,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    metadata_json TEXT DEFAULT '{}',
                    FOREIGN KEY (account_id) REFERENCES credit_accounts(account_id),
                    UNIQUE (account_id, idempotency_key)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_call_reservations (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'held' CHECK (
                        status IN ('held', 'settled', 'released')
                    ),
                    reserved_micro_usd INTEGER NOT NULL,
                    settled_micro_usd INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    submitted_at REAL,
                    settled_at REAL,
                    released_at REAL,
                    FOREIGN KEY (run_id) REFERENCES credit_runs(run_id),
                    FOREIGN KEY (account_id) REFERENCES credit_accounts(account_id),
                    UNIQUE (run_id, idempotency_key)
                )
            """)

            # Forward-compatible migration for databases created before the
            # persisted pre-submit/submitted distinction was introduced.
            reservation_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(credit_call_reservations)"
                ).fetchall()
            }
            if "submitted_at" not in reservation_columns:
                conn.execute(
                    "ALTER TABLE credit_call_reservations ADD COLUMN submitted_at REAL"
                )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_provider_usage (
                    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                    provider_cost_micro_usd INTEGER NOT NULL DEFAULT 0,
                    provider_response_json TEXT DEFAULT '{}',
                    pricing_schedule_version TEXT,
                    pricing_tier TEXT,
                    pricing_basis_ts REAL,
                    input_cache_hit_rate_micro_usd_per_million INTEGER,
                    input_cache_miss_rate_micro_usd_per_million INTEGER,
                    output_rate_micro_usd_per_million INTEGER,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (call_id) REFERENCES credit_call_reservations(call_id),
                    FOREIGN KEY (run_id) REFERENCES credit_runs(run_id),
                    FOREIGN KEY (account_id) REFERENCES credit_accounts(account_id)
                )
            """)

            # Cache-aware token breakdown (DeepSeek direct) — added via ALTER
            # for pre-existing databases; the CREATE TABLE above includes them
            # only for fresh installs, so keep the ALTER path authoritative.
            provider_usage_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(credit_provider_usage)"
                ).fetchall()
            }
            if "prompt_cache_hit_tokens" not in provider_usage_columns:
                conn.execute(
                    "ALTER TABLE credit_provider_usage "
                    "ADD COLUMN prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "prompt_cache_miss_tokens" not in provider_usage_columns:
                conn.execute(
                    "ALTER TABLE credit_provider_usage "
                    "ADD COLUMN prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0"
                )

            # Time-versioned DeepSeek pricing audit metadata (nullable so
            # pre-existing usage rows are never repriced — they simply lack a
            # pricing tier/rates rather than claiming a fabricated one).
            pricing_audit_columns = {
                "pricing_schedule_version": "TEXT",
                "pricing_tier": "TEXT",
                "pricing_basis_ts": "REAL",
                "input_cache_hit_rate_micro_usd_per_million": "INTEGER",
                "input_cache_miss_rate_micro_usd_per_million": "INTEGER",
                "output_rate_micro_usd_per_million": "INTEGER",
            }
            for column, sql_type in pricing_audit_columns.items():
                if column not in provider_usage_columns:
                    conn.execute(
                        f"ALTER TABLE credit_provider_usage "
                        f"ADD COLUMN {column} {sql_type}"
                    )

            # Indexes — including created_at for stale-run sweep + audit
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_ledger_account "
                "ON credit_ledger(account_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_runs_account "
                "ON credit_runs(account_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_runs_user "
                "ON credit_runs(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_runs_created_at "
                "ON credit_runs(created_at, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_call_reservations_run "
                "ON credit_call_reservations(run_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_call_reservations_created "
                "ON credit_call_reservations(created_at, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_credit_provider_usage_created "
                "ON credit_provider_usage(created_at)"
            )

            # Provider account-balance snapshots (cost reconciliation). Written
            # by the hosted worker (which holds the provider key) and read by
            # the server-side reconciliation job to compare realized balance
            # deltas against ledger-estimated spend.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS provider_balance_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total_balance REAL NOT NULL DEFAULT 0,
                    granted_balance REAL,
                    topped_up_balance REAL,
                    is_available INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            # Component balance columns (granted/topped-up) — added via ALTER
            # for pre-existing databases so balance snapshots can distinguish
            # grant expiry from genuine top-ups without repricing old rows.
            snapshot_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(provider_balance_snapshots)"
                ).fetchall()
            }
            if "granted_balance" not in snapshot_columns:
                conn.execute(
                    "ALTER TABLE provider_balance_snapshots "
                    "ADD COLUMN granted_balance REAL"
                )
            if "topped_up_balance" not in snapshot_columns:
                conn.execute(
                    "ALTER TABLE provider_balance_snapshots "
                    "ADD COLUMN topped_up_balance REAL"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provider_balance_snapshots_provider "
                "ON provider_balance_snapshots(provider, created_at)"
            )

            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def ensure_account(self, user_id: str) -> dict:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")
        now = _now_ts()
        with self._conn() as conn:
            _begin_immediate(conn)
            return self._ensure_account_under_lock(conn, uid, now)

    @staticmethod
    def _ensure_account_under_lock(
        conn: sqlite3.Connection, user_id: str, now: float
    ) -> dict:
        """Read or create an account within an existing BEGIN IMMEDIATE context."""
        row = conn.execute(
            "SELECT account_id, user_id, balance_micro_usd, total_granted_micro_usd, "
            "total_spent_micro_usd, created_at, updated_at "
            "FROM credit_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return {
                "account_id": row[0],
                "user_id": row[1],
                "balance_micro_usd": int(row[2]),
                "total_granted_micro_usd": int(row[3]),
                "total_spent_micro_usd": int(row[4]),
                "balance_usd": _micro_to_usd(int(row[2])),
                "total_granted_usd": _micro_to_usd(int(row[3])),
                "total_spent_usd": _micro_to_usd(int(row[4])),
                "created_at": row[5],
                "updated_at": row[6],
            }
        account_id = f"ca-{_uuid_hex()[:12]}"
        conn.execute(
            "INSERT INTO credit_accounts (account_id, user_id, balance_micro_usd, "
            "total_granted_micro_usd, total_spent_micro_usd, created_at, updated_at) "
            "VALUES (?, ?, 0, 0, 0, ?, ?)",
            (account_id, user_id, now, now),
        )
        return {
            "account_id": account_id,
            "user_id": user_id,
            "balance_micro_usd": 0,
            "total_granted_micro_usd": 0,
            "total_spent_micro_usd": 0,
            "balance_usd": 0.0,
            "total_granted_usd": 0.0,
            "total_spent_usd": 0.0,
            "created_at": now,
            "updated_at": now,
        }

    def get_account(self, user_id: str) -> dict | None:
        uid = str(user_id or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT account_id, user_id, balance_micro_usd, total_granted_micro_usd, "
                "total_spent_micro_usd, created_at, updated_at "
                "FROM credit_accounts WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                return None
            return {
                "account_id": row[0],
                "user_id": row[1],
                "balance_micro_usd": int(row[2]),
                "total_granted_micro_usd": int(row[3]),
                "total_spent_micro_usd": int(row[4]),
                "balance_usd": _micro_to_usd(int(row[2])),
                "total_granted_usd": _micro_to_usd(int(row[3])),
                "total_spent_usd": _micro_to_usd(int(row[4])),
                "created_at": row[5],
                "updated_at": row[6],
            }

    def get_balance(self, user_id: str) -> int:
        acct = self.get_account(user_id)
        return acct["balance_micro_usd"] if acct else 0

    # ------------------------------------------------------------------
    # Idempotent grant / reversal (TOCTOU-safe)
    # ------------------------------------------------------------------

    def grant(self, user_id: str, amount_micro_usd: int, *,
              idempotency_key: str = "",
              metadata: dict | None = None) -> dict:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")
        if amount_micro_usd <= 0:
            raise ValueError("amount_micro_usd must be positive")

        ikey = str(idempotency_key or "").strip()
        if not ikey:
            ikey = f"grant-{uid}-{_uuid_hex()[:8]}-{int(_now_ts())}"

        now = _now_ts()

        with self._conn() as conn:
            _begin_immediate(conn)
            # Read account under the SAME lock — no separate ensure_account call
            account = self._ensure_account_under_lock(conn, uid, now)
            account_id = account["account_id"]

            existing = conn.execute(
                "SELECT entry_id, amount_micro_usd, balance_after_micro_usd "
                "FROM credit_ledger WHERE account_id = ? AND idempotency_key = ?",
                (account_id, ikey),
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {**account, "already_applied": True,
                        "entry_amount_micro_usd": existing[1],
                        "entry_balance_after": existing[2]}

            new_balance = account["balance_micro_usd"] + amount_micro_usd
            new_granted = account["total_granted_micro_usd"] + amount_micro_usd

            conn.execute(
                "UPDATE credit_accounts SET balance_micro_usd = ?, "
                "total_granted_micro_usd = ?, updated_at = ? "
                "WHERE account_id = ?",
                (new_balance, new_granted, now, account_id),
            )
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, metadata_json, created_at) "
                "VALUES (?, ?, 'grant', ?, ?, ?, ?)",
                (account_id, ikey, amount_micro_usd, new_balance,
                 _json_meta(metadata or {}), now),
            )
            conn.execute("COMMIT")

            return {
                **account,
                "balance_micro_usd": new_balance,
                "total_granted_micro_usd": new_granted,
                "balance_usd": _micro_to_usd(new_balance),
                "total_granted_usd": _micro_to_usd(new_granted),
                "already_applied": False,
            }

    def reverse_grant(self, user_id: str, amount_micro_usd: int, *,
                      idempotency_key: str = "",
                      metadata: dict | None = None) -> dict:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")
        if amount_micro_usd <= 0:
            raise ValueError("amount_micro_usd must be positive")

        ikey = str(idempotency_key or "").strip()
        if not ikey:
            ikey = f"reversal-{uid}-{_uuid_hex()[:8]}-{int(_now_ts())}"

        now = _now_ts()

        with self._conn() as conn:
            _begin_immediate(conn)
            # Read under the SAME lock
            account = self._ensure_account_under_lock(conn, uid, now)
            account_id = account["account_id"]

            existing = conn.execute(
                "SELECT entry_id FROM credit_ledger "
                "WHERE account_id = ? AND idempotency_key = ?",
                (account_id, ikey),
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {**account, "already_applied": True}

            held = self._held_reservation_total_under_lock(conn, account_id)
            available = account["balance_micro_usd"] - held
            if available < amount_micro_usd:
                conn.execute("ROLLBACK")
                raise InsufficientBalanceError(
                    f"Available balance {_micro_to_usd(available):.6f} USD "
                    f"(held={_micro_to_usd(held):.6f}) "
                    f"insufficient for reversal of {_micro_to_usd(amount_micro_usd):.6f}"
                )

            new_balance = account["balance_micro_usd"] - amount_micro_usd
            new_granted = account["total_granted_micro_usd"] - amount_micro_usd

            conn.execute(
                "UPDATE credit_accounts SET balance_micro_usd = ?, "
                "total_granted_micro_usd = ?, updated_at = ? "
                "WHERE account_id = ?",
                (new_balance, new_granted, now, account_id),
            )
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, metadata_json, created_at) "
                "VALUES (?, ?, 'reversal', ?, ?, ?, ?)",
                (account_id, ikey, -amount_micro_usd, new_balance,
                 _json_meta(metadata or {}), now),
            )
            conn.execute("COMMIT")

            return {
                **account,
                "balance_micro_usd": new_balance,
                "total_granted_micro_usd": max(0, new_granted),
                "balance_usd": _micro_to_usd(new_balance),
                "total_granted_usd": _micro_to_usd(max(0, new_granted)),
                "already_applied": False,
            }

    @staticmethod
    def _held_reservation_total_under_lock(conn: sqlite3.Connection, account_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(reserved_micro_usd - settled_micro_usd), 0) "
            "FROM credit_call_reservations WHERE account_id = ? AND status = 'held'",
            (account_id,),
        ).fetchone()
        return max(0, int(row[0] or 0))

    def held_reservation_total(self, account_id: str) -> int:
        with self._conn() as conn:
            return self._held_reservation_total_under_lock(conn, account_id)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, user_id: str, *,
                   model: str = "unknown",
                   idempotency_key: str = "",
                   metadata: dict | None = None) -> dict:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")

        ikey = str(idempotency_key or "").strip()
        if not ikey:
            raise ValueError("idempotency_key required for runs")

        now = _now_ts()
        model_name = (model or "unknown").strip()

        with self._conn() as conn:
            _begin_immediate(conn)
            account = self._ensure_account_under_lock(conn, uid, now)
            account_id = account["account_id"]

            existing = conn.execute(
                "SELECT run_id, status, reserved_micro_usd, settled_micro_usd "
                "FROM credit_runs WHERE account_id = ? AND idempotency_key = ?",
                (account_id, ikey),
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {
                    "run_id": existing[0],
                    "account_id": account_id,
                    "user_id": uid,
                    "model": model_name,
                    "status": existing[1],
                    "reserved_micro_usd": int(existing[2]),
                    "settled_micro_usd": int(existing[3]),
                    "already_exists": True,
                }

            run_id = f"run-{_uuid_hex()[:16]}"
            conn.execute(
                "INSERT INTO credit_runs "
                "(run_id, account_id, user_id, model, status, "
                "idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (run_id, account_id, uid, model_name, ikey, now),
            )
            conn.execute("COMMIT")

            return {
                "run_id": run_id,
                "account_id": account_id,
                "user_id": uid,
                "model": model_name,
                "status": "active",
                "reserved_micro_usd": 0,
                "settled_micro_usd": 0,
                "already_exists": False,
            }

    def finish_run(self, run_id: str, *,
                   status: str = "completed",
                   release_held: bool = True) -> dict:
        if status not in ("completed", "cancelled", "exhausted"):
            raise ValueError(f"Invalid finish status: {status}")

        now = _now_ts()
        with self._conn() as conn:
            _begin_immediate(conn)
            row = conn.execute(
                "SELECT run_id, account_id, user_id, status, reserved_micro_usd, "
                "settled_micro_usd FROM credit_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return {}
            if row[3] not in ("active",):
                conn.execute("COMMIT")
                return {
                    "run_id": row[0],
                    "account_id": row[1],
                    "user_id": row[2],
                    "status": row[3],
                    "already_finished": True,
                }

            if release_held:
                # A submitted provider request may have been billed even when
                # cancellation, timeout, or a lost callback prevents normal
                # settlement. Capture those holds conservatively. Reservations
                # that never crossed the provider boundary are safe to release.
                self._capture_submitted_calls_under_lock(conn, run_id, row[1], now)
                self._release_unsubmitted_calls_under_lock(conn, run_id, row[1], now)

            conn.execute(
                "UPDATE credit_runs SET status = ?, finished_at = ? "
                "WHERE run_id = ?",
                (status, now, run_id),
            )
            conn.execute("COMMIT")

            return {
                "run_id": run_id,
                "account_id": row[1],
                "user_id": row[2],
                "status": status,
            }

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT run_id, account_id, user_id, model, status, "
                "reserved_micro_usd, settled_micro_usd, call_count, "
                "created_at, finished_at "
                "FROM credit_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "run_id": row[0],
                "account_id": row[1],
                "user_id": row[2],
                "model": row[3],
                "status": row[4],
                "reserved_micro_usd": int(row[5]),
                "settled_micro_usd": int(row[6]),
                "call_count": int(row[7]),
                "created_at": row[8],
                "finished_at": row[9],
            }

    def get_runs_for_user(self, user_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT r.run_id, r.account_id, r.user_id, r.model, r.status, "
                "r.reserved_micro_usd, r.settled_micro_usd, r.call_count, "
                "r.created_at, r.finished_at, a.balance_micro_usd "
                "FROM credit_runs r "
                "JOIN credit_accounts a ON r.account_id = a.account_id "
                "WHERE r.user_id = ? "
                "ORDER BY r.created_at DESC LIMIT ?",
                (str(user_id or "").strip(), limit),
            ).fetchall()
        return [
            {
                "run_id": r[0],
                "account_id": r[1],
                "user_id": r[2],
                "model": r[3],
                "status": r[4],
                "reserved_micro_usd": int(r[5]),
                "reserved_usd": _micro_to_usd(int(r[5])),
                "settled_micro_usd": int(r[6]),
                "settled_usd": _micro_to_usd(int(r[6])),
                "call_count": int(r[7]),
                "created_at": r[8],
                "finished_at": r[9],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Per-call reservation/settle/release
    # ------------------------------------------------------------------

    def reserve_call(
        self,
        run_id: str,
        *,
        model: str = "",
        reservation_micro_usd: int = 0,
        idempotency_key: str = "",
        user_id: str = "",
        cap_reservation_to_available: bool = False,
    ) -> dict:
        if reservation_micro_usd < 0:
            reservation_micro_usd = 0
        if reservation_micro_usd == 0:
            reservation_micro_usd = _default_reservation(model)
        nominal_reservation_micro_usd = reservation_micro_usd

        ikey = str(idempotency_key or "").strip()
        if not ikey:
            raise ValueError("idempotency_key required for reservations")

        now = _now_ts()
        model_name = (model or "unknown").strip()

        with self._conn() as conn:
            _begin_immediate(conn)

            run_row = conn.execute(
                "SELECT run_id, account_id, user_id, status FROM credit_runs "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Run not found: {run_id}")
            if run_row[3] not in ("active",):
                conn.execute("ROLLBACK")
                raise RunNotActiveError(f"Run {run_id} is {run_row[3]}")

            account_id = run_row[1]
            run_owner = run_row[2]

            if user_id and user_id != run_owner:
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"user_id {user_id} does not own run {run_id} "
                    f"(owner: {run_owner})"
                )

            existing = conn.execute(
                "SELECT call_id, status, reserved_micro_usd, settled_micro_usd, "
                "submitted_at "
                "FROM credit_call_reservations "
                "WHERE run_id = ? AND idempotency_key = ?",
                (run_id, ikey),
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {
                    "call_id": existing[0],
                    "run_id": run_id,
                    "account_id": account_id,
                    "status": (
                        "submitted" if existing[1] == "held" and existing[4]
                        else existing[1]
                    ),
                    "reserved_micro_usd": int(existing[2]),
                    "nominal_reservation_micro_usd": nominal_reservation_micro_usd,
                    "reservation_capped": (
                        int(existing[2]) < nominal_reservation_micro_usd
                    ),
                    "submitted_at": existing[4],
                    "already_reserved": True,
                }

            acct_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = int(acct_row[0]) if acct_row else 0
            held = self._held_reservation_total_under_lock(conn, account_id)
            available = balance - held

            if (
                cap_reservation_to_available
                and nominal_reservation_micro_usd > 0
                and reservation_micro_usd > available
            ):
                reservation_micro_usd = max(0, available)

            if nominal_reservation_micro_usd > 0 and reservation_micro_usd <= 0:
                conn.execute("ROLLBACK")
                raise InsufficientBalanceError(
                    f"Available balance {_micro_to_usd(available):.6f} USD "
                    f"(held={_micro_to_usd(held):.6f}) is exhausted"
                )

            if available < reservation_micro_usd:
                conn.execute("ROLLBACK")
                raise InsufficientBalanceError(
                    f"Available balance {_micro_to_usd(available):.6f} USD "
                    f"(held={_micro_to_usd(held):.6f}) "
                    f"insufficient for reservation of {_micro_to_usd(reservation_micro_usd):.6f}"
                )

            call_id = f"call-{_uuid_hex()[:20]}"
            conn.execute(
                "INSERT INTO credit_call_reservations "
                "(call_id, run_id, account_id, user_id, model, status, "
                "reserved_micro_usd, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'held', ?, ?, ?)",
                (call_id, run_id, account_id, run_owner, model_name,
                 reservation_micro_usd, ikey, now),
            )
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'reservation', ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    f"reserve-{ikey}",
                    -reservation_micro_usd,
                    balance,
                    run_id,
                    call_id,
                    model_name,
                    _json_meta({
                        "type": "reservation",
                        "nominal_reservation_micro_usd": nominal_reservation_micro_usd,
                        "reservation_capped": (
                            reservation_micro_usd < nominal_reservation_micro_usd
                        ),
                    }),
                    now,
                ),
            )
            conn.execute(
                "UPDATE credit_runs SET reserved_micro_usd = reserved_micro_usd + ?, "
                "call_count = call_count + 1 WHERE run_id = ?",
                (reservation_micro_usd, run_id),
            )
            conn.execute("COMMIT")

            return {
                "call_id": call_id,
                "run_id": run_id,
                "account_id": account_id,
                "user_id": run_owner,
                "model": model_name,
                "status": "held",
                "reserved_micro_usd": reservation_micro_usd,
                "nominal_reservation_micro_usd": nominal_reservation_micro_usd,
                "reservation_capped": (
                    reservation_micro_usd < nominal_reservation_micro_usd
                ),
                "reserved_usd": _micro_to_usd(reservation_micro_usd),
                "submitted_at": None,
                "already_reserved": False,
            }

    def mark_call_submitted(self, call_id: str) -> dict:
        """Persist the provider-boundary transition before network I/O.

        Once submitted, a reservation cannot be released. A terminal run or
        stale sweep captures the hold if the normal usage callback is lost.
        """
        cid = str(call_id or "").strip()
        if not cid:
            raise ValueError("call_id required")
        now = _now_ts()
        with self._conn() as conn:
            _begin_immediate(conn)
            row = conn.execute(
                "SELECT call_id, run_id, status, submitted_at "
                "FROM credit_call_reservations WHERE call_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Call not found: {cid}")
            if row[2] != "held":
                conn.execute("ROLLBACK")
                raise ValueError(f"Cannot submit call in state: {row[2]}")
            if row[3] is None:
                conn.execute(
                    "UPDATE credit_call_reservations SET submitted_at = ? "
                    "WHERE call_id = ? AND status = 'held' AND submitted_at IS NULL",
                    (now, cid),
                )
            conn.execute("COMMIT")
            return {
                "call_id": cid,
                "run_id": row[1],
                "status": "submitted",
                "submitted_at": row[3] if row[3] is not None else now,
                "already_submitted": row[3] is not None,
            }

    def settle_call(
        self,
        call_id: str,
        *,
        actual_cost_micro_usd: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        provider_response: dict | None = None,
        cost_absent: bool = False,
        provider_cost_micro_usd: int = 0,
        prompt_cache_hit_tokens: int = 0,
        prompt_cache_miss_tokens: int = 0,
        pricing_schedule_version: str = "",
        pricing_tier: str = "",
        pricing_basis_ts: float | None = None,
        input_cache_hit_rate_micro_usd_per_million: int = 0,
        input_cache_miss_rate_micro_usd_per_million: int = 0,
        output_rate_micro_usd_per_million: int = 0,
    ) -> dict:
        """Settle a reserved call.

        When *cost_absent* is True the provider did not report any cost
        field at all (distinct from a reported zero cost).  In that case
        the full reserved amount is settled (conservative accounting).

        *provider_cost_micro_usd* records the actual provider-reported cost for
        reconciliation. A cost above the hold consumes additional unreserved
        balance when available, without stealing funds held by concurrent
        calls. Any amount that cannot be funded is recorded for audit.
        """
        if cost_absent and int(actual_cost_micro_usd or 0) != 0:
            raise ValueError(
                "actual_cost_micro_usd must be zero when cost_absent is true"
            )
        now = _now_ts()

        with self._conn() as conn:
            _begin_immediate(conn)

            row = conn.execute(
                "SELECT call_id, run_id, account_id, user_id, model, status, "
                "reserved_micro_usd, settled_micro_usd, submitted_at "
                "FROM credit_call_reservations WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Call not found: {call_id}")
            if row[5] == "settled":
                conn.execute("COMMIT")
                return {"call_id": call_id, "status": "settled", "already_settled": True}
            if row[5] != "held":
                conn.execute("ROLLBACK")
                raise ValueError(f"Cannot settle call in state: {row[5]}")

            # Preserve idempotent settled replays above, but reject impossible
            # usage before any mutation of a live reservation.
            validate_provider_usage_tokens(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                prompt_cache_hit_tokens=prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=prompt_cache_miss_tokens,
            )

            run_id = row[1]
            account_id = row[2]
            user_id = row[3]
            model_name = row[4]
            reserved = int(row[6])
            submitted_at = row[8]

            # ------------------------------------------------------------------
            # Authoritative DeepSeek pricing. For a known scheduled DeepSeek
            # model the control plane derives the schedule from the recorded
            # reservation ``submitted_at`` (not the worker clock), validates any
            # worker-supplied metadata against it, and recomputes cost from the
            # normalized token breakdown. A buggy worker that sends correct
            # tier/rates but a wrong cost is overridden, not trusted.
            # ------------------------------------------------------------------
            scheduled_deepseek = model_name in DEEPSEEK_LEGACY_RATES_MICRO_USD_PER_MILLION
            authoritative_cost_micro: int | None = None
            schedule_version: str | None
            tier: str | None
            validated_basis_ts: float | None
            hit_rate: int | None
            miss_rate: int | None
            out_rate: int | None

            if scheduled_deepseek and submitted_at is not None:
                authoritative = deepseek_pricing_for_timestamp(model_name, submitted_at)
                _validate_pricing_against_authoritative(
                    authoritative,
                    pricing_schedule_version,
                    pricing_tier,
                    pricing_basis_ts,
                    input_cache_hit_rate_micro_usd_per_million,
                    input_cache_miss_rate_micro_usd_per_million,
                    output_rate_micro_usd_per_million,
                )
                schedule_version = authoritative["schedule_version"]
                tier = authoritative["tier"]
                validated_basis_ts = float(submitted_at)
                hit_rate = authoritative["input_cache_hit_micro_usd_per_million"]
                miss_rate = authoritative["input_cache_miss_micro_usd_per_million"]
                out_rate = authoritative["output_micro_usd_per_million"]

                authoritative_usage = deepseek_cost_for_tokens(
                    model_name,
                    submitted_at,
                    prompt_tokens,
                    prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens,
                    completion_tokens,
                    total_tokens,
                )
                pt = authoritative_usage["prompt_tokens"]
                hit_t = authoritative_usage["prompt_cache_hit_tokens"]
                miss_t = authoritative_usage["prompt_cache_miss_tokens"]
                ct = authoritative_usage["completion_tokens"]
                tt = authoritative_usage["total_tokens"]
                authoritative_cost_micro = authoritative_usage["cost_micro_usd"]
            else:
                # Fallback (non-DeepSeek, unpriced, or a direct ledger settle
                # without a recorded submission): validate any worker-supplied
                # metadata against the worker's own basis and keep the worker
                # cost unchanged.
                schedule_version, tier, validated_basis_ts, hit_rate, miss_rate, out_rate = (
                    _validate_pricing_audit_metadata(
                        model_name,
                        pricing_schedule_version,
                        pricing_tier,
                        pricing_basis_ts,
                        input_cache_hit_rate_micro_usd_per_million,
                        input_cache_miss_rate_micro_usd_per_million,
                        output_rate_micro_usd_per_million,
                    )
                )
                pt = max(0, int(prompt_tokens or 0))
                ct = max(0, int(completion_tokens or 0))
                tt = max(0, int(total_tokens or 0))
                hit_t = max(0, int(prompt_cache_hit_tokens or 0))
                miss_t = max(0, int(prompt_cache_miss_tokens or 0))
                if tt <= 0:
                    tt = pt + ct

            # --- Cost determination ---
            if authoritative_cost_micro is not None:
                # Authoritative recomputed cost overrides worker-supplied
                # actual/provider cost for a successful (cost_absent=False)
                # known-priced request. A definitive zero-token rejection still
                # recomputes to zero; a known-priced nonzero request rounding to
                # zero stays zero (never a full-hold fallback). cost_absent
                # keeps the full-hold fallback.
                if cost_absent:
                    requested_actual = reserved
                    recorded_provider = 0
                else:
                    requested_actual = authoritative_cost_micro
                    recorded_provider = authoritative_cost_micro
            else:
                if cost_absent and actual_cost_micro_usd <= 0:
                    # Provider reported no cost — settle the full conservative
                    # reservation so the hold isn't silently released.
                    requested_actual = reserved
                else:
                    requested_actual = max(0, int(actual_cost_micro_usd or 0))

                # Record provider cost independently from the amount the account
                # can fund. Exclude other active holds from this call's charge
                # capacity.
                recorded_provider = max(0, int(provider_cost_micro_usd or 0))
                if recorded_provider <= 0 and not cost_absent:
                    recorded_provider = requested_actual

            acct_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = max(0, int(acct_row[0]) if acct_row else 0)
            other_holds_row = conn.execute(
                "SELECT COALESCE(SUM(reserved_micro_usd - settled_micro_usd), 0) "
                "FROM credit_call_reservations "
                "WHERE account_id = ? AND status = 'held' AND call_id != ?",
                (account_id, call_id),
            ).fetchone()
            other_holds = max(0, int(other_holds_row[0] or 0))
            charge_capacity = max(0, balance - other_holds)
            actual = min(requested_actual, charge_capacity)
            released = max(0, reserved - actual)
            overage = max(0, actual - reserved)
            unfunded = max(0, requested_actual - actual)
            discrepancy = max(unfunded, recorded_provider - actual)
            new_balance = balance - actual

            conn.execute(
                "UPDATE credit_accounts SET balance_micro_usd = ?, "
                "total_spent_micro_usd = total_spent_micro_usd + ?, "
                "updated_at = ? WHERE account_id = ?",
                (new_balance, actual, now, account_id),
            )
            conn.execute(
                "UPDATE credit_call_reservations SET status = 'settled', "
                "settled_micro_usd = ?, settled_at = ? WHERE call_id = ?",
                (actual, now, call_id),
            )
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'settlement', ?, ?, ?, ?, ?, ?, ?)",
                (account_id, f"settle-{call_id}-{now}", -actual, new_balance,
                 run_id, call_id, model_name,
                 _json_meta({
                     "type": "settlement",
                      "reserved_micro_usd": reserved,
                      "requested_cost_micro_usd": requested_actual,
                      "settled_micro_usd": actual,
                      "released_micro_usd": released,
                      "overage_micro_usd": overage,
                      "unfunded_micro_usd": unfunded,
                     "cost_absent": cost_absent,
                     "provider_cost_micro_usd": recorded_provider,
                     "discrepancy_micro_usd": discrepancy,
                 }), now),
            )

            conn.execute(
                "INSERT INTO credit_provider_usage "
                "(call_id, run_id, account_id, model, prompt_tokens, "
                "completion_tokens, total_tokens, prompt_cache_hit_tokens, "
                "prompt_cache_miss_tokens, provider_cost_micro_usd, "
                "provider_response_json, pricing_schedule_version, pricing_tier, "
                "pricing_basis_ts, input_cache_hit_rate_micro_usd_per_million, "
                "input_cache_miss_rate_micro_usd_per_million, "
                "output_rate_micro_usd_per_million, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (call_id, run_id, account_id, model_name,
                 pt, ct, tt, hit_t, miss_t, recorded_provider,
                 _json_meta(provider_response or {}), schedule_version, tier,
                 validated_basis_ts, hit_rate, miss_rate, out_rate, now),
            )
            conn.execute(
                "UPDATE credit_runs SET settled_micro_usd = settled_micro_usd + ? "
                "WHERE run_id = ?",
                (actual, run_id),
            )
            conn.execute("COMMIT")

            return {
                "call_id": call_id,
                "run_id": run_id,
                "account_id": account_id,
                "user_id": user_id,
                "model": model_name,
                "status": "settled",
                "reserved_micro_usd": reserved,
                "settled_micro_usd": actual,
                "released_micro_usd": released,
                "overage_micro_usd": overage,
                "unfunded_micro_usd": unfunded,
                "balance_after_micro_usd": new_balance,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "prompt_cache_hit_tokens": hit_t,
                "prompt_cache_miss_tokens": miss_t,
                "provider_cost_micro_usd": recorded_provider,
                "discrepancy_micro_usd": discrepancy,
                "pricing_schedule_version": schedule_version or "",
                "pricing_tier": tier or "",
                "pricing_basis_ts": validated_basis_ts,
                "input_cache_hit_rate_micro_usd_per_million": hit_rate or 0,
                "input_cache_miss_rate_micro_usd_per_million": miss_rate or 0,
                "output_rate_micro_usd_per_million": out_rate or 0,
                "cost_absent": cost_absent,
                "already_settled": False,
            }

    def release_call(self, call_id: str) -> dict:
        now = _now_ts()

        with self._conn() as conn:
            _begin_immediate(conn)

            row = conn.execute(
                "SELECT call_id, run_id, account_id, user_id, model, status, "
                "reserved_micro_usd, settled_micro_usd, submitted_at "
                "FROM credit_call_reservations WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Call not found: {call_id}")
            if row[5] == "released":
                conn.execute("COMMIT")
                return {"call_id": call_id, "status": "released", "already_released": True}
            if row[5] != "held":
                conn.execute("ROLLBACK")
                raise ValueError(f"Cannot release call in state: {row[5]}")
            if row[8] is not None:
                conn.execute("ROLLBACK")
                raise ValueError("Cannot release a submitted provider call")

            account_id = row[2]
            model_name = row[4]
            reserved = int(row[6])
            run_id = row[1]

            conn.execute(
                "UPDATE credit_call_reservations SET status = 'released', "
                "released_at = ? WHERE call_id = ?",
                (now, call_id),
            )

            acct_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = int(acct_row[0]) if acct_row else 0

            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'release', ?, ?, ?, ?, ?, ?, ?)",
                (account_id, f"release-{call_id}-{now}", reserved, balance,
                 run_id, call_id, model_name,
                 _json_meta({"type": "release", "released_micro_usd": reserved}),
                 now),
            )
            conn.execute("COMMIT")

            return {
                "call_id": call_id,
                "status": "released",
                "reserved_micro_usd": reserved,
                "released_micro_usd": reserved,
                "already_released": False,
            }

    @staticmethod
    def _capture_submitted_calls_under_lock(
        conn: sqlite3.Connection, run_id: str, account_id: str, now: float
    ) -> None:
        held_calls = conn.execute(
            "SELECT call_id, reserved_micro_usd, model "
            "FROM credit_call_reservations "
            "WHERE run_id = ? AND status = 'held' AND submitted_at IS NOT NULL",
            (run_id,),
        ).fetchall()
        for call_id, reserved_raw, model_name in held_calls:
            reserved = max(0, int(reserved_raw or 0))
            account_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = max(0, int(account_row[0] if account_row else 0))
            charged = min(balance, reserved)
            new_balance = balance - charged
            conn.execute(
                "UPDATE credit_accounts SET balance_micro_usd = ?, "
                "total_spent_micro_usd = total_spent_micro_usd + ?, "
                "updated_at = ? WHERE account_id = ?",
                (new_balance, charged, now, account_id),
            )
            conn.execute(
                "UPDATE credit_call_reservations SET status = 'settled', "
                "settled_micro_usd = ?, settled_at = ? WHERE call_id = ?",
                (charged, now, call_id),
            )
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'settlement', ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    f"settle-fallback-{call_id}",
                    -charged,
                    new_balance,
                    run_id,
                    call_id,
                    model_name,
                    _json_meta({
                        "type": "settlement",
                        "cost_absent": True,
                        "settlement_callback_missing": True,
                        "reserved_micro_usd": reserved,
                        "settled_micro_usd": charged,
                    }),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO credit_provider_usage "
                "(call_id, run_id, account_id, model, provider_cost_micro_usd, "
                "provider_response_json, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (
                    call_id,
                    run_id,
                    account_id,
                    model_name,
                    _json_meta({
                        "cost_absent": True,
                        "settlement_callback_missing": True,
                    }),
                    now,
                ),
            )
            conn.execute(
                "UPDATE credit_runs SET settled_micro_usd = settled_micro_usd + ? "
                "WHERE run_id = ?",
                (charged, run_id),
            )

    @staticmethod
    def _release_unsubmitted_calls_under_lock(
        conn: sqlite3.Connection, run_id: str, account_id: str, now: float
    ) -> None:
        held_calls = conn.execute(
            "SELECT call_id, reserved_micro_usd, settled_micro_usd, model "
            "FROM credit_call_reservations "
            "WHERE run_id = ? AND status = 'held' AND submitted_at IS NULL",
            (run_id,),
        ).fetchall()
        for call in held_calls:
            call_id = call[0]
            reserved = int(call[1])
            settled = int(call[2])
            model_name = call[3]
            released = max(0, reserved - settled)
            conn.execute(
                "UPDATE credit_call_reservations SET status = 'released', "
                "released_at = ? WHERE call_id = ?",
                (now, call_id),
            )
            acct_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = int(acct_row[0]) if acct_row else 0
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'release', ?, ?, ?, ?, ?, ?, ?)",
                (account_id, f"release-{call_id}-{now}", released, balance,
                 run_id, call_id, model_name,
                 _json_meta({"type": "release", "released_micro_usd": released}),
                 now),
            )

    # ------------------------------------------------------------------
    # Stale-run sweep — crash recovery
    # ------------------------------------------------------------------

    def sweep_stale_runs(self, ttl_seconds: int = 0) -> int:
        """Finalize unresolved reservations and mark stale runs *exhausted*.

        Pre-submit holds are released; submitted holds are captured at their
        conservative reservation amount.

        Returns the number of runs expired.  This is safe to call periodically
        (it is idempotent — already-finished runs are skipped).
        """
        if ttl_seconds <= 0:
            ttl_seconds = STALE_RUN_TTL_SECONDS
        cutoff = _now_ts() - ttl_seconds
        now = _now_ts()
        expired_count = 0

        with self._conn() as conn:
            _begin_immediate(conn)

            stale = conn.execute(
                "SELECT run_id, account_id FROM credit_runs "
                "WHERE status = 'active' AND created_at < ?",
                (cutoff,),
            ).fetchall()

            for run_id, account_id in stale:
                self._capture_submitted_calls_under_lock(conn, run_id, account_id, now)
                self._release_unsubmitted_calls_under_lock(conn, run_id, account_id, now)
                # Mark run as exhausted
                conn.execute(
                    "UPDATE credit_runs SET status = 'exhausted', "
                    "finished_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
                expired_count += 1

            conn.execute("COMMIT")

        if expired_count:
            log.info(
                "[credit] sweep: expired %d stale run(s) "
                "(ttl=%ds cutoff_age=%.0fs)",
                expired_count, ttl_seconds, ttl_seconds,
            )
        return expired_count

    # ------------------------------------------------------------------
    # History / ledger queries
    # ------------------------------------------------------------------

    def get_ledger_for_user(self, user_id: str, limit: int = 100) -> list[dict]:
        uid = str(user_id or "").strip()
        account = self.get_account(uid)
        if not account:
            return []
        account_id = account["account_id"]
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT entry_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at "
                "FROM credit_ledger WHERE account_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        return [
            {
                "entry_id": r[0],
                "idempotency_key": r[1],
                "entry_type": r[2],
                "amount_micro_usd": int(r[3]),
                "amount_usd": _micro_to_usd(int(r[3])),
                "balance_after_micro_usd": int(r[4]),
                "balance_after_usd": _micro_to_usd(int(r[4])),
                "run_id": r[5],
                "call_id": r[6],
                "model": r[7],
                "metadata": _maybe_json(r[8]),
                "created_at": r[9],
            }
            for r in rows
        ]

    def get_usage_for_run(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT usage_id, call_id, model, prompt_tokens, completion_tokens, "
                "total_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
                "provider_cost_micro_usd, created_at, pricing_schedule_version, "
                "pricing_tier, pricing_basis_ts, "
                "input_cache_hit_rate_micro_usd_per_million, "
                "input_cache_miss_rate_micro_usd_per_million, "
                "output_rate_micro_usd_per_million "
                "FROM credit_provider_usage WHERE run_id = ? "
                "ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [
            {
                "usage_id": r[0],
                "call_id": r[1],
                "model": r[2],
                "prompt_tokens": int(r[3]),
                "completion_tokens": int(r[4]),
                "total_tokens": int(r[5]),
                "prompt_cache_hit_tokens": int(r[6]),
                "prompt_cache_miss_tokens": int(r[7]),
                "provider_cost_micro_usd": int(r[8]),
                "provider_cost_usd": _micro_to_usd(int(r[8])),
                "created_at": r[9],
                "pricing_schedule_version": r[10] or "",
                "pricing_tier": r[11] or "",
                "pricing_basis_ts": r[12],
                "input_cache_hit_rate_micro_usd_per_million": int(r[13] or 0),
                "input_cache_miss_rate_micro_usd_per_million": int(r[14] or 0),
                "output_rate_micro_usd_per_million": int(r[15] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Ledger invariant verification
    # ------------------------------------------------------------------

    _MONETARY_ENTRY_TYPES = frozenset({"grant", "reversal", "settlement", "fee"})

    def reconstruct_balance(self, account_id: str) -> int:
        """Sum monetary ledger entries for one account.

        Returns the balance that the append-only ledger would compute for
        *account_id* from grant, reversal, settlement, and fee entries
        alone (reservations/releases are holds, not monetary debits).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_micro_usd), 0) "
                "FROM credit_ledger WHERE account_id = ? "
                "AND entry_type IN ('grant', 'reversal', 'settlement', 'fee')",
                (account_id,),
            ).fetchone()
        return int(row[0] or 0)

    # ------------------------------------------------------------------
    # Trial grant initialisation (without swallowing programming errors)
    # ------------------------------------------------------------------

    # Errors that are expected during trial-grant (idempotency / integrity)
    _NON_PROGRAMMING_GRANT_ERRORS: tuple[type[BaseException], ...] = (
        ValueError,
        sqlite3.IntegrityError,
        InsufficientBalanceError,
    )

    def ensure_trial_grant_from_openrouter_budget(
        self,
        user_id: str,
        current_spend_usd: float = 0.0,
        budget_usd: float = 1.0,
    ) -> dict:
        """Lazily create an opening trial grant from existing OpenRouter budget.

        Grant amount = budget - spend (converted to micro-USD). Idempotent.

        Raises programming / schema / I/O errors so callers can detect real
        failures instead of silently returning partial state.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {}

        grant_micro = _usd_to_micro(max(0.0, float(budget_usd) - float(current_spend_usd)))
        if grant_micro <= 0:
            return {"granted_micro_usd": 0, "reason": "no_remaining_budget"}

        ikey = f"trial-grant-openrouter-budget-{uid}"
        try:
            result = self.grant(uid, grant_micro, idempotency_key=ikey)
            return {
                **result,
                "granted_micro_usd": grant_micro if not result.get("already_applied") else 0,
                "idempotency_key": ikey,
            }
        except self._NON_PROGRAMMING_GRANT_ERRORS:
            # Idempotency / integrity conflict — already applied or constraint
            acct = self.get_account(uid)
            if acct:
                return {
                    **acct,
                    "granted_micro_usd": 0,
                    "idempotency_key": ikey,
                    "reason": "already_granted_or_constraint",
                }
            return {"granted_micro_usd": 0, "reason": "error"}

    # ------------------------------------------------------------------
    # Provider usage metering + balance reconciliation
    # ------------------------------------------------------------------

    def provider_usage_since(self, since_ts: float, provider: str = "") -> dict:
        """Aggregate provider token usage + estimated cost since a timestamp.

        This is the per-interval "meter" used for near-realtime cost tracking:
        sum the cache-aware token breakdown and the settled estimated cost
        across all users since *since_ts*. When *provider* is given (e.g.
        ``"deepseek"``), only that provider's rows are counted.
        """
        scope, params = _provider_model_scope(provider)
        where = " WHERE created_at >= ?"
        if scope is not None:
            where += " AND model LIKE ?"
            params = [max(0.0, float(since_ts)), scope]
        else:
            params = [max(0.0, float(since_ts))]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT "
                "COALESCE(SUM(prompt_cache_hit_tokens), 0), "
                "COALESCE(SUM(prompt_cache_miss_tokens), 0), "
                "COALESCE(SUM(completion_tokens), 0), "
                "COALESCE(SUM(total_tokens), 0), "
                "COALESCE(SUM(provider_cost_micro_usd), 0) "
                "FROM credit_provider_usage" + where,
                params,
            ).fetchone()
        return {
            "prompt_cache_hit_tokens": int(row[0] or 0),
            "prompt_cache_miss_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "total_tokens": int(row[3] or 0),
            "estimated_cost_micro_usd": int(row[4] or 0),
        }

    def provider_usage_series(
        self,
        window_seconds: int = 600,
        since_ts: float = 0.0,
        provider: str = "",
    ) -> list[dict]:
        """Bucketed provider usage (default 10-minute windows, since *since_ts*).

        Each bucket carries the cache-aware token breakdown and the settled
        estimated cost, so the operator can compute cost per 10-minute window
        (lagged by one window) across all users. When *provider* is given, only
        that provider's rows are counted.
        """
        window_seconds = max(60, int(window_seconds))
        scope, params = _provider_model_scope(provider)
        where = " WHERE created_at >= ?"
        if scope is not None:
            where += " AND model LIKE ?"
            params = [window_seconds, max(0.0, float(since_ts)), scope]
        else:
            params = [window_seconds, max(0.0, float(since_ts))]
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT CAST(created_at / ? AS INTEGER) AS bucket, "
                "COALESCE(SUM(prompt_cache_hit_tokens), 0), "
                "COALESCE(SUM(prompt_cache_miss_tokens), 0), "
                "COALESCE(SUM(completion_tokens), 0), "
                "COALESCE(SUM(total_tokens), 0), "
                "COALESCE(SUM(provider_cost_micro_usd), 0) "
                "FROM credit_provider_usage" + where + " "
                "GROUP BY bucket ORDER BY bucket",
                params,
            ).fetchall()
        return [
            {
                "bucket_start": int(r[0]) * window_seconds,
                "prompt_cache_hit_tokens": int(r[1] or 0),
                "prompt_cache_miss_tokens": int(r[2] or 0),
                "completion_tokens": int(r[3] or 0),
                "total_tokens": int(r[4] or 0),
                "estimated_cost_micro_usd": int(r[5] or 0),
            }
            for r in rows
        ]

    def record_provider_balance_snapshot(
        self,
        *,
        provider: str,
        currency: str,
        total_balance: float,
        is_available: bool,
        granted_balance: float | None = None,
        topped_up_balance: float | None = None,
        snapshot_at: float | None = None,
    ) -> None:
        """Persist one provider account-balance snapshot (from the worker).

        Non-finite balances/timestamps (NaN/inf) are rejected with
        ``ValueError`` so the caller can skip the snapshot rather than store a
        corrupted value.
        """
        provider = str(provider or "").strip()[:64]
        currency = str(currency or "").strip()[:16]
        if not provider or not currency:
            raise ValueError("provider and currency are required")

        def _finite_nonnegative(value, field, *, allow_none=False):
            if allow_none and (value is None or value == ""):
                return None
            try:
                f = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must be numeric") from exc
            if not math.isfinite(f):
                raise ValueError(f"{field} must be finite")
            if f < 0:
                raise ValueError(f"{field} must be nonnegative")
            return f

        total = _finite_nonnegative(total_balance, "total_balance")
        granted = _finite_nonnegative(
            granted_balance, "granted_balance", allow_none=True
        )
        topped = _finite_nonnegative(
            topped_up_balance, "topped_up_balance", allow_none=True
        )

        if snapshot_at is None or snapshot_at == "":
            now = _now_ts()
        else:
            try:
                now = float(snapshot_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("snapshot_at must be numeric") from exc
            if not math.isfinite(now):
                raise ValueError("snapshot_at must be finite")

        with self._conn() as conn:
            _begin_immediate(conn)
            conn.execute(
                "INSERT INTO provider_balance_snapshots "
                "(provider, currency, total_balance, granted_balance, "
                "topped_up_balance, is_available, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (provider, currency, total, granted, topped,
                 int(bool(is_available)), now),
            )
            conn.execute("COMMIT")

    def latest_provider_balance_snapshot(self, provider: str) -> dict | None:
        """Return the most recent balance snapshot for a provider (per currency)."""
        provider = str(provider or "").strip()[:64]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT currency, total_balance, granted_balance, "
                "topped_up_balance, is_available, created_at "
                "FROM provider_balance_snapshots "
                "WHERE provider = ? "
                "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1",
                (provider,),
            ).fetchone()
        if row is None:
            return None
        return {
            "provider": provider,
            "currency": row[0],
            "total_balance": float(row[1] or 0),
            "granted_balance": (float(row[2]) if row[2] is not None else None),
            "topped_up_balance": (float(row[3]) if row[3] is not None else None),
            "is_available": bool(row[4]),
            "snapshot_at": float(row[5]),
        }

    def provider_cost_reconciliation(self, provider: str) -> dict:
        """Compare realized balance delta vs ledger-estimated spend.

        Ground truth: provider account balance drops between snapshots in one
        currency (prefer USD). Balance is reconciled over *monotonic spans* — a
        maximal run of adjacent intervals that are each unchanged or decreasing.
        An actual increase (top-up / grant) closes the span and the increasing
        interval itself is skipped (it would otherwise contaminate the window).
        Unchanged intervals (cent-rounding artifacts) stay inside the span so
        their spend estimate is not lost. Expected spend is the sum of per-call
        estimated costs settled into ``credit_provider_usage`` for this
        provider's models across each span. ``drift_percent`` is positive when
        the provider charged us MORE than our price schedule estimated (a stale
        or too-low schedule).
        """
        provider = str(provider or "").strip()[:64]
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT currency, total_balance, created_at "
                "FROM provider_balance_snapshots WHERE provider = ? "
                "ORDER BY created_at ASC, snapshot_id ASC",
                (provider,),
            ).fetchall()
        if not rows:
            return {"provider": provider, "stale": True, "reason": "no_snapshots"}
        currencies = [str(r[0]) for r in rows]
        if "USD" not in currencies:
            # Ledger spend is USD-denominated (micro-USD); a CNY-only balance
            # cannot be compared without FX, so skip reconciliation.
            return {
                "provider": provider,
                "stale": True,
                "reason": "reconciliation_requires_usd_balance",
                "currencies": currencies,
            }
        preferred = "USD"
        snaps = [r for r in rows if str(r[0]) == preferred]
        if len(snaps) < 2:
            return {
                "provider": provider,
                "stale": True,
                "reason": "insufficient_snapshots",
                "currency": preferred,
            }

        scope, _ = _provider_model_scope(provider)
        epsilon = 1e-6
        # Classify adjacent intervals and build monotonic spans. An interval is
        # a top-up only when the balance strictly increased; unchanged balances
        # are neither spend nor top-up and remain inside the current span.
        spans: list[tuple[float, float, float, float]] = []
        unchanged_segments = 0
        topup_segments = 0
        spend_segments = 0
        i = 0
        n = len(snaps)
        while i < n:
            span_start = i
            i += 1
            while i < n:
                prev_ts, prev_bal = float(snaps[i - 1][2]), float(snaps[i - 1][1])
                cur_ts, cur_bal = float(snaps[i][2]), float(snaps[i][1])
                delta = prev_bal - cur_bal
                if delta < -epsilon:
                    # Strict increase → top-up boundary; close span before it.
                    topup_segments += 1
                    break
                if delta > epsilon:
                    spend_segments += 1
                else:
                    unchanged_segments += 1
                i += 1
            span_end = i - 1
            if span_end > span_start:
                spans.append((
                    float(snaps[span_start][2]),
                    float(snaps[span_end][2]),
                    float(snaps[span_start][1]),
                    float(snaps[span_end][1]),
                ))

        actual_spend_usd = 0.0
        estimated_spend_micro = 0
        usable_segments = 0
        for start_ts, end_ts, start_bal, end_bal in spans:
            span_spend = start_bal - end_bal
            if span_spend <= epsilon:
                # A span with no net decrease carries no measurable spend.
                continue
            actual_spend_usd += span_spend
            usable_segments += 1
            # Sum estimated cost across the whole span (inclusive endpoints).
            # Spans are disjoint — each top-up interval between them is skipped,
            # so inclusive boundaries cannot be double-counted across spans.
            with self._conn() as conn:
                if scope is not None:
                    est = conn.execute(
                        "SELECT COALESCE(SUM(provider_cost_micro_usd), 0) "
                        "FROM credit_provider_usage "
                        "WHERE created_at >= ? AND created_at <= ? AND model LIKE ?",
                        (start_ts, end_ts, scope),
                    ).fetchone()
                else:
                    est = conn.execute(
                        "SELECT COALESCE(SUM(provider_cost_micro_usd), 0) "
                        "FROM credit_provider_usage "
                        "WHERE created_at >= ? AND created_at <= ?",
                        (start_ts, end_ts),
                    ).fetchone()
            estimated_spend_micro += int(est[0] or 0)

        actual_spend_usd = round(max(0.0, actual_spend_usd), 6)
        estimated_spend_usd = round((estimated_spend_micro / 1_000_000.0), 6)
        drift_percent = 0.0
        if actual_spend_usd > 0 and estimated_spend_usd > 0:
            drift_percent = round(
                ((actual_spend_usd - estimated_spend_usd) / actual_spend_usd) * 100.0,
                2,
            )
        return {
            "provider": provider,
            "stale": False,
            "currency": preferred,
            "snapshot_count": len(snaps),
            "span_count": len(spans),
            "usable_segments": usable_segments,
            "spend_segments": spend_segments,
            "unchanged_segments": unchanged_segments,
            "topup_segments": topup_segments,
            "first_snapshot_at": float(snaps[0][2]),
            "last_snapshot_at": float(snaps[-1][2]),
            "first_balance_usd": float(snaps[0][1]),
            "last_balance_usd": float(snaps[-1][1]),
            "actual_spend_usd": actual_spend_usd,
            "estimated_spend_usd": estimated_spend_usd,
            "drift_percent": drift_percent,
            "balance_increased": topup_segments > 0,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_meta(data: dict) -> str:
    import json
    return json.dumps(data or {}, separators=(",", ":"))


def _maybe_json(raw: str | None) -> dict:
    import json
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
