"""Unchained Auth — SQLite-backed API key management.

Manages API keys for agent authentication. Keys use the format
``uc_live_`` + 24 random hex characters (e.g. ``uc_live_a1b2c3...``).

Usage:
    from auth import Auth

    auth = Auth()                      # Default: ~/.unchained/auth.db
    key = auth.create_key("user123")   # Generate new key
    info = auth.validate_key(key)      # Returns {user_id, key} or None
    auth.revoke_key(key)               # Deactivate key

CLI:
    uv run auth.py create <user_id>    # Create and print a new key
    uv run auth.py revoke <key>        # Revoke a key
    uv run auth.py list                # List all active keys
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import sys
import time

KEY_PREFIX = "uc_live_"
KEY_RAND_BYTES = 12  # 24 hex chars


class Auth:
    """SQLite-backed API key store."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.environ.get(
                "UNCHAINED_DB_PATH",
                os.path.expanduser("~/.unchained/auth.db"),
            )
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    active INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT,
                    picture TEXT,
                    api_key TEXT,
                    created_at REAL NOT NULL,
                    last_login_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS install_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_codes (
                    code TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER DEFAULT 0
                )
            """)
            # Migration: add status column (existing users default to 'approved')
            try:
                conn.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'approved'")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Migration: add user_type column (existing users default to 'claude')
            try:
                conn.execute("ALTER TABLE users ADD COLUMN user_type TEXT DEFAULT 'claude'")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Migration: add demo_prompt_count column (existing users default to 0)
            try:
                conn.execute("ALTER TABLE users ADD COLUMN demo_prompt_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Migration: add turn-based rate limiting columns
            for col_def in (
                "daily_turns_used INTEGER DEFAULT 0",
                "daily_turns_date TEXT",
                "window_turns_used INTEGER DEFAULT 0",
                "window_start REAL DEFAULT 0",
            ):
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass  # Column already exists
            # Migration: add OpenRouter trial usage budget columns
            for col_def in (
                "openrouter_spend_usd REAL DEFAULT 0",
                "openrouter_budget_usd REAL",
                "openrouter_budget_assigned_at REAL",
                "openrouter_prompt_tokens INTEGER DEFAULT 0",
                "openrouter_completion_tokens INTEGER DEFAULT 0",
                "openrouter_total_tokens INTEGER DEFAULT 0",
                "openrouter_usage_events INTEGER DEFAULT 0",
                "openrouter_last_usage_at REAL",
            ):
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass  # Column already exists
            # Auto-approve trigger: any new user inserted with status='pending'
            # is immediately promoted to 'approved' with a fresh API key.
            # Drop+recreate keeps the trigger body in sync with KEY_PREFIX /
            # KEY_RAND_BYTES if those constants ever change.
            conn.execute("DROP TRIGGER IF EXISTS auto_approve_pending_users")
            conn.execute(f"""
                CREATE TRIGGER auto_approve_pending_users
                AFTER INSERT ON users
                WHEN NEW.status = 'pending'
                BEGIN
                    UPDATE users
                    SET status = 'approved',
                        api_key = '{KEY_PREFIX}' || lower(hex(randomblob({KEY_RAND_BYTES})))
                    WHERE user_id = NEW.user_id;
                    INSERT INTO api_keys (key, user_id, created_at, active)
                    SELECT api_key, user_id, created_at, 1
                    FROM users WHERE user_id = NEW.user_id;
                END
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def validate_key(self, key: str) -> dict | None:
        """Validate an API key. Returns ``{user_id, key}`` or ``None``."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM api_keys WHERE key = ? AND active = 1",
                (key,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key = ?",
                (time.time(), key),
            )
            return {"user_id": row[0], "key": key}

    def create_key(self, user_id: str) -> str:
        """Generate and store a new API key. Returns the key string."""
        key = KEY_PREFIX + secrets.token_hex(KEY_RAND_BYTES)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (key, user_id, created_at) VALUES (?, ?, ?)",
                (key, user_id, time.time()),
            )
        return key

    def revoke_key(self, key: str) -> bool:
        """Deactivate an API key. Returns True if the key existed."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET active = 0 WHERE key = ?",
                (key,),
            )
            return cur.rowcount > 0

    def get_keys_for_user(self, user_id: str) -> list[str]:
        """Get all active API keys belonging to a user."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key FROM api_keys WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def list_keys(self, active_only: bool = True) -> list[dict]:
        """List API keys."""
        query = "SELECT key, user_id, created_at, last_used_at, active FROM api_keys"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(query).fetchall()
        return [
            {
                "key": r[0],
                "user_id": r[1],
                "created_at": r[2],
                "last_used_at": r[3],
                "active": bool(r[4]),
            }
            for r in rows
        ]

    # --- Install tokens (curl|bash installer) ---

    def create_install_token(self, user_id: str, api_key: str, ttl: int = 900) -> str:
        """Create a short-lived install token. Returns ``inst_`` + 32 hex chars."""
        token = "inst_" + secrets.token_hex(16)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO install_tokens (token, user_id, api_key, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, user_id, api_key, now, now + ttl),
            )
        return token

    def validate_install_token(self, token: str, consume: bool = True) -> dict | None:
        """Validate an install token.

        Returns ``{user_id, api_key}`` or ``None``.
        When ``consume`` is true, the token is marked used on success.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id, api_key FROM install_tokens "
                "WHERE token = ? AND used = 0 AND expires_at > ?",
                (token, time.time()),
            ).fetchone()
            if row is None:
                return None
            if consume:
                conn.execute(
                    "UPDATE install_tokens SET used = 1 WHERE token = ?",
                    (token,),
                )
            return {"user_id": row[0], "api_key": row[1]}

    def cleanup_expired_tokens(self):
        """Delete expired or used install tokens."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM install_tokens WHERE expires_at < ? OR used = 1",
                (time.time(),),
            )

    # --- User management (Google sign-in) ---

    def find_user_by_email(self, email: str) -> dict | None:
        """Find user by email. Returns {user_id, email, name, picture, api_key, status, user_type} or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id, email, name, picture, api_key, status, user_type FROM users WHERE email = ?",
                (email.lower(),),
            ).fetchone()
            if row is None:
                return None
            return {
                "user_id": row[0], "email": row[1], "name": row[2],
                "picture": row[3], "api_key": row[4], "status": row[5] or "approved",
                "user_type": row[6] or "claude",
            }

    def get_or_create_user(self, email: str, name: str = "", picture: str = "") -> dict:
        """Find user by email or create with a new API key. Returns user dict."""
        email = email.lower()
        user = self.find_user_by_email(email)
        if user:
            # Update last login and profile info
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET last_login_at = ?, name = ?, picture = ? WHERE email = ?",
                    (time.time(), name or user["name"], picture or user["picture"], email),
                )
            return user
        # Create new user with auto-generated API key
        user_id = "u-" + secrets.token_hex(4)
        api_key = self.create_key(user_id)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id, email, name, picture, api_key, created_at, last_login_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, name, picture, api_key, time.time(), time.time()),
            )
        return {
            "user_id": user_id, "email": email, "name": name,
            "picture": picture, "api_key": api_key,
        }

    def create_pending_user(self, email: str, name: str = "", picture: str = "", user_type: str = "claude") -> dict:
        """Create a user with status='pending' and no API key."""
        email = email.lower()
        user_id = "u-" + secrets.token_hex(4)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id, email, name, picture, api_key, created_at, last_login_at, status, user_type) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?)",
                (user_id, email, name, picture, now, now, user_type),
            )
        return {"user_id": user_id, "email": email, "name": name, "picture": picture,
                "api_key": None, "status": "pending", "user_type": user_type}

    def approve_user(self, email: str) -> dict | None:
        """Approve a pending user by setting status='approved'.

        If the user already has an API key (for example, pending trial/demo
        access), keep that key. Otherwise create one.
        """
        email = email.lower()
        user = self.find_user_by_email(email)
        if not user:
            return None
        api_key = user.get("api_key")
        if not api_key:
            api_key = self.create_key(user["user_id"])
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET status = 'approved', api_key = ? WHERE email = ?",
                (api_key, email),
            )
        user["status"] = "approved"
        user["api_key"] = api_key
        return user

    def reject_user(self, email: str) -> bool:
        """Reject a user. Returns True if user existed."""
        email = email.lower()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET status = 'rejected' WHERE email = ?",
                (email,),
            )
            return cur.rowcount > 0

    def list_pending_users(self) -> list[dict]:
        """Return all users with status='pending'."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, email, name, picture, created_at FROM users WHERE status = 'pending' "
                "ORDER BY created_at DESC",
            ).fetchall()
        return [{"user_id": r[0], "email": r[1], "name": r[2], "picture": r[3],
                 "created_at": r[4]} for r in rows]

    def list_all_users(self) -> list[dict]:
        """Return all users ordered by created_at desc."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, email, name, picture, created_at, status, last_login_at, user_type, "
                "openrouter_spend_usd, openrouter_budget_usd, "
                "openrouter_prompt_tokens, openrouter_completion_tokens, openrouter_total_tokens, "
                "openrouter_usage_events, openrouter_last_usage_at "
                "FROM users ORDER BY created_at DESC",
            ).fetchall()
        return [{"user_id": r[0], "email": r[1], "name": r[2], "picture": r[3],
                 "created_at": r[4], "status": r[5] or "approved", "last_login_at": r[6],
                 "user_type": r[7] or "claude",
                 "openrouter_spend_usd": float(r[8] or 0.0),
                 "openrouter_budget_usd": (float(r[9]) if r[9] is not None else None),
                 "openrouter_prompt_tokens": int(r[10] or 0),
                 "openrouter_completion_tokens": int(r[11] or 0),
                 "openrouter_total_tokens": int(r[12] or 0),
                 "openrouter_usage_events": int(r[13] or 0),
                 "openrouter_last_usage_at": r[14]} for r in rows]

    def get_user_status(self, email: str) -> str | None:
        """Return user status ('pending', 'approved', 'rejected') or None if not found."""
        email = email.lower()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM users WHERE email = ?", (email,),
            ).fetchone()
            if row is None:
                return None
            return row[0] or "approved"

    def get_user_agent_id(self, email: str) -> str | None:
        """Derive agent_id from user's API key. Returns 'claude-{hash}' or None."""
        user = self.find_user_by_email(email)
        if not user or not user.get("api_key"):
            return None
        return f"claude-{hashlib.sha256(user['api_key'].encode()).hexdigest()[:8]}"

    # --- Demo prompt quota ---

    def increment_demo_count(self, email: str) -> int:
        """Increment demo_prompt_count for a user. Returns the new count."""
        email = email.lower()
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET demo_prompt_count = COALESCE(demo_prompt_count, 0) + 1 WHERE email = ?",
                (email,),
            )
            row = conn.execute(
                "SELECT demo_prompt_count FROM users WHERE email = ?", (email,),
            ).fetchone()
            return (row[0] or 0) if row else 0

    def get_demo_count(self, email: str) -> int:
        """Return current demo_prompt_count for a user."""
        email = email.lower()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT demo_prompt_count FROM users WHERE email = ?", (email,),
            ).fetchone()
            return (row[0] or 0) if row else 0

    # --- Turn-based rate limiting ---

    def check_and_consume_turn(
        self, email: str, daily_limit: int, window_limit: int, window_seconds: float
    ) -> dict:
        """Check both daily and 5-min window limits; consume a turn if allowed.

        Returns dict with keys:
          allowed (bool), daily_remaining (int), window_remaining (int),
          and optionally resets_in (float seconds) when not allowed.
        """
        email = email.lower()
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT daily_turns_used, daily_turns_date, window_turns_used, window_start "
                "FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row is None:
                return {"allowed": False, "daily_remaining": 0, "window_remaining": 0}

            daily_used, daily_date, win_used, win_start = row
            daily_used = daily_used or 0
            win_used = win_used or 0
            win_start = win_start or 0.0

            # Auto-reset daily count if date changed
            if daily_date != today:
                daily_used = 0
                daily_date = today

            # Auto-reset window if window_seconds elapsed
            if now - win_start >= window_seconds:
                win_used = 0
                win_start = now

            # Check limits
            if daily_used >= daily_limit:
                conn.execute(
                    "UPDATE users SET daily_turns_used=?, daily_turns_date=?, "
                    "window_turns_used=?, window_start=? WHERE email=?",
                    (daily_used, daily_date, win_used, win_start, email),
                )
                return {
                    "allowed": False,
                    "daily_remaining": 0,
                    "window_remaining": max(0, window_limit - win_used),
                    "resets_in": 0,  # resets next calendar day
                }
            if win_used >= window_limit:
                conn.execute(
                    "UPDATE users SET daily_turns_used=?, daily_turns_date=?, "
                    "window_turns_used=?, window_start=? WHERE email=?",
                    (daily_used, daily_date, win_used, win_start, email),
                )
                resets_in = window_seconds - (now - win_start)
                return {
                    "allowed": False,
                    "daily_remaining": max(0, daily_limit - daily_used),
                    "window_remaining": 0,
                    "resets_in": max(0, resets_in),
                }

            # Consume turn
            daily_used += 1
            win_used += 1
            conn.execute(
                "UPDATE users SET daily_turns_used=?, daily_turns_date=?, "
                "window_turns_used=?, window_start=? WHERE email=?",
                (daily_used, daily_date, win_used, win_start, email),
            )
            return {
                "allowed": True,
                "daily_remaining": max(0, daily_limit - daily_used),
                "window_remaining": max(0, window_limit - win_used),
            }

    def get_turn_usage(self, email: str, daily_limit: int, window_limit: int, window_seconds: float) -> dict:
        """Return current usage stats without consuming a turn."""
        email = email.lower()
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT daily_turns_used, daily_turns_date, window_turns_used, window_start "
                "FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row is None:
                return {"daily_remaining": 0, "window_remaining": 0}

            daily_used, daily_date, win_used, win_start = row
            daily_used = daily_used or 0
            win_used = win_used or 0
            win_start = win_start or 0.0

            if daily_date != today:
                daily_used = 0
            if now - win_start >= window_seconds:
                win_used = 0

            return {
                "daily_remaining": max(0, daily_limit - daily_used),
                "window_remaining": max(0, window_limit - win_used),
            }

    # --- OpenRouter trial spend/budget ---

    @staticmethod
    def _derive_openrouter_budget(user_id: str, min_usd: float, max_usd: float) -> float:
        """Deterministically pick a per-user budget in [min_usd, max_usd]."""
        lo = max(0.0, float(min_usd))
        hi = max(lo, float(max_usd))
        if hi <= lo:
            return round(lo, 6)
        h = hashlib.sha256(user_id.encode("utf-8")).digest()
        frac = int.from_bytes(h[:8], "big") / float(2**64 - 1)
        return round(lo + (hi - lo) * frac, 6)

    def get_or_init_openrouter_budget(
        self,
        user_id: str,
        min_budget_usd: float = 1.0,
        max_budget_usd: float = 1.0,
    ) -> dict:
        """Return user's OpenRouter budget state and initialize budget on first use."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT openrouter_spend_usd, openrouter_budget_usd, "
                "openrouter_prompt_tokens, openrouter_completion_tokens, openrouter_total_tokens, "
                "openrouter_usage_events, openrouter_last_usage_at "
                "FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return {
                    "spent_usd": 0.0,
                    "budget_usd": 0.0,
                    "remaining_usd": 0.0,
                    "capped": True,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "usage_events": 0,
                    "last_usage_at": None,
                }

            spent = max(0.0, float(row[0] or 0.0))
            budget = row[1]
            prompt_tokens = max(0, int(row[2] or 0))
            completion_tokens = max(0, int(row[3] or 0))
            total_tokens = max(0, int(row[4] or 0))
            usage_events = max(0, int(row[5] or 0))
            last_usage_at = row[6]
            if budget is None or float(budget) <= 0:
                budget = self._derive_openrouter_budget(user_id, min_budget_usd, max_budget_usd)
                now = time.time()
                conn.execute(
                    "UPDATE users SET openrouter_budget_usd = ?, openrouter_budget_assigned_at = COALESCE(openrouter_budget_assigned_at, ?) "
                    "WHERE user_id = ?",
                    (budget, now, user_id),
                )
            budget_f = max(0.0, float(budget))
            remaining = max(0.0, budget_f - spent)
            return {
                "spent_usd": round(spent, 6),
                "budget_usd": round(budget_f, 6),
                "remaining_usd": round(remaining, 6),
                "capped": spent >= budget_f if budget_f > 0 else True,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "usage_events": usage_events,
                "last_usage_at": last_usage_at,
            }

    def add_openrouter_spend(
        self,
        user_id: str,
        cost_usd: float,
        min_budget_usd: float = 1.0,
        max_budget_usd: float = 1.0,
    ) -> dict:
        """Increment user's tracked OpenRouter spend and return budget state."""
        return self.add_openrouter_usage(
            user_id=user_id,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_usd=cost_usd,
            min_budget_usd=min_budget_usd,
            max_budget_usd=max_budget_usd,
        )

    def add_openrouter_usage(
        self,
        user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float,
        min_budget_usd: float = 1.0,
        max_budget_usd: float = 1.0,
    ) -> dict:
        """Increment user's OpenRouter spend + token counters and return budget state."""
        delta = max(0.0, float(cost_usd or 0.0))
        delta_prompt = max(0, int(prompt_tokens or 0))
        delta_completion = max(0, int(completion_tokens or 0))
        delta_total = max(0, int(total_tokens or 0))
        if delta_total <= 0:
            delta_total = delta_prompt + delta_completion
        with self._conn() as conn:
            row = conn.execute(
                "SELECT openrouter_spend_usd, openrouter_budget_usd, "
                "openrouter_prompt_tokens, openrouter_completion_tokens, openrouter_total_tokens, "
                "openrouter_usage_events "
                "FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return {
                    "spent_usd": 0.0,
                    "budget_usd": 0.0,
                    "remaining_usd": 0.0,
                    "capped": True,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "usage_events": 0,
                    "last_usage_at": None,
                }

            spent = max(0.0, float(row[0] or 0.0))
            budget = row[1]
            prompt_total = max(0, int(row[2] or 0))
            completion_total = max(0, int(row[3] or 0))
            token_total = max(0, int(row[4] or 0))
            usage_events = max(0, int(row[5] or 0))
            if budget is None or float(budget) <= 0:
                budget = self._derive_openrouter_budget(user_id, min_budget_usd, max_budget_usd)
                now = time.time()
                conn.execute(
                    "UPDATE users SET openrouter_budget_usd = ?, openrouter_budget_assigned_at = COALESCE(openrouter_budget_assigned_at, ?) "
                    "WHERE user_id = ?",
                    (budget, now, user_id),
                )
            spent += delta
            prompt_total += delta_prompt
            completion_total += delta_completion
            token_total += delta_total
            usage_events += 1
            now = time.time()
            conn.execute(
                "UPDATE users SET openrouter_spend_usd = ?, "
                "openrouter_prompt_tokens = ?, openrouter_completion_tokens = ?, openrouter_total_tokens = ?, "
                "openrouter_usage_events = ?, openrouter_last_usage_at = ? "
                "WHERE user_id = ?",
                (spent, prompt_total, completion_total, token_total, usage_events, now, user_id),
            )
            budget_f = max(0.0, float(budget))
            remaining = max(0.0, budget_f - spent)
            return {
                "spent_usd": round(spent, 6),
                "budget_usd": round(budget_f, 6),
                "remaining_usd": round(remaining, 6),
                "capped": spent >= budget_f if budget_f > 0 else True,
                "prompt_tokens": prompt_total,
                "completion_tokens": completion_total,
                "total_tokens": token_total,
                "usage_events": usage_events,
                "last_usage_at": now,
            }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("""Usage: uv run auth.py <command> [args]

Commands:
    create <user_id>   Create a new API key for user
    revoke <key>       Revoke an API key
    list               List all active keys
    list --all         List all keys (including revoked)""")
        return

    auth = Auth()
    cmd = args[0]

    if cmd == "create":
        if len(args) < 2:
            print("Usage: uv run auth.py create <user_id>", file=sys.stderr)
            sys.exit(1)
        key = auth.create_key(args[1])
        print(f"Created key for {args[1]}:")
        print(f"  {key}")
    elif cmd == "revoke":
        if len(args) < 2:
            print("Usage: uv run auth.py revoke <key>", file=sys.stderr)
            sys.exit(1)
        if auth.revoke_key(args[1]):
            print(f"Revoked: {args[1]}")
        else:
            print(f"Key not found: {args[1]}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "list":
        active_only = "--all" not in args
        keys = auth.list_keys(active_only=active_only)
        if not keys:
            print("No keys found.")
            return
        for k in keys:
            status = "active" if k["active"] else "revoked"
            print(f"  {k['key']}  user={k['user_id']}  {status}")
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
