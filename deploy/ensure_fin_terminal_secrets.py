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


def ensure_fin_terminal_secrets(env_path: Path) -> bool:
    """Return True when a proxy token was generated or replaced."""
    if env_path.is_symlink():
        raise ValueError("refusing symlinked production .env")
    if not env_path.is_file():
        raise ValueError("production .env is missing")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    openrouter_key = _env_value(lines, "OPENROUTER_API_KEY")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY is missing from production .env")

    proxy_token = _env_value(lines, "FIN_TERMINAL_PROXY_TOKEN")
    if (
        len(proxy_token) >= 64
        and proxy_token != openrouter_key
    ):
        os.chmod(env_path, 0o600)
        return False

    # Generate on the deployment host so the token never passes through CI
    # logs or shell arguments and can never reuse the provider billing key.
    token = secrets.token_hex(32)
    updated = [
        line for line in lines
        if not line.startswith("FIN_TERMINAL_PROXY_TOKEN=")
    ]
    updated.append(f"FIN_TERMINAL_PROXY_TOKEN={token}")
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
    return True


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
        print("    Generated an independent fin-terminal proxy token on the host.")
    else:
        print("    Existing independent fin-terminal proxy token retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
