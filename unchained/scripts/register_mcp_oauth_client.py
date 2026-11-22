#!/usr/bin/env python3
"""Register an OAuth client for a remote MCP connector.

Dynamic registration is enough for Claude, ChatGPT and the Gemini consumer app:
they register themselves at ``/register`` and the user only pastes the MCP URL.
Consoles that ask for a client id and secret up front (Gemini Enterprise, or the
Gemini app's "Advanced features" path when DCR is unavailable) need a client
that already exists, which is what this script creates.

Usage:
    uv run scripts/register_mcp_oauth_client.py \\
        --client-id gemini-enterprise \\
        --name "Gemini Enterprise" \\
        --redirect-uri https://vertexaisearch.cloud.google.com/oauth-redirect

    uv run scripts/register_mcp_oauth_client.py --list
    uv run scripts/register_mcp_oauth_client.py --revoke-client gemini-enterprise

The client is public (``token_endpoint_auth_method=none``) and PKCE is
mandatory, so the console's "client secret" field can hold any placeholder; it
is never checked.  Values are written to the same SQLite database the MCP
service reads (``UNCHAINED_DB_PATH``, or ``--db``).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_oauth_policy import RedirectURIError, validate_redirect_uri  # noqa: E402
from mcp_oauth_store import OAuthStore  # noqa: E402


def _store(args) -> OAuthStore:
    return OAuthStore(args.db or os.environ.get("UNCHAINED_DB_PATH"))


def _list(store: OAuthStore, db_path: str) -> int:
    with store._conn() as conn:
        rows = conn.execute(
            """
            SELECT c.client_id, c.client_name, c.source, c.redirect_uris,
                   c.created_at,
                   (SELECT COUNT(*) FROM mcp_oauth_grants g
                     WHERE g.client_id = c.client_id AND g.revoked_at IS NULL) AS grants
              FROM mcp_oauth_clients c ORDER BY c.created_at DESC
            """
        ).fetchall()
    if not rows:
        print(f"No OAuth clients in {db_path}.")
        return 0
    print(f"{len(rows)} OAuth client(s) in {db_path}:\n")
    for row in rows:
        print(f"  {row['client_id']}")
        print(f"    name:     {row['client_name']}")
        print(f"    source:   {row['source']}")
        print(f"    grants:   {row['grants']}")
        print(f"    redirect: {row['redirect_uris']}")
    return 0


def _revoke_client(store: OAuthStore, client_id: str) -> int:
    result = store.delete_client_atomic(client_id)
    print(
        f"Revoked {result['grants_revoked']} grant(s); "
        f"discarded {result['requests_discarded']} pending request(s); "
        f"removed {1 if result['client_deleted'] else 0} client row(s)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite path (default: UNCHAINED_DB_PATH)")
    parser.add_argument("--client-id")
    parser.add_argument("--name")
    parser.add_argument(
        "--redirect-uri",
        action="append",
        default=[],
        help="Repeatable. Must be https, or http on loopback for native clients.",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--revoke-client", metavar="CLIENT_ID")
    args = parser.parse_args()

    store = _store(args)
    if args.list:
        return _list(store, store.db_path)
    if args.revoke_client:
        return _revoke_client(store, args.revoke_client)

    if not (args.client_id and args.name and args.redirect_uri):
        parser.error("--client-id, --name and at least one --redirect-uri are required")

    issuer = os.environ.get("MCP_OAUTH_ISSUER", "https://unchainedsky.com").strip().rstrip("/")
    for uri in args.redirect_uri:
        try:
            validate_redirect_uri(uri, issuer=issuer, allow_loopback=True)
        except RedirectURIError as exc:
            print(f"Refusing redirect URI {uri}: {exc}")
            return 2

    store.save_client(
        client_id=args.client_id,
        client_name=args.name,
        redirect_uris=args.redirect_uri,
        source="manual",
    )
    print(f"Registered {args.client_id} in {store.db_path}\n")
    print("Paste these into the connector's advanced settings:")
    print(f"  Client ID:     {args.client_id}")
    print("  Client Secret: leave empty if the field is optional; otherwise any")
    print("                 placeholder works because the client is public and")
    print("                 the exchange is proven by PKCE, not by a secret.")
    for uri in args.redirect_uri:
        print(f"  Redirect URI:  {uri}")
    print("\nAuthorization and token URLs come from discovery; no need to type them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
