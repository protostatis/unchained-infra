#!/usr/bin/env python3
"""Fail if public boundary modules import private implementation modules."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "unchained"

PUBLIC_BOUNDARY_FILES = [
    SRC / "cloud_tools.py",
    SRC / "api.py",
    SRC / "mcp_server.py",
    SRC / "private_core_client.py",
]
FORBIDDEN = {"cdp", "ddm", "intel"}


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


def main() -> int:
    violations: list[str] = []
    for path in PUBLIC_BOUNDARY_FILES:
        imported = imports_for(path)
        hit = sorted(imported & FORBIDDEN)
        if hit:
            violations.append(f"{path.relative_to(REPO_ROOT)} imports {', '.join(hit)}")

    if violations:
        print("Private import boundary violations found:")
        for line in violations:
            print(f"- {line}")
        return 1

    print("Private import boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
