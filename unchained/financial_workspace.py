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
_NONCE_BYTES = 32
_HANDOFF_SECRET_BYTES = 32
_EFFECT_MAX_RETRIES = 5
_GUARD_KEY_BYTES = 32

# Feature flag — off by default, enabled via env
_FEATURE_FLAG_ENV = "FIN_WORKSPACE_ENABLED"

# Control-token for internal S2S calls
_FIN_CONTROL_TOKEN_ENV = "FIN_WORKSPACE_CONTROL_TOKEN"

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
def is_fin_workspace_enabled() -> bool:
    return os.environ.get(_FEATURE_FLAG_ENV, "").strip().lower() in ("1", "true", "yes")


def _resolve_control_token() -> str:
    token = os.environ.get(_FIN_CONTROL_TOKEN_ENV, "").strip()
    if not token:
        token = os.environ.get("JWT_SECRET", "").strip()
    return token


def _guard_key() -> bytes:
    """Derive a guard key from the control token for HMAC-based claim binding."""
    token = _resolve_control_token()
    if not token:
        return hashlib.sha256(b"fin-workspace-guard").digest()
    return hashlib.sha256(token.encode()).digest()


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
                    rejected_reason TEXT,
                    created_at REAL NOT NULL,
                    accepted_at REAL,
                    rejected_at REAL,
                    FOREIGN KEY (checkpoint_id) REFERENCES fin_terminal_checkpoints(checkpoint_id)
                )
            """)

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
                # auth_codes table doesn't exist yet (fresh DB before Auth init)
                pass

            # Guard key for claim binding
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fin_workspace_guard (
                    guard_id TEXT PRIMARY KEY DEFAULT 'primary',
                    guard_key_hash TEXT NOT NULL DEFAULT ''
                )
            """)

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
                "SELECT checkpoint_id, status, handoff_id, expires_at "
                "FROM fin_terminal_checkpoints WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if existing:
                if existing[1] == "ready":
                    handoff_id = existing[2]
                    handoff_secret = self._derive_handoff_secret(handoff_id)
                    auth_url = self._build_auth_url(handoff_id, handoff_secret)
                    from urllib.parse import urlencode
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

        # Generate handoff credentials
        handoff_id = f"fh-{_uuid_hex()[:16]}"
        handoff_secret = secrets.token_hex(_HANDOFF_SECRET_BYTES)
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
    ) -> dict:
        """Validate handoff and create a one-time claim token.

        The claim secret is an opaque bearer stored in an HttpOnly
        Secure SameSite=Lax cookie. The raw secret is never in the URL,
        log, referrer, or analytics.

        Returns the claim_id and the auth_code redirect for OAuth.
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
            claim_expires = _format_expiry(_CHECKPOINT_EXPIRY_SECONDS)

            browser_nonce_hash = hashlib.sha256(
                str(browser_nonce or "").encode()
            ).hexdigest()

            conn.execute(
                "INSERT INTO fin_terminal_claims "
                "(claim_id, checkpoint_id, claim_secret_hash, claim_secret_expires_at, "
                "browser_nonce_hash, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (claim_id, checkpoint_id, claim_secret_hash, claim_expires,
                 browser_nonce_hash, now),
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
                "final_account_user_id "
                "FROM fin_terminal_claims WHERE claim_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                raise ClaimRejectedError("claim not found")

            (db_claim_id, checkpoint_id, db_secret_hash,
             claim_expires, db_browser_nonce_hash, status,
             existing_account_user_id) = row

            # Reject if already accepted
            if status == "accepted":
                if existing_account_user_id and existing_account_user_id != uid:
                    raise ClaimRejectedError("claim already accepted by a different account")
                # Idempotent re-accept for same account (retry)
                return self._finalize_accept(conn, cid, checkpoint_id, uid, email, now,
                                            auth_code_id, already_accepted=True)

            if status != "pending":
                raise ClaimRejectedError(f"claim is {status}, not pending")
            if now > claim_expires:
                conn.execute(
                    "UPDATE fin_terminal_claims SET status = 'expired', rejected_reason = 'expired' "
                    "WHERE claim_id = ?",
                    (cid,),
                )
                conn.execute(
                    "UPDATE fin_terminal_checkpoints SET status = 'expired', expired_at = ? "
                    "WHERE checkpoint_id = ? AND status = 'claiming'",
                    (now, checkpoint_id),
                )
                raise ClaimRejectedError("claim expired")

            # Verify claim secret
            if not hmac.compare_digest(secret_hash, db_secret_hash or ""):
                raise ClaimRejectedError("claim secret mismatch")

            # Verify browser nonce (same-tab binding)
            expected_bnonce = hashlib.sha256(bnonce.encode()).hexdigest()
            if db_browser_nonce_hash and not hmac.compare_digest(expected_bnonce, db_browser_nonce_hash):
                raise ClaimRejectedError("browser nonce mismatch — cross-tab claim rejected")

            # Mark claim accepted
            ostate_hash = hashlib.sha256(ostate.encode()).hexdigest()
            conn.execute(
                "UPDATE fin_terminal_claims SET status = 'accepted', "
                "final_account_user_id = ?, final_account_email = ?, "
                "oauth_state_hash = ?, auth_code_id = ?, accepted_at = ? "
                "WHERE claim_id = ? AND status = 'pending'",
                (uid, email, ostate_hash, str(auth_code_id or ""), now, cid),
            )

            cur = conn.execute("SELECT changes()")
            if cur.fetchone()[0] == 0:
                # Lost race — another callback won
                recheck = conn.execute(
                    "SELECT status, final_account_user_id FROM fin_terminal_claims WHERE claim_id = ?",
                    (cid,),
                ).fetchone()
                if recheck and recheck[0] == "accepted" and recheck[1] != uid:
                    raise ClaimRejectedError("claim accepted by a different account")
                elif recheck and recheck[0] == "accepted":
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

        # Create snapshot from checkpoint data
        ckpt_data = self.store.get(checkpoint_id)
        if ckpt_data is not None:
            try:
                snapshot_json_str = ckpt_data.decode("utf-8")
                # Validate it's valid JSON
                json.loads(snapshot_json_str)
            except (UnicodeDecodeError, json.JSONDecodeError):
                snapshot_json_str = "{}"
        else:
            snapshot_json_str = "{}"

        snapshot_id = f"fsn-{_uuid_hex()[:16]}"
        conn.execute(
            "INSERT INTO financial_workspace_snapshots "
            "(snapshot_id, workspace_id, checkpoint_id, version, snapshot_json, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (snapshot_id, workspace_id, checkpoint_id, snapshot_json_str, now),
        )

        # Enqueue outbox effects
        self._enqueue_effect_under_lock(conn, "workspace_upsert", {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "is_new": is_new_workspace,
        }, now)

        if is_new_workspace:
            self._enqueue_effect_under_lock(conn, "account_grant", {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "grant_micro_usd": _NEW_ACCOUNT_GRANT_MICRO_USD,
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

    def get_claim(self, claim_id: str) -> dict | None:
        cid = str(claim_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT claim_id, checkpoint_id, status, final_account_user_id, "
                "final_account_email, rejected_reason, created_at, accepted_at "
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
                    "WHERE checkpoint_id = ?",
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

        The handoff_secret is included so the front-end can POST to the
        claim endpoint. The raw secret MUST NOT appear in logs.

        The contract with the public app: the app opens this URL in a
        new or same tab, the page reads the handoff from the URL
        fragment or server-rendered state, then initiates OAuth.
        """
        from urllib.parse import urlencode
        base = os.environ.get("FIN_TERMINAL_BASE_URL", "https://terminal.unchainedsky.com")
        params = urlencode({
            "handoff_id": handoff_id,
            "handoff_secret": handoff_secret,
            "action": "claim",
        })
        return f"{base}/fin-terminal/claim?{params}"

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
        with self._conn() as conn:
            # Delete snapshots, imports, then workspace
            conn.execute(
                "DELETE FROM financial_workspace_snapshots WHERE workspace_id = ?",
                (wid,),
            )
            conn.execute(
                "DELETE FROM financial_workspace_imports WHERE workspace_id = ?",
                (wid,),
            )
            # Delete related checkpoints from storage
            chk_rows = conn.execute(
                "SELECT checkpoint_id FROM financial_workspace_imports WHERE workspace_id = ?",
                (wid,),
            ).fetchall()
            conn.execute(
                "DELETE FROM fin_terminal_claims "
                "WHERE checkpoint_id IN (SELECT checkpoint_id FROM financial_workspace_imports WHERE workspace_id = ?)",
                (wid,),
            )
            conn.execute(
                "DELETE FROM fin_terminal_checkpoints "
                "WHERE checkpoint_id IN (SELECT checkpoint_id FROM financial_workspace_imports WHERE workspace_id = ?)",
                (wid,),
            )
            for (chk_id,) in chk_rows:
                self.store.delete(chk_id)
            conn.execute(
                "DELETE FROM financial_workspace_imports WHERE workspace_id = ?",
                (wid,),
            )
            conn.execute(
                "DELETE FROM financial_workspaces WHERE workspace_id = ?",
                (wid,),
            )
        return {"deleted": True, "workspace_id": wid, "user_id": uid}

    def export_workspace_metadata(self, user_id: str) -> dict | None:
        """Export metadata for data portability."""
        ws = self.get_workspace_for_user(user_id)
        if ws is None:
            return None
        snapshots = self.get_snapshots_for_workspace(ws["workspace_id"])
        return {
            "workspace": ws,
            "snapshots": snapshots,
        }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
def create_financial_workspace(db_path: str, store: CheckpointStore | None = None) -> FinancialWorkspace:
    """Create a FinancialWorkspace instance with the default store adapter."""
    if store is None:
        from checkpoint_store import create_checkpoint_store
        store = create_checkpoint_store()
    return FinancialWorkspace(db_path, store)
