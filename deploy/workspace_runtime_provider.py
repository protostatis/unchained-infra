#!/usr/bin/env python3
"""Host-side account-scoped workspace runtime provider (fin-terminal workspace).

The control plane never touches the Docker socket. This host-side systemd
service owns the Docker authority and provisions ONE isolated runtime
container per workspace account:

- per-account private Docker network (internal: true) that carries the runtime,
  the control-plane container, and the session-isolating unbrowser MCP broker
  (``fin_ws_<slug>``). The broker starts a distinct worker for each MCP
  session; a separate per-account NON-internal egress network
  (``fin_ws_<slug>_egress``) carries ONLY the runtime so model/MCP traffic can
  leave the host while no sibling runtime can ever reach another account's
  network or data. Sibling runtimes never share a network.
- checkpoint-file provisioning: the control plane's imported workspace
  snapshot is written to the per-account volume at
  ``/data/checkpoint.json`` (``FIN_WORKSPACE_CHECKPOINT_FILE``), which the
  pinned app runtime consumes
- allowlisted environment only: exactly the app's private-workspace contract
  is passed to the container (proxy token, allowed origins, control token,
  model/OpenRouter config, MCP URL, account session id) — never a broad
  host-environment injection
- flush contract: before sleep/shutdown the provider asks the RUNNING app
  runtime to export its current authoritative checkpoint (authenticated with
  the proxy + control tokens), then persists it to the control plane (S2S).
  The control plane is Docker-internal only and never publishes a host port,
  so the S2S persist request is executed INSIDE the control-plane container
  (``docker exec -i`` against ``127.0.0.1:8790``) — the host never resolves
  the Docker service name. The payload travels on bounded stdin and the token
  is JSON-escaped into the JS literal (never argv/shell/logs).
  A read of the original checkpoint file is used only as a fallback when the
  file's content was durably acknowledged (equals the last snapshot written).
- no published host ports; ``cap_drop ALL``, ``no-new-privileges``, read-only
  rootfs, tmpfs, pids/mem/cpu limits, and never a Docker socket in a container

Lifecycle: wake / attach / flush / sleep / delete (idempotent). The provider
is a *validated* runtime provider only while it declares the ``accountRuntime``
and ``checkpointFile`` capabilities at ``/v1/health`` AND a real image-contract
probe of the pinned app image passes (build mode ``live``, base path
``/fin-terminal/``, private-workspace mode, export path present). The control
plane refuses to activate the workspace feature without those capabilities
(hard enablement gate).

Environment:
  FIN_WORKSPACE_RUNTIME_TOKEN                str   — shared secret (>=32 chars)
  FIN_WORKSPACE_RUNTIME_LISTEN               str   (default: 0.0.0.0:8793)
  FIN_WORKSPACE_RUNTIME_APP_IMAGE            str   — immutable pinned app image
  FIN_WORKSPACE_RUNTIME_APP_PORT             int   (default: 8787)
  FIN_WORKSPACE_RUNTIME_APP_CAPABLE          bool  (default: false) — operator
                                                  prerequisite; the capability
                                                  only turns on when a real probe
                                                  of the pinned image also passes
  FIN_WORKSPACE_RUNTIME_CONTROL_CONTAINER    str   (default: fin-terminal-workspace-control)
  FIN_WORKSPACE_RUNTIME_CONTROL_PORT         int   (default: 8790) — control-plane
                                                  listener port reached on the
                                                  control container's loopback
  FIN_WORKSPACE_RUNTIME_MCP_CONTAINER        str   (default: fin-terminal-workspace-unbrowser-mcp)
  FIN_WORKSPACE_RUNTIME_CHECKPOINT_FILE      str   (default: /data/checkpoint.json)
  FIN_WORKSPACE_RUNTIME_PROXY_TOKEN          str   — app MARKET_PROXY_TOKEN (>=32)
  FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS      str   (default: https://unbrowser.unchainedsky.com)
  FIN_WORKSPACE_RUNTIME_MCP_URL              str   (default: http://fin-terminal-workspace-unbrowser-mcp:8767/mcp)
  FIN_WORKSPACE_RUNTIME_MODEL_PROVIDER       str   (default: openrouter)
  FIN_WORKSPACE_RUNTIME_MODEL_ID             str   (optional explicit id)
  FIN_WORKSPACE_RUNTIME_OPENROUTER_MODEL     str   (default: deepseek/deepseek-v4-flash-0731)
  FIN_WORKSPACE_RUNTIME_OPENROUTER_API_KEY   str   (>=16, required)
  FIN_WORKSPACE_RUNTIME_MAX_OUTPUT_TOKENS    int   (default: 4096)
  FIN_WORKSPACE_RUNTIME_STATE_DIR            str   (default: /var/lib/unchained/fin-workspace)

HTTP API (token required, header ``X-Workspace-Runtime-Token``):
  GET  /v1/health
  GET  /v1/probe
  GET  /v1/accounts/{slug}/status
  POST /v1/accounts/{slug}/wake
  POST /v1/accounts/{slug}/flush
  POST /v1/accounts/{slug}/sleep
  POST /v1/accounts/{slug}/delete
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

_log = logging.getLogger("workspace-runtime-provider")

_SLUG_RE = re.compile(r"^[a-f0-9]{24}$")
_TRUE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})

# Upper bound on a flushed checkpoint payload (bytes). The S2S flush body is
# bounded before it is piped into the control container over stdin so a runaway
# checkpoint can never exhaust the container's stdin/HTTP buffers.
_MAX_FLUSH_PAYLOAD_BYTES = 8 * 1024 * 1024

# Labels the provider stamps on every resource it owns so it can verify, before
# any destructive mutation, that a container/network is really its own.
_LABEL_SLUG = "com.unchained.fin-workspace.slug"
_LABEL_IMAGE = "com.unchained.fin-workspace.image"
_LABEL_GENERATION = "com.unchained.fin-workspace.generation"
_LABEL_SESSION = "com.unchained.fin-workspace.session-id"


def parse_feature_flag(value: str | None) -> bool:
    """Same cross-repo boolean contract as the control plane: 1|true|yes|on."""
    return (value or "").strip().lower() in _TRUE_FLAG_VALUES


def _is_immutable_image_ref(image: str) -> bool:
    """Return whether an image reference is actually immutable.

    Accepted forms:
      - digest-pinned: ``repo@sha256:<64 hex>``
      - content-derived tag: ``<name>:<40 hex>`` (e.g. ``unbrowser-fin-terminal:``
        followed by the app commit sha)

    Rejected forms (mutable / unqualified): ``latest``, ``repo`` (no tag,
    implies ``latest``), and any other tag (``repo:v1``, ``repo:stable``, ...).
    """
    value = (image or "").strip()
    if not value:
        return False
    if re.search(r"@sha256:[0-9a-f]{64}$", value, flags=re.IGNORECASE):
        return True
    return bool(re.fullmatch(r"[\w./:\-]+:[0-9a-f]{40}", value, flags=re.IGNORECASE))


def _imul(a: int, b: int) -> int:
    """32-bit wrapping multiply (Math.imul semantics)."""
    return (a * b) & 0xFFFFFFFF


def worker_generation_epoch(generation: str) -> int:
    """Deterministic generation→epoch hash, mirroring the app's
    ``workerGenerationEpoch`` so the flush export request authorizes for the
    exact generation the app runtime was started with."""
    h = 2_166_136_261
    for ch in generation:
        h ^= ord(ch)
        h = _imul(h, 16_777_619)
        if h >= 2**31:  # treat as signed 32-bit for Math.abs parity
            h -= 2**32
    return abs(h) % 2_000_000_000


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
        self.control_port = int(
            os.environ.get("FIN_WORKSPACE_RUNTIME_CONTROL_PORT", "8790")
        )
        self.mcp_container = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_MCP_CONTAINER", "fin-terminal-workspace-unbrowser-mcp"
        ).strip()
        self.checkpoint_file = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_CHECKPOINT_FILE", "/data/checkpoint.json"
        ).strip()
        self.proxy_token = os.environ.get("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN", "").strip()
        self.allowed_origins = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS",
            "https://unbrowser.unchainedsky.com",
        ).strip()
        self.mcp_url = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_MCP_URL",
            "http://fin-terminal-workspace-unbrowser-mcp:8767/mcp",
        ).strip()
        self.model_provider = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_MODEL_PROVIDER", "openrouter"
        ).strip()
        self.model_id = os.environ.get("FIN_WORKSPACE_RUNTIME_MODEL_ID", "").strip()
        self.openrouter_model = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_OPENROUTER_MODEL",
            "deepseek/deepseek-v4-flash-0731",
        ).strip()
        self.openrouter_api_key = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_OPENROUTER_API_KEY", ""
        ).strip()
        self.max_output_tokens = int(
            os.environ.get("FIN_WORKSPACE_RUNTIME_MAX_OUTPUT_TOKENS", "4096")
        )
        self.local_research_concurrency = os.environ.get(
            "FIN_WORKSPACE_LOCAL_RESEARCH_CONCURRENCY", ""
        ).strip()
        self.state_dir = os.environ.get(
            "FIN_WORKSPACE_RUNTIME_STATE_DIR",
            "/var/lib/unchained/fin-workspace",
        ).strip()

    def errors(self) -> list[str]:
        errs: list[str] = []
        if not self.token or len(self.token) < 32:
            errs.append("FIN_WORKSPACE_RUNTIME_TOKEN must be >= 32 chars")
        if not self.app_image or not _is_immutable_image_ref(self.app_image):
            errs.append(
                "FIN_WORKSPACE_RUNTIME_APP_IMAGE must be an actually immutable "
                "image reference: a digest (@sha256:<64 hex>) or a content-derived "
                "tag (<name>:<40 hex>). Mutable tags ('latest', 'v1', unqualified "
                "refs) are rejected."
            )
        host, _sep, port = self.listen.partition(":")
        if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            errs.append(f"invalid FIN_WORKSPACE_RUNTIME_LISTEN: {self.listen!r}")
        for label, value in (
            ("control container", self.control_container),
            ("MCP container", self.mcp_container),
        ):
            if not value or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value):
                errs.append(f"invalid {label} name: {value!r}")
        if not 1 <= self.control_port <= 65535:
            errs.append("FIN_WORKSPACE_RUNTIME_CONTROL_PORT must be 1-65535")
        if not self.checkpoint_file.startswith("/"):
            errs.append("checkpoint file path must be absolute inside the container")
        if not self.proxy_token or len(self.proxy_token) < 32:
            errs.append("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN must be >= 32 chars")
        if not self.openrouter_api_key or len(self.openrouter_api_key) < 16:
            errs.append("FIN_WORKSPACE_RUNTIME_OPENROUTER_API_KEY must be >= 16 chars")
        if self.model_provider not in ("openrouter",):
            errs.append(
                "FIN_WORKSPACE_RUNTIME_MODEL_PROVIDER must be 'openrouter' "
                "(other providers are not wired for workspace runtimes)"
            )
        if not self.mcp_url.startswith(("http://", "https://")):
            errs.append("FIN_WORKSPACE_RUNTIME_MCP_URL must be an HTTP(S) URL")
        if not 256 <= self.max_output_tokens <= 16384:
            errs.append("FIN_WORKSPACE_RUNTIME_MAX_OUTPUT_TOKENS must be 256..16384")
        if self.local_research_concurrency and self.local_research_concurrency not in ("1", "2"):
            errs.append("FIN_WORKSPACE_LOCAL_RESEARCH_CONCURRENCY must be 1 or 2")
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

    def egress_network_name(self, slug: str) -> str:
        return f"fin_ws_{slug}_egress"

    def volume_name(self, slug: str) -> str:
        return f"fin_ws_{slug}_data"

    def image_present(self, image: str) -> bool:
        try:
            result = self._run("image", "inspect", image, check=False)
            return result.returncode == 0
        except Exception:
            return False

    # ── Container identity / labels ──────────────────────────────────────
    def container_inspect(self, name: str) -> dict | None:
        result = self._run("inspect", name, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data[0] if isinstance(data, list) and data else None

    def container_state(self, name: str) -> str:
        result = self._run("inspect", "--format", "{{.State.Status}}", name, check=False)
        state = result.stdout.strip() if result.returncode == 0 else "absent"
        return state or "absent"

    def container_labels(self, name: str) -> dict[str, str]:
        info = self.container_inspect(name)
        if info is None:
            return {}
        return info.get("Config", {}).get("Labels", {}) or {}

    def container_networks(self, name: str) -> list[str]:
        info = self.container_inspect(name)
        if info is None:
            return []
        nets = info.get("NetworkSettings", {}).get("Networks", {}) or {}
        return sorted(nets.keys())

    def container_running(self, name: str) -> bool:
        return self.container_state(name) == "running"

    def assert_owned_container(self, name: str, slug: str, image: str) -> None:
        """Fail closed unless the container we are about to mutate is ours:
        its labels must name this exact slug/image. A foreign container with a
        colliding name must never be stopped/removed."""
        labels = self.container_labels(name)
        if not labels:
            raise RuntimeError(f"container {name} has no provider labels; refusing to mutate")
        if labels.get(_LABEL_SLUG) != slug:
            raise RuntimeError(
                f"container {name} label slug {labels.get(_LABEL_SLUG)!r} != {slug!r}; refusing to mutate"
            )
        if image and labels.get(_LABEL_IMAGE) and labels[_LABEL_IMAGE] != image:
            raise RuntimeError(
                f"container {name} label image {labels.get(_LABEL_IMAGE)!r} != {image!r}; refusing to mutate"
            )

    # ── Networks ─────────────────────────────────────────────────────────
    def network_exists(self, name: str) -> bool:
        result = self._run("network", "inspect", name, check=False)
        return result.returncode == 0

    def ensure_network(self, slug: str, image: str) -> bool:
        net = self.network_name(slug)
        if self.network_exists(net):
            return True
        created = self._run(
            "network", "create",
            "--internal",
            "--label", f"{_LABEL_SLUG}={slug}",
            "--label", f"{_LABEL_IMAGE}={image}",
            net,
            check=False,
        )
        if created.returncode != 0 and "already exists" not in (created.stderr or ""):
            _log.error("network create failed for %s: %s", net, created.stderr)
            return False
        return True

    def ensure_egress_network(self, slug: str, image: str) -> bool:
        net = self.egress_network_name(slug)
        if self.network_exists(net):
            return True
        created = self._run(
            "network", "create",
            "--label", f"{_LABEL_SLUG}={slug}",
            "--label", f"{_LABEL_IMAGE}={image}",
            net,
            check=False,
        )
        if created.returncode != 0 and "already exists" not in (created.stderr or ""):
            _log.error("egress network create failed for %s: %s", net, created.stderr)
            return False
        return True

    def remove_network(self, name: str) -> None:
        """Remove a per-account network, retrying briefly while Docker still
        holds an endpoint after a disconnect (fail-soft: a lingering network
        is harmless and the next wake reuses it)."""
        for _attempt in range(5):
            result = self._run("network", "rm", name, check=False)
            if result.returncode == 0:
                return
            stderr = result.stderr or ""
            if "not found" in stderr:
                return  # already gone
            time.sleep(1)
        _log.warning("network %s could not be removed (endpoints may linger)", name)

    def connect_shared_services(self, slug: str, control_container: str, mcp_container: str) -> bool:
        """Attach the control plane AND the shared MCP container to the
        per-account private network so both can reach the account runtime
        (control proxy) and the runtime can reach the shared MCP (research)."""
        for container in (control_container, mcp_container):
            if not self.container_running(container):
                _log.error("connect_shared_services: %s is not running", container)
                return False
            self._run("network", "connect", self.network_name(slug), container, check=False)
        return True

    def disconnect_shared_services(self, slug: str, control_container: str, mcp_container: str) -> None:
        for container in (control_container, mcp_container):
            self._run(
                "network", "disconnect", self.network_name(slug), container,
                check=False,
            )

    # ── Checkpoint file helpers ──────────────────────────────────────────
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

    # ── Runtime container ────────────────────────────────────────────────
    def start_runtime(self, cfg: ProviderConfig, slug: str, control_token: str, generation: str) -> bool:
        name = self.container_name(slug)
        state = self.container_state(name)
        if state == "running":
            # Already running — but verify it is OUR container for this slug.
            self.assert_owned_container(name, slug, cfg.app_image)
            return True
        if state != "absent":
            # stale container (exited/dead) — replace it deterministically
            self._run("rm", "-f", name, check=False)

        env = [
            # The exact private-workspace contract — nothing else leaks in.
            "-e", "HOST=0.0.0.0",
            "-e", f"PORT={cfg.app_port}",
            "-e", "NODE_ENV=production",
            "-e", "MARKET_ROOT=/app",
            "-e", "MARKET_DATA_DIR=/data/market-terminal",
            "-e", "PI_CODING_AGENT_DIR=/data/pi-agent",
            "-e", "PUBLIC_BASE_PATH=/fin-terminal/",
            "-e", "TERMINAL_RUNTIME_MODE=private-workspace",
            "-e", "FINANCIAL_WORKSPACE_CHECKPOINTS=1",
            "-e", f"FIN_WORKSPACE_CHECKPOINT_FILE={cfg.checkpoint_file}",
            "-e", f"FIN_WORKSPACE_CONTROL_TOKEN={control_token}",
            "-e", f"FIN_WORKSPACE_SESSION_ID={slug}",
            "-e", f"TERMINAL_RUNTIME_WORKER_GENERATION={generation}",
            "-e", f"MARKET_PROXY_TOKEN={cfg.proxy_token}",
            "-e", f"ALLOWED_ORIGINS={cfg.allowed_origins}",
            "-e", "UNBROWSER_MCP_REQUIRED=1",
            "-e", f"UNBROWSER_MCP_URL={cfg.mcp_url}",
            "-e", f"MARKET_MODEL_PROVIDER={cfg.model_provider}",
            "-e", "MARKET_MODEL_ID=" + (cfg.model_id or cfg.openrouter_model),
            "-e", f"OPENROUTER_MODEL={cfg.openrouter_model}",
            "-e", f"OPENROUTER_API_KEY={cfg.openrouter_api_key}",
            "-e", f"MARKET_MAX_OUTPUT_TOKENS={cfg.max_output_tokens}",
            "-e", "MARKET_RESEARCH_CONCURRENCY=1",
        ]
        if cfg.local_research_concurrency:
            env.append("-e")
            env.append(f"FIN_WORKSPACE_LOCAL_RESEARCH_CONCURRENCY={cfg.local_research_concurrency}")

        cmd = [
            "run", "-d",
            "--name", name,
            "--network", self.network_name(slug),
            "--network", self.egress_network_name(slug),
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
            "--label", f"{_LABEL_SLUG}={slug}",
            "--label", f"{_LABEL_IMAGE}={cfg.app_image}",
            "--label", f"{_LABEL_GENERATION}={generation}",
            "--label", f"{_LABEL_SESSION}={slug}",
            *env,
            cfg.app_image,
        ]
        result = self._run(*cmd, check=False)
        if result.returncode != 0:
            _log.error("runtime start failed for %s: %s", slug, result.stderr)
            return False
        return True

    def stop_runtime(self, slug: str, control_container: str, mcp_container: str, image: str) -> bool:
        name = self.container_name(slug)
        state = self.container_state(name)
        if state == "absent":
            self.disconnect_shared_services(slug, control_container, mcp_container)
            self.remove_network(self.network_name(slug))
            self.remove_network(self.egress_network_name(slug))
            return True
        # Verify identity BEFORE destroying anything.
        self.assert_owned_container(name, slug, image)
        self._run("stop", "-t", "10", name, check=False)
        self._run("rm", "-f", name, check=False)
        # Cleanly detach the control plane + MCP from the per-account network
        # and remove the per-account networks (volume is preserved on sleep).
        self.disconnect_shared_services(slug, control_container, mcp_container)
        self.remove_network(self.network_name(slug))
        self.remove_network(self.egress_network_name(slug))
        return True

    def remove_runtime_data(self, slug: str) -> bool:
        self._run("volume", "rm", "-f", self.volume_name(slug), check=False)
        return True

    # ── Image contract probe ─────────────────────────────────────────────
    def probe_image_contract(self, image: str) -> dict:
        """Run a one-off container from the pinned image that inspects the
        BUILT artifacts and asserts the exact app/image contract the control
        plane depends on: live build mode, /fin-terminal/ base path,
        private-workspace mode, and the checkpoint-export path. This is a real
        probe of the pinned image content — not a manual boolean."""
        script = r"""
const fs = require('fs');
const results = {};
function has(p) { try { fs.accessSync(p); return true; } catch { return false; } }
results.indexPresent = has('/app/dist-web/index.html');
if (results.indexPresent) {
  const html = fs.readFileSync('/app/dist-web/index.html', 'utf8');
  results.buildModeLive = /<meta name="x-build-mode" content="live"/.test(html);
  results.basePathFinTerminal = html.includes('/fin-terminal/assets/') || html.includes('href="/fin-terminal/');
}
results.runtimeModeJsPresent = has('/app/dist-server/server/runtime-mode.js');
if (results.runtimeModeJsPresent) {
  results.privateWorkspaceMode = fs.readFileSync('/app/dist-server/server/runtime-mode.js', 'utf8').includes('private-workspace');
}
const exportModule = '/app/dist-server/shared/financial-workspace-checkpoint.js';
results.exportModulePresent = has(exportModule);
if (results.exportModulePresent) {
  try {
    const mod = require(exportModule);
    results.exportPath = mod.CHECKPOINT_EXPORT_PATH || null;
  } catch (e) { results.exportPathError = String(e); }
}
process.stdout.write(JSON.stringify(results));
"""
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "--entrypoint", "node",
                    image, "-e", script,
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except Exception as exc:
            return {"probed": False, "error": str(exc)}
        if result.returncode != 0:
            return {"probed": False, "error": (result.stderr or result.stdout or "probe failed").strip()[:500]}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"probed": False, "error": "probe returned non-JSON"}
        ok = bool(
            data.get("indexPresent")
            and data.get("buildModeLive")
            and data.get("basePathFinTerminal")
            and data.get("runtimeModeJsPresent")
            and data.get("privateWorkspaceMode")
            and data.get("exportModulePresent")
            and data.get("exportPath") == "/internal/financial-workspace/checkpoint-export"
        )
        return {
            "probed": True,
            "ok": ok,
            "buildMode": "live" if ok else None,
            "basePath": "/fin-terminal/" if ok else None,
            "exportPath": data.get("exportPath"),
        }


# ---------------------------------------------------------------------------
# Provider core
# ---------------------------------------------------------------------------
class WorkspaceRuntimeProvider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.docker = Docker()
        # slug -> generation string the runtime was started with.
        self._generations: dict[str, str] = {}
        # slug -> sha256 of the last checkpoint content durably acknowledged
        # (either the file we wrote at wake or the last export the control
        # plane accepted). Used as the fail-closed file fallback gate.
        self._durable_hashes: dict[str, str] = {}
        self._probe_cache: dict[str, dict] = {}
        self._probe_cached_at: float = 0.0
        self._probe_ttl = 60.0
        os.makedirs(self.cfg.state_dir, exist_ok=True)

    def _state_path(self, slug: str) -> Path:
        return Path(self.cfg.state_dir) / f"{slug}.json"

    def _load_state(self, slug: str) -> dict:
        path = self._state_path(slug)
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception:
            pass
        return {}

    def _save_state(self, slug: str, state: dict) -> None:
        path = self._state_path(slug)
        try:
            path.write_text(json.dumps(state, separators=(",", ":")))
        except Exception as exc:
            _log.error("state save failed for %s: %s", slug, exc)

    # ── Capability health (real image-contract probe) ────────────────────
    def probe_image_contract(self) -> dict:
        """Real probe of the pinned image contract, cached per image."""
        image = self.cfg.app_image
        now = time.time()
        cached = self._probe_cache.get(image)
        if cached and now - self._probe_cached_at < self._probe_ttl:
            return cached
        if not self.docker.image_present(image):
            result = {"probed": False, "ok": False, "error": "pinned image not present locally"}
        else:
            result = self.docker.probe_image_contract(image)
        self._probe_cache[image] = result
        self._probe_cached_at = now
        return result

    def health(self) -> dict:
        probe = self.probe_image_contract()
        # Capability is tied to a real probe of the pinned image contract; the
        # operator prerequisite flag alone never turns the capability on.
        capable = bool(self.cfg.app_capable and probe.get("ok"))
        return {
            "status": "ok",
            "provider": "host-side-v1",
            "image": self.cfg.app_image,
            "capabilities": {
                "accountRuntime": capable,
                "checkpointFile": capable,
            },
            "imageContract": {
                "probed": probe.get("probed", False),
                "ok": probe.get("ok", False),
                "buildMode": probe.get("buildMode"),
                "basePath": probe.get("basePath"),
                "exportPath": probe.get("exportPath"),
                "error": probe.get("error"),
            },
        }

    # ── Status ───────────────────────────────────────────────────────────
    def status(self, slug: str) -> dict | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        name = self.docker.container_name(slug)
        state = self.docker.container_state(name)
        labels = self.docker.container_labels(name)
        return {
            "slug": slug,
            "container": name,
            "state": state,
            "image": self.cfg.app_image,
            "networks": self.docker.container_networks(name),
            "generation": labels.get(_LABEL_GENERATION, ""),
        }

    # ── Wake ─────────────────────────────────────────────────────────────
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
        probe = self.probe_image_contract()
        if not probe.get("ok"):
            _log.error(
                "wake refused for %s: pinned image %s failed the image-contract probe: %s",
                slug, self.cfg.app_image, probe.get("error") or "capability not probed",
            )
            return None
        if not self.docker.image_present(self.cfg.app_image):
            _log.error("wake refused for %s: pinned image %s not present", slug, self.cfg.app_image)
            return None
        if not self.docker.ensure_network(slug, self.cfg.app_image):
            return None
        if not self.docker.ensure_egress_network(slug, self.cfg.app_image):
            return None
        if not self.docker.write_checkpoint_file(slug, self.cfg.app_image, checkpoint):
            return None
        # Durable acknowledgement of the file we just wrote: a later flush can
        # only fall back to the file when its content still matches this.
        self._durable_hashes[slug] = hashlib.sha256(
            json.dumps(checkpoint, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        generation = f"gen-{secrets.token_hex(12)}"
        if not self.docker.start_runtime(self.cfg, slug, control_token, generation):
            return None
        if not self.docker.connect_shared_services(
            slug, self.cfg.control_container, self.cfg.mcp_container
        ):
            return None
        self._generations[slug] = generation
        self._save_state(slug, {
            "generation": generation,
            "durable_hash": self._durable_hashes[slug],
        })
        return self.status(slug)

    # ── Flush (export from the RUNNING runtime, then S2S persist) ────────
    def flush(self, slug: str, control_url: str, control_token: str) -> dict:
        """Export the RUNNING runtime's authoritative checkpoint, then persist
        it to the control plane (S2S).

        ``control_url`` is the control-plane S2S base the control plane sends
        (default ``http://fin-terminal-workspace-control:8790``). The host can
        never resolve that Docker-internal name; only its PORT is used — the
        persist request is executed inside the control container on loopback.
        """
        if not _SLUG_RE.fullmatch(slug):
            return {"ok": False, "reason": "invalid slug"}
        if not control_url or not control_token:
            return {"ok": False, "reason": "control url/token required"}

        name = self.docker.container_name(slug)
        if self.docker.container_running(name):
            self.docker.assert_owned_container(name, slug, self.cfg.app_image)
            checkpoint = self._export_from_runtime(slug, control_token)
            if checkpoint is None:
                return {"ok": False, "reason": "runtime export failed; refusing stale file fallback"}
        else:
            checkpoint = self._file_fallback(slug)
            if checkpoint is None:
                return {"ok": False, "reason": "no checkpoint file and no running runtime"}
        if not isinstance(checkpoint, dict):
            return {"ok": False, "reason": "exported checkpoint is not an object"}

        result = self._persist_to_control_plane(slug, checkpoint, control_url, control_token)
        if result.get("ok"):
            self._durable_hashes[slug] = hashlib.sha256(
                json.dumps(checkpoint, separators=(",", ":"), default=str).encode()
            ).hexdigest()
            state = self._load_state(slug)
            state["durable_hash"] = self._durable_hashes[slug]
            self._save_state(slug, state)
        return result

    def _export_from_runtime(self, slug: str, control_token: str) -> dict | None:
        """Ask the running app runtime for its current authoritative
        checkpoint (authenticated with proxy + control tokens).

        The provider is host-side and the runtime sits on a per-account
        Docker-internal network, so the host cannot resolve the container
        name. The request is made INSIDE the runtime's own network namespace
        via ``docker exec`` (payload over stdin, no shell interpolation) —
        exactly the pattern the reconciler uses for the gateway's private
        management API.
        """
        name = self.docker.container_name(slug)
        generation = self._generations.get(slug, "")
        labels = self.docker.container_labels(name)
        if not generation:
            generation = labels.get(_LABEL_GENERATION, "")
        if not generation:
            return None
        body = json.dumps({
            "sessionId": slug,
            "generation": worker_generation_epoch(generation),
        })
        token_lit = json.dumps(control_token)
        proxy_lit = json.dumps(self.cfg.proxy_token)
        slug_lit = json.dumps(slug)
        port = int(self.cfg.app_port)
        script = (
            "const http = require('http');"
            "let d = '';"
            "process.stdin.setEncoding('utf8');"
            "process.stdin.on('data', (c) => d += c);"
            "process.stdin.on('end', () => {"
            "  const opts = {"
            "    hostname: '127.0.0.1', port: %d,"
            "    path: '/internal/financial-workspace/checkpoint-export',"
            "    method: 'POST',"
            "    headers: {"
            "      'Content-Type': 'application/json',"
            "      'X-Fin-Terminal-Proxy-Token': %s,"
            "      'X-Fin-Terminal-Control-Token': %s,"
            "      'Content-Length': Buffer.byteLength(d)"
            "    },"
            "    timeout: 30000"
            "  };"
            "  const req = http.request(opts, (res) => {"
            "    let out = '';"
            "    res.on('data', (c) => out += c);"
            "    res.on('end', () => {"
            "      if (res.statusCode !== 200) { process.exit(1); return; }"
            "      process.stdout.write(out);"
            "      process.exit(0);"
            "    });"
            "  });"
            "  req.on('error', () => process.exit(1));"
            "  req.write(d);"
            "  req.end();"
            "});"
        ) % (port, proxy_lit, token_lit)
        result = subprocess.run(
            ["docker", "exec", "-i", name, "node", "-e", script],
            input=body,
            capture_output=True, text=True, timeout=45, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _log.error("runtime export failed for %s: %s", slug, result.stderr.strip()[:300])
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        checkpoint = payload.get("checkpoint") if isinstance(payload, dict) else None
        return checkpoint if isinstance(checkpoint, dict) else None

    def _file_fallback(self, slug: str) -> dict | None:
        """Read the checkpoint file, but ONLY when its content is durably
        acknowledged (equals the last snapshot we wrote or persisted). Never
        blindly persist an unacknowledged file: a partial write or a foreign
        edit must fail closed instead of overwriting a good snapshot."""
        expected = self._durable_hashes.get(slug)
        state = self._load_state(slug)
        if expected is None:
            expected = state.get("durable_hash", "")
        checkpoint = self.docker.read_checkpoint_file(slug, self.cfg.app_image)
        if checkpoint is None:
            return None
        if not expected:
            return None  # no durable acknowledgement recorded → fail closed
        actual = hashlib.sha256(
            json.dumps(checkpoint, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        if actual != expected:
            _log.error(
                "flush fallback refused for %s: checkpoint file changed since the last "
                "durably acknowledged snapshot",
                slug,
            )
            return None
        return checkpoint

    def _control_port_from_url(self, control_url: str) -> int:
        """Derive the control-plane listener port from the S2S base URL.

        The host can never resolve the Docker-internal control service name;
        only the PORT is used (the request itself is executed inside the
        control container on its loopback). Defaults to the configured
        ``FIN_WORKSPACE_RUNTIME_CONTROL_PORT`` (8790)."""
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit((control_url or "").strip())
            if parsed.port:
                return parsed.port
        except Exception:
            pass
        return int(self.cfg.control_port)

    def _persist_to_control_plane(
        self, slug: str, checkpoint: dict, control_url: str, control_token: str
    ) -> dict:
        """Persist the flushed checkpoint to the control plane S2S endpoint.

        The control plane is Docker-internal only (``fin-terminal-workspace-control``)
        and never publishes a host port, so the host cannot resolve its name.
        The S2S request is therefore executed INSIDE the control-plane
        container via ``docker exec -i`` against its loopback
        (``127.0.0.1:<control_port>/internal/financial-workspace/runtime/flush``)
        — the same pattern the reconciler uses for the gateway's private
        management API. The payload travels on bounded stdin; the token is
        JSON-escaped into the JS string literal so it never appears in argv,
        the shell, or the logs.
        """
        try:
            body = json.dumps(
                {"slug": slug, "checkpoint": checkpoint},
                separators=(",", ":"), default=str,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return {"ok": False, "reason": "checkpoint not JSON-serializable"}
        if len(body) > _MAX_FLUSH_PAYLOAD_BYTES:
            return {"ok": False, "reason": f"flush payload exceeds {_MAX_FLUSH_PAYLOAD_BYTES} bytes"}

        port = self._control_port_from_url(control_url)
        token_lit = json.dumps(control_token)
        script = (
            "const http = require('http');"
            "let d = '';"
            "process.stdin.setEncoding('utf8');"
            "process.stdin.on('data', (c) => d += c);"
            "process.stdin.on('end', () => {"
            "  const opts = {"
            "    hostname: '127.0.0.1', port: %d,"
            "    path: '/internal/financial-workspace/runtime/flush',"
            "    method: 'POST',"
            "    headers: {"
            "      'Content-Type': 'application/json',"
            "      'Authorization': 'Bearer ' + %s,"
            "      'Content-Length': Buffer.byteLength(d)"
            "    },"
            "    timeout: 30000"
            "  };"
            "  const req = http.request(opts, (res) => {"
            "    let out = '';"
            "    res.on('data', (c) => out += c);"
            "    res.on('end', () => {"
            "      if (res.statusCode !== 200) { process.exit(1); return; }"
            "      process.stdout.write(out);"
            "      process.exit(0);"
            "    });"
            "  });"
            "  req.on('error', () => process.exit(1));"
            "  req.write(d);"
            "  req.end();"
            "});"
        ) % (port, token_lit)
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", self.cfg.control_container, "node", "-e", script],
                input=body.decode("utf-8"),
                capture_output=True, text=True, timeout=45, check=False,
            )
        except Exception as exc:
            _log.error("flush failed for %s: %s", slug, exc)
            return {"ok": False, "reason": str(exc)}
        if result.returncode != 0:
            _log.error(
                "control-plane flush failed for %s: %s",
                slug, (result.stderr or result.stdout or "").strip()[:300],
            )
            return {"ok": False, "reason": "control plane flush request failed"}
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": True}
        return payload if isinstance(payload, dict) else {"ok": True}

    # ── Sleep / delete ───────────────────────────────────────────────────
    def sleep(self, slug: str) -> dict | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        self.docker.stop_runtime(
            slug, self.cfg.control_container, self.cfg.mcp_container, self.cfg.app_image
        )
        self._generations.pop(slug, None)
        return self.status(slug)

    def delete(self, slug: str) -> dict | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        self.sleep(slug)
        self.docker.remove_runtime_data(slug)
        path = self._state_path(slug)
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        self._durable_hashes.pop(slug, None)
        return {"slug": slug, "deleted": True}


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
        if path == "/v1/probe":
            self._send(200, self.provider.probe_image_contract())
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
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/flush", path)
        if match:
            result = self.provider.flush(
                match.group(1),
                str(body.get("controlUrl", "http://fin-terminal-workspace-control:8790")),
                str(body.get("controlToken", "")),
            )
            self._send(200 if result.get("ok") else 502, result)
            return
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/sleep", path)
        if match:
            result = self.provider.sleep(match.group(1))
            if result is None:
                self._send(400, {"error": "invalid slug"})
            else:
                self._send(200, result)
            return
        match = re.fullmatch(r"/v1/accounts/([a-f0-9]{24})/delete", path)
        if match:
            result = self.provider.delete(match.group(1))
            if result is None:
                self._send(400, {"error": "invalid slug"})
            else:
                self._send(200, result)
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
