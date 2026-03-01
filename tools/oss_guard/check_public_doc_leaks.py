#!/usr/bin/env python3
"""Fail if public docs contain private operational details."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

FORBIDDEN_PATHS = {
    REPO_ROOT / "unchained" / "LABEL_RESOLUTION.md":
        "private algorithm notes belong in unchained-core-private",
}

REQUIRED_MARKERS = {
    REPO_ROOT / "CLAUDE.md": "Public Repo Guide",
    REPO_ROOT / "unchained" / "CLAUDE.md": "Public Stub",
}

FORBIDDEN_SUBSTRINGS = {
    "PanicRadar": "internal account references must stay out of the public repo",
}


def tracked_markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    files: list[Path] = []
    for rel in result.stdout.splitlines():
        rel = rel.strip()
        if rel:
            path = REPO_ROOT / rel
            if path.exists():
                files.append(path)
    return sorted(files)


def find_global_ipv4s(text: str) -> list[str]:
    hits: set[str] = set()
    for match in IPV4_RE.findall(text):
        try:
            ip = ipaddress.ip_address(match)
        except ValueError:
            continue
        if ip.is_global:
            hits.add(match)
    return sorted(hits)


def main() -> int:
    violations: list[str] = []

    for path, reason in FORBIDDEN_PATHS.items():
        if path.exists():
            violations.append(f"{path.relative_to(REPO_ROOT)} present: {reason}")

    for path, marker in REQUIRED_MARKERS.items():
        if not path.exists():
            violations.append(f"{path.relative_to(REPO_ROOT)} missing required public stub")
            continue
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            violations.append(
                f"{path.relative_to(REPO_ROOT)} missing required marker '{marker}'"
            )

    for path in tracked_markdown_files():
        text = path.read_text(encoding="utf-8")
        for ip in find_global_ipv4s(text):
            violations.append(f"{path.relative_to(REPO_ROOT)} contains routable IPv4 {ip}")
        for needle, reason in FORBIDDEN_SUBSTRINGS.items():
            if needle in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains '{needle}': {reason}")

    if violations:
        print("Public doc leak check failed:")
        for violation in sorted(set(violations)):
            print(f"- {violation}")
        return 1

    print("Public doc leak check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
