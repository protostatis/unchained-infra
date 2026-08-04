#!/usr/bin/env python3
"""Host-side account-scoped workspace runtime provider (fin-terminal workspace).

The control plane never touches the Docker socket. This host-side systemd
service owns the Docker authority and provisions ONE isolated runtime
container per workspace account:

- per-account private Docker network (internal: true) + named data volume
- checkpoint-file provisioning: the control plane's imported workspace
  snapshot is written to the per-account volume at
  ``/data/checkpoint.json`` (``FIN_WORKSPACE_CHECKPOINT_FILE``), which the
  pinned app runtime consumes
- the control-plane container is attached to the per-account network so it can
  proxy ``/fin-terminal/`` (HTTP + WebSocket) to ``fin-workspace-<slug>:8787``
- no published host ports; ``cap_drop ALL``, ``no-new-privileges``, read-only
  rootfs, tmpfs, pids/mem/cpu limits, and never a Docker socket in a container

Lifecycle: wake / attach / flush / sleep (idempotent). The provider is a
*validated* runtime provider only while it declares the ``accountRuntime`` and
``checkpointFile`` capabilities at ``/v1/health``; the control plane refuses
to activate the workspace feature without them (hard enablement gate).

Environment:
  FIN_WORKSPACE_RUNTIME_TOKEN            str   — shared secret (>=32 chars)
  FIN_WORKSPACE_RUNTIME_LISTEN           str   (default: 0.0.0.0:8793)
  FIN_WORKSPACE_RUNTIME_APP_IMAGE        str   — immutable pinned app image
  FIN_WORKSPACE_RUNTIME_APP_PORT         int   (default: 8787)
  FIN_WORKSPACE_RUNTIME_APP_CAPABLE      bool  (default: false) — set true only
                                               after the app runtime support is
                                               verified against the pinned image
  FIN_WORKSPACE_RUNTIME_CONTROL_CONTAINER str  (default: fin-terminal-workspace-control)
  FIN_WORKSPACE_RUNTIME_CHECKPOINT_FILE  str   (default: /data/checkpoint.json)

HTTP API (token required, header ``X-Workspace-Runtime-Token``):
  GET  /v1/health
  GET  /v1/accounts/{slug}/status
  POST /v1/accounts/{slug}/wake
  POST /v1/accounts/{slug}/sleep
  POST /v1/accounts/{slug}/flush
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

_log = logging.getLogger("workspace-runtime-provider")

_SLUG_RE = re.compile(r"^[a-f0-9]{24}$")
_TRUE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})


def parse_feature_flag(value: str | None) -> bool:
    """Same cross-repo boolean contract as the control plane: 1|true|yes|on."""
    return (value or "").strip().lower() in _TRUE_FLAG_VALUES


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class ProviderConfig:
    def __init__(self) -> None:
        self.token = os.environ.get("FIN_WORKSPACE_RUNTIME_TOKEN", "").strip()
        self.listen = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_LISTEN", "0.0.0.0:8793"
        ).strip()
        self.app_image = os.environ.get("FIN_WORKSPACE_RUNTIME_APP_IMAGE", "").strip()
        self.app_port = int(os.environ.get("FIN_WORKSPACE_RUNTIME_APP_PORT", "8787"))
        self.app_capable = parse_feature_flag(
            os.environ.get("FIN_WORKSPACE_RUNTIME_APP_CAPABLE", "false")
        )
        self.control_container = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_CONTROL_CONTAINER", "fin-terminal-workspace-control"
        ).strip()
        self.checkpoint_file = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_CHECKPOINT_FILE", "/data/checkpoint.json"
        ).strip()

    def errors(self) -> list[str]:
        errs: list[str] = []
        if not self.token or len(self.token) < 32:
            errs.append("FIN_WORKSPACE_RUNTIME_TOKEN must be >= 32 chars")
        if not self.app_image:
            errs.append(
                "FIN_WORKSPACE_RUNTIME_APP_IMAGE must be the immutable pinned "
                "app image (no mutable tags)"
            )
        host, _sep, port = self.listen.partition(":")
        if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            errs.append(f"invalid FIN_WORKSPACE_RUNTIME_LISTEN: {self.listen!r}")
        if not self.control_container or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.control_container
        ):
            errs.append(f"invalid control container name: {self.control_container!r}")
        if not self.checkpoint_file.startswith("/"):
            errs.append("checkpoint file path must be absolute inside the container")
        return errs


# ---------------------------------------------------------------------------
# Docker helpers (host authority only)
# ---------------------------------------------------------------------------
class Docker:
    def _run(self, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
        _log.debug("docker %s", " ".join(args))
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
        )

    def container_name(self, slug: str) -> str:
        return f"fin-workspace-{slug}"

    def network_name(self, slug: str) -> str:
        return f"fin_ws_{slug}"

    def volume_name(self, slug: str) -> str:
        return f"fin_ws_{slug}_data"

    def image_present(self, image: str) -> bool:
        try:
            result = self._run("image", "inspect", image, check=False)
            return result.returncode == 0
        except Exception:
            return False

    def container_state(self, slug: str) -> str:
        name = self.container_name(slug)
        result = self._run("inspect", "--format", "{{.State.Status}}", name, check=False)
        state = result.stdout.strip() if result.returncode == 0 else "absent"
        return state or "absent"

    def ensure_network(self, slug: str) -> bool:
        net = self.network_name(slug)
        result = self._run(
            "network", "inspect", net, check=False,
        )
        if result.returncode == 0:
            return True
        created = self._run("network", "create", "--internal", net, check=False)
        if created.returncode != 0 and "already exists" not in (created.stderr or ""):
            _log.error("network create failed for %s: %s", net, created.stderr)
            return False
        return True

    def write_checkpoint_file(self, slug: str, image: str, checkpoint: dict) -> bool:
        """Provision /data/checkpoint.json on the per-account volume.

        Runs a one-off helper container from the pinned app image (never a
        mutable tool image) with the volume mounted; the JSON travels over
        stdin — no shell interpolation, no temp file on the host.
        """
        payload = json.dumps(checkpoint, separators=(",", ":"), default=str)
        script = (
            "let d='';process.stdin.setEncoding('utf8');"
            "process.stdin.on('data',c=>d+=c);"
            "process.stdin.on('end',()=>{"
            "require('fs').writeFileSync('/data/checkpoint.json',d);"
            "});"
        )
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm", "-i",
                    "-v", f"{self.volume_name(slug)}:/data",
                    "--entrypoint", "node",
                    image, "-e", script,
                ],
                input=payload,
                capture_output=True, text=True, timeout=60, check=False,
            )
        except Exception as exc:
            _log.error("checkpoint provisioning failed: %s", exc)
            return False
        if result.returncode != 0:
            _log.error(
                "checkpoint provisioning failed for %s: %s", slug, result.stderr
            )
            return False
        return True

    def start_runtime(self, cfg: ProviderConfig, slug: str, control_token: str) -> bool:
        name = self.container_name(slug)
        state = self.container_state(slug)
        if state == "running":
            return True
        if state != "absent":
            # stale container (exited/dead) — replace it deterministically
            self._run("rm", "-f", name, check=False)
        cmd = [
            "run", "-d",
            "--name", name,
            "--network", self.network_name(slug),
            "--network-alias", name,
            "-v", f"{self.volume_name(slug)}:/data",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=134217728,mode=1777",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--init",
            "--pids-limit", "96",
            "--cpus", "0.5",
            "--memory", "512m",
            "--memory-reservation", "128m",
            "--restart", "unless-stopped",
            "-e", "HOST=0.0.0.0",
            "-e", f"PORT={cfg.app_port}",
            "-e", "NODE_ENV=production",
            "-e", f"FIN_WORKSPACE_CHECKPOINT_FILE={cfg.checkpoint_file}",
            "-e", f"FIN_WORKSPACE_CONTROL_TOKEN={control_token}",
            cfg.app_image,
        ]
        result = self._run(*cmd, check=False)
        if result.returncode != 0:
            _log.error("runtime start failed for %s: %s", slug, result.stderr)
            return False
        return True

    def connect_control_plane(self, slug: str, control_container: str) -> None:
        """Attach the control-plane container to the per-account network so it
        can proxy /fin-terminal/ to the account runtime (Docker DNS)."""
        self._run(
            "network", "connect", self.network_name(slug), control_container,
            check=False,
        )

    def stop_runtime(self, slug: str, control_container: str) -> bool:
        name = self.container_name(slug)
        state = self.container_state(slug)
        if state == "absent":
            return True
        self._run("stop", "-t", "10", name, check=False)
        self._run("rm", "-f", name, check=False)
        # The control plane may have been connected to this network.
        self._run(
            "network", "disconnect", self.network_name(slug), control_container,
            check=False,
        )
        return True

    def read_checkpoint_file(self, slug: str, image: str) -> dict | None:
        """Read the current checkpoint file from the per-account volume."""
        script = (
            "const fs=require('fs');"
            "try{process.stdout.write(fs.readFileSync('/data/checkpoint.json','utf8'));}"
            "catch(e){process.exit(1);}"
        )
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm", "-i",
                    "-v", f"{self.volume_name(slug)}:/data",
                    "--entrypoint", "node",
                    image, "-e", script,
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except Exception:
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Provider core
# ---------------------------------------------------------------------------
class WorkspaceRuntimeProvider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.docker = Docker()

    def health(self) -> dict:
        return {
            "status": "ok",
            "provider": "host-side-v1",
            "image": self.cfg.app_image,
            "capabilities": {
                # The operator flips accountRuntime/checkpointFile to true only
                # after the pinned app image's runtime support is verified. The
                # control plane treats an unvalidated provider as fail-closed.
                "accountRuntime": self.cfg.app_capable,
                "checkpointFile": self.cfg.app_capable,
            },
        }

    def status(self, slug: str) -> dict | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        state = self.docker.container_state(slug)
        return {
            "slug": slug,
            "container": self.docker.container_name(slug),
            "state": state,
            "image": self.cfg.app_image,
        }

    def wake(self, slug: str, checkpoint: dict, control_token: str = "") -> dict | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        if not isinstance(checkpoint, dict):
            return None
        if not self.cfg.app_capable:
            _log.error(
                "wake refused for %s: app runtime capability not validated "
                "(FIN_WORKSPACE_RUNTIME_APP_CAPABLE=false)",
                slug,
            )
            return None
        if not self.docker.image_present(self.cfg.app_image):
            _log.error("wake refused for %s: pinned image %s not present", slug, self.cfg.app_image)
            return None
        if not self.docker.ensure_network(slug):
            return None
        if not self.docker.write_checkpoint_file(slug, self.cfg.app_image, checkpoint):
            return None
        if not self.docker.start_runtime(self.cfg, slug, control_token):
            return None
        self.docker.connect_control_plane(slug, self.cfg.control_container)
        return self.status(slug)

    def sleep(self, slug: str) -> dict | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        self.docker.stop_runtime(slug, self.cfg.control_container)
        return self.status(slug)

    def flush(self, slug: str, control_url: str, control_token: str) -> dict:
        """Export the current checkpoint file to the control plane (S2S)."""
        if not _SLUG_RE.fullmatch(slug):
            return {"ok": False, "reason": "invalid slug"}
        checkpoint = self.docker.read_checkpoint_file(slug, self.cfg.app_image)
        if checkpoint is None:
            return {"ok": False, "reason": "no checkpoint file"}
        try:
            import urllib.request

            body = json.dumps({"slug": slug, "checkpoint": checkpoint}).encode("utf-8")
            req = urllib.request.Request(
                f"{control_url.rstrip('/')}/internal/financial-workspace/runtime/flush",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {control_token}",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    return {"ok": False, "reason": f"control plane returned {resp.status}"}
                payload = resp.read().decode("utf-8")
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    return {"ok": True}
        except Exception as exc:
            _log.error("flush failed for %s: %s", slug, exc)
            return {"ok": False, "reason": str(exc)}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class ProviderHandler(BaseHTTPRequestHandler):
    provider: WorkspaceRuntimeProvider | None = None
    token: str = ""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        import hmac

        got = self.headers.get("X-Workspace-Runtime-Token", "")
        return bool(got) and hmac.compare_digest(got, self.token)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        path = self.path
        if path == "/v1/health":
            self._send(200, self.provider.health())
            return
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/status", path)
        if match:
            status = self.provider.status(match.group(1))
            if status is None:
                self._send(400, {"error": "invalid slug"})
            else:
                self._send(200, status)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        body = self._read_body()
        path = self.path
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/wake", path)
        if match:
            slug = match.group(1)
            result = self.provider.wake(
                slug,
                body.get("checkpoint"),
                control_token=str(body.get("controlToken", "")),
            )
            if result is None:
                self._send(503, {"error": "runtime not provisioned"})
            else:
                self._send(200, result)
            return
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/sleep", path)
        if match:
            result = self.provider.sleep(match.group(1))
            if result is None:
                self._send(400, {"error": "invalid slug"})
            else:
                self._send(200, result)
            return
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/flush", path)
        if match:
            result = self.provider.flush(
                match.group(1),
                str(body.get("controlUrl", "http://fin-terminal-workspace-control:8790")),
                str(body.get("controlToken", "")),
            )
            self._send(200 if result.get("ok") else 502, result)
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        _log.debug(fmt, *args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    cfg = ProviderConfig()
    errors = cfg.errors()
    if errors:
        for err in errors:
            _log.error("Configuration error: %s", err)
        sys.exit(1)

    provider = WorkspaceRuntimeProvider(cfg)
    host, _sep, port = cfg.listen.partition(":")
    server = ThreadingHTTPServer((host, int(port)), ProviderHandler)
    ProviderHandler.provider = provider
    ProviderHandler.token = cfg.token
    _log.info(
        "Workspace runtime provider listening on %s (image=%s, capable=%s)",
        cfg.listen, cfg.app_image, cfg.app_capable,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
