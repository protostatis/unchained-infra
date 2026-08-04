"""Financial workspace control plane — checkpoints, claims, imports, credit.

State machine for a checkpoint::

    BUILDING → READY → CLAIMING → IMPORTED (terminal, success)
                         ↘ EXPIRED   (terminal, timeout)

- ``BUILDING``  — checkpoint created, envelope stored; not yet accessible
- ``READY``     — checkpoint confirmed, claim URL available
- ``CLAIMING``  — one-time claim token has been issued, OAuth flow in progress
- ``IMPORTED``  — workspace data has been persisted successfully
- ``EXPIRED``   — authorization expired (1-hour window from READY)

Only ``BUILDING``→``READY`` is an internal progression (called after
checkpoint store write completes). All other transitions are driven by
the claim/import flow.

Usage (from handlers)::

    from financial_workspace import FinancialWorkspace
    from checkpoint_store import create_checkpoint_store

    store = create_checkpoint_store()
    fw = FinancialWorkspace(db_path, store)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid as _uuid_module
from contextlib import contextmanager
from typing import TYPE_CHECKING

from checkpoint_store import CheckpointStore

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHECKPOINT_EXPIRY_SECONDS = 3600  # 1 hour
_AUTH_CODE_EXPIRY_SECONDS = 600    # 10 minutes
_MAX_CHECKPOINT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MiB
_MAX_REQUEST_ID_LENGTH = 256
_MAX_SESSION_ID_LENGTH = 128
_MAX_WORKER_ID_LENGTH = 128
_MAX_GENERATION_LENGTH = 64
_MAX_SOURCE_REVISION_LENGTH = 128
_CLAIM_SECRET_BYTES = 32
_EFFECT_MAX_RETRIES = 5

# Feature flag — off by default, enabled via env
_FEATURE_FLAG_ENV = "FIN_WORKSPACE_ENABLED"

# Control-token for internal S2S calls
_FIN_CONTROL_TOKEN_ENV = "FIN_WORKSPACE_CONTROL_TOKEN"

# Account-scoped runtime provider (host-side Docker authority). The control
# plane never touches the Docker socket; it calls the validated host-side
# provider over the Docker-internal network. Enabling the feature without a
# validated provider is a fail-closed startup error (no false marketing
# routing under /fin-terminal/).
_FIN_RUNTIME_PROVIDER_URL_ENV = "FIN_WORKSPACE_RUNTIME_PROVIDER_URL"
_FIN_RUNTIME_PROVIDER_TOKEN_ENV = "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"

# Canonical truthy spellings for feature-flag env values (trimmed,
# case-insensitive): 1|true|yes|on. This is the single cross-repo contract;
# Compose/Caddy use the canonical ``true`` while every runtime consumer
# normalizes all four spellings.
_TRUE_FEATURE_VALUES = frozenset({"1", "true", "yes", "on"})

# Grant amount for new accounts: $1.00 in micro-USD
_NEW_ACCOUNT_GRANT_MICRO_USD = 1_000_000

_CHECKPOINT_STATUS_VALUES = frozenset({
    "building", "ready", "claiming", "imported", "expired",
})

_EFFECT_TYPE_VALUES = frozenset({
    "workspace_upsert", "snapshot_import", "account_grant",
})


def _now_ts() -> float:
    return time.time()


def _uuid_hex() -> str:
    return _uuid_module.uuid4().hex


def _format_expiry(seconds: float) -> float:
    return _now_ts() + seconds


# ---------------------------------------------------------------------------
# Control token helpers
# ---------------------------------------------------------------------------
def parse_feature_flag(value: str | None) -> bool:
    """Normalize a feature-flag env value to a boolean.

    Accepted truthy spellings (trimmed, case-insensitive): ``1``, ``true``,
    ``yes``, ``on``. Anything else (including empty/None) is False. This is
    the canonical cross-repo boolean contract.
    """
    return (value or "").strip().lower() in _TRUE_FEATURE_VALUES


def is_fin_workspace_enabled() -> bool:
    return parse_feature_flag(os.environ.get(_FEATURE_FLAG_ENV, ""))


def _runtime_provider_url() -> str:
    """Return the configured host-side runtime provider base URL ('' if none)."""
    return os.environ.get(_FIN_RUNTIME_PROVIDER_URL_ENV, "").strip()


def _runtime_provider_token() -> str:
    return os.environ.get(_FIN_RUNTIME_PROVIDER_TOKEN_ENV, "").strip()


def _runtime_control_url() -> str:
    """Control-plane S2S base the provider uses for flush callbacks.

    Defaults to the Docker-internal control-plane service name. The host can
    never resolve that name (no published control-plane port): the provider
    uses only the URL's PORT and executes the S2S request inside the control
    container on its loopback. ``FIN_WORKSPACE_CONTROL_URL`` is therefore a
    functional default — no host-reachable override is required.
    """
    return os.environ.get("FIN_WORKSPACE_CONTROL_URL", "").strip() or (
        "http://fin-terminal-workspace-control:8790"
    )


def _runtime_provider_headers() -> dict[str, str]:
    return {
        "X-Workspace-Runtime-Token": _runtime_provider_token(),
        "Content-Type": "application/json",
    }


def runtime_provider_validate(timeout: float = 5.0) -> dict | None:
    """Validate the configured host-side runtime provider.

    GET ``{url}/v1/health`` with the shared token. Returns the provider's
    status payload only when it is reachable AND declares the two capabilities
    the private workspace leg requires (``accountRuntime`` and
    ``checkpointFile``). Returns None otherwise — callers must fail closed.
    """
    url = _runtime_provider_url()
    if not url or not _runtime_provider_token():
        return None
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{url.rstrip('/')}/v1/health", headers=_runtime_provider_headers())
            if resp.status_code != 200:
                return None
            data = resp.json()
        if not isinstance(data, dict) or data.get("status") != "ok":
            return None
        caps = data.get("capabilities") or {}
        if not (caps.get("accountRuntime") and caps.get("checkpointFile")):
            return None
        return data
    except Exception:
        return None


def runtime_provider_wake(
    slug: str,
    checkpoint: dict,
    *,
    control_token: str,
    timeout: float = 30.0,
) -> dict | None:
    """Ask the host-side provider to provision the account's isolated runtime.

    ``checkpoint`` is the imported workspace snapshot (the checkpoint-file
    payload the app runtime consumes). ``control_token`` is the same
    ``FIN_WORKSPACE_CONTROL_TOKEN`` so the provider can reach back over the
    private network. Returns the provider's status dict or None on failure.
    """
    url = _runtime_provider_url()
    if not url or not _runtime_provider_token() or not slug:
        return None
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{url.rstrip('/')}/v1/accounts/{slug}/wake",
                json={
                    "checkpoint": checkpoint,
                    "controlUrl": _runtime_control_url(),
                    "controlToken": control_token,
                },
                headers=_runtime_provider_headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def runtime_provider_sleep(slug: str, timeout: float = 30.0) -> bool:
    """Ask the host-side provider to put the account runtime to sleep."""
    url = _runtime_provider_url()
    if not url or not _runtime_provider_token() or not slug:
        return False
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{url.rstrip('/')}/v1/accounts/{slug}/sleep",
                json={},
                headers=_runtime_provider_headers(),
            )
            return resp.status_code == 200
    except Exception:
        return False


def runtime_provider_status(slug: str, timeout: float = 10.0) -> dict | None:
    """Return the provider's status for one account runtime (or None)."""
    url = _runtime_provider_url()
    if not url or not _runtime_provider_token() or not slug:
        return None
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"{url.rstrip('/')}/v1/accounts/{slug}/status",
                headers=_runtime_provider_headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def runtime_provider_flush(slug: str, timeout: float = 45.0) -> dict:
    """Ask the host-side provider to flush the account runtime's CURRENT
    authoritative checkpoint to the control plane (S2S).

    The provider exports from the RUNNING app runtime (proxy + control
    tokens), then posts the snapshot here. It only falls back to the
    checkpoint file when the file's content is durably acknowledged.

    Returns the provider's flush result dict — ``{"ok": true, ...}`` on
    success. Callers must treat anything else as fail-closed.
    """
    url = _runtime_provider_url()
    if not url or not _runtime_provider_token() or not slug:
        return {"ok": False, "reason": "runtime provider not configured"}
    from financial_workspace import _resolve_control_token
    control_token = _resolve_control_token()
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{url.rstrip('/')}/v1/accounts/{slug}/flush",
                json={
                    "controlUrl": _runtime_control_url(),
                    "controlToken": control_token,
                },
                headers=_runtime_provider_headers(),
            )
            if resp.status_code != 200:
                return {"ok": False, "reason": f"provider returned {resp.status_code}"}
            data = resp.json()
        return data if isinstance(data, dict) else {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def workspace_runtime_slug(user_id: str) -> str:
    """Stable, path-safe per-account runtime namespace (24 hex chars)."""
    return hashlib.sha256((str(user_id or "")).encode()).hexdigest()[:24]


def _resolve_control_token() -> str:
    """Explicit control token only.

    Never falls back to ``JWT_SECRET`` or any other secret: the workspace
    control token is a distinct capability scoped to the S2S checkpoint API.
    When the feature is enabled and no token is configured, startup fails
    closed (see :func:`validate_fin_workspace_config`).
    """
    return os.environ.get(_FIN_CONTROL_TOKEN_ENV, "").strip()


def validate_fin_workspace_config(env: dict | None = None) -> list[str]:
    """Return configuration errors when the feature is enabled (empty if OK).

    Fail-closed contract: enabling ``FIN_WORKSPACE_ENABLED`` without an
    explicit control token, S3 bucket, region, KMS key, and storage
    configuration is a startup error. The app must never fall back to
    ``JWT_SECRET`` or local storage in production.
    """
    if env is None:
        env = os.environ
    if not is_fin_workspace_enabled():
        return []

    errors: list[str] = []
    token = env.get(_FIN_CONTROL_TOKEN_ENV, "").strip()
    if not token:
        errors.append(
            f"{_FIN_CONTROL_TOKEN_ENV} must be set when the financial "
            "workspace is enabled (JWT_SECRET is never a fallback)"
        )
    elif len(token) < 32:
        errors.append(f"{_FIN_CONTROL_TOKEN_ENV} must be >= 32 characters")

    if not env.get("FIN_WORKSPACE_COOKIE_DOMAIN", "").strip():
        errors.append(
            "FIN_WORKSPACE_COOKIE_DOMAIN must be set when the financial "
            "workspace is enabled (used for the parent-domain claim cookie)"
        )

    try:
        from checkpoint_store import validate_s3_store_config
        for missing in validate_s3_store_config(env):
            errors.append(f"checkpoint storage: {missing} required")
    except Exception as exc:  # pragma: no cover - import safety
        errors.append(f"checkpoint storage validation error: {exc}")

    # Hard enablement gate: without a validated runtime provider the private
    # workspace leg (/fin-terminal/) cannot open the imported checkpoint in an
    # isolated app runtime. Activating the feature without one would otherwise
    # falsely route to the marketing index — fail activation instead.
    provider_url = env.get(_FIN_RUNTIME_PROVIDER_URL_ENV, "").strip()
    provider_token = env.get(_FIN_RUNTIME_PROVIDER_TOKEN_ENV, "").strip()
    if not provider_url:
        errors.append(
            f"{_FIN_RUNTIME_PROVIDER_URL_ENV} must be set when the financial "
            "workspace is enabled (no validated runtime provider: "
            "/fin-terminal/ would fail closed with no CTA)"
        )
    if provider_url and not provider_token:
        errors.append(
            f"{_FIN_RUNTIME_PROVIDER_TOKEN_ENV} must be set when the financial "
            "workspace is enabled (shared secret with the host-side runtime provider)"
        )
    elif provider_token and len(provider_token) < 32:
        errors.append(f"{_FIN_RUNTIME_PROVIDER_TOKEN_ENV} must be >= 32 characters")
    return errors


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class FinancialWorkspaceError(Exception):
    """Base error for financial workspace operations."""


class CheckpointValidationError(FinancialWorkspaceError):
    """Checkpoint content or metadata failed validation."""


class CheckpointNotFoundError(FinancialWorkspaceError):
    """Checkpoint does not exist or has been deleted."""


class CheckpointStateError(FinancialWorkspaceError):
    """Checkpoint is not in the expected state for this operation."""


class ClaimRejectedError(FinancialWorkspaceError):
    """One-time claim was rejected (expired, replayed, browser mismatch, etc.)."""


class ImportConflictError(FinancialWorkspaceError):
    """Import already completed for a different account or workspace."""


class UnauthorizedError(FinancialWorkspaceError):
    """Bearer token invalid or missing."""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class FinancialWorkspace:
    """Core business logic for the financial workspace control plane."""

    def __init__(self, db_path: str, store: CheckpointStore):
        self.db_path = db_path
        self.store = store
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
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
            conn.execute("PRAGMA journal_mode=WAL")

            # ------------------------------------------------------------------
            # Checkpoints
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fin_terminal_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    generation TEXT NOT NULL DEFAULT '',
                    source_revision TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'building'
                        CHECK (status IN ('building', 'ready', 'claiming', 'imported', 'expired')),
                    object_key TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    content_size_bytes INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    handoff_id TEXT NOT NULL DEFAULT '',
                    handoff_secret_hash TEXT NOT NULL DEFAULT '',
                    auth_url TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    ready_at REAL,
                    claimed_at REAL,
                    imported_at REAL,
                    expired_at REAL,
                    created_at REAL NOT NULL,
                    UNIQUE (request_id)
                )
            """)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_chk_sess "
                "ON fin_terminal_checkpoints(session_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_chk_status "
                "ON fin_terminal_checkpoints(status, expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_chk_request "
                "ON fin_terminal_checkpoints(request_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_chk_handoff "
                "ON fin_terminal_checkpoints(handoff_id)"
            )

            # ------------------------------------------------------------------
            # Claims
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fin_terminal_claims (
                    claim_id TEXT PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL UNIQUE,
                    claim_secret_hash TEXT NOT NULL,
                    claim_secret_expires_at REAL NOT NULL,
                    browser_nonce_hash TEXT NOT NULL DEFAULT '',
                    oauth_state_hash TEXT NOT NULL DEFAULT '',
                    final_account_user_id TEXT,
                    final_account_email TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'rejected', 'expired')),
                    auth_code_id TEXT,
                    purpose TEXT NOT NULL DEFAULT 'fin-workspace-claim',
                    audience TEXT NOT NULL DEFAULT '',
                    rejected_reason TEXT,
                    created_at REAL NOT NULL,
                    accepted_at REAL,
                    rejected_at REAL,
                    FOREIGN KEY (checkpoint_id) REFERENCES fin_terminal_checkpoints(checkpoint_id)
                )
            """)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_claims_secret "
                "ON fin_terminal_claims(claim_secret_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_claims_checkpoint "
                "ON fin_terminal_claims(checkpoint_id)"
            )

            # Additive columns for pre-existing claim tables
            try:
                claim_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(fin_terminal_claims)"
                    ).fetchall()
                }
                for col_name, col_def in (
                    ("purpose", "TEXT NOT NULL DEFAULT 'fin-workspace-claim'"),
                    ("audience", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if col_name not in claim_columns:
                        conn.execute(
                            f"ALTER TABLE fin_terminal_claims ADD COLUMN {col_name} {col_def}"
                        )
            except sqlite3.OperationalError:
                pass

            # ------------------------------------------------------------------
            # Workspaces
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    account_email TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_ws_user "
                "ON financial_workspaces(user_id)"
            )

            # ------------------------------------------------------------------
            # Snapshots
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_workspace_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES financial_workspaces(workspace_id),
                    FOREIGN KEY (checkpoint_id) REFERENCES fin_terminal_checkpoints(checkpoint_id)
                )
            """)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_ws_snap_ws "
                "ON financial_workspace_snapshots(workspace_id, created_at DESC)"
            )

            # ------------------------------------------------------------------
            # Imports
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_workspace_imports (
                    import_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL UNIQUE,
                    claim_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    account_email TEXT NOT NULL DEFAULT '',
                    import_version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES financial_workspaces(workspace_id),
                    FOREIGN KEY (checkpoint_id) REFERENCES fin_terminal_checkpoints(checkpoint_id),
                    FOREIGN KEY (claim_id) REFERENCES fin_terminal_claims(claim_id)
                )
            """)

            # ------------------------------------------------------------------
            # Account origins — get-or-create user origin across auth providers
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_workspace_account_origins (
                    origin_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    provider_account_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE (user_id, provider)
                )
            """)

            # ------------------------------------------------------------------
            # Effects (outbox)
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_workspace_effects (
                    effect_id TEXT PRIMARY KEY,
                    effect_type TEXT NOT NULL CHECK (
                        effect_type IN ('workspace_upsert', 'snapshot_import', 'account_grant')
                    ),
                    context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead')),
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                )
            """)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fin_eff_status "
                "ON financial_workspace_effects(status, created_at ASC)"
            )

            # ------------------------------------------------------------------
            # Account-scoped runtime control (wake/sleep/status) — canary state
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_workspace_runtimes (
                    user_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    runtime_state TEXT NOT NULL DEFAULT 'asleep'
                        CHECK (runtime_state IN ('awake', 'asleep', 'draining')),
                    last_wake_at REAL,
                    last_sleep_at REAL,
                    wake_reason TEXT NOT NULL DEFAULT '',
                    sleep_reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES financial_workspaces(workspace_id)
                )
            """)

            # ------------------------------------------------------------------
            # Backward-compatible columns on auth_codes
            # ------------------------------------------------------------------
            try:
                auth_code_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(auth_codes)"
                    ).fetchall()
                }
                fin_auth_cols = {
                    "purpose": "TEXT NOT NULL DEFAULT ''",
                    "audience": "TEXT NOT NULL DEFAULT ''",
                    "claim_id": "TEXT",
                    "state_binding": "TEXT NOT NULL DEFAULT ''",
                }
                for col_name, col_def in fin_auth_cols.items():
                    if col_name not in auth_code_columns:
                        conn.execute(
                            f"ALTER TABLE auth_codes ADD COLUMN {col_name} {col_def}"
                        )
            except sqlite3.OperationalError:
                # auth_codes table doesn't exist yet (fresh DB before Auth init);
                # Auth._init_db owns the columns unconditionally and adds them
                # when it creates the table.
                pass

    # ==================================================================
    # User origin — get-or-create across providers
    # ==================================================================

    def get_or_create_user_origin(
        self,
        user_id: str,
        *,
        provider: str = "",
        provider_account_id: str = "",
    ) -> dict:
        """Atomically get-or-create a user origin record.

        Centralized so all auth providers thread through the same
        concurrency-safe upsert, without relying on timestamps.
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")
        provider_name = str(provider or "").strip().lower()
        provider_acct = str(provider_account_id or "").strip()[:256]
        now = _now_ts()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT origin_id, user_id, provider, provider_account_id, created_at "
                "FROM financial_workspace_account_origins "
                "WHERE user_id = ? AND provider = ?",
                (uid, provider_name),
            ).fetchone()
            if row:
                return {
                    "origin_id": row[0],
                    "user_id": row[1],
                    "provider": row[2],
                    "provider_account_id": row[3],
                    "created_at": row[4],
                    "already_exists": True,
                }
            origin_id = f"fo-{_uuid_hex()[:12]}"
            conn.execute(
                "INSERT INTO financial_workspace_account_origins "
                "(origin_id, user_id, provider, provider_account_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (origin_id, uid, provider_name, provider_acct, now),
            )
        return {
            "origin_id": origin_id,
            "user_id": uid,
            "provider": provider_name,
            "provider_account_id": provider_acct,
            "created_at": now,
            "already_exists": False,
        }

    # ==================================================================
    # Checkpoint lifecycle
    # ==================================================================

    def create_checkpoint(
        self,
        *,
        request_id: str,
        session_id: str,
        worker_id: str,
        generation: str = "",
        source_revision: str = "",
        checkpoint: bytes,
    ) -> dict:
        """Create a checkpoint envelope and store it.

        The checkpoint must be server-generated/sanitized — never trusted
        from the browser upload. Validation is strict.

        Returns checkpoint metadata including the handoff for claim auth.
        """
        # Validate bounded inputs
        rid = str(request_id or "").strip()[: _MAX_REQUEST_ID_LENGTH]
        sid = str(session_id or "").strip()[: _MAX_SESSION_ID_LENGTH]
        wid = str(worker_id or "").strip()[: _MAX_WORKER_ID_LENGTH]
        gen = str(generation or "").strip()[: _MAX_GENERATION_LENGTH]
        rev = str(source_revision or "").strip()[: _MAX_SOURCE_REVISION_LENGTH]

        if not rid:
            raise CheckpointValidationError("requestId required")
        if not sid:
            raise CheckpointValidationError("source.sessionId required")
        if not wid:
            raise CheckpointValidationError("source.workerId required")

        ckpt_bytes = checkpoint
        if not isinstance(ckpt_bytes, bytes) or len(ckpt_bytes) == 0:
            raise CheckpointValidationError("checkpoint must be non-empty bytes")
        if len(ckpt_bytes) > _MAX_CHECKPOINT_SIZE_BYTES:
            raise CheckpointValidationError(
                f"checkpoint exceeds max size of {_MAX_CHECKPOINT_SIZE_BYTES} bytes"
            )

        # Check idempotency
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT checkpoint_id, status, handoff_id, expires_at, handoff_secret_hash "
                "FROM fin_terminal_checkpoints WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if existing:
                if existing[1] == "ready":
                    handoff_id = existing[2]
                    # The handoff secret is deterministically derived from the
                    # handoff_id + control token, so an idempotent retry returns
                    # the SAME secret that matches the stored hash.
                    handoff_secret = self._derive_handoff_secret(handoff_id)
                    derived_hash = hashlib.sha256(handoff_secret.encode()).hexdigest()
                    if not hmac.compare_digest(derived_hash, existing[4] or ""):
                        # Control token rotated since creation: the original
                        # secret cannot be recovered without storing it in
                        # plaintext. Fail closed rather than hand back a secret
                        # that would be rejected at claim initiation.
                        raise CheckpointStateError(
                            "existing checkpoint handoff secret cannot be recovered "
                            "(control token rotated); create a new checkpoint"
                        )
                    auth_url = self._build_auth_url(handoff_id, handoff_secret)
                    return {
                        "checkpoint_id": existing[0],
                        "expires_at": existing[3],
                        "handoff_id": handoff_id,
                        "handoff_secret": handoff_secret,
                        "auth_url": auth_url,
                        "already_exists": True,
                    }
                return {
                    "checkpoint_id": existing[0],
                    "status": existing[1],
                    "already_exists": True,
                }

        checkpoint_id = f"fcp-{_uuid_hex()[:24]}"
        version = 1
        expires_at = _format_expiry(_CHECKPOINT_EXPIRY_SECONDS)

        # Generate handoff credentials: deterministic derivation (HMAC over
        # handoff_id keyed by the control token) so a retry of the same
        # requestId can re-derive the identical secret. Only the SHA-256 hash
        # is persisted — never the plaintext secret.
        handoff_id = f"fh-{_uuid_hex()[:16]}"
        handoff_secret = self._derive_handoff_secret(handoff_id)
        handoff_secret_hash = hashlib.sha256(handoff_secret.encode()).hexdigest()

        # Store envelope-encrypted checkpoint
        store_result = self.store.put(
            checkpoint_id=checkpoint_id,
            plaintext=ckpt_bytes,
            version=version,
            expires_at=expires_at,
        )

        auth_url = self._build_auth_url(handoff_id, handoff_secret)
        now = _now_ts()

        with self._conn() as conn:
            conn.execute(
                "INSERT INTO fin_terminal_checkpoints "
                "(checkpoint_id, request_id, session_id, worker_id, generation, "
                "source_revision, status, object_key, content_hash, content_size_bytes, "
                "version, handoff_id, handoff_secret_hash, auth_url, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint_id, rid, sid, wid, gen, rev,
                    store_result["object_key"],
                    store_result["content_hash"],
                    store_result["size_bytes"],
                    version,
                    handoff_id,
                    handoff_secret_hash,
                    auth_url,
                    expires_at,
                    now,
                ),
            )
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET ready_at = ? WHERE checkpoint_id = ?",
                (now, checkpoint_id),
            )

        return {
            "checkpoint_id": checkpoint_id,
            "expires_at": expires_at,
            "handoff_id": handoff_id,
            "handoff_secret": handoff_secret,
            "auth_url": auth_url,
            "already_exists": False,
        }

    def get_checkpoint(self, checkpoint_id: str) -> dict | None:
        """Return checkpoint metadata."""
        cid = str(checkpoint_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT checkpoint_id, request_id, session_id, worker_id, "
                "generation, source_revision, status, content_hash, content_size_bytes, "
                "version, handoff_id, auth_url, expires_at, ready_at, claimed_at, "
                "imported_at, expired_at, created_at "
                "FROM fin_terminal_checkpoints WHERE checkpoint_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                return None
            return {
                "checkpoint_id": row[0],
                "request_id": row[1],
                "session_id": row[2],
                "worker_id": row[3],
                "generation": row[4],
                "source_revision": row[5],
                "status": row[6],
                "content_hash": row[7],
                "content_size_bytes": row[8],
                "version": row[9],
                "handoff_id": row[10],
                "auth_url": row[11],
                "expires_at": row[12],
                "ready_at": row[13],
                "claimed_at": row[14],
                "imported_at": row[15],
                "expired_at": row[16],
                "created_at": row[17],
            }

    def _transition_checkpoint(self, conn: sqlite3.Connection, checkpoint_id: str,
                               from_status: str, to_status: str, now: float) -> bool:
        col = f"{to_status}_at" if to_status in ("ready", "claimed", "imported", "expired") else None
        if col:
            cur = conn.execute(
                f"UPDATE fin_terminal_checkpoints SET status = ?, {col} = ? "
                "WHERE checkpoint_id = ? AND status = ?",
                (to_status, now, checkpoint_id, from_status),
            )
        else:
            cur = conn.execute(
                "UPDATE fin_terminal_checkpoints SET status = ? "
                "WHERE checkpoint_id = ? AND status = ?",
                (to_status, checkpoint_id, from_status),
            )
        return cur.rowcount > 0

    def get_or_checkpoint_bytes(self, checkpoint_id: str) -> bytes | None:
        """Retrieve decrypted checkpoint bytes."""
        return self.store.get(checkpoint_id)

    # ==================================================================
    # Claim flow — same-tab auth with opaque one-time claim token
    # ==================================================================

    def initiate_claim(
        self,
        handoff_id: str,
        handoff_secret: str,
        *,
        browser_nonce: str,
        audience: str = "",
        purpose: str = "fin-workspace-claim",
    ) -> dict:
        """Validate handoff and create a one-time claim token.

        The claim secret is an opaque bearer stored in an HttpOnly
        Secure SameSite=Lax cookie. The raw secret is never in the URL,
        log, referrer, or analytics.

        ``audience`` is the exact OAuth provider (google|facebook|github)
        and ``purpose`` the claim purpose; both are bound to the claim and
        verified at accept time (exact callback allowlist).
        """
        h_id = str(handoff_id or "").strip()
        h_secret = str(handoff_secret or "").strip()

        if not h_id or not h_secret:
            raise UnauthorizedError("handoff credentials required")

        secret_hash = hashlib.sha256(h_secret.encode()).hexdigest()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT checkpoint_id, status, expires_at, handoff_secret_hash "
                "FROM fin_terminal_checkpoints WHERE handoff_id = ?",
                (h_id,),
            ).fetchone()
            if row is None:
                raise CheckpointNotFoundError("handoff not found")

            checkpoint_id, status, expires_at, stored_hash = row

            if status not in ("ready",):
                raise CheckpointStateError(f"checkpoint is {status}, not ready for claim")
            if _now_ts() > expires_at:
                self._transition_checkpoint(conn, checkpoint_id, status, "expired", _now_ts())
                raise ClaimRejectedError("checkpoint authorization expired")
            if not hmac.compare_digest(secret_hash, stored_hash or ""):
                raise UnauthorizedError("credential mismatch")

            # Transition to claiming
            now = _now_ts()
            self._transition_checkpoint(conn, checkpoint_id, "ready", "claiming", now)

            # Create the claim
            claim_id = f"fcl-{_uuid_hex()[:20]}"
            claim_secret = secrets.token_hex(_CLAIM_SECRET_BYTES)
            claim_secret_hash = hashlib.sha256(claim_secret.encode()).hexdigest()
            # A claim must never outlive its checkpoint: the authorization
            # window is bounded by the checkpoint's own expiry. If the claim
            # were issued a fresh full hour, it could stay valid after the
            # checkpoint had already been swept.
            claim_expires = min(
                _format_expiry(_CHECKPOINT_EXPIRY_SECONDS),
                expires_at,
            )

            browser_nonce_hash = hashlib.sha256(
                str(browser_nonce or "").encode()
            ).hexdigest()

            conn.execute(
                "INSERT INTO fin_terminal_claims "
                "(claim_id, checkpoint_id, claim_secret_hash, claim_secret_expires_at, "
                "browser_nonce_hash, purpose, audience, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (claim_id, checkpoint_id, claim_secret_hash, claim_expires,
                 browser_nonce_hash, str(purpose or "").strip()[:64],
                 str(audience or "").strip().lower()[:32], now),
            )
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET claimed_at = ? WHERE checkpoint_id = ?",
                (now, checkpoint_id),
            )

        return {
            "claim_id": claim_id,
            "claim_secret": claim_secret,
            "checkpoint_id": checkpoint_id,
            "expires_at": claim_expires,
        }

    def bind_oauth_state(self, claim_id: str, oauth_state: str, *,
                         audience: str = "") -> bool:
        """Bind the provider ``oauth_state`` (and audience) to a pending claim.

        Called by the claim-aware OAuth start endpoint so the callback can
        verify state binding exactly. Returns False if the claim is not
        pending or not found.
        """
        cid = str(claim_id or "").strip()
        state = str(oauth_state or "").strip()
        if not cid or not state:
            return False
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE fin_terminal_claims SET oauth_state_hash = ?, audience = ? "
                "WHERE claim_id = ? AND status = 'pending'",
                (state_hash, str(audience or "").strip().lower()[:32], cid),
            )
            return cur.rowcount > 0

    def accept_claim(
        self,
        claim_id: str,
        claim_secret: str,
        *,
        final_account_user_id: str,
        final_account_email: str,
        browser_nonce: str,
        oauth_state: str,
        auth_code_id: str = "",
    ) -> dict:
        """Accept a one-time claim after OAuth callback.

        This is the critical concurrency point: exactly-one workspace/import
        must be created. The claim secret is consumed atomically.
        """
        cid = str(claim_id or "").strip()
        csecret = str(claim_secret or "").strip()
        uid = str(final_account_user_id or "").strip()
        email = str(final_account_email or "").strip().lower()
        bnonce = str(browser_nonce or "").strip()
        ostate = str(oauth_state or "").strip()

        if not cid or not csecret:
            raise UnauthorizedError("claim credentials required")
        if not uid or not email:
            raise ClaimRejectedError("final account identity required")

        secret_hash = hashlib.sha256(csecret.encode()).hexdigest()
        now = _now_ts()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT claim_id, checkpoint_id, claim_secret_hash, "
                "claim_secret_expires_at, browser_nonce_hash, status, "
                "final_account_user_id, final_account_email, oauth_state_hash, "
                "purpose, audience "
                "FROM fin_terminal_claims WHERE claim_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                raise ClaimRejectedError("claim not found")

            (db_claim_id, checkpoint_id, db_secret_hash,
             claim_expires, db_browser_nonce_hash, status,
             existing_account_user_id, existing_account_email,
             db_oauth_state_hash, db_purpose, db_audience) = row

            # Purpose/audience binding: the claim must be a fin-workspace claim
            # bound to the exact audience (provider) before accept is allowed.
            if db_purpose and db_purpose != "fin-workspace-claim":
                raise ClaimRejectedError("claim purpose mismatch")

            # 1) Capability check FIRST — for pending AND already-accepted
            # claims. A wrong claim secret must never read the victim's
            # workspace/user/email/snapshot from an accepted claim.
            if not hmac.compare_digest(secret_hash, db_secret_hash or ""):
                raise ClaimRejectedError("claim secret mismatch")

            # 2) Transactional checkpoint recheck: the checkpoint must still
            # exist, be in a claimable state, and be inside its expiry window.
            # This closes the sweep race where the checkpoint expired between
            # claim initiation and callback acceptance.
            chk_row = conn.execute(
                "SELECT status, expires_at FROM fin_terminal_checkpoints "
                "WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            if chk_row is None:
                raise ClaimRejectedError("checkpoint deleted; claim rejected")
            chk_status, chk_expires = chk_row
            if now > chk_expires:
                # Durable bookkeeping: mark the claim/checkpoint expired in a
                # separate short transaction. The accept transaction itself
                # rolls back on raise, so the expiry marks must commit here.
                self._mark_expired(checkpoint_id, cid, now)
                raise ClaimRejectedError("checkpoint authorization expired")
            if chk_status not in ("ready", "claiming", "imported"):
                raise ClaimRejectedError(f"checkpoint is {chk_status}, not claimable")

            # 3) Same-tab browser nonce binding (both paths).
            if db_browser_nonce_hash:
                expected_bnonce = hashlib.sha256(bnonce.encode()).hexdigest()
                if not hmac.compare_digest(expected_bnonce, db_browser_nonce_hash):
                    raise ClaimRejectedError("browser nonce mismatch — cross-tab claim rejected")

            # 4) OAuth state binding (both paths): the exact state bound at
            # OAuth start must be presented at accept.
            if db_oauth_state_hash:
                expected_state_hash = hashlib.sha256(ostate.encode()).hexdigest()
                if not hmac.compare_digest(expected_state_hash, db_oauth_state_hash):
                    raise ClaimRejectedError("oauth state binding mismatch")

            if status == "accepted":
                # Idempotent re-accept: only the account that originally won
                # the claim may retry. Never create a duplicate snapshot or
                # effect, and never reveal the accepted account to a different
                # user/email (the secret check above already gates this path).
                if existing_account_user_id and existing_account_user_id != uid:
                    raise ClaimRejectedError("claim already accepted by a different account")
                if existing_account_email and existing_account_email != email:
                    raise ClaimRejectedError(
                        "claim already accepted with a different email"
                    )
                return self._finalize_accept(conn, cid, checkpoint_id, uid, email, now,
                                            auth_code_id, already_accepted=True)

            if status != "pending":
                raise ClaimRejectedError(f"claim is {status}, not pending")
            if now > claim_expires:
                self._mark_expired(checkpoint_id, cid, now, reason="expired")
                raise ClaimRejectedError("claim expired")

            # Mark claim accepted (atomic compare-and-set; exactly one winner)
            ostate_hash = hashlib.sha256(ostate.encode()).hexdigest()
            cur = conn.execute(
                "UPDATE fin_terminal_claims SET status = 'accepted', "
                "final_account_user_id = ?, final_account_email = ?, "
                "oauth_state_hash = ?, auth_code_id = ?, accepted_at = ? "
                "WHERE claim_id = ? AND status = 'pending'",
                (uid, email, ostate_hash, str(auth_code_id or ""), now, cid),
            )
            if cur.rowcount == 0:
                # Lost race — another callback won.
                recheck = conn.execute(
                    "SELECT status, final_account_user_id, final_account_email, "
                    "claim_secret_hash, browser_nonce_hash, oauth_state_hash "
                    "FROM fin_terminal_claims WHERE claim_id = ?",
                    (cid,),
                ).fetchone()
                if recheck and recheck[0] == "accepted":
                    if recheck[1] != uid or (recheck[2] or "") != email:
                        raise ClaimRejectedError("claim accepted by a different account")
                    if not hmac.compare_digest(secret_hash, recheck[3] or ""):
                        raise ClaimRejectedError("claim secret mismatch")
                    if recheck[4] and not hmac.compare_digest(
                        hashlib.sha256(bnonce.encode()).hexdigest(), recheck[4]
                    ):
                        raise ClaimRejectedError("browser nonce mismatch — cross-tab claim rejected")
                    if recheck[5] and not hmac.compare_digest(
                        hashlib.sha256(ostate.encode()).hexdigest(), recheck[5]
                    ):
                        raise ClaimRejectedError("oauth state binding mismatch")
                    return self._finalize_accept(conn, cid, checkpoint_id, uid, email, now,
                                                auth_code_id, already_accepted=True)
                raise ClaimRejectedError("claim race lost")

            return self._finalize_accept(conn, cid, checkpoint_id, uid, email, now,
                                        auth_code_id, already_accepted=False)

    def _finalize_accept(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
        checkpoint_id: str,
        user_id: str,
        email: str,
        now: float,
        auth_code_id: str,
        *,
        already_accepted: bool,
    ) -> dict:
        """After claim is accepted, create workspace, snapshot, import, effects."""
        # Create workspace (idempotent upsert)
        w_row = conn.execute(
            "SELECT workspace_id FROM financial_workspaces WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        is_new_workspace = False
        if w_row is None:
            workspace_id = f"fws-{_uuid_hex()[:16]}"
            conn.execute(
                "INSERT INTO financial_workspaces "
                "(workspace_id, user_id, account_email, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, '{}', ?, ?)",
                (workspace_id, user_id, email, now, now),
            )
            is_new_workspace = True
        else:
            workspace_id = w_row[0]
            conn.execute(
                "UPDATE financial_workspaces SET account_email = ?, updated_at = ? "
                "WHERE workspace_id = ?",
                (email, now, workspace_id),
            )

        # Create import (idempotent)
        self._ensure_import_under_lock(conn, workspace_id, checkpoint_id, claim_id,
                                       user_id, email, now)

        # Snapshot: reuse the existing snapshot for this checkpoint when the
        # accept is being retried (exactly-once; never a duplicate row).
        existing_snap = conn.execute(
            "SELECT snapshot_id FROM financial_workspace_snapshots "
            "WHERE workspace_id = ? AND checkpoint_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (workspace_id, checkpoint_id),
        ).fetchone()
        if existing_snap is not None:
            snapshot_id = existing_snap[0]
        else:
            ckpt_data = self.store.get(checkpoint_id)
            if ckpt_data is None:
                # The checkpoint envelope is gone (swept or deleted). Importing
                # an empty "{}" snapshot would silently destroy the user's data
                # — reject instead so the whole accept transaction rolls back.
                raise ClaimRejectedError(
                    "checkpoint data unavailable; checkpoint was swept or deleted"
                )
            try:
                snapshot_json_str = ckpt_data.decode("utf-8")
                # Validate it's valid JSON
                json.loads(snapshot_json_str)
            except (UnicodeDecodeError, json.JSONDecodeError):
                snapshot_json_str = "{}"

            snapshot_id = f"fsn-{_uuid_hex()[:16]}"
            conn.execute(
                "INSERT INTO financial_workspace_snapshots "
                "(snapshot_id, workspace_id, checkpoint_id, version, snapshot_json, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (snapshot_id, workspace_id, checkpoint_id, snapshot_json_str, now),
            )

        # Outbox effects are enqueued only on the FIRST accept. A re-accept of
        # an already-accepted claim must never create duplicate effects.
        if not already_accepted:
            self._enqueue_effect_under_lock(conn, "workspace_upsert", {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "is_new": is_new_workspace,
            }, now)

            if is_new_workspace:
                # New-account-only idempotent $1 credit, transactionally tied to
                # this claim-created workspace/account. Existing accounts (an
                # existing credit_accounts row) receive no grant.
                credit_account_exists = self._credit_account_exists(conn, user_id)
                if not credit_account_exists:
                    self._enqueue_effect_under_lock(conn, "account_grant", {
                        "user_id": user_id,
                        "workspace_id": workspace_id,
                        "grant_micro_usd": _NEW_ACCOUNT_GRANT_MICRO_USD,
                        "account_was_new": True,
                    }, now)

            self._enqueue_effect_under_lock(conn, "snapshot_import", {
                "workspace_id": workspace_id,
                "snapshot_id": snapshot_id,
                "checkpoint_id": checkpoint_id,
                "user_id": user_id,
            }, now)

        # Mark checkpoint imported
        conn.execute(
            "UPDATE fin_terminal_checkpoints SET status = 'imported', imported_at = ? "
            "WHERE checkpoint_id = ? AND status = 'claiming'",
            (now, checkpoint_id),
        )

        return {
            "claim_id": claim_id,
            "checkpoint_id": checkpoint_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "account_email": email,
            "is_new_workspace": is_new_workspace,
            "snapshot_id": snapshot_id,
            "already_accepted": already_accepted,
        }

    def _mark_expired(
        self,
        checkpoint_id: str,
        claim_id: str,
        now: float,
        *,
        reason: str = "checkpoint_expired",
    ) -> None:
        """Durably mark a claim/checkpoint expired in its own transaction.

        Called on the rejection paths of ``accept_claim``, where the accept
        transaction rolls back when the error propagates. This bookkeeping must
        survive, so it commits separately; the sweep loop is the authoritative
        cleanup regardless.
        """
        try:
            with self._conn() as mark_conn:
                mark_conn.execute(
                    "UPDATE fin_terminal_checkpoints SET status = 'expired', expired_at = ? "
                    "WHERE checkpoint_id = ? AND status IN ('ready', 'claiming')",
                    (now, checkpoint_id),
                )
                mark_conn.execute(
                    "UPDATE fin_terminal_claims SET status = 'expired', "
                    "rejected_reason = ? "
                    "WHERE claim_id = ? AND status = 'pending'",
                    (reason, claim_id),
                )
        except Exception:
            log.warning(
                "fin-workspace: failed to durably mark claim %s expired", claim_id
            )

    def _ensure_import_under_lock(
        self, conn: sqlite3.Connection, workspace_id: str, checkpoint_id: str,
        claim_id: str, user_id: str, email: str, now: float,
    ) -> None:
        existing = conn.execute(
            "SELECT import_id, user_id FROM financial_workspace_imports "
            "WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if existing:
            if existing[1] != user_id:
                raise ImportConflictError(
                    f"checkpoint {checkpoint_id} already imported by different user {existing[1]}"
                )
            return

        import_id = f"fim-{_uuid_hex()[:16]}"
        conn.execute(
            "INSERT INTO financial_workspace_imports "
            "(import_id, workspace_id, checkpoint_id, claim_id, user_id, "
            "account_email, import_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (import_id, workspace_id, checkpoint_id, claim_id, user_id, email, now),
        )

    def _enqueue_effect_under_lock(
        self, conn: sqlite3.Connection, effect_type: str,
        context: dict, now: float,
    ) -> None:
        effect_id = f"fe-{_uuid_hex()[:16]}"
        conn.execute(
            "INSERT INTO financial_workspace_effects "
            "(effect_id, effect_type, context_json, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (effect_id, effect_type, json.dumps(context, separators=(",", ":")), now),
        )

    @staticmethod
    def _credit_account_exists(conn: sqlite3.Connection, user_id: str) -> bool:
        """True when a credit account already exists for the user.

        The credit ledger is lazily initialized in production, so a missing
        ``credit_accounts`` table is treated as "no account exists" (a new
        account is eligible for the grant).
        """
        try:
            row = conn.execute(
                "SELECT 1 FROM credit_accounts WHERE user_id = ? LIMIT 1",
                (str(user_id or "").strip(),),
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False

    def get_claim(self, claim_id: str) -> dict | None:
        cid = str(claim_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT claim_id, checkpoint_id, status, final_account_user_id, "
                "final_account_email, rejected_reason, created_at, accepted_at, "
                "purpose, audience, oauth_state_hash "
                "FROM fin_terminal_claims WHERE claim_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                return None
            return {
                "claim_id": row[0],
                "checkpoint_id": row[1],
                "status": row[2],
                "final_account_user_id": row[3],
                "final_account_email": row[4],
                "rejected_reason": row[5],
                "created_at": row[6],
                "accepted_at": row[7],
                "purpose": row[8],
                "audience": row[9],
                "oauth_state_hash": row[10] or "",
            }

    def get_claim_by_secret(self, claim_secret: str) -> dict | None:
        """Return a claim owned by the presented claim secret (browser reads)."""
        secret = str(claim_secret or "").strip()
        if not secret:
            return None
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT claim_id, checkpoint_id, status, final_account_user_id, "
                "final_account_email, rejected_reason, created_at, accepted_at, "
                "purpose, audience, oauth_state_hash "
                "FROM fin_terminal_claims WHERE claim_secret_hash = ?",
                (secret_hash,),
            ).fetchone()
            if row is None:
                return None
            return {
                "claim_id": row[0],
                "checkpoint_id": row[1],
                "status": row[2],
                "final_account_user_id": row[3],
                "final_account_email": row[4],
                "rejected_reason": row[5],
                "created_at": row[6],
                "accepted_at": row[7],
                "purpose": row[8],
                "audience": row[9],
                "oauth_state_hash": row[10] or "",
            }

    # ==================================================================
    # Import status
    # ==================================================================

    def get_import_for_checkpoint(self, checkpoint_id: str) -> dict | None:
        cid = str(checkpoint_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT import_id, workspace_id, checkpoint_id, claim_id, "
                "user_id, account_email, created_at "
                "FROM financial_workspace_imports WHERE checkpoint_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                return None
            return {
                "import_id": row[0],
                "workspace_id": row[1],
                "checkpoint_id": row[2],
                "claim_id": row[3],
                "user_id": row[4],
                "account_email": row[5],
                "created_at": row[6],
            }

    # ==================================================================
    # Workspace queries
    # ==================================================================

    def get_workspace_for_user(self, user_id: str) -> dict | None:
        uid = str(user_id or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT workspace_id, user_id, account_email, metadata_json, "
                "created_at, updated_at "
                "FROM financial_workspaces WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                return None
            return {
                "workspace_id": row[0],
                "user_id": row[1],
                "account_email": row[2],
                "metadata": json.loads(row[3] or "{}"),
                "created_at": row[4],
                "updated_at": row[5],
            }

    def get_snapshots_for_workspace(self, workspace_id: str, limit: int = 50) -> list[dict]:
        wid = str(workspace_id or "").strip()
        if not wid:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT snapshot_id, workspace_id, checkpoint_id, version, "
                "snapshot_json, created_at "
                "FROM financial_workspace_snapshots WHERE workspace_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (wid, limit),
            ).fetchall()
        return [
            {
                "snapshot_id": r[0],
                "workspace_id": r[1],
                "checkpoint_id": r[2],
                "version": r[3],
                "snapshot": json.loads(r[4] or "{}"),
                "created_at": r[5],
            }
            for r in rows
        ]

    # ==================================================================
    # Account-scoped runtime control (wake/sleep/status) — canary runtime
    # ==================================================================

    def get_workspace_runtime_checkpoint(self, user_id: str) -> dict | None:
        """Return the provisioning payload for the account's isolated runtime.

        Prefers the most recent imported snapshot (the exact checkpoint the
        claim flow wrote); falls back to the workspace metadata. The host-side
        runtime provider writes this payload to the per-account checkpoint
        file that the app runtime consumes.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return None
        ws = self.get_workspace_for_user(uid)
        if ws is None:
            return None
        snapshots = self.get_snapshots_for_workspace(ws["workspace_id"], limit=1)
        if snapshots:
            return snapshots[0].get("snapshot") or {}
        return ws.get("metadata") or {}

    def _iter_workspace_user_ids(self) -> list[tuple[str]]:
        """Yield (user_id,) for every workspace (slug → account reverse map)."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT user_id FROM financial_workspaces"
            ).fetchall()

    def import_flushed_checkpoint(self, user_id: str, checkpoint: dict) -> dict | None:
        """Persist a checkpoint flushed back from the account runtime.

        Writes a new snapshot row for the user's workspace so a subsequent
        wake provisions the flushed state (the app's authoritative checkpoint
        after a session). ``checkpoint_id`` is reused from the workspace's
        most recent snapshot (the FK target); when the workspace has none, a
        fresh checkpoint envelope is created so the reference stays valid.
        Returns the snapshot record, or None when the user has no workspace or
        the checkpoint is not a dict.
        """
        uid = str(user_id or "").strip()
        if not uid or not isinstance(checkpoint, dict):
            return None
        ws = self.get_workspace_for_user(uid)
        if ws is None:
            return None
        snapshot_json_str = json.dumps(checkpoint, separators=(",", ":"), default=str)
        snapshot_id = f"fsn-{_uuid_hex()[:16]}"
        now = _now_ts()
        with self._conn() as conn:
            # Reuse the workspace's most recent checkpoint as the FK target;
            # the checkpoint envelope is immutable once stored.
            row = conn.execute(
                "SELECT checkpoint_id FROM financial_workspace_snapshots "
                "WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1",
                (ws["workspace_id"],),
            ).fetchone()
            checkpoint_id = row[0] if row else ""
            if not checkpoint_id:
                ckpt_id = f"fcp-{_uuid_hex()[:16]}"
                conn.execute(
                    "INSERT INTO fin_terminal_checkpoints "
                    "(checkpoint_id, request_id, session_id, worker_id, "
                    "status, expires_at, created_at) VALUES (?, ?, '', '', 'imported', ?, ?)",
                    (ckpt_id, f"flush-{snapshot_id}", now, now),
                )
                checkpoint_id = ckpt_id
            version = self._next_snapshot_version(conn, ws["workspace_id"])
            conn.execute(
                "INSERT INTO financial_workspace_snapshots "
                "(snapshot_id, workspace_id, checkpoint_id, version, snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot_id, ws["workspace_id"], checkpoint_id, version,
                 snapshot_json_str, now),
            )
        return {
            "snapshot_id": snapshot_id,
            "workspace_id": ws["workspace_id"],
            "version": version,
            "created_at": now,
        }

    def _next_snapshot_version(self, conn: sqlite3.Connection, workspace_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM financial_workspace_snapshots "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row[0] or 1)

    def runtime_wake(self, user_id: str, *, reason: str = "") -> dict | None:
        """Wake the account-scoped workspace runtime (idempotent).

        Returns the runtime row, or None when the user has no workspace.
        Used by the feature-flagged canary to warm a workspace before use.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return None
        ws = self.get_workspace_for_user(uid)
        if ws is None:
            return None
        now = _now_ts()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT runtime_state, last_wake_at FROM financial_workspace_runtimes "
                "WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO financial_workspace_runtimes "
                    "(user_id, workspace_id, runtime_state, last_wake_at, "
                    "wake_reason, updated_at) VALUES (?, ?, 'awake', ?, ?, ?)",
                    (uid, ws["workspace_id"], now,
                     str(reason or "")[:256], now),
                )
            else:
                conn.execute(
                    "UPDATE financial_workspace_runtimes SET runtime_state = 'awake', "
                    "last_wake_at = ?, wake_reason = ?, sleep_reason = '', updated_at = ? "
                    "WHERE user_id = ?",
                    (now, str(reason or "")[:256], now, uid),
                )
        return self.runtime_status(uid)

    def runtime_sleep(self, user_id: str, *, reason: str = "") -> dict | None:
        """Put the account-scoped workspace runtime to sleep (idempotent)."""
        uid = str(user_id or "").strip()
        if not uid:
            return None
        ws = self.get_workspace_for_user(uid)
        if ws is None:
            return None
        now = _now_ts()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM financial_workspace_runtimes WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO financial_workspace_runtimes "
                    "(user_id, workspace_id, runtime_state, last_sleep_at, "
                    "sleep_reason, updated_at) VALUES (?, ?, 'asleep', ?, ?, ?)",
                    (uid, ws["workspace_id"], now,
                     str(reason or "")[:256], now),
                )
            else:
                conn.execute(
                    "UPDATE financial_workspace_runtimes SET runtime_state = 'asleep', "
                    "last_sleep_at = ?, sleep_reason = ?, wake_reason = '', updated_at = ? "
                    "WHERE user_id = ?",
                    (now, str(reason or "")[:256], now, uid),
                )
        return self.runtime_status(uid)

    def runtime_status(self, user_id: str) -> dict | None:
        """Return the account-scoped runtime state for the user."""
        uid = str(user_id or "").strip()
        if not uid:
            return None
        ws = self.get_workspace_for_user(uid)
        if ws is None:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT runtime_state, last_wake_at, last_sleep_at, "
                "wake_reason, sleep_reason, updated_at "
                "FROM financial_workspace_runtimes WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                return {
                    "user_id": uid,
                    "workspace_id": ws["workspace_id"],
                    "runtime_state": "asleep",
                    "last_wake_at": None,
                    "last_sleep_at": None,
                    "wake_reason": "",
                    "sleep_reason": "never woken",
                    "updated_at": None,
                }
            return {
                "user_id": uid,
                "workspace_id": ws["workspace_id"],
                "runtime_state": row[0],
                "last_wake_at": row[1],
                "last_sleep_at": row[2],
                "wake_reason": row[3],
                "sleep_reason": row[4],
                "updated_at": row[5],
            }

    def runtime_sleep_durable(self, user_id: str, *, reason: str = "") -> dict:
        """Sleep the account runtime ONLY after a durable checkpoint flush.

        Fail-closed contract (shared with the idle scheduler):
          1. The provider must export the runtime's CURRENT authoritative
             checkpoint (from the running app) and persist it to the control
             plane (S2S).
          2. Only after a durably acknowledged flush does the provider stop
             the container and remove the per-account networks.
          3. If the flush fails (or the provider is unreachable), the runtime
             STAYS AWAKE and no state is lost.

        Returns a status-like dict with an extra ``flush`` result and an
        ``error`` field when the sleep could not proceed.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {"error": "invalid user"}
        status = self.runtime_status(uid)
        if status is None:
            return {"error": "no workspace for user"}
        if status["runtime_state"] != "awake":
            # Nothing to flush or stop; sleeping is idempotent.
            return {**status, "flush": {"ok": True, "skipped": True}}
        slug = workspace_runtime_slug(uid)
        flush_result = runtime_provider_flush(slug)
        if not flush_result.get("ok"):
            return {
                **status,
                "flush": flush_result,
                "error": "flush failed; runtime stays awake",
            }
        provider_sleep_ok = runtime_provider_sleep(slug)
        if not provider_sleep_ok:
            # The snapshot is durably persisted; a failed stop is retried by
            # the next sweep. Mark asleep so the idle scheduler stops spinning.
            pass
        slept = self.runtime_sleep(uid, reason=reason)
        return {**(slept or status), "flush": flush_result}

    # ==================================================================
    # Sweep — delete expired checkpoints
    # ==================================================================

    def sweep_expired(self) -> int:
        """Expire ready/claiming checkpoints past their TTL and delete storage."""
        now = _now_ts()
        count = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT checkpoint_id, status FROM fin_terminal_checkpoints "
                "WHERE status IN ('ready', 'claiming') AND expires_at < ?",
                (now,),
            ).fetchall()
            for checkpoint_id, status in rows:
                conn.execute(
                    "UPDATE fin_terminal_checkpoints SET status = 'expired', expired_at = ? "
                    "WHERE checkpoint_id = ? AND status IN ('ready', 'claiming')",
                    (now, checkpoint_id),
                )
                conn.execute(
                    "UPDATE fin_terminal_claims SET status = 'expired', "
                    "rejected_reason = 'checkpoint_expired' "
                    "WHERE checkpoint_id = ? AND status = 'pending'",
                    (checkpoint_id,),
                )
                self.store.delete(checkpoint_id)
                count += 1

        # Storage-level sweep
        self.store.sweep_expired()
        return count

    # ==================================================================
    # Effects processing (outbox consumer)
    # ==================================================================

    def poll_pending_effects(self, limit: int = 10) -> list[dict]:
        """Return pending effects ready for processing."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT effect_id, effect_type, context_json, retry_count, created_at "
                "FROM financial_workspace_effects "
                "WHERE status IN ('pending', 'failed') AND retry_count < ? "
                "ORDER BY created_at ASC LIMIT ?",
                (_EFFECT_MAX_RETRIES, limit),
            ).fetchall()
        return [
            {
                "effect_id": r[0],
                "effect_type": r[1],
                "context": json.loads(r[2] or "{}"),
                "retry_count": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def mark_effect_processing(self, effect_id: str) -> bool:
        eid = str(effect_id or "").strip()
        if not eid:
            return False
        now = _now_ts()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE financial_workspace_effects SET status = 'processing', "
                "started_at = ?, retry_count = retry_count + 1 "
                "WHERE effect_id = ? AND status IN ('pending', 'failed')",
                (now, eid),
            )
        return cur.rowcount > 0

    def mark_effect_completed(self, effect_id: str, *, failed: bool = False,
                               error: str = "") -> bool:
        eid = str(effect_id or "").strip()
        if not eid:
            return False
        now = _now_ts()
        status = "failed" if failed else "completed"
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE financial_workspace_effects SET status = ?, "
                "completed_at = ?, last_error = ? "
                "WHERE effect_id = ? AND status = 'processing'",
                (status, now, str(error or "")[:2000], eid),
            )
            if not failed and cur.rowcount == 0:
                # Already completed (idempotent)
                recheck = conn.execute(
                    "SELECT status FROM financial_workspace_effects WHERE effect_id = ?",
                    (eid,),
                ).fetchone()
                if recheck and recheck[0] == "completed":
                    return True
        return cur.rowcount > 0

    # ==================================================================
    # Account grant effect handler (called by outbox processor)
    # ==================================================================

    def process_account_grant_effect(self, context: dict, ledger) -> dict:
        """Execute idempotent USD 1.00 grant for new accounts.

        The ledger is the CreditLedger instance from credit.py.
        Existing accounts that already have a balance retain it and
        get no additional grant (the grant is idempotent and
        keyed on the user_id).
        """
        user_id = context.get("user_id", "")
        workspace_id = context.get("workspace_id", "")
        grant_amount = int(context.get("grant_micro_usd", _NEW_ACCOUNT_GRANT_MICRO_USD))

        if not user_id:
            return {"granted": False, "reason": "no user_id"}

        # Existing accounts receive none: the grant is only for accounts that
        # did not exist when the claim-created workspace was recorded. The
        # effect context carries the accept-time decision; re-check here for
        # defense in depth against out-of-band account creation.
        if context.get("account_was_new") is not True:
            return {"granted": False, "reason": "account_was_new not set", "user_id": user_id}

        if ledger.get_account(user_id) is not None:
            return {
                "granted": False,
                "reason": "existing credit account",
                "user_id": user_id,
                "workspace_id": workspace_id,
            }

        # Idempotency key: scoped to the workspace+user pair
        ikey = f"fin-workspace-grant-{user_id}"
        try:
            result = ledger.grant(user_id, grant_amount, idempotency_key=ikey)
            return {
                "granted": not result.get("already_applied", False),
                "user_id": user_id,
                "workspace_id": workspace_id,
                "amount_micro_usd": grant_amount,
                "already_applied": result.get("already_applied", False),
            }
        except Exception as exc:
            log.error("grant effect failed for user %s: %s", user_id, exc)
            raise

    # ==================================================================
    # Handoff helpers
    # ==================================================================

    def _derive_handoff_secret(self, handoff_id: str) -> str:
        """Deterministically re-derive handoff secret from handoff_id."""
        control = _resolve_control_token()
        mac = hmac.new(
            control.encode() if control else b"fin-workspace",
            f"handoff:{handoff_id}".encode(),
            hashlib.sha256,
        )
        return mac.hexdigest()

    def _build_auth_url(self, handoff_id: str, handoff_secret: str) -> str:
        """Build the public auth URL for the claim flow.

        The URL carries only the opaque ``handoff_id`` — never the handoff
        secret. The secret travels S2S in the create-checkpoint response and
        then in the claim-initiation POST body; it never appears in a URL,
        log, referrer, or analytics.

        The path is the exact Caddy workspace auth route (no wildcard). The
        claim surface lives under the dedicated ``/workspace/*`` namespace so
        it can never shadow (or be shadowed by) the site's own login OAuth
        routes (``/auth/facebook/...``, ``/auth/github/...``).
        """
        from urllib.parse import urlencode
        base = os.environ.get(
            "FIN_TERMINAL_BASE_URL",
            "https://unbrowser.unchainedsky.com/fin-terminal-workspace",
        ).strip().rstrip("/")
        params = urlencode({"handoff_id": handoff_id, "action": "claim"})
        return f"{base}/workspace/auth/claim?{params}"

    # ==================================================================
    # Delete / export metadata
    # ==================================================================

    def delete_workspace_for_user(self, user_id: str) -> dict:
        """Delete workspace data for a user (GDPR/data deletion)."""
        uid = str(user_id or "").strip()
        if not uid:
            return {"deleted": False, "reason": "no user_id"}

        ws = self.get_workspace_for_user(uid)
        if ws is None:
            return {"deleted": False, "reason": "no workspace"}

        wid = ws["workspace_id"]
        deleted_checkpoint_ids: list[str] = []
        with self._conn() as conn:
            # Capture the workspace's checkpoint ids BEFORE deleting imports:
            # the claims/checkpoints rows and the S3/local blobs are keyed off
            # this list, so it must be read first.
            chk_rows = conn.execute(
                "SELECT checkpoint_id FROM financial_workspace_imports "
                "WHERE workspace_id = ?",
                (wid,),
            ).fetchall()
            deleted_checkpoint_ids = [r[0] for r in chk_rows]

            # Delete dependents in foreign-key order: snapshots and imports
            # reference workspaces/checkpoints/claims and must go first.
            conn.execute(
                "DELETE FROM financial_workspace_snapshots WHERE workspace_id = ?",
                (wid,),
            )
            conn.execute(
                "DELETE FROM financial_workspace_imports WHERE workspace_id = ?",
                (wid,),
            )
            if deleted_checkpoint_ids:
                placeholders = ",".join("?" * len(deleted_checkpoint_ids))
                conn.execute(
                    f"DELETE FROM fin_terminal_claims "
                    f"WHERE checkpoint_id IN ({placeholders})",
                    deleted_checkpoint_ids,
                )
                conn.execute(
                    f"DELETE FROM fin_terminal_checkpoints "
                    f"WHERE checkpoint_id IN ({placeholders})",
                    deleted_checkpoint_ids,
                )
            conn.execute(
                "DELETE FROM financial_workspace_runtimes WHERE workspace_id = ?",
                (wid,),
            )
            conn.execute(
                "DELETE FROM financial_workspace_account_origins WHERE user_id = ?",
                (uid,),
            )
            conn.execute(
                "DELETE FROM financial_workspaces WHERE workspace_id = ?",
                (wid,),
            )
            # Permanently delete the encrypted checkpoint objects.
            for chk_id in deleted_checkpoint_ids:
                self.store.delete(chk_id)
        return {
            "deleted": True,
            "workspace_id": wid,
            "user_id": uid,
            "deleted_checkpoint_ids": deleted_checkpoint_ids,
            "deleted_checkpoint_count": len(deleted_checkpoint_ids),
        }

    def export_workspace_metadata(self, user_id: str) -> dict | None:
        """Export metadata for data portability."""
        ws = self.get_workspace_for_user(user_id)
        if ws is None:
            return None
        snapshots = self.get_snapshots_for_workspace(ws["workspace_id"])
        imports = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT import_id, workspace_id, checkpoint_id, claim_id, "
                "user_id, account_email, created_at "
                "FROM financial_workspace_imports WHERE workspace_id = ?",
                (ws["workspace_id"],),
            ).fetchall()
            imports = [
                {
                    "import_id": r[0],
                    "workspace_id": r[1],
                    "checkpoint_id": r[2],
                    "claim_id": r[3],
                    "user_id": r[4],
                    "account_email": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]
        return {
            "workspace": ws,
            "snapshots": snapshots,
            "imports": imports,
        }


# ---------------------------------------------------------------------------
# Account runtime idle scheduler (durable-flush-then-sleep)
# ---------------------------------------------------------------------------
class FinancialWorkspaceRuntimeScheduler:
    """Tracks live WebSocket/HTTP activity per account runtime and sleeps
    idle runtimes ONLY after a durable checkpoint flush.

    Contract:
      - ``attach``/``detach`` count live browser connections through the
        control-plane proxy; a runtime with any active WebSocket is never a
        sleep candidate.
      - ``touch`` records the last proxy activity so the idle window only
        starts once traffic has fully stopped.
      - ``tick()`` sweeps awake runtimes whose idle window has elapsed: it
        calls ``runtime_sleep_durable`` (provider flush → S2S persist → stop).
        If the flush fails the runtime STAYS AWAKE (fail closed) and the
        account is put on a back-off so the sweeper does not spin.
    """

    def __init__(
        self,
        fw: FinancialWorkspace,
        *,
        idle_seconds: float = 600.0,
        backoff_seconds: float = 300.0,
        now: "callable | None" = None,
    ) -> None:
        self._fw = fw
        self._idle_seconds = max(30.0, float(idle_seconds))
        self._backoff_seconds = max(30.0, float(backoff_seconds))
        self._now = now or time.time
        self._active_sockets: dict[str, int] = {}
        self._last_activity: dict[str, float] = {}
        self._last_failed: dict[str, float] = {}

    def attach(self, user_id: str) -> None:
        uid = str(user_id or "").strip()
        self._active_sockets[uid] = self._active_sockets.get(uid, 0) + 1
        self.touch(uid)

    def detach(self, user_id: str) -> None:
        uid = str(user_id or "").strip()
        current = self._active_sockets.get(uid, 0)
        if current <= 1:
            self._active_sockets.pop(uid, None)
        else:
            self._active_sockets[uid] = current - 1
        self.touch(uid)

    def touch(self, user_id: str) -> None:
        uid = str(user_id or "").strip()
        if uid:
            self._last_activity[uid] = self._now()

    def active_socket_count(self, user_id: str) -> int:
        return self._active_sockets.get(str(user_id or "").strip(), 0)

    def idle_candidates(self) -> list[str]:
        """Awake runtimes with no live WebSocket and an elapsed idle window."""
        now = self._now()
        candidates: list[str] = []
        for row in self._fw._iter_workspace_user_ids():
            uid = row[0]
            status = self._fw.runtime_status(uid)
            if status is None or status.get("runtime_state") != "awake":
                continue
            if self._active_sockets.get(uid, 0) > 0:
                continue
            if now - self._last_failed.get(uid, 0.0) < self._backoff_seconds:
                continue
            last_activity = self._last_activity.get(uid, status.get("last_wake_at") or 0.0)
            if now - last_activity < self._idle_seconds:
                continue
            candidates.append(uid)
        return sorted(candidates)

    def tick(self) -> list[dict]:
        """Sweep idle runtimes: durable flush first, stop only on success."""
        results: list[dict] = []
        for uid in self.idle_candidates():
            result = self._fw.runtime_sleep_durable(uid, reason="idle")
            if result.get("error") or not (result.get("flush") or {}).get("ok"):
                self._last_failed[uid] = self._now()
            results.append({"user_id": uid, **result})
        return results


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
def create_financial_workspace(db_path: str, store: CheckpointStore | None = None) -> FinancialWorkspace:
    """Create a FinancialWorkspace instance with the default store adapter."""
    if store is None:
        from checkpoint_store import create_checkpoint_store
        store = create_checkpoint_store()
    return FinancialWorkspace(db_path, store)
