#!/usr/bin/env python3
"""Report services whose resolved Docker Compose configuration changed.

The deploy host runs this helper after rendering both the pre-deploy snapshot
and uploaded Compose files with the same project directory and environment.
It deliberately fails closed: malformed input, service topology changes, or
top-level Compose changes produce ``ALL`` so deploy.sh performs a full deploy.

The only ignored field is caddy's ``depends_on``. That field affects startup
ordering, not the running Caddy container, and should not interrupt the edge
proxy when another service's dependency condition changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_config(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Compose config must be a JSON object")
    return value


def _services(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    services = config.get("services")
    if not isinstance(services, dict) or not services or not all(
        isinstance(name, str) and isinstance(value, dict)
        for name, value in services.items()
    ):
        return None
    return services


def _normalized_service(name: str, service: Dict[str, Any]) -> Dict[str, Any]:
    # JSON round-tripping makes a deep copy using only the value types Compose
    # emits, so removing caddy.depends_on cannot mutate the parsed config.
    normalized = json.loads(json.dumps(service))
    if name == "caddy":
        normalized.pop("depends_on", None)
    return normalized


def changed_services(
    old_config: Dict[str, Any], new_config: Dict[str, Any]
) -> Optional[List[str]]:
    """Return changed service names, or ``None`` when a full deploy is needed."""
    old_services = _services(old_config)
    new_services = _services(new_config)
    if old_services is None or new_services is None:
        return None

    # Network, volume, secret, config, or project-name changes can alter the
    # runtime of multiple services without appearing inside a service object.
    # Treat them as a full deploy instead of guessing ownership.
    old_topology = {key: value for key, value in old_config.items() if key != "services"}
    new_topology = {key: value for key, value in new_config.items() if key != "services"}
    if old_topology != new_topology or set(old_services) != set(new_services):
        return None

    return sorted(
        name
        for name in new_services
        if _normalized_service(name, old_services[name])
        != _normalized_service(name, new_services[name])
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("old_config", type=Path)
    parser.add_argument("new_config", type=Path)
    args = parser.parse_args(argv)

    try:
        changed = changed_services(
            _load_config(args.old_config),
            _load_config(args.new_config),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not compare resolved Compose config: {exc}", file=sys.stderr)
        return 2

    if changed is None:
        print("ALL")
    else:
        print("\n".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
