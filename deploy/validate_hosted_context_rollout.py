#!/usr/bin/env python3
"""Require an explicit, safe hosted context budget before production deploy."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat


ENV_MAX_BYTES = 1024 * 1024
HOSTED_CONTEXT_ENV = "HOSTED_MAX_INTERNAL_CONTEXT_CHARS"
MIN_CONTEXT_CHARS = 10_000
MAX_CONTEXT_CHARS = 400_000


def _read_regular_env(env_path: Path) -> str:
    # Production accepts the staged regular file only; never follow a symlink
    # that could be swapped after staging.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(env_path, flags)
    except OSError as exc:
        raise ValueError("production .env is missing or unsafe") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("production .env is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            content = handle.read(ENV_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(content.encode("utf-8")) > ENV_MAX_BYTES:
        raise ValueError("production .env is too large")
    return content


def _values_for_name(content: str, name: str) -> list[str]:
    prefix = f"{name}="
    values = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        values.append(line[len(prefix) :].strip())
    return [
        value[1:-1]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}
        else value
        for value in values
    ]


def validate_hosted_context_rollout(env_path: Path) -> int:
    """Return the explicit context budget or raise before production mutation."""
    values = _values_for_name(_read_regular_env(env_path), HOSTED_CONTEXT_ENV)
    if len(values) != 1 or not values[0]:
        raise ValueError(
            "production .env must define exactly one non-empty "
            f"{HOSTED_CONTEXT_ENV}"
        )
    value = values[0]
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{HOSTED_CONTEXT_ENV} must be an integer")
    budget = int(value)
    if not MIN_CONTEXT_CHARS <= budget <= MAX_CONTEXT_CHARS:
        raise ValueError(
            f"{HOSTED_CONTEXT_ENV} must be between "
            f"{MIN_CONTEXT_CHARS} and {MAX_CONTEXT_CHARS}"
        )
    return budget


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_path", type=Path)
    args = parser.parse_args()
    validate_hosted_context_rollout(args.env_path)
    print("hosted_context_rollout_valid=true")


if __name__ == "__main__":
    main()
