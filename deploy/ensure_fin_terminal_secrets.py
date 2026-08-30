#!/usr/bin/env python3
"""Ensure production fin-terminal credentials are present and separated."""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile


RETIRED_TOKEN_NAMES = (
    "FIN_TERMINAL_DEMO_PROXY_TOKEN",
)

TOKEN_NAMES = (
    "FIN_TERMINAL_PROXY_TOKEN",
    "FIN_TERMINAL_BROWSER_PROXY_TOKEN",
    "FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY",
    "FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN",
    "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN",
)
PUBLIC_TOKEN_NAMES = frozenset(
    {
        "FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY",
        "FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN",
        "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN",
    }
)
PUBLIC_EXTERNAL_NAMES = (
    "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY",
    "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET",
)
TURNSTILE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
TURNSTILE_PAYLOAD_MAX_BYTES = 515
ENV_MAX_BYTES = 1024 * 1024


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


def _read_regular_env(env_path: Path) -> tuple[str, os.stat_result]:
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
    return content, opened


def _replace_env_atomically(
    env_path: Path,
    content: str,
    expected: os.stat_result,
) -> None:
    current = os.stat(env_path, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("production .env is not a regular file")
    if current.st_dev != expected.st_dev or current.st_ino != expected.st_ino:
        raise ValueError("production .env changed while preparing credentials")

    fd, tmp_name = tempfile.mkstemp(prefix=".env.fin-terminal.", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_path)
        directory_fd = os.open(env_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _validate_turnstile_value(label: str, value: str) -> None:
    if not TURNSTILE_VALUE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {label}")


def install_public_turnstile_values(
    env_path: Path,
    site_key: str,
    secret: str,
) -> bool:
    """Atomically upsert externally issued Turnstile values into a staged .env."""
    _validate_turnstile_value("Turnstile site key", site_key)
    _validate_turnstile_value("Turnstile secret", secret)

    original, opened = _read_regular_env(env_path)
    lines = original.splitlines()
    public_enabled_count = sum(
        line.startswith("FIN_TERMINAL_PUBLIC_ENABLED=") for line in lines
    )
    if public_enabled_count > 1:
        raise ValueError("duplicate FIN_TERMINAL_PUBLIC_ENABLED definitions")
    public_enabled_value = _env_value(lines, "FIN_TERMINAL_PUBLIC_ENABLED")
    if public_enabled_value not in {"", "false", "true"}:
        raise ValueError("FIN_TERMINAL_PUBLIC_ENABLED must be true or false")

    replacements = {
        "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY": site_key,
        "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET": secret,
    }
    if all(
        sum(line.startswith(f"{name}=") for line in lines) == 1
        and _env_value(lines, name) == replacements[name]
        for name in PUBLIC_EXTERNAL_NAMES
    ):
        _replace_env_atomically(env_path, original, opened)
        return False

    prefixes = tuple(f"{name}=" for name in PUBLIC_EXTERNAL_NAMES)
    updated = [line for line in lines if not line.startswith(prefixes)]
    updated.extend(f"{name}={replacements[name]}" for name in PUBLIC_EXTERNAL_NAMES)
    content = "\n".join(updated) + "\n"
    changed = content != original
    if changed and public_enabled_value == "true":
        raise ValueError("disable the public route before changing Turnstile credentials")

    _replace_env_atomically(env_path, content, opened)
    return changed


def _read_turnstile_payload() -> tuple[str, str]:
    payload = sys.stdin.buffer.read(TURNSTILE_PAYLOAD_MAX_BYTES + 1)
    if len(payload) > TURNSTILE_PAYLOAD_MAX_BYTES:
        raise ValueError("Turnstile payload is too large")
    parts = payload.split(b"\0")
    if len(parts) != 3 or parts[-1] != b"":
        raise ValueError("invalid Turnstile payload framing")
    try:
        site_key, secret = (part.decode("ascii") for part in parts[:2])
    except UnicodeDecodeError as exc:
        raise ValueError("Turnstile payload must be ASCII") from exc
    return site_key, secret


def ensure_fin_terminal_secrets(env_path: Path) -> bool:
    """Return True when a terminal credential was generated or replaced."""
    original, opened = _read_regular_env(env_path)
    lines = original.splitlines()
    openrouter_key = _env_value(lines, "OPENROUTER_API_KEY")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY is missing from production .env")

    public_enabled_count = sum(
        line.startswith("FIN_TERMINAL_PUBLIC_ENABLED=") for line in lines
    )
    if public_enabled_count > 1:
        raise ValueError("duplicate FIN_TERMINAL_PUBLIC_ENABLED definitions")
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
        _validate_turnstile_value(
            "Turnstile site key",
            _env_value(lines, "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY"),
        )
        _validate_turnstile_value(
            "Turnstile secret",
            _env_value(lines, "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET"),
        )

    browser_enabled_count = sum(
        line.startswith("FIN_TERMINAL_BROWSER_ENABLED=") for line in lines
    )
    if browser_enabled_count > 1:
        raise ValueError("duplicate FIN_TERMINAL_BROWSER_ENABLED definitions")
    browser_enabled_value = _env_value(lines, "FIN_TERMINAL_BROWSER_ENABLED")
    if browser_enabled_value not in {"", "false", "true"}:
        raise ValueError("FIN_TERMINAL_BROWSER_ENABLED must be true or false")
    browser_enabled = browser_enabled_value == "true"

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
            if browser_enabled and name == "FIN_TERMINAL_BROWSER_PROXY_TOKEN":
                raise ValueError(
                    "FIN_TERMINAL_BROWSER_PROXY_TOKEN is invalid while "
                    "FIN_TERMINAL_BROWSER_ENABLED=true; disable the browser route before rotating it"
                )
            value = _new_token(excluding=protected_values | existing_values)
            tokens[name] = value
            existing_values.add(value)
            changed = True
        protected_values.add(value)

    # Strip retired token lines silently. This is not treated as a credential
    # rotation — the old token value is simply removed from the active .env
    # without reporting a credential change to the deploy orchestrator.
    active_token_prefixes = tuple(f"{name}=" for name in TOKEN_NAMES)
    retired_token_prefixes = tuple(f"{name}=" for name in RETIRED_TOKEN_NAMES)
    updated = [
        line for line in lines
        if not line.startswith(active_token_prefixes)
        and not line.startswith(retired_token_prefixes)
    ]
    # Materialize the fail-closed default on first deployment. Activation reads
    # exactly one explicit true/false definition and never relies on a Compose
    # interpolation default for an operational state transition.
    if public_enabled_count == 0:
        updated.append("FIN_TERMINAL_PUBLIC_ENABLED=false")
    if browser_enabled_count == 0:
        updated.append("FIN_TERMINAL_BROWSER_ENABLED=false")
    updated.extend(f"{name}={tokens[name]}" for name in TOKEN_NAMES)
    content = "\n".join(updated) + "\n"
    _replace_env_atomically(env_path, content, opened)
    return changed


def main(argv: list[str]) -> int:
    install_turnstile = len(argv) == 3 and argv[1] == "--install-public-turnstile"
    ensure_status = len(argv) == 3 and argv[1] == "--ensure-status"
    if len(argv) != 2 and not install_turnstile and not ensure_status:
        print(
            f"Usage: {argv[0]} [--ensure-status|--install-public-turnstile] /path/to/.env",
            file=sys.stderr,
        )
        return 2
    try:
        env_path = Path(argv[2] if install_turnstile or ensure_status else argv[1])
        if install_turnstile:
            site_key, secret = _read_turnstile_payload()
            changed = install_public_turnstile_values(env_path, site_key, secret)
            print(f"turnstile_changed={str(changed).lower()}")
            return 0
        generated = ensure_fin_terminal_secrets(env_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if ensure_status:
        print(f"fin_terminal_credentials_changed={str(generated).lower()}")
        return 0
    if generated:
        print("    Generated independent fin-terminal credential(s) on the host.")
    else:
        print("    Existing independent fin-terminal credentials retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
