#!/usr/bin/env python3
"""Ensure production fin-terminal credentials are present and separated."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import tempfile


def _env_value(lines: list[str], name: str) -> str:
    prefix = f"{name}="
    value = ""
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def _new_token(*, excluding: set[str]) -> str:
    while True:
        token = secrets.token_hex(32)
        if token not in excluding:
            return token


def ensure_fin_terminal_secrets(env_path: Path) -> bool:
    """Return True when either terminal proxy token was generated or replaced."""
    if env_path.is_symlink():
        raise ValueError("refusing symlinked production .env")
    if not env_path.is_file():
        raise ValueError("production .env is missing")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    openrouter_key = _env_value(lines, "OPENROUTER_API_KEY")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY is missing from production .env")

    terminal_token = _env_value(lines, "FIN_TERMINAL_PROXY_TOKEN")
    demo_token = _env_value(lines, "FIN_TERMINAL_DEMO_PROXY_TOKEN")
    changed = False

    # Generate on the deployment host so tokens never pass through CI logs or
    # shell arguments and can never reuse provider billing credentials. The
    # public kiosk receives a different token from the persistent terminal.
    if len(terminal_token) < 64 or terminal_token == openrouter_key:
        terminal_token = _new_token(excluding={openrouter_key, demo_token})
        changed = True
    if (
        len(demo_token) < 64
        or demo_token == openrouter_key
        or demo_token == terminal_token
    ):
        demo_token = _new_token(excluding={openrouter_key, terminal_token})
        changed = True

    updated = [
        line for line in lines
        if not line.startswith(("FIN_TERMINAL_PROXY_TOKEN=", "FIN_TERMINAL_DEMO_PROXY_TOKEN="))
    ]
    updated.extend(
        (
            f"FIN_TERMINAL_PROXY_TOKEN={terminal_token}",
            f"FIN_TERMINAL_DEMO_PROXY_TOKEN={demo_token}",
        )
    )
    content = "\n".join(updated) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".env.fin-terminal.", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} /path/to/.env", file=sys.stderr)
        return 2
    try:
        generated = ensure_fin_terminal_secrets(Path(argv[1]))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if generated:
        print("    Generated independent fin-terminal proxy token(s) on the host.")
    else:
        print("    Existing independent fin-terminal proxy tokens retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
