#!/usr/bin/env python3
"""Manage the callback addresses that automatic (DCR) registration may use.

Automatic registration is an unauthenticated endpoint, so the destination a
connector may send an authorization code to is an operator decision.  Each
policy names an exact scheme, host, port and path (optionally a path prefix)
and whether a query string is allowed.

Usage:
    uv run scripts/manage_mcp_oauth_policy.py list
    uv run scripts/manage_mcp_oauth_policy.py add \\
        --host chatgpt.com --path /connector_platform_oauth_redirect
    uv run scripts/manage_mcp_oauth_policy.py remove --id 3
    uv run scripts/manage_mcp_oauth_policy.py revalidate --apply

``revalidate`` finds automatic clients whose stored callbacks no longer match a
policy, revokes their grants and deletes them.  It prints the impact and does
nothing unless ``--apply`` is given; on apply it recomputes the impact from the
current database, so a stale preview cannot authorise a different destructive
operation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_oauth_policy import (  # noqa: E402
    RedirectURIError,
    client_callback_status,
    describe_policy,
    validate_redirect_uri,
)
from mcp_oauth_store import OAuthStore  # noqa: E402


def _issuer() -> str:
    return os.environ.get("MCP_OAUTH_ISSUER", "https://unchainedsky.com").strip().rstrip("/")


def _store(args) -> OAuthStore:
    return OAuthStore(args.db or os.environ.get("UNCHAINED_DB_PATH"))


def cmd_list(store: OAuthStore) -> int:
    policies = store.list_policies()
    if not policies:
        print("No callback policies. Automatic registration will accept nothing.")
        return 0
    print(f"{len(policies)} callback polic{'y' if len(policies) == 1 else 'ies'}:\n")
    for policy in policies:
        print(f"  [{policy['id']}] {describe_policy(policy)}")
        if policy.get("note"):
            print(f"       {policy['note']}")
    return 0


def cmd_add(store: OAuthStore, args) -> int:
    try:
        validate_redirect_uri(
            f"{args.scheme}://{args.host}{args.path}",
            issuer=_issuer(),
            allow_loopback=True,
            allow_query=bool(args.allow_query),
        )
    except RedirectURIError as exc:
        print(f"Refusing to add policy: {exc}")
        return 2
    policy_id = store.add_policy(
        scheme=args.scheme,
        host=args.host,
        port=args.port,
        path=args.path,
        match=args.match,
        allow_query=1 if args.allow_query else 0,
        note=args.note or "",
    )
    print(f"Policy [{policy_id}] configured for {args.scheme}://{args.host}:{args.port}{args.path}")
    print("Run `revalidate` to check existing automatic clients against the new set.")
    return 0


def cmd_remove(store: OAuthStore, args) -> int:
    if not store.remove_policy(args.id):
        print(f"No policy with id {args.id}.")
        return 1
    print(f"Policy {args.id} removed. Run `revalidate --apply` to withdraw clients that relied on it.")
    return 0


def _impact(store: OAuthStore) -> list[dict]:
    policies = store.list_policies()
    affected = []
    for client in store.dcr_clients():
        if client_callback_status(client, policies) == "blocked":
            affected.append(client)
    return affected


def cmd_revalidate(store: OAuthStore, args) -> int:
    affected = _impact(store)
    if not affected:
        print("Every automatic client still matches a policy. Nothing to do.")
        return 0

    print(f"{len(affected)} automatic client(s) no longer match any policy:\n")
    for client in affected:
        print(
            f"  {client['client_id']}  {client['client_name']}  "
            f"({client.get('grants', 0)} live grant(s))"
        )
        for uri in client["redirect_uris"]:
            print(f"       {uri}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to revoke these clients and their grants.")
        return 0

    # Recompute against current state so the applied set is exactly what is
    # reported now, not what a previous run printed.
    affected = _impact(store)
    for client in affected:
        result = store.delete_client_atomic(client["client_id"])
        print(
            f"Removed {client['client_id']}: "
            f"{result['grants_revoked']} grant(s) revoked, "
            f"{result['requests_discarded']} pending request(s) discarded"
        )
    return 0


def cmd_export(store: OAuthStore, args) -> int:
    fields = ("scheme", "host", "port", "path", "match", "allow_query", "note")
    data = [{f: p[f] for f in fields} for p in store.list_policies()]
    text = json.dumps(data, indent=2) + "\n"
    if args.file:
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {len(data)} policies to {args.file}")
    else:
        print(text, end="")
    return 0


def cmd_import(store: OAuthStore, args) -> int:
    try:
        with open(args.file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {args.file}: {exc}")
        return 2
    if not isinstance(data, list):
        print("Expected a JSON array of policies.")
        return 2

    # Normalise both sides: the store lowercases scheme and host on insert, but
    # a row written by hand may not be canonical, and dedup must not depend on
    # that.
    existing = {
        (
            str(p["scheme"]).lower(),
            str(p["host"]).lower(),
            int(p["port"]),
            str(p["path"]),
            str(p["match"]),
            int(p["allow_query"]),
        )
        for p in store.list_policies()
    }
    to_add = []
    for entry in data:
        try:
            scheme = str(entry["scheme"]).lower()
            host = str(entry["host"]).lower()
            port = int(entry["port"])
            path = str(entry["path"])
            match = str(entry.get("match", "exact"))
            allow_query = int(entry.get("allow_query", 0))
        except (KeyError, TypeError, ValueError):
            print(f"Skipping malformed entry: {entry!r}")
            continue
        if match not in ("exact", "prefix"):
            print(f"Skipping entry with invalid match mode: {entry!r}")
            continue
        uri = f"{scheme}://{host}" + ("" if port in (443, 80) else f":{port}") + path
        try:
            validate_redirect_uri(uri, issuer=_issuer(), allow_loopback=True)
        except RedirectURIError as exc:
            print(f"Skipping {uri}: {exc}")
            continue
        key = (scheme, host, port, path, match, allow_query)
        if key not in existing:
            to_add.append(dict(scheme=scheme, host=host, port=port, path=path,
                               match=match, allow_query=allow_query,
                               note=str(entry.get("note", ""))))

    print(f"{len(data)} entries in file; {len(to_add)} not present in this database.")
    for entry in to_add:
        print("  +", describe_policy(entry))
    if not args.apply:
        print("Dry run. Re-run with --apply to add them.")
        return 0
    for entry in to_add:
        store.add_policy(**entry)
    print(f"Added {len(to_add)} policies.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite path (default: UNCHAINED_DB_PATH)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show configured callback policies")

    add = sub.add_parser("add", help="add a callback policy")
    add.add_argument("--scheme", default="https")
    add.add_argument("--host", required=True)
    add.add_argument("--port", type=int, default=443)
    add.add_argument("--path", required=True)
    add.add_argument("--match", choices=["exact", "prefix"], default="exact")
    add.add_argument("--allow-query", action="store_true")
    add.add_argument("--note", default="")

    remove = sub.add_parser("remove", help="remove a callback policy by id")
    remove.add_argument("--id", type=int, required=True)

    revalidate = sub.add_parser(
        "revalidate", help="withdraw automatic clients that no longer match a policy"
    )
    revalidate.add_argument("--apply", action="store_true")

    export = sub.add_parser("export", help="dump policies as JSON (backup or review)")
    export.add_argument("--file", help="write to this file instead of stdout")

    imp = sub.add_parser("import", help="add policies from a JSON export")
    imp.add_argument("--file", required=True)
    imp.add_argument("--apply", action="store_true")

    args = parser.parse_args()
    store = _store(args)
    if args.command == "list":
        return cmd_list(store)
    if args.command == "add":
        return cmd_add(store, args)
    if args.command == "remove":
        return cmd_remove(store, args)
    if args.command == "export":
        return cmd_export(store, args)
    if args.command == "import":
        return cmd_import(store, args)
    return cmd_revalidate(store, args)


if __name__ == "__main__":
    raise SystemExit(main())
