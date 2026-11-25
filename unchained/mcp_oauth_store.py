"""SQLite-backed OAuth 2.1 state for the Unchained MCP connector.

Remote MCP connectors (Claude, ChatGPT, Gemini Apps, Gemini Enterprise) can
only authenticate with OAuth.  They have no field for a static
``Authorization: Bearer`` header, which is how every Unchained MCP client has
authenticated until now.  This module stores the authorization-server state
that lets those connectors sign in and receive their own token:

    client registration -> authorization request -> user consent ->
    authorization code -> access/refresh tokens -> grant

The same SQLite file (``UNCHAINED_DB_PATH``) is already mounted by both the
``web`` and ``mcp`` containers, so the website owns the consent screen while
the MCP server owns the protocol endpoints, without an extra shared secret.

Design rules enforced here:

* Only hashes of authorization codes, access tokens and refresh tokens are
  stored.  A database read cannot mint a usable token.
* Codes, approvals and refresh tokens are consumed with a single conditional
  ``UPDATE`` so two concurrent exchanges cannot both succeed.
* Every token points at a *grant*, and every grant is pinned to one exact
  agent id.  Revoking the grant kills all of its tokens at once.
* Secrets are never written to logs by this module.

The store is deliberately synchronous and short-lived per call, matching
``auth.py``.  Callers that run on the asyncio event loop should wrap writes in
``asyncio.to_thread`` where a slow disk could block (see ``mcp_server.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

# Token prefixes make credentials greppable in logs so they can be redacted,
# and let the MCP server route a request to the right verifier without a
# database hit on the API-key path.
ACCESS_TOKEN_PREFIX = "uco_at_"
REFRESH_TOKEN_PREFIX = "uco_rt_"
AUTHORIZATION_CODE_PREFIX = "uco_ac_"

# Lifetimes.  Short enough that a leaked authorization code is useless, long
# enough for a human to finish a consent screen.
AUTHORIZATION_REQUEST_TTL = 10 * 60
APPROVAL_TICKET_TTL = 2 * 60
AUTHORIZATION_CODE_TTL = 2 * 60
ACCESS_TOKEN_TTL = 15 * 60
REFRESH_TOKEN_TTL = 30 * 24 * 60 * 60
# A refresh family has an absolute lifetime, so rotation cannot renew a
# connection forever. Documented separately from the sliding refresh TTL.
FAMILY_TTL = 90 * 24 * 60 * 60
# How long revoked/expired family state is retained for replay detection and
# audit before pruning.
REVOKED_RETENTION = 7 * 24 * 60 * 60

# Terminal states for an authorization request.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_CONSUMED = "consumed"

_SCOPES_DEFAULT = ("mcp:browser",)


def _now() -> float:
    return time.time()


def _chunks(values, size: int = 400):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]





def _hash(value: str) -> str:
    """Hash a bearer secret for storage.  Never store the raw value."""
    return hashlib.sha256(value.encode()).hexdigest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class BudgetExceeded(Exception):
    """Raised when a bounded allocation (clients or requests) is full."""


class OAuthStore:
    """OAuth client, grant and token storage in the Unchained SQLite database."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.environ.get(
                "UNCHAINED_DB_PATH",
                os.path.expanduser("~/.unchained/auth.db"),
            )
        self.db_path = db_path
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            # WAL is already on for this file (auth.py sets it); setting it
            # again is cheap and keeps a fresh database consistent.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    grant_types TEXT NOT NULL,
                    token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_requests (
                    id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    state TEXT,
                    scopes TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    code_challenge_method TEXT NOT NULL,
                    resource TEXT,
                    status TEXT NOT NULL,
                    grant_id TEXT,
                    user_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_grants (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    key_fingerprint TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_approvals (
                    ticket_hash TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_families (
                    family_id TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_bindings (
                    provider TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'blocked', 'retired')),
                    authorization_version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (provider, binding_id)
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scheme TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 443,
                    path TEXT NOT NULL,
                    match TEXT NOT NULL CHECK (match IN ('exact', 'prefix')),
                    allow_query INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    owner_user_id TEXT,
                    binding_id TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE (scheme, host, port, path, match, allow_query)
                );

                CREATE TABLE IF NOT EXISTS mcp_oauth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_oauth_requests_expiry
                    ON mcp_oauth_requests (expires_at);
                CREATE INDEX IF NOT EXISTS idx_oauth_codes_grant
                    ON mcp_oauth_codes (grant_id);
                CREATE INDEX IF NOT EXISTS idx_oauth_tokens_grant
                    ON mcp_oauth_tokens (grant_id);
                CREATE INDEX IF NOT EXISTS idx_oauth_tokens_family
                    ON mcp_oauth_tokens (family_id);
                CREATE INDEX IF NOT EXISTS idx_oauth_families_expiry
                    ON mcp_oauth_families (expires_at);

                CREATE INDEX IF NOT EXISTS idx_oauth_grants_user
                    ON mcp_oauth_grants (user_id);
                CREATE INDEX IF NOT EXISTS idx_oauth_grants_client
                    ON mcp_oauth_grants (client_id);
                """
            )
            # Additive migration: give any pre-existing token rows a family so
            # the joins below keep working on an older database.
            conn.execute(
                """
                INSERT OR IGNORE INTO mcp_oauth_families
                    (family_id, grant_id, client_id, created_at, expires_at)
                SELECT family_id, grant_id, client_id, MIN(created_at),
                       MAX(expires_at) + ?
                  FROM mcp_oauth_tokens
                 GROUP BY family_id
                """,
                (FAMILY_TTL,),
            )
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(mcp_oauth_policies)")}
            if "owner_user_id" not in columns:
                # Additive: older databases gain the column without a rewrite.
                conn.execute(
                    "ALTER TABLE mcp_oauth_policies ADD COLUMN owner_user_id TEXT"
                )
            if "binding_id" not in columns:
                conn.execute(
                    "ALTER TABLE mcp_oauth_policies ADD COLUMN binding_id TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_oauth_policies_binding "
                "ON mcp_oauth_policies (binding_id)"
            )
            self._migrate_bindings(conn)
            self._seed_policies(conn)

    def _seed_policies(self, conn) -> None:
        """Install static callback policies, versioned, without resurrecting.

        Each entry records the seed version it arrived in.  A fresh database
        gets everything; an existing one gets only the entries newer than its
        marker, so a policy an operator removed stays removed while a newly
        shipped provider still reaches a live database.
        """
        from mcp_oauth_policy import DEFAULT_POLICIES, STATIC_POLICY_SET_VERSION

        row = conn.execute(
            "SELECT value FROM mcp_oauth_meta WHERE key = 'policy_seed_version'"
        ).fetchone()
        try:
            current = int(row["value"]) if row is not None else 0
        except (TypeError, ValueError):
            current = 0
        if current >= STATIC_POLICY_SET_VERSION:
            return

        for policy in DEFAULT_POLICIES:
            if int(policy.get("since", 1)) <= current:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO mcp_oauth_policies
                    (scheme, host, port, path, match, allow_query, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy["scheme"],
                    policy["host"].lower(),
                    int(policy["port"]),
                    policy["path"],
                    policy["match"],
                    int(policy["allow_query"]),
                    policy.get("note", ""),
                    _now(),
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO mcp_oauth_meta (key, value) VALUES ('policy_seed_version', ?)",
            (str(STATIC_POLICY_SET_VERSION),),
        )

    # -- clients -----------------------------------------------------------

    def save_client(
        self,
        *,
        client_id: str,
        client_name: str,
        redirect_uris: Iterable[str],
        grant_types: Iterable[str] | None = None,
        token_endpoint_auth_method: str = "none",
        source: str = "manual",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO mcp_oauth_clients
                    (client_id, client_name, redirect_uris, grant_types,
                     token_endpoint_auth_method, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_name = excluded.client_name,
                    redirect_uris = excluded.redirect_uris,
                    grant_types = excluded.grant_types,
                    token_endpoint_auth_method = excluded.token_endpoint_auth_method
                """,
                (
                    client_id,
                    client_name,
                    _json(list(redirect_uris)),
                    _json(list(grant_types or ("authorization_code", "refresh_token"))),
                    token_endpoint_auth_method,
                    source,
                    _now(),
                ),
            )

    def get_client(self, client_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "redirect_uris": _loads(row["redirect_uris"], []),
            "grant_types": _loads(row["grant_types"], []),
            "token_endpoint_auth_method": row["token_endpoint_auth_method"],
            "source": row["source"],
            "created_at": row["created_at"],
        }

    def touch_client(self, client_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_clients SET last_used_at = ? WHERE client_id = ?",
                (_now(), client_id),
            )

    def count_clients(self, *, since: float | None = None) -> int:
        with self._conn() as conn:
            if since is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM mcp_oauth_clients"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM mcp_oauth_clients WHERE created_at >= ?",
                    (since,),
                ).fetchone()
        return int(row["n"] if row else 0)

    def prune_dynamic_clients(self, *, older_than: float, keep_granted: bool = True) -> int:
        """Drop idle dynamic clients so open registration cannot grow forever."""
        with self._conn() as conn:
            if keep_granted:
                cur = conn.execute(
                    """
                    DELETE FROM mcp_oauth_clients
                    WHERE source = 'dcr'
                      AND created_at < ?
                      AND client_id NOT IN (SELECT client_id FROM mcp_oauth_grants)
                    """,
                    (older_than,),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM mcp_oauth_clients WHERE source = 'dcr' AND created_at < ?",
                    (older_than,),
                )
            return cur.rowcount or 0

    # -- callback bindings -------------------------------------------------

    def _migrate_bindings(self, conn) -> None:
        """Promote owned personal callbacks into canonical binding rows.

        Ownership used to be duplicated across the six policy rows of a
        binding.  A binding row is now authoritative; policies reference it.
        A binding whose rows disagree keeps a blocked tombstone instead of
        silently choosing an owner, and an unowned personal callback stays
        unbound, which callers treat as blocked.
        """
        from mcp_oauth_policy import PERSONAL_CALLBACK_MARKER, binding_id_from_uri, provider_for_binding

        marker = conn.execute(
            "SELECT value FROM mcp_oauth_meta WHERE key = 'bindings_migrated'"
        ).fetchone()
        if marker is not None and marker["value"] == "1":
            return
        rows = conn.execute(
            "SELECT id, path, owner_user_id FROM mcp_oauth_policies WHERE path LIKE ?",
            (f"%{PERSONAL_CALLBACK_MARKER}%",),
        ).fetchall()
        groups: dict[str, list] = {}
        for row in rows:
            binding_id = binding_id_from_uri(row["path"])
            if binding_id:
                groups.setdefault(binding_id, []).append(row)

        now = _now()
        for binding_id, members in groups.items():
            owners = sorted({str(r["owner_user_id"]) for r in members if r["owner_user_id"]})
            if not owners:
                continue
            if len(owners) != 1:
                # Conflicting owners: leave the rows unbound so every personal
                # callback fails closed until an operator enrolls it explicitly.
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO mcp_oauth_bindings
                    (provider, binding_id, owner_user_id, status,
                     authorization_version, created_at, updated_at)
                VALUES (?, ?, ?, 'active', 1, ?, ?)
                """,
                (provider_for_binding(binding_id), binding_id, owners[0], now, now),
            )
            ids = [int(r["id"]) for r in members]
            for chunk in _chunks(ids):
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"UPDATE mcp_oauth_policies SET binding_id = ? WHERE id IN ({placeholders})",
                    (binding_id, *chunk),
                )
        conn.execute(
            "INSERT OR REPLACE INTO mcp_oauth_meta (key, value) VALUES ('bindings_migrated', '1')"
        )

    def bindings_map(self) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM mcp_oauth_bindings").fetchall()
        return {
            r["binding_id"]: {
                "binding_id": r["binding_id"],
                "provider": r["provider"],
                "owner_user_id": r["owner_user_id"],
                "status": r["status"],
                "authorization_version": int(r["authorization_version"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        }

    def get_binding(self, binding_id: str) -> dict | None:
        return self.bindings_map().get(binding_id)

    def upsert_binding(
        self,
        binding_id: str,
        owner_user_id: str,
        *,
        provider: str = "google",
        status: str = "active",
    ) -> dict:
        """Create the binding, or refuse to silently change its owner."""
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM mcp_oauth_bindings WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()
                if row is not None:
                    if row["owner_user_id"] != owner_user_id:
                        raise ValueError(
                            "binding is enrolled to another account; use transfer-binding"
                        )
                    conn.execute(
                        "UPDATE mcp_oauth_bindings SET status = ?, updated_at = ? "
                        "WHERE binding_id = ?",
                        (status, now, binding_id),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO mcp_oauth_bindings
                            (provider, binding_id, owner_user_id, status,
                             authorization_version, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (provider, binding_id, owner_user_id, status, now, now),
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return self.get_binding(binding_id)

    def attach_policies_to_binding(self, binding_id: str) -> int:
        """Point matching policy rows at their binding record."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE mcp_oauth_policies SET binding_id = ? "
                "WHERE path LIKE ? AND (binding_id IS NULL OR binding_id = '')",
                (binding_id, f"%{binding_id}%"),
            )
            return cur.rowcount or 0

    def binding_impact(self, binding_id: str) -> dict:
        client_ids = self.clients_for_binding(binding_id)
        if not client_ids:
            return {"clients": 0, "active_grants": 0, "pending_requests": 0}
        placeholders = ",".join("?" for _ in client_ids)
        with self._conn() as conn:
            grants = conn.execute(
                f"SELECT COUNT(*) AS n FROM mcp_oauth_grants "
                f"WHERE client_id IN ({placeholders}) AND revoked_at IS NULL",
                tuple(client_ids),
            ).fetchone()["n"]
            pending = conn.execute(
                f"SELECT COUNT(*) AS n FROM mcp_oauth_requests "
                f"WHERE client_id IN ({placeholders}) AND status = ? AND expires_at > ?",
                (*client_ids, STATUS_PENDING, _now()),
            ).fetchone()["n"]
        return {
            "clients": len(client_ids),
            "active_grants": int(grants),
            "pending_requests": int(pending),
        }

    def enroll_binding(
        self,
        binding_id: str,
        owner_user_id: str,
        entries: Iterable[dict],
        *,
        provider: str = "google",
    ) -> list[int]:
        """Create the binding and its policies in one transaction."""
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT owner_user_id FROM mcp_oauth_bindings WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()
                if row is not None:
                    if row["owner_user_id"] != owner_user_id:
                        raise ValueError(
                            "binding is enrolled to another account; use transfer-binding"
                        )
                    conn.execute(
                        "UPDATE mcp_oauth_bindings SET status = 'active', updated_at = ? "
                        "WHERE binding_id = ?",
                        (now, binding_id),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO mcp_oauth_bindings
                            (provider, binding_id, owner_user_id, status,
                             authorization_version, created_at, updated_at)
                        VALUES (?, ?, ?, 'active', 1, ?, ?)
                        """,
                        (provider, binding_id, owner_user_id, now, now),
                    )
                ids = [
                    self._upsert_policy(conn, entry, owner_user_id, binding_id)
                    for entry in entries
                ]
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return ids

    def clients_for_binding(self, binding_id: str) -> set[str]:
        """Clients whose registered callbacks belong to this binding."""
        policies = [
            p for p in self.list_policies() if p.get("binding_id") == binding_id
        ]
        if not policies:
            return set()
        from mcp_oauth_policy import match_policy

        result = set()
        for client in self.dcr_clients():
            if any(
                match_policy(uri, policy)
                for uri in client["redirect_uris"]
                for policy in policies
            ):
                result.add(client["client_id"])
        return result

    @staticmethod
    def _revoke_clients_tx(conn, client_ids: Iterable[str], now: float) -> dict:
        """Revoke a client set inside the caller's transaction.

        Batched: SQLite binds at most ~999 parameters, and one account can own
        many grants.
        """
        ids = list(dict.fromkeys(str(c) for c in client_ids if c))
        if not ids:
            return {"grants_revoked": 0, "requests_discarded": 0}

        grant_ids: list[str] = []
        request_ids: list[str] = []
        for chunk in _chunks(ids):
            ph = ",".join("?" for _ in chunk)
            grant_ids.extend(
                r["id"]
                for r in conn.execute(
                    f"SELECT id FROM mcp_oauth_grants WHERE client_id IN ({ph}) "
                    f"AND revoked_at IS NULL",
                    tuple(chunk),
                ).fetchall()
            )
            request_ids.extend(
                r["id"]
                for r in conn.execute(
                    f"SELECT id FROM mcp_oauth_requests WHERE client_id IN ({ph})",
                    tuple(chunk),
                ).fetchall()
            )

        for chunk in _chunks(grant_ids):
            gph = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE mcp_oauth_grants SET revoked_at = ? WHERE id IN ({gph})",
                (now, *chunk),
            )
            conn.execute(
                f"UPDATE mcp_oauth_tokens SET revoked_at = ? "
                f"WHERE revoked_at IS NULL AND grant_id IN ({gph})",
                (now, *chunk),
            )
            conn.execute(
                f"UPDATE mcp_oauth_families SET revoked_at = ? "
                f"WHERE revoked_at IS NULL AND grant_id IN ({gph})",
                (now, *chunk),
            )
        for chunk in _chunks(request_ids):
            rph = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM mcp_oauth_approvals WHERE request_id IN ({rph})",
                tuple(chunk),
            )
        for chunk in _chunks(ids):
            ph = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM mcp_oauth_codes WHERE client_id IN ({ph})", tuple(chunk)
            )
            conn.execute(
                f"DELETE FROM mcp_oauth_requests WHERE client_id IN ({ph})", tuple(chunk)
            )
        return {"grants_revoked": len(grant_ids), "requests_discarded": len(request_ids)}

    def revoke_client_authorizations(self, client_ids: Iterable[str]) -> dict:
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._revoke_clients_tx(conn, client_ids, now)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return result

    def transfer_binding(
        self,
        binding_id: str,
        *,
        from_owner: str,
        to_owner: str,
        actor: str = "",
        reason: str = "",
    ) -> dict:
        """Move a binding to another account, revoking everything it authorized.

        The previous owner must match, which makes two operators racing the same
        transfer fail instead of overwriting each other.
        """
        now = _now()
        client_ids = self.clients_for_binding(binding_id)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT owner_user_id, status FROM mcp_oauth_bindings WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("binding not found")
                if row["owner_user_id"] != from_owner:
                    raise ValueError("binding owner changed; re-run the preview")
                if row["status"] == "retired":
                    raise ValueError("binding is retired")
                conn.execute(
                    """
                    UPDATE mcp_oauth_bindings
                       SET owner_user_id = ?, status = 'active',
                           authorization_version = authorization_version + 1,
                           updated_at = ?
                     WHERE binding_id = ?
                    """,
                    (to_owner, now, binding_id),
                )
                conn.execute(
                    "UPDATE mcp_oauth_policies SET owner_user_id = ? WHERE binding_id = ?",
                    (to_owner, binding_id),
                )
                # Revocation shares the transaction: a failure here leaves the
                # old owner and the old grants intact, never a half-transfer.
                revoked = self._revoke_clients_tx(conn, client_ids, now)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

        return {
            "binding_id": binding_id,
            "from_owner": from_owner,
            "to_owner": to_owner,
            "version": (self.get_binding(binding_id) or {}).get("authorization_version", 0),
            "clients": len(client_ids),
            **revoked,
        }

    def block_bindings_for_user(self, user_id: str) -> int:
        """Tombstone an account's bindings and revoke what they authorized."""
        now = _now()
        bindings = [
            b["binding_id"]
            for b in self.bindings_map().values()
            if b["owner_user_id"] == user_id and b["status"] == "active"
        ]
        client_ids: set[str] = set()
        for binding_id in bindings:
            client_ids |= self.clients_for_binding(binding_id)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE mcp_oauth_bindings SET status = 'blocked', updated_at = ? "
                    "WHERE owner_user_id = ? AND status = 'active'",
                    (now, user_id),
                )
                count = cur.rowcount or 0
                self._revoke_clients_tx(conn, client_ids, now)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return count

    # -- callback policies -------------------------------------------------

    def list_policies(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_oauth_policies ORDER BY host, path"
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _upsert_policy(conn, entry: dict, owner_user_id: str, binding_id: str = "") -> int:
        scheme = str(entry["scheme"]).lower()
        host = str(entry["host"]).lower()
        port = int(entry["port"])
        path = str(entry["path"])
        match = str(entry.get("match", "exact"))
        allow_query = int(entry.get("allow_query", 0))
        conn.execute(
            """
            INSERT OR IGNORE INTO mcp_oauth_policies
                (scheme, host, port, path, match, allow_query, note,
                 owner_user_id, binding_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scheme,
                host,
                port,
                path,
                match,
                allow_query,
                str(entry.get("note", "")),
                owner_user_id or None,
                binding_id or None,
                _now(),
            ),
        )
        row = conn.execute(
            """
            SELECT id, owner_user_id, binding_id FROM mcp_oauth_policies
             WHERE scheme = ? AND host = ? AND port = ? AND path = ?
               AND match = ? AND allow_query = ?
            """,
            (scheme, host, port, path, match, allow_query),
        ).fetchone()
        if row is None:
            return 0
        # Enrollment may revisit a row that was added before owners existed.
        if owner_user_id and not row["owner_user_id"]:
            conn.execute(
                "UPDATE mcp_oauth_policies SET owner_user_id = ? WHERE id = ?",
                (owner_user_id, row["id"]),
            )
        if binding_id and not row["binding_id"]:
            conn.execute(
                "UPDATE mcp_oauth_policies SET binding_id = ? WHERE id = ?",
                (binding_id, row["id"]),
            )
        return int(row["id"])

    def add_policy(
        self,
        *,
        scheme: str,
        host: str,
        port: int,
        path: str,
        match: str = "exact",
        allow_query: int = 0,
        note: str = "",
        owner_user_id: str = "",
        binding_id: str = "",
    ) -> int:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                policy_id = self._upsert_policy(
                    conn,
                    dict(scheme=scheme, host=host, port=port, path=path,
                         match=match, allow_query=allow_query, note=note),
                    owner_user_id,
                    binding_id,
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return policy_id

    def add_policy_group(
        self,
        entries: Iterable[dict],
        *,
        owner_user_id: str,
        binding_id: str = "",
    ) -> list[int]:
        """Add a bundle of callbacks for one owner in a single transaction."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [
                    self._upsert_policy(conn, entry, owner_user_id, binding_id)
                    for entry in entries
                ]
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return ids

    def set_policy_owner(self, policy_id: int, user_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE mcp_oauth_policies SET owner_user_id = ? WHERE id = ?",
                (user_id or None, int(policy_id)),
            )
            return (cur.rowcount or 0) == 1

    def active_grant_users_for_client(self, client_id: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM mcp_oauth_grants "
                "WHERE client_id = ? AND revoked_at IS NULL",
                (client_id,),
            ).fetchall()
        return {str(r["user_id"]) for r in rows}

    def remove_policy(self, policy_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM mcp_oauth_policies WHERE id = ?", (int(policy_id),)
            )
            return (cur.rowcount or 0) == 1

    def dcr_clients(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM mcp_oauth_grants g
                         WHERE g.client_id = c.client_id AND g.revoked_at IS NULL) AS grants
                  FROM mcp_oauth_clients c
                 WHERE c.source = 'dcr'
                 ORDER BY c.created_at
                """
            ).fetchall()
        return [
            {
                "client_id": r["client_id"],
                "client_name": r["client_name"],
                "redirect_uris": _loads(r["redirect_uris"], []),
                "grants": int(r["grants"] or 0),
            }
            for r in rows
        ]

    def register_dynamic_client(
        self,
        *,
        client_id: str,
        client_name: str,
        redirect_uris: Iterable[str],
        max_per_hour: int = 0,
        max_total: int = 0,
    ) -> None:
        """Admit a DCR client atomically, enforcing the registration budget."""
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if max_per_hour:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM mcp_oauth_clients WHERE created_at >= ?",
                        (now - 3600,),
                    ).fetchone()
                    if int(row["n"]) >= max_per_hour:
                        raise BudgetExceeded("registration rate limit reached, try again later")
                if max_total:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM mcp_oauth_clients"
                    ).fetchone()
                    if int(row["n"]) >= max_total:
                        raise BudgetExceeded("registration capacity reached")
                conn.execute(
                    """
                    INSERT INTO mcp_oauth_clients
                        (client_id, client_name, redirect_uris, grant_types,
                         token_endpoint_auth_method, source, created_at)
                    VALUES (?, ?, ?, ?, 'none', 'dcr', ?)
                    """,
                    (
                        client_id,
                        client_name,
                        _json(list(redirect_uris)),
                        _json(["authorization_code", "refresh_token"]),
                        now,
                    ),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def delete_client_atomic(self, client_id: str) -> dict:
        """Remove a client and everything it could still use, in one transaction.

        Deleting the row alone would leave live grants, tokens, pending requests
        and approval tickets behind, so a policy withdrawal has to cascade.
        """
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                grant_ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM mcp_oauth_grants WHERE client_id = ? AND revoked_at IS NULL",
                        (client_id,),
                    ).fetchall()
                ]
                if grant_ids:
                    placeholders = ",".join("?" for _ in grant_ids)
                    conn.execute(
                        f"UPDATE mcp_oauth_grants SET revoked_at = ? WHERE id IN ({placeholders})",
                        (now, *grant_ids),
                    )
                    conn.execute(
                        f"UPDATE mcp_oauth_tokens SET revoked_at = ? "
                        f"WHERE revoked_at IS NULL AND grant_id IN ({placeholders})",
                        (now, *grant_ids),
                    )
                requests = conn.execute(
                    "SELECT id FROM mcp_oauth_requests WHERE client_id = ?", (client_id,)
                ).fetchall()
                request_ids = [r["id"] for r in requests]
                if request_ids:
                    placeholders = ",".join("?" for _ in request_ids)
                    conn.execute(
                        f"DELETE FROM mcp_oauth_approvals WHERE request_id IN ({placeholders})",
                        tuple(request_ids),
                    )
                conn.execute("DELETE FROM mcp_oauth_codes WHERE client_id = ?", (client_id,))
                conn.execute("DELETE FROM mcp_oauth_requests WHERE client_id = ?", (client_id,))
                cur = conn.execute(
                    "DELETE FROM mcp_oauth_clients WHERE client_id = ?", (client_id,)
                )
                deleted = (cur.rowcount or 0) == 1
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return {
            "client_deleted": deleted,
            "grants_revoked": len(grant_ids),
            "requests_discarded": len(request_ids),
        }

    # -- maintenance -------------------------------------------------------

    @staticmethod
    def _sweep_expired(conn, now: float) -> None:
        """Delete a bounded batch of expired one-shot rows.

        Called from the allocation path, which is the only place an
        unauthenticated caller can create rows. Bounded so a large backlog is
        drained across requests instead of holding the write lock.
        """
        # An approval ticket outlives its request by APPROVAL_TICKET_TTL, so a
        # request must not be swept while a ticket minted from it is still
        # usable; completion needs the request row.
        conn.execute(
            """
            DELETE FROM mcp_oauth_requests WHERE rowid IN (
                SELECT rowid FROM mcp_oauth_requests WHERE expires_at < ? LIMIT 200
            )
            """,
            (now - APPROVAL_TICKET_TTL,),
        )
        conn.execute(
            """
            DELETE FROM mcp_oauth_approvals WHERE rowid IN (
                SELECT rowid FROM mcp_oauth_approvals WHERE expires_at < ? LIMIT 200
            )
            """,
            (now,),
        )
        conn.execute(
            """
            DELETE FROM mcp_oauth_codes WHERE rowid IN (
                SELECT rowid FROM mcp_oauth_codes WHERE expires_at < ? LIMIT 200
            )
            """,
            (now,),
        )

    # -- authorization requests -------------------------------------------

    def create_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str | None,
        scopes: Iterable[str],
        code_challenge: str,
        code_challenge_method: str,
        resource: str | None,
        max_per_client: int = 0,
        max_global: int = 0,
    ) -> str:
        request_id = secrets.token_urlsafe(32)
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # The ceilings below count only unexpired rows, so the sweep is
                # what bounds total churn: without it an unauthenticated caller
                # could keep expiring rows forever and never hit a cap.
                self._sweep_expired(conn, now)
                # Count only outstanding, unexpired work: completing or denying
                # a request frees its slot, so a pending-count bound cannot be
                # bypassed by allocating faster than it can be drained.
                if max_global:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM mcp_oauth_requests
                         WHERE status = ? AND expires_at > ?
                        """,
                        (STATUS_PENDING, now),
                    ).fetchone()
                    if int(row["n"]) >= max_global:
                        raise BudgetExceeded("too many authorization requests are pending")
                if max_per_client:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM mcp_oauth_requests
                         WHERE client_id = ? AND status = ? AND expires_at > ?
                        """,
                        (client_id, STATUS_PENDING, now),
                    ).fetchone()
                    if int(row["n"]) >= max_per_client:
                        raise BudgetExceeded("too many authorization requests are pending")
                conn.execute(
                    """
                    INSERT INTO mcp_oauth_requests
                        (id, client_id, redirect_uri, state, scopes, code_challenge,
                         code_challenge_method, resource, status, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        client_id,
                        redirect_uri,
                        state,
                        _json(list(scopes)),
                        code_challenge,
                        code_challenge_method,
                        resource,
                        STATUS_PENDING,
                        now,
                        now + AUTHORIZATION_REQUEST_TTL,
                    ),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return request_id

    def get_request(self, request_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_oauth_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "redirect_uri": row["redirect_uri"],
            "state": row["state"],
            "scopes": _loads(row["scopes"], []),
            "code_challenge": row["code_challenge"],
            "code_challenge_method": row["code_challenge_method"],
            "resource": row["resource"],
            "status": row["status"],
            "grant_id": row["grant_id"],
            "user_id": row["user_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def pending_request(self, request_id: str) -> dict | None:
        """Return a request only while it can still be decided."""
        request = self.get_request(request_id)
        if request is None:
            return None
        if request["status"] != STATUS_PENDING:
            return None
        if request["expires_at"] < _now():
            return None
        return request

    def approve_request(
        self,
        *,
        request_id: str,
        user_id: str,
        agent_id: str,
        key_hash: str,
        key_fingerprint: str,
        scopes: Iterable[str],
        resource: str | None,
    ) -> tuple[str, str]:
        """Create the grant and a single-use completion ticket.

        Returns ``(grant_id, ticket)``.  The ticket is handed to the browser
        and back to the MCP server; only its hash is stored.
        """
        grant_id = secrets.token_urlsafe(18)
        ticket = secrets.token_urlsafe(32)
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                registered = conn.execute(
                    """
                    SELECT 1 FROM mcp_oauth_clients
                     WHERE client_id = (SELECT client_id FROM mcp_oauth_requests WHERE id = ?)
                    """,
                    (request_id,),
                ).fetchone()
                if registered is None:
                    raise ValueError("client is no longer registered")
                cur = conn.execute(
                    """
                    UPDATE mcp_oauth_requests
                       SET status = ?, grant_id = ?, user_id = ?, scopes = ?
                     WHERE id = ? AND status = ? AND expires_at > ?
                    """,
                    (
                        STATUS_APPROVED,
                        grant_id,
                        user_id,
                        _json(list(scopes)),
                        request_id,
                        STATUS_PENDING,
                        now,
                    ),
                )
                if (cur.rowcount or 0) != 1:
                    raise ValueError("request is no longer pending")
                conn.execute(
                    """
                    INSERT INTO mcp_oauth_grants
                        (id, user_id, client_id, agent_id, key_hash,
                         key_fingerprint, scopes, resource, created_at)
                    SELECT ?, ?, client_id, ?, ?, ?, ?, ?, ?
                      FROM mcp_oauth_requests WHERE id = ?
                    """,
                    (
                        grant_id,
                        user_id,
                        agent_id,
                        key_hash,
                        key_fingerprint,
                        _json(list(scopes)),
                        resource,
                        now,
                        request_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO mcp_oauth_approvals
                        (ticket_hash, request_id, grant_id, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (_hash(ticket), request_id, grant_id, now + APPROVAL_TICKET_TTL),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return grant_id, ticket

    def deny_request(self, request_id: str, user_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE mcp_oauth_requests
                   SET status = ?, user_id = ?
                 WHERE id = ? AND status = ? AND expires_at > ?
                """,
                (STATUS_DENIED, user_id, request_id, STATUS_PENDING, _now()),
            )
            return (cur.rowcount or 0) == 1

    # -- code issuance -----------------------------------------------------

    def complete_request(self, ticket: str) -> dict:
        """Consume an approval ticket and mint an authorization code.

        Returns the redirect payload the MCP server forwards to the client:
        ``redirect_uri``, ``code``, ``state`` and ``request_id``.

        Raises ``ValueError`` for an unknown, expired or already-used ticket.
        """
        code = _new_token(AUTHORIZATION_CODE_PREFIX)
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT a.request_id, a.grant_id
                      FROM mcp_oauth_approvals a
                     WHERE a.ticket_hash = ? AND a.consumed_at IS NULL
                       AND a.expires_at > ?
                    """,
                    (_hash(ticket), now),
                ).fetchone()
                if row is None:
                    raise ValueError("approval ticket is invalid or expired")

                cur = conn.execute(
                    """
                    UPDATE mcp_oauth_approvals SET consumed_at = ?
                     WHERE ticket_hash = ? AND consumed_at IS NULL
                    """,
                    (now, _hash(ticket)),
                )
                if (cur.rowcount or 0) != 1:
                    raise ValueError("approval ticket already used")

                request_row = conn.execute(
                    "SELECT * FROM mcp_oauth_requests WHERE id = ?",
                    (row["request_id"],),
                ).fetchone()
                if request_row is None:
                    raise ValueError("authorization request is missing")
                if request_row["status"] != STATUS_APPROVED:
                    raise ValueError("authorization request was not approved")
                registered = conn.execute(
                    "SELECT 1 FROM mcp_oauth_clients WHERE client_id = ?",
                    (request_row["client_id"],),
                ).fetchone()
                if registered is None:
                    raise ValueError("client is no longer registered")

                conn.execute(
                    "UPDATE mcp_oauth_requests SET status = ? WHERE id = ?",
                    (STATUS_CONSUMED, row["request_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO mcp_oauth_codes
                        (code_hash, grant_id, client_id, redirect_uri,
                         code_challenge, scopes, resource, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _hash(code),
                        row["grant_id"],
                        request_row["client_id"],
                        request_row["redirect_uri"],
                        request_row["code_challenge"],
                        request_row["scopes"],
                        request_row["resource"],
                        now + AUTHORIZATION_CODE_TTL,
                    ),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return {
            "request_id": row["request_id"],
            "grant_id": row["grant_id"],
            "redirect_uri": request_row["redirect_uri"],
            "state": request_row["state"],
            "code": code,
        }

    def peek_approval(self, ticket: str) -> dict | None:
        """Read an approval before consuming it (used to reject bad tickets)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT request_id, grant_id, expires_at, consumed_at
                  FROM mcp_oauth_approvals WHERE ticket_hash = ?
                """,
                (_hash(ticket),),
            ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return None
        if row["expires_at"] < _now():
            return None
        return {"request_id": row["request_id"], "grant_id": row["grant_id"]}

    def get_authorization_code(self, code: str) -> dict | None:
        """Read an authorization code without consuming it.

        The SDK calls this first (to validate client, redirect and PKCE) and
        only then calls ``exchange_authorization_code``, so the actual
        consumption happens in :meth:`consume_authorization_code`.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_oauth_codes WHERE code_hash = ?",
                (_hash(code),),
            ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return None
        return {
            "grant_id": row["grant_id"],
            "client_id": row["client_id"],
            "redirect_uri": row["redirect_uri"],
            "code_challenge": row["code_challenge"],
            "scopes": _loads(row["scopes"], []),
            "resource": row["resource"],
            "expires_at": row["expires_at"],
        }

    def consume_authorization_code(
        self, code: str, *, client_id: str
    ) -> dict | None:
        """Atomically consume a code.  Returns the code row on success."""
        now = _now()
        code_hash = _hash(code)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM mcp_oauth_codes
                     WHERE code_hash = ? AND consumed_at IS NULL AND expires_at > ?
                    """,
                    (code_hash, now),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return None
                if row["client_id"] != client_id:
                    conn.execute("ROLLBACK")
                    return None
                cur = conn.execute(
                    """
                    UPDATE mcp_oauth_codes SET consumed_at = ?
                     WHERE code_hash = ? AND consumed_at IS NULL
                    """,
                    (now, code_hash),
                )
                if (cur.rowcount or 0) != 1:
                    conn.execute("ROLLBACK")
                    return None
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return {
            "grant_id": row["grant_id"],
            "client_id": row["client_id"],
            "redirect_uri": row["redirect_uri"],
            "code_challenge": row["code_challenge"],
            "scopes": _loads(row["scopes"], []),
            "resource": row["resource"],
        }

    # -- grants ------------------------------------------------------------

    def get_grant(self, grant_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_oauth_grants WHERE id = ?",
                (grant_id,),
            ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "client_id": row["client_id"],
            "agent_id": row["agent_id"],
            "key_hash": row["key_hash"],
            "key_fingerprint": row["key_fingerprint"],
            "scopes": _loads(row["scopes"], []),
            "resource": row["resource"],
            "created_at": row["created_at"],
        }

    def list_grants_for_user(self, user_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT g.*, c.client_name
                  FROM mcp_oauth_grants g
                  LEFT JOIN mcp_oauth_clients c ON c.client_id = g.client_id
                 WHERE g.user_id = ? AND g.revoked_at IS NULL
                 ORDER BY g.created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "client_id": r["client_id"],
                "client_name": r["client_name"] or r["client_id"],
                "agent_id": r["agent_id"],
                "scopes": _loads(r["scopes"], []),
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]

    def revoke_grant(self, grant_id: str, *, user_id: str | None = None) -> bool:
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if user_id is None:
                    cur = conn.execute(
                        "UPDATE mcp_oauth_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (now, grant_id),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE mcp_oauth_grants SET revoked_at = ?
                         WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                        """,
                        (now, grant_id, user_id),
                    )
                if (cur.rowcount or 0) != 1:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "UPDATE mcp_oauth_tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
                    (now, grant_id),
                )
                conn.execute(
                    "UPDATE mcp_oauth_families SET revoked_at = ? "
                    "WHERE grant_id = ? AND revoked_at IS NULL",
                    (now, grant_id),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return True

    def revoke_grants_for_user(self, user_id: str) -> int:
        """Revoke every connector an account delegated."""
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                grant_ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM mcp_oauth_grants WHERE user_id = ? AND revoked_at IS NULL",
                        (user_id,),
                    ).fetchall()
                ]
                if not grant_ids:
                    conn.execute("ROLLBACK")
                    return 0
                placeholders = ",".join("?" for _ in grant_ids)
                conn.execute(
                    f"UPDATE mcp_oauth_grants SET revoked_at = ? WHERE id IN ({placeholders})",
                    (now, *grant_ids),
                )
                conn.execute(
                    f"UPDATE mcp_oauth_tokens SET revoked_at = ? "
                    f"WHERE revoked_at IS NULL AND grant_id IN ({placeholders})",
                    (now, *grant_ids),
                )
                conn.execute(
                    f"UPDATE mcp_oauth_families SET revoked_at = ? "
                    f"WHERE revoked_at IS NULL AND grant_id IN ({placeholders})",
                    (now, *grant_ids),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return len(grant_ids)

    def revoke_grants_for_key(self, key_fingerprint: str) -> int:
        """Key rotation/revocation invalidates the connectors bound to it."""
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                grant_ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM mcp_oauth_grants WHERE key_fingerprint = ? AND revoked_at IS NULL",
                        (key_fingerprint,),
                    ).fetchall()
                ]
                if not grant_ids:
                    conn.execute("ROLLBACK")
                    return 0
                placeholders = ",".join("?" for _ in grant_ids)
                conn.execute(
                    f"UPDATE mcp_oauth_grants SET revoked_at = ? WHERE id IN ({placeholders})",
                    (now, *grant_ids),
                )
                conn.execute(
                    f"""
                    UPDATE mcp_oauth_tokens SET revoked_at = ?
                     WHERE revoked_at IS NULL AND grant_id IN ({placeholders})
                    """,
                    (now, *grant_ids),
                )
                conn.execute(
                    f"""
                    UPDATE mcp_oauth_families SET revoked_at = ?
                     WHERE revoked_at IS NULL AND grant_id IN ({placeholders})
                    """,
                    (now, *grant_ids),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return len(grant_ids)

    # -- tokens ------------------------------------------------------------

    def _insert_tokens(
        self,
        conn,
        *,
        grant_id: str,
        client_id: str,
        family_id: str,
        family_expires_at: float,
        scopes: Iterable[str],
        resource: str | None,
    ) -> dict:
        """Insert an access/refresh pair inside an existing transaction."""
        access = _new_token(ACCESS_TOKEN_PREFIX)
        refresh = _new_token(REFRESH_TOKEN_PREFIX)
        now = _now()
        scope_json = _json(list(scopes))
        refresh_expiry = min(now + REFRESH_TOKEN_TTL, family_expires_at)
        conn.execute(
            """
            INSERT INTO mcp_oauth_tokens
                (token_hash, kind, family_id, grant_id, client_id,
                 scopes, resource, created_at, expires_at)
            VALUES (?, 'access', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(access),
                family_id,
                grant_id,
                client_id,
                scope_json,
                resource,
                now,
                now + ACCESS_TOKEN_TTL,
            ),
        )
        conn.execute(
            """
            INSERT INTO mcp_oauth_tokens
                (token_hash, kind, family_id, grant_id, client_id,
                 scopes, resource, created_at, expires_at)
            VALUES (?, 'refresh', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(refresh),
                family_id,
                grant_id,
                client_id,
                scope_json,
                resource,
                now,
                refresh_expiry,
            ),
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": ACCESS_TOKEN_TTL,
            "family_id": family_id,
        }

    def issue_tokens(
        self,
        *,
        grant_id: str,
        client_id: str,
        scopes: Iterable[str],
        resource: str | None,
        family_id: str | None = None,
    ) -> dict:
        """Issue a token pair against a live grant.

        Rejects a revoked or missing grant even when starting a new family, so
        an authorization-code exchange cannot race grant revocation.
        """
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                grant = conn.execute(
                    "SELECT id, client_id, revoked_at FROM mcp_oauth_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
                if grant is None or grant["revoked_at"] is not None:
                    raise ValueError("grant is not active")
                if grant["client_id"] != client_id:
                    raise ValueError("grant belongs to another client")

                if family_id is None:
                    family_id = secrets.token_urlsafe(18)
                    family_expires = now + FAMILY_TTL
                    conn.execute(
                        """
                        INSERT INTO mcp_oauth_families
                            (family_id, grant_id, client_id, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (family_id, grant_id, client_id, now, family_expires),
                    )
                else:
                    family = conn.execute(
                        "SELECT * FROM mcp_oauth_families WHERE family_id = ?",
                        (family_id,),
                    ).fetchone()
                    if family is None:
                        raise ValueError("token family is unknown")
                    if family["revoked_at"] is not None:
                        raise ValueError("token family is revoked")
                    if family["expires_at"] < now:
                        raise ValueError("token family has expired")
                    if family["grant_id"] != grant_id:
                        raise ValueError("token family belongs to another grant")
                    family_expires = family["expires_at"]

                issued = self._insert_tokens(
                    conn,
                    grant_id=grant_id,
                    client_id=client_id,
                    family_id=family_id,
                    family_expires_at=family_expires,
                    scopes=scopes,
                    resource=resource,
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return issued

    def load_access_token(self, token: str) -> dict | None:
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT t.*, g.agent_id, g.user_id, g.key_hash,
                       g.key_fingerprint, g.revoked_at AS grant_revoked_at,
                       f.revoked_at AS family_revoked_at,
                       f.expires_at AS family_expires_at
                  FROM mcp_oauth_tokens t
                  JOIN mcp_oauth_grants g ON g.id = t.grant_id
                  LEFT JOIN mcp_oauth_families f ON f.family_id = t.family_id
                 WHERE t.token_hash = ? AND t.kind = 'access'
                """,
                (_hash(token),),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None or row["grant_revoked_at"] is not None:
                return None
            if row["family_revoked_at"] is not None:
                return None
            if row["family_expires_at"] is not None and row["family_expires_at"] < now:
                return None
            if row["consumed_at"] is not None or row["expires_at"] < now:
                return None
            # Best-effort activity stamp; never fail the request for it.
            try:
                conn.execute(
                    "UPDATE mcp_oauth_grants SET last_used_at = ? WHERE id = ?",
                    (now, row["grant_id"]),
                )
            except sqlite3.Error:
                pass
        return {
            "grant_id": row["grant_id"],
            "client_id": row["client_id"],
            "user_id": row["user_id"],
            "agent_id": row["agent_id"],
            "key_hash": row["key_hash"],
            "key_fingerprint": row["key_fingerprint"],
            "scopes": _loads(row["scopes"], []),
            "resource": row["resource"],
            "expires_at": row["expires_at"],
        }

    def load_refresh_token(self, token: str, *, client_id: str | None = None) -> dict | None:
        """Read a refresh token without mutating anything.

        Replay handling lives in :meth:`rotate_refresh_token`, so that a token
        presented by the wrong client never causes a revocation.  A consumed
        token is still returned (``consumed=True``) because the SDK asks here
        first and would otherwise answer invalid_grant before the exchange that
        performs the revocation.
        """
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT t.*, g.revoked_at AS grant_revoked_at,
                       f.revoked_at AS family_revoked_at,
                       f.expires_at AS family_expires_at
                  FROM mcp_oauth_tokens t
                  JOIN mcp_oauth_grants g ON g.id = t.grant_id
                  LEFT JOIN mcp_oauth_families f ON f.family_id = t.family_id
                 WHERE t.token_hash = ? AND t.kind = 'refresh'
                """,
                (_hash(token),),
            ).fetchone()
        if row is None:
            return None
        if client_id is not None and row["client_id"] != client_id:
            return None
        if row["revoked_at"] is not None or row["grant_revoked_at"] is not None:
            return None
        if row["family_revoked_at"] is not None:
            return None
        if row["family_expires_at"] is not None and row["family_expires_at"] < now:
            return None
        if row["expires_at"] < now:
            return None
        return {
            "grant_id": row["grant_id"],
            "client_id": row["client_id"],
            "scopes": _loads(row["scopes"], []),
            "resource": row["resource"],
            "family_id": row["family_id"],
            "expires_at": row["expires_at"],
            "consumed": row["consumed_at"] is not None,
        }

    def rotate_refresh_token(
        self,
        token: str,
        *,
        client_id: str,
        scopes: Iterable[str],
    ) -> dict | None:
        """Consume a refresh token and issue its successor in one transaction.

        Returns ``{"replay": True}`` when an already-consumed token is presented
        by its own client; the family is revoked in that same transaction.  Any
        other refusal returns ``None`` without mutating state.
        """
        now = _now()
        token_hash = _hash(token)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT t.*, g.revoked_at AS grant_revoked_at,
                           f.revoked_at AS family_revoked_at,
                           f.expires_at AS family_expires_at
                      FROM mcp_oauth_tokens t
                      JOIN mcp_oauth_grants g ON g.id = t.grant_id
                      LEFT JOIN mcp_oauth_families f ON f.family_id = t.family_id
                     WHERE t.token_hash = ? AND t.kind = 'refresh'
                    """,
                    (token_hash,),
                ).fetchone()
                if row is None or row["client_id"] != client_id:
                    conn.execute("ROLLBACK")
                    return None

                if row["consumed_at"] is not None:
                    self._revoke_family_tx(conn, row["family_id"], now)
                    conn.execute("COMMIT")
                    return {"replay": True}

                if row["revoked_at"] is not None or row["grant_revoked_at"] is not None:
                    conn.execute("ROLLBACK")
                    return None
                if row["family_revoked_at"] is not None:
                    conn.execute("ROLLBACK")
                    return None
                if row["family_expires_at"] is not None and row["family_expires_at"] < now:
                    conn.execute("ROLLBACK")
                    return None
                if row["expires_at"] < now:
                    conn.execute("ROLLBACK")
                    return None

                # RFC 6749: a refresh may narrow scopes, never widen them.
                # Without this an old grant could add mcp:provision without
                # the updated consent screen ever being shown.
                granted_scopes = _loads(row["scopes"], [])
                if not granted_scopes:
                    # Tokens written before scopes were persisted behaved as
                    # the base browser scope; never emit an empty scope set.
                    granted_scopes = ["mcp:browser"]
                requested = list(scopes or [])
                if requested:
                    final_scopes = [s for s in requested if s in granted_scopes]
                    if not final_scopes:
                        # Asking for only ungranted scopes is a client error,
                        # not a reason to silently restore the full set.
                        conn.execute("ROLLBACK")
                        return None
                else:
                    final_scopes = granted_scopes

                cur = conn.execute(
                    "UPDATE mcp_oauth_tokens SET consumed_at = ? "
                    "WHERE token_hash = ? AND consumed_at IS NULL",
                    (now, token_hash),
                )
                if (cur.rowcount or 0) != 1:
                    conn.execute("ROLLBACK")
                    return None

                issued = self._insert_tokens(
                    conn,
                    grant_id=row["grant_id"],
                    client_id=client_id,
                    family_id=row["family_id"],
                    family_expires_at=row["family_expires_at"] or (now + FAMILY_TTL),
                    scopes=final_scopes,
                    resource=row["resource"],
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return {
            "access_token": issued["access_token"],
            "refresh_token": issued["refresh_token"],
            "expires_in": issued["expires_in"],
            "family_id": issued["family_id"],
            "grant_id": row["grant_id"],
            "scopes": final_scopes,
        }

    def _revoke_family_tx(self, conn, family_id: str, now: float) -> None:
        conn.execute(
            "UPDATE mcp_oauth_families SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
            (now, family_id),
        )
        conn.execute(
            "UPDATE mcp_oauth_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
            (now, family_id),
        )

    def revoke_family(self, family_id: str) -> bool:
        now = _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._revoke_family_tx(conn, family_id, now)
                cur = conn.execute(
                    "SELECT 1 FROM mcp_oauth_tokens WHERE family_id = ? LIMIT 1",
                    (family_id,),
                ).fetchone()
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return cur is not None

    def revoke_token(self, token: str) -> bool:
        """RFC 7009 revocation: a refresh token kills its family; an access
        token is revoked on its own and does not stop future refreshes."""
        now = _now()
        token_hash = _hash(token)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT family_id, kind FROM mcp_oauth_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("BEGIN IMMEDIATE")
            try:
                if row["kind"] == "refresh":
                    self._revoke_family_tx(conn, row["family_id"], now)
                else:
                    conn.execute(
                        "UPDATE mcp_oauth_tokens SET revoked_at = ? "
                        "WHERE token_hash = ? AND revoked_at IS NULL",
                        (now, token_hash),
                    )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return True

    # -- maintenance -------------------------------------------------------

    def prune(self) -> dict:
        """Delete expired state with family-aware retention.

        Consumed ancestors stay while their family is alive, because that is
        what makes refresh replay detectable.  Revoked or expired families are
        retained for ``REVOKED_RETENTION`` and then removed with their tokens.
        """
        now = _now()
        with self._conn() as conn:
            requests = conn.execute(
                "DELETE FROM mcp_oauth_requests WHERE expires_at < ?",
                (now - APPROVAL_TICKET_TTL,),
            ).rowcount or 0
            approvals = conn.execute(
                "DELETE FROM mcp_oauth_approvals WHERE expires_at < ?",
                (now,),
            ).rowcount or 0
            codes = conn.execute(
                "DELETE FROM mcp_oauth_codes WHERE expires_at < ?",
                (now,),
            ).rowcount or 0
            tokens = conn.execute(
                "DELETE FROM mcp_oauth_tokens WHERE kind = 'access' AND expires_at < ?",
                (now,),
            ).rowcount or 0
            families = conn.execute(
                """
                DELETE FROM mcp_oauth_families
                 WHERE expires_at < ?
                    OR (revoked_at IS NOT NULL AND revoked_at < ?)
                """,
                (now - REVOKED_RETENTION, now - REVOKED_RETENTION),
            ).rowcount or 0
            orphans = conn.execute(
                """
                DELETE FROM mcp_oauth_tokens
                 WHERE family_id NOT IN (SELECT family_id FROM mcp_oauth_families)
                """
            ).rowcount or 0
        return {
            "requests": requests,
            "approvals": approvals,
            "codes": codes,
            "access_tokens": tokens,
            "families": families,
            "orphan_tokens": orphans,
        }


def fingerprint_key(api_key: str) -> str:
    """Full fingerprint of an API key, for grant binding."""
    return _hash(api_key)


def agent_id_for_key(api_key: str, profile: str = "") -> str:
    """Mirror relay.py/mcp_server.py agent-id derivation exactly."""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:8]
    if api_key.startswith("uc_headless_"):
        return f"headless-{key_hash}"
    if profile and profile != "default":
        return f"claude-{key_hash}-{profile}"
    return f"claude-{key_hash}"
