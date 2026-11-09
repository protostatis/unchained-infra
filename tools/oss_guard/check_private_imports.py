#!/usr/bin/env python3
"""Fail if the public side reaches into private implementation modules or paths.

Two boundaries are enforced:

1. Public boundary modules must not import the private implementation modules
   (cdp, ddm, intel) or the rhythm runtime — they go through cloud_tools.
2. Only the private core may know where the rhythm runtime lives. No other
   Python module may reference RHYTHM_ROOT or load a rhythm asset, and only the
   private-core compose service may set RHYTHM_ROOT. That is what makes
   "rebuild only private-core when rhythm changes" a checkable invariant rather
   than a comment that a future service can quietly invalidate.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "unchained"
COMPOSE = REPO_ROOT / "docker-compose.yml"

PUBLIC_BOUNDARY_FILES = [
    SRC / "cloud_tools.py",
    SRC / "api.py",
    SRC / "mcp_server.py",
    SRC / "private_core_client.py",
]
FORBIDDEN = {"cdp", "ddm", "intel", "rhythm"}

# The only module allowed to resolve the rhythm runtime (it is overlaid from the
# private core at deploy time; the public copy is a stub).
RHYTHM_RUNTIME_OWNER = SRC / "private_core_engine.py"
RHYTHM_RUNTIME_MARKERS = ("RHYTHM_ROOT", "catcher_agent.js")
RHYTHM_COMPOSE_SERVICE = "private-core"
# Compose service keys may be quoted, followed by an inline comment, or opened
# as `name: {}`; an unquoted-only regex would leave the previous service name in
# place and misattribute a later RHYTHM_ROOT to it. Two-space indentation is
# canonical; a key indented otherwise fails closed as "outside any service".
SERVICE_LINE = re.compile(
    r'^  "?([A-Za-z0-9_.-]+)"?:\s*(?:#.*|\{\s*\})?$'
)


def imports_for(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def rhythm_runtime_violations(
    src: Path = SRC, repo_root: Path = REPO_ROOT
) -> list[str]:
    violations: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        if path == src / RHYTHM_RUNTIME_OWNER.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [marker for marker in RHYTHM_RUNTIME_MARKERS if marker in text]
        if hits:
            violations.append(
                f"{path.relative_to(repo_root)} references {', '.join(hits)}; "
                "only the private core may resolve the rhythm runtime"
            )
    return violations


def compose_violations(compose: Path = COMPOSE) -> list[str]:
    if not compose.exists():
        # Failing open here would let a rename or move of the compose file
        # silently disable the whole service-boundary rule.
        return [
            f"{compose.name} not found; cannot verify that only "
            f"{RHYTHM_COMPOSE_SERVICE} sets RHYTHM_ROOT"
        ]
    service: str | None = None
    section: str | None = None
    owners: set[str] = set()
    unattributed: list[int] = []
    for number, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            # Top-level key. This is where an unindented `RHYTHM_ROOT:` or an
            # `x-` anchor lives, neither of which belongs to a service, so check
            # the marker before treating the line as a section header.
            if "RHYTHM_ROOT" in line:
                unattributed.append(number)
                continue
            if ":" in line:
                section = line.split(":", 1)[0].strip()
                service = None
            continue
        if section == "services":
            match = SERVICE_LINE.match(line)
            if match:
                service = match.group(1)
                continue
        if "RHYTHM_ROOT" not in line:
            continue
        if section != "services" or service is None:
            unattributed.append(number)
        else:
            owners.add(service)

    violations: list[str] = []
    unexpected = sorted(owners - {RHYTHM_COMPOSE_SERVICE})
    if unexpected:
        violations.append(
            "docker-compose.yml sets RHYTHM_ROOT on "
            f"{', '.join(unexpected)}; only {RHYTHM_COMPOSE_SERVICE} may, so that a "
            "rhythm change only ever rebuilds that service"
        )
    for number in unattributed:
        violations.append(
            f"docker-compose.yml:{number} sets RHYTHM_ROOT outside any service block "
            "(an anchor or top-level default can be merged into any service)"
        )
    return violations


def main() -> int:
    violations: list[str] = []
    for path in PUBLIC_BOUNDARY_FILES:
        imported = imports_for(path)
        hit = sorted(imported & FORBIDDEN)
        if hit:
            violations.append(f"{path.relative_to(REPO_ROOT)} imports {', '.join(hit)}")
    violations.extend(rhythm_runtime_violations())
    violations.extend(compose_violations())

    if violations:
        print("Private import boundary violations found:")
        for line in violations:
            print(f"- {line}")
        return 1

    print("Private import boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
