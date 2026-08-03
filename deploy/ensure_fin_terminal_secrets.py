#!/usr/bin/env python3
"""Ensure production fin-terminal credentials are present and separated."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import tempfile


TOKEN_NAMES = (
    "FIN_TERMINAL_PROXY_TOKEN",
    "FIN_TERMINAL_DEMO_PROXY_TOKEN",
    "FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY",
    "FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN",
    "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN",
)
PUBLIC_TOKEN_NAMES = frozenset(TOKEN_NAMES[2:])
PUBLIC_EXTERNAL_NAMES = (
    "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY",
    "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET",
)


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
    """Return True when a terminal credential was generated or replaced."""
    if env_path.is_symlink():
        raise ValueError("refusing symlinked production .env")
    if not env_path.is_file():
        raise ValueError("production .env is missing")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    openrouter_key = _env_value(lines, "OPENROUTER_API_KEY")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY is missing from production .env")

    public_enabled_value = _env_value(lines, "FIN_TERMINAL_PUBLIC_ENABLED")
    if public_enabled_value not in {"", "false", "true"}:
        raise ValueError("FIN_TERMINAL_PUBLIC_ENABLED must be true or false")
    public_enabled = public_enabled_value == "true"
    if public_enabled:
        missing = [name for name in PUBLIC_EXTERNAL_NAMES if not _env_value(lines, name)]
        if missing:
            raise ValueError(
                "public terminal is enabled but required external values are missing: "
                + ", ".join(missing)
            )

    tokens = {name: _env_value(lines, name) for name in TOKEN_NAMES}
    existing_values = {value for value in tokens.values() if value}
    protected_values = {openrouter_key}
    changed = False

    # Generate on the deployment host so tokens never pass through CI logs or
    # shell arguments and can never reuse provider billing credentials. Every
    # trust boundary receives an independent 256-bit value. Public credentials
    # may be prepared while the pilot is disabled, but are never rotated
    # implicitly while its edge route is enabled.
    for name in TOKEN_NAMES:
        value = tokens[name]
        if len(value) < 64 or value in protected_values:
            if public_enabled and name in PUBLIC_TOKEN_NAMES:
                raise ValueError(
                    f"{name} is invalid while FIN_TERMINAL_PUBLIC_ENABLED=true; "
                    "disable the public route before rotating it"
                )
            value = _new_token(excluding=protected_values | existing_values)
            tokens[name] = value
            existing_values.add(value)
            changed = True
        protected_values.add(value)

    updated = [
        line for line in lines
        if not line.startswith(tuple(f"{name}=" for name in TOKEN_NAMES))
    ]
    updated.extend(f"{name}={tokens[name]}" for name in TOKEN_NAMES)
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
        print("    Generated independent fin-terminal credential(s) on the host.")
    else:
        print("    Existing independent fin-terminal credentials retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
