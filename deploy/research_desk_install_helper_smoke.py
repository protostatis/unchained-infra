#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
UNCHAINED_DIR = REPO_ROOT / "unchained"
PACKAGE_PATH = "/web/research-desk/files"
LOCAL_HOST = "127.0.0.1"
LOCAL_DESK_URL = "http://127.0.0.1:8766/"
PORT_CANDIDATES = (8088, 8080)
SMOKE_TIMEOUT_SECONDS = 120
BRIDGE_PORT = 9333
DESK_PORT = 8766
EXPECTED_PACKAGE = "unchained-pyreplab"
EXPECTED_VERSION = "0.1.0"

sys.path.insert(0, str(UNCHAINED_DIR))

from agent_package import build_research_desk_zip  # noqa: E402


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


class _FakePortListener:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None

    def __enter__(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._port))
        sock.listen(1)
        self._sock = sock
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def _listener_context(host: str, port: int):
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.2)
        if sock.connect_ex((host, port)) == 0:
            return contextlib.nullcontext()
    return _FakePortListener(host, port)


def _pick_local_package_port() -> int:
    for port in PORT_CANDIDATES:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            try:
                sock.bind((LOCAL_HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        "No free local package port found. Expected one of: "
        + ", ".join(str(p) for p in PORT_CANDIDATES)
    )


def _smoke_helper_python() -> str:
    candidates = [
        REPO_ROOT / ".venv" / "bin" / "python",
        UNCHAINED_DIR / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def _clean_system_path(fake_bin: Path) -> str:
    return os.pathsep.join([str(fake_bin), "/usr/local/bin", "/opt/homebrew/bin", os.defpath])


def _smoke_base_python() -> str:
    python3_bin = shutil.which(
        "python3",
        path=os.pathsep.join(["/usr/local/bin", "/opt/homebrew/bin", os.defpath]),
    )
    for candidate in (python3_bin, sys.executable):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("No executable python3 found for smoke verification")


def _write_fake_browser_openers(fake_bin: Path, open_log: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {str(open_log)!r}
exit 0
"""
    for name in ("open", "xdg-open"):
        path = fake_bin / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def _serve_research_desk_zip(host: str, port: int, auth_token: str):
    requests: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                }
            )
            if self.path != PACKAGE_PATH:
                self.send_response(404)
                self.end_headers()
                return
            if self.headers.get("Authorization") != f"Bearer {auth_token}":
                self.send_response(401)
                self.end_headers()
                return
            payload = build_research_desk_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = _ThreadedTCPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, requests


def _run_helper(env: dict[str, str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    helper_cmd = [_smoke_helper_python(), "chat_agent_cli.py", "--research-desk-install-helper"]
    return subprocess.run(
        helper_cmd,
        cwd=UNCHAINED_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def _verify_installed(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    python_bin = _smoke_base_python()
    return subprocess.run(
        [
            python_bin,
            "-c",
            (
                "import importlib.metadata as metadata, json, unchained_pyreplab; "
                f"print(json.dumps({{'module': unchained_pyreplab.__file__, "
                f"'name': '{EXPECTED_PACKAGE}', "
                f"'version': metadata.version('{EXPECTED_PACKAGE}')}}))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Research Desk install helper fully locally against a temp package server.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=SMOKE_TIMEOUT_SECONDS,
        help=f"Helper timeout in seconds (default: {SMOKE_TIMEOUT_SECONDS}).",
    )
    args = parser.parse_args()

    package_port = _pick_local_package_port()
    package_url = f"http://{LOCAL_HOST}:{package_port}{PACKAGE_PATH}"
    auth_token = f"uc_live_smoke_{secrets.token_hex(12)}"

    temp_root = Path(tempfile.mkdtemp(prefix="research-desk-helper-smoke-"))
    try:
        home_dir = temp_root / "home"
        agent_root = home_dir / "unchained-agent"
        user_base = temp_root / "userbase"
        fake_bin = temp_root / "bin"
        open_log = temp_root / "browser-open.log"
        for path in (home_dir, agent_root, user_base, fake_bin):
            path.mkdir(parents=True, exist_ok=True)
        _write_fake_browser_openers(fake_bin, open_log)

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home_dir),
                "PYTHONUSERBASE": str(user_base),
                "UNCHAINED_API_KEY": auth_token,
                "UNCHAINED_API_URL": f"http://{LOCAL_HOST}:{package_port}",
                "UNCHAINED_RESEARCH_DESK_PACKAGE_URL": package_url,
                "UNCHAINED_ALLOW_LOCAL_RESEARCH_DESK_PACKAGE_URL": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONUTF8": "1",
                "PATH": _clean_system_path(fake_bin),
            }
        )

        server, requests = _serve_research_desk_zip(LOCAL_HOST, package_port, auth_token)
        try:
            with _listener_context(LOCAL_HOST, BRIDGE_PORT), _listener_context(LOCAL_HOST, DESK_PORT):
                result = _run_helper(env, args.timeout)
        finally:
            server.shutdown()
            server.server_close()

        install_check = _verify_installed(env)
        install_payload = {}
        if install_check.returncode == 0 and install_check.stdout:
            install_payload = json.loads(install_check.stdout)
        config_path = home_dir / ".config" / "unchained-pyreplab" / "config.json"
        config_payload = {}
        if config_path.exists():
            config_payload = json.loads(config_path.read_text(encoding="utf-8"))
        summary = {
            "package_url": package_url,
            "helper_returncode": result.returncode,
            "helper_stdout": result.stdout,
            "helper_stderr": result.stderr,
            "requests": requests,
            "install_check_returncode": install_check.returncode,
            "install_check_stdout": install_payload,
            "install_check_stderr": install_check.stderr,
            "setup_config": config_payload,
            "open_log": open_log.read_text(encoding="utf-8") if open_log.exists() else "",
            "user_base": str(user_base),
        }
        print(json.dumps(summary, indent=2))

        if result.returncode != 0:
            return result.returncode
        if not requests:
            print("No local package requests were observed.", file=sys.stderr)
            return 1
        if any(req["path"] != PACKAGE_PATH for req in requests):
            print("Unexpected request path observed during smoke.", file=sys.stderr)
            return 1
        if any(req["authorization"] != f"Bearer {auth_token}" for req in requests):
            print("Unexpected authorization header observed during smoke.", file=sys.stderr)
            return 1
        if install_check.returncode != 0:
            return install_check.returncode
        if install_payload.get("name") != EXPECTED_PACKAGE:
            print("Unexpected installed package name.", file=sys.stderr)
            return 1
        if install_payload.get("version") != EXPECTED_VERSION:
            print("Unexpected installed package version.", file=sys.stderr)
            return 1
        if "agent_id" in config_payload:
            print("Setup config still persisted agent_id.", file=sys.stderr)
            return 1
        return 0
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
