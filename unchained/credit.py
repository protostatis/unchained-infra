"""Credit / inference accounting module for hosted OpenRouter inference.

Stores integer micro-USD. Provides atomic, idempotent operations around
credit accounts, immutable ledger entries, inference runs, per-call reservations,
and provider usage records.

Uses SQLite WAL, foreign_keys, busy_timeout, short BEGIN IMMEDIATE transactions.

Usage:
    from credit import CreditLedger

    ledger = CreditLedger(db_path="/path/to/auth.db")
    account = ledger.ensure_account("u-abc123")
    run_id = ledger.create_run(account_id=account["account_id"],
                                user_id="u-abc123",
                                idempotency_key="chat-turn-...")
    call_id = ledger.reserve_call(run_id=run_id, model="google/gemini-flash-lite",
                                  reservation_micro_usd=10, idempotency_key="or-call-...")
    # ... make the API call ...
    ledger.settle_call(call_id, actual_cost_micro_usd=7)
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid as _uuid_module


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

def _enable_wal(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# Model catalog / allowlist
# ---------------------------------------------------------------------------

# Hosted model catalog — models the server is willing to reserve for.
# Each entry maps a model identifier to a conservative per-call reservation
# in micro-USD. Unknown/unlisted models are rejected.
# Conservative means each reservation covers at least one typical
# provider call; actual settlement may be lower.
HOSTED_MODEL_CATALOG: dict[str, int] = {
    # Google Gemini family
    "google/gemini-3.1-flash-lite": 5,  # very cheap
    "google/gemini-2.5-flash-lite": 5,
    "google/gemini-2.5-flash": 10,
    "google/gemini-2.5-pro": 50,
    "google/gemma-3-27b-it:free": 0,  # free tier
    # Trinity / StepFun free-tier fallbacks
    "arcee-ai/trinity-large-preview:free": 0,
    "stepfun/step-3.5-flash:free": 0,
    # Meta Llama
    "meta-llama/llama-3.3-70b-instruct:free": 0,
    # DeepSeek
    "deepseek/deepseek-chat-v3-0324:free": 0,
    "deepseek/deepseek-chat:free": 0,
    "deepseek/deepseek-r1:free": 0,
}

# Default reservation when a model is in the catalog but has no explicit
# reservation amount (free models). 1 micro-USD = $0.000001 — effectively
# prevents zero-reservation call accounting gaps.
_DEFAULT_CONSERVATIVE_RESERVATION_MICRO_USD: int = max(
    1, int(os.environ.get("CREDIT_DEFAULT_RESERVATION_MICRO_USD", "100"))
)  # default $0.0001 per call


def _default_reservation(model: str) -> int:
    """Return the conservative per-call reservation in micro-USD."""
    m = (model or "").strip()
    if not m:
        return _DEFAULT_CONSERVATIVE_RESERVATION_MICRO_USD
    catalog_val = HOSTED_MODEL_CATALOG.get(m)
    if catalog_val is not None:
        if catalog_val > 0:
            return catalog_val
        return 1  # free model, but non-zero to track
    return _DEFAULT_CONSERVATIVE_RESERVATION_MICRO_USD


def is_hosted_model_allowed(
    model: str,
    *,
    admin_allowlist: set[str] | None = None,
    allow_slash_models: bool = False,
) -> bool:
    """Check if a model is in the hosted catalog or admin-allowed.

    By default, only models in HOSTED_MODEL_CATALOG are allowed.
    Models containing slashes that aren't in the catalog are rejected
    unless ``allow_slash_models`` is True (admin override).
    """
    m = (model or "").strip()
    if not m:
        return False
    if m in HOSTED_MODEL_CATALOG:
        return True
    if admin_allowlist and m in admin_allowlist:
        return True
    if allow_slash_models and "/" in m:
        return True
    return False


# Default admin-allowable models (can be extended via env)
_ADMIN_ALLOWLIST_ENV = os.environ.get("CREDIT_ADMIN_ALLOWLIST", "").strip()
_ADMIN_ALLOWLIST: set[str] = set(
    m.strip() for m in _ADMIN_ALLOWLIST_ENV.split(",") if m.strip()
)

# Whether slash-containing models not in the catalog are allowed (off by default)
_ALLOW_SLASH_MODELS = os.environ.get("CREDIT_ALLOW_SLASH_MODELS", "") == "1"


# ---------------------------------------------------------------------------
# CreditLedger
# ---------------------------------------------------------------------------

class CreditLedger:
    """Atomic credit accounting backed by SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        _enable_wal(conn)
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            _begin_immediate(conn)

            # Credit accounts
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

            # Immutable ledger entries (append-only)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_ledger (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    entry_type TEXT NOT NULL CHECK (
                        entry_type IN ('grant', 'reversal', 'reservation', 'settlement', 'release', 'fee')
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

            # Inference runs
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

            # Per-provider-call reservations
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
                    settled_at REAL,
                    released_at REAL,
                    FOREIGN KEY (run_id) REFERENCES credit_runs(run_id),
                    FOREIGN KEY (account_id) REFERENCES credit_accounts(account_id),
                    UNIQUE (run_id, idempotency_key)
                )
            """)

            # Provider usage records
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
                    provider_cost_micro_usd INTEGER NOT NULL DEFAULT 0,
                    provider_response_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (call_id) REFERENCES credit_call_reservations(call_id),
                    FOREIGN KEY (run_id) REFERENCES credit_runs(run_id),
                    FOREIGN KEY (account_id) REFERENCES credit_accounts(account_id)
                )
            """)

            # Indexes
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
                "CREATE INDEX IF NOT EXISTS idx_credit_call_reservations_run "
                "ON credit_call_reservations(run_id, status)"
            )

            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def ensure_account(self, user_id: str) -> dict:
        """Get or create a credit account for a user.

        Returns {account_id, user_id, balance_micro_usd, ...}
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")
        now = _now_ts()
        with self._conn() as conn:
            _begin_immediate(conn)
            row = conn.execute(
                "SELECT account_id, user_id, balance_micro_usd, total_granted_micro_usd, "
                "total_spent_micro_usd, created_at, updated_at "
                "FROM credit_accounts WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row:
                conn.execute("COMMIT")
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
                (account_id, uid, now, now),
            )
            conn.execute("COMMIT")
            return {
                "account_id": account_id,
                "user_id": uid,
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
        """Get credit account state for a user."""
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
        """Return balance_micro_usd for a user (0 if no account)."""
        acct = self.get_account(user_id)
        return acct["balance_micro_usd"] if acct else 0

    # ------------------------------------------------------------------
    # Idempotent grant / reversal
    # ------------------------------------------------------------------

    def grant(self, user_id: str, amount_micro_usd: int, *,
              idempotency_key: str = "",
              metadata: dict | None = None) -> dict:
        """Idempotently add credit to a user's account.

        Returns updated account state and the grant entry.
        """
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
            account = self.ensure_account(uid)
            account_id = account["account_id"]

            _begin_immediate(conn)

            # Check idempotency
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
        """Idempotently reverse (deduct) credit from a user.

        Only usable when no reservations are held against the reversed amount.
        """
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
            account = self.ensure_account(uid)
            account_id = account["account_id"]

            _begin_immediate(conn)

            # Check idempotency
            existing = conn.execute(
                "SELECT entry_id FROM credit_ledger "
                "WHERE account_id = ? AND idempotency_key = ?",
                (account_id, ikey),
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {**account, "already_applied": True}

            # Check held reservations — balance must cover held + reversal
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
        """Return total currently held micro-USD for an account."""
        with self._conn() as conn:
            return self._held_reservation_total_under_lock(conn, account_id)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, user_id: str, *,
                   model: str = "unknown",
                   idempotency_key: str = "",
                   metadata: dict | None = None) -> dict:
        """Create an inference run.

        Returns {run_id, ...}
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id required")

        ikey = str(idempotency_key or "").strip()
        if not ikey:
            raise ValueError("idempotency_key required for runs")

        now = _now_ts()
        model_name = (model or "unknown").strip()

        with self._conn() as conn:
            account = self.ensure_account(uid)
            account_id = account["account_id"]

            _begin_immediate(conn)

            # Idempotency check
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
        """Finish an inference run.

        If ``release_held`` is True, all remaining held reservations are released.
        """
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
                self._release_held_calls_under_lock(conn, run_id, row[1], now)

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
    ) -> dict:
        """Reserve credit for an upcoming provider API call.

        Deducts from available balance (balance - held = available).
        Returns {call_id, ...}
        Raises InsufficientBalanceError if not enough available credit.
        """
        if reservation_micro_usd < 0:
            reservation_micro_usd = 0
        if reservation_micro_usd == 0:
            reservation_micro_usd = _default_reservation(model)

        ikey = str(idempotency_key or "").strip()
        if not ikey:
            raise ValueError("idempotency_key required for reservations")

        now = _now_ts()
        model_name = (model or "unknown").strip()

        with self._conn() as conn:
            _begin_immediate(conn)

            # Verify run exists and is active
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

            # Enforce user_id ownership if provided
            if user_id and user_id != run_owner:
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"user_id {user_id} does not own run {run_id} "
                    f"(owner: {run_owner})"
                )

            # Idempotency check (scoped to run)
            existing = conn.execute(
                "SELECT call_id, status, reserved_micro_usd, settled_micro_usd "
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
                    "status": existing[1],
                    "reserved_micro_usd": int(existing[2]),
                    "already_reserved": True,
                }

            # Check available balance
            acct_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = int(acct_row[0]) if acct_row else 0
            held = self._held_reservation_total_under_lock(conn, account_id)
            available = balance - held

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

            # Log in ledger
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'reservation', ?, ?, ?, ?, ?, ?, ?)",
                (account_id, f"reserve-{ikey}", -reservation_micro_usd, balance,
                 run_id, call_id, model_name,
                 _json_meta({"type": "reservation"}), now),
            )

            # Update run counters
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
                "reserved_usd": _micro_to_usd(reservation_micro_usd),
                "already_reserved": False,
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
    ) -> dict:
        """Settle a reserved call with actual usage.

        Only the reserved amount (``actual_cost_micro_usd`` capped at
        ``reserved_micro_usd``) is deducted from the balance. The remaining
        reservation amount is released back.
        """
        now = _now_ts()

        with self._conn() as conn:
            _begin_immediate(conn)

            row = conn.execute(
                "SELECT call_id, run_id, account_id, user_id, model, status, "
                "reserved_micro_usd, settled_micro_usd "
                "FROM credit_call_reservations WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Call not found: {call_id}")
            if row[5] == "settled":
                conn.execute("COMMIT")
                return {
                    "call_id": call_id,
                    "status": "settled",
                    "already_settled": True,
                }
            if row[5] != "held":
                conn.execute("ROLLBACK")
                raise ValueError(f"Cannot settle call in state: {row[5]}")

            run_id = row[1]
            account_id = row[2]
            user_id = row[3]
            model_name = row[4]
            reserved = int(row[6])
            previously_settled = int(row[7])

            # Actual cost is capped at reserved amount
            actual = max(0, min(actual_cost_micro_usd, reserved))

            # Release the difference back
            released = reserved - actual

            # Update account balance: deduct actual cost
            acct_row = conn.execute(
                "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            new_balance = max(0, int(acct_row[0]) - actual)

            conn.execute(
                "UPDATE credit_accounts SET balance_micro_usd = ?, "
                "total_spent_micro_usd = total_spent_micro_usd + ?, "
                "updated_at = ? WHERE account_id = ?",
                (new_balance, actual, now, account_id),
            )

            # Update reservation
            conn.execute(
                "UPDATE credit_call_reservations SET status = 'settled', "
                "settled_micro_usd = ?, settled_at = ? WHERE call_id = ?",
                (actual, now, call_id),
            )

            # Log settlement
            conn.execute(
                "INSERT INTO credit_ledger "
                "(account_id, idempotency_key, entry_type, amount_micro_usd, "
                "balance_after_micro_usd, run_id, call_id, model, "
                "metadata_json, created_at) "
                "VALUES (?, ?, 'settlement', ?, ?, ?, ?, ?, ?, ?)",
                (account_id, f"settle-{call_id}-{_now_ts()}", -actual, new_balance,
                 run_id, call_id, model_name,
                 _json_meta({
                     "type": "settlement",
                     "reserved_micro_usd": reserved,
                     "settled_micro_usd": actual,
                     "released_micro_usd": released,
                 }), now),
            )

            # Record provider usage
            pt = max(0, int(prompt_tokens or 0))
            ct = max(0, int(completion_tokens or 0))
            tt = max(0, int(total_tokens or 0))
            if tt <= 0:
                tt = pt + ct
            conn.execute(
                "INSERT INTO credit_provider_usage "
                "(call_id, run_id, account_id, model, prompt_tokens, "
                "completion_tokens, total_tokens, provider_cost_micro_usd, "
                "provider_response_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (call_id, run_id, account_id, model_name,
                 pt, ct, tt, actual,
                 _json_meta(provider_response or {}), now),
            )

            # Update run
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
                "balance_after_micro_usd": new_balance,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "already_settled": False,
            }

    def release_call(self, call_id: str) -> dict:
        """Release a held call reservation — no cost is deducted."""
        now = _now_ts()

        with self._conn() as conn:
            _begin_immediate(conn)

            row = conn.execute(
                "SELECT call_id, run_id, account_id, user_id, model, status, "
                "reserved_micro_usd, settled_micro_usd "
                "FROM credit_call_reservations WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Call not found: {call_id}")
            if row[5] == "released":
                conn.execute("COMMIT")
                return {
                    "call_id": call_id,
                    "status": "released",
                    "already_released": True,
                }
            if row[5] != "held":
                conn.execute("ROLLBACK")
                raise ValueError(f"Cannot release call in state: {row[5]}")

            account_id = row[2]
            model_name = row[4]
            reserved = int(row[6])
            run_id = row[1]

            # Mark released
            conn.execute(
                "UPDATE credit_call_reservations SET status = 'released', "
                "released_at = ? WHERE call_id = ?",
                (now, call_id),
            )

            # Log release (no balance change; credit was not deducted, only held)
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
                (account_id, f"release-{call_id}-{_now_ts()}", reserved, balance,
                 run_id, call_id, model_name,
                 _json_meta({
                     "type": "release",
                     "released_micro_usd": reserved,
                 }), now),
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
    def _release_held_calls_under_lock(conn: sqlite3.Connection, run_id: str,
                                        account_id: str, now: float) -> None:
        """Release all held call reservations for a run."""
        held_calls = conn.execute(
            "SELECT call_id, reserved_micro_usd, settled_micro_usd, model "
            "FROM credit_call_reservations "
            "WHERE run_id = ? AND status = 'held'",
            (run_id,),
        ).fetchall()
        for call in held_calls:
            call_id = call[0]
            reserved = int(call[1])
            settled = int(call[2])
            model_name = call[3]
            if reserved > settled:
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

    def release_all_held(self, account_id: str) -> int:
        """Release all held call reservations for an account. Returns count."""
        now = _now_ts()
        with self._conn() as conn:
            _begin_immediate(conn)
            self._release_held_calls_under_lock_all(conn, account_id, now)
            held = conn.execute(
                "SELECT COUNT(*) FROM credit_call_reservations "
                "WHERE account_id = ? AND status = 'held'",
                (account_id,),
            ).fetchone()
            conn.execute("COMMIT")
            return int(held[0])

    @staticmethod
    def _release_held_calls_under_lock_all(conn: sqlite3.Connection,
                                            account_id: str, now: float) -> None:
        held_calls = conn.execute(
            "SELECT call_id, run_id, reserved_micro_usd, settled_micro_usd, model "
            "FROM credit_call_reservations "
            "WHERE account_id = ? AND status = 'held'",
            (account_id,),
        ).fetchall()
        for call in held_calls:
            call_id = call[0]
            run_id = call[1]
            reserved = int(call[2])
            settled = int(call[3])
            model_name = call[4]
            if reserved > settled:
                conn.execute(
                    "UPDATE credit_call_reservations SET status = 'released', "
                    "released_at = ? WHERE call_id = ?",
                    (now, call_id),
                )
                balance = int(
                    conn.execute(
                        "SELECT balance_micro_usd FROM credit_accounts WHERE account_id = ?",
                        (account_id,),
                    ).fetchone()[0]
                )
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

    # ------------------------------------------------------------------
    # History / ledger queries
    # ------------------------------------------------------------------

    def get_ledger_for_user(self, user_id: str, limit: int = 100) -> list[dict]:
        """Return immutable ledger entries for a user."""
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
        """Get provider usage records for a run."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT usage_id, call_id, model, prompt_tokens, completion_tokens, "
                "total_tokens, provider_cost_micro_usd, created_at "
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
                "provider_cost_micro_usd": int(r[6]),
                "provider_cost_usd": _micro_to_usd(int(r[6])),
                "created_at": r[7],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Trial grant initialisation
    # ------------------------------------------------------------------

    def ensure_trial_grant_from_openrouter_budget(
        self,
        user_id: str,
        current_spend_usd: float = 0.0,
        budget_usd: float = 1.0,
    ) -> dict:
        """Lazily create an opening trial grant from existing OpenRouter budget.

        Grant amount = budget - spend (converted to micro-USD). Idempotent.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {}

        grant_micro = _usd_to_micro(max(0.0, float(budget_usd) - float(current_spend_usd)))
        if grant_micro <= 0:
            return {"granted_micro_usd": 0, "reason": "no_remaining_budget"}

        # Use a stable, idempotent key to prevent double-grants on first run
        ikey = f"trial-grant-openrouter-budget-{uid}"
        try:
            result = self.grant(uid, grant_micro, idempotency_key=ikey)
            return {
                **result,
                "granted_micro_usd": grant_micro if not result.get("already_applied") else 0,
                "idempotency_key": ikey,
            }
        except Exception:
            # If already applied or error, try to return current account
            acct = self.get_account(uid)
            if acct:
                return {
                    **acct,
                    "granted_micro_usd": 0,
                    "idempotency_key": ikey,
                    "reason": "already_granted_or_error",
                }
            return {"granted_micro_usd": 0, "reason": "error"}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InsufficientBalanceError(Exception):
    """Not enough available credit (balance minus held reservations)."""
    pass


class RunNotActiveError(Exception):
    """Run is not in active state."""
    pass


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
