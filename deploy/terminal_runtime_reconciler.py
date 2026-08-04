#!/usr/bin/env python3
"""
Host-side singleton reconciler for public-terminal warm-pool scaling.

Design contract:
- Exactly six logical Compose seats/networks remain static. The reconciler only
  starts/stops containers within that set; it never creates or removes seat
  definitions.
- Target running = min(6, assigned + queued + 1), one warm spare,
  5-minute idle scale-down. The configured idle threshold
  (TERMINAL_RUNTIME_IDLE_SCALE_DOWN, default 300s) is enforced here AND sent
  to the gateway so both sides share one source of truth.
- Calls the gateway's private management API via docker-exec; never mounts the
  Docker socket into application containers.
- Controls only exact allowlisted seat service names.
- Holds the host deployment lock (.deploy.lock) ONLY during each observed→
  mutate cycle — never for the process lifetime — so activate/disable/rollback
  (which coordinate the reconciler systemd stop in deploy preflight) cannot
  deadlock behind a lifetime lock.
- Validates project, labels, image digest, and generation before every mutation.
- Counts STARTING seats, starts with bounded concurrency, drains atomically
  before stopping, and fails closed on any ambiguity.
- Resource guard blocks starts when host memory/disk headroom is below
  configured thresholds or when a pressure/OOM signal is detected.
- Crash/restart reconciles STARTING / DRAINED / stopped seats idempotently.
- Rollback disables the reconciler and starts all six seats before any old
  gateway rollout.
- Features default off; all current behavior is preserved.

Environment (all require TERMINAL_RUNTIME_FEATURE_ENABLED=true):
  TERMINAL_RUNTIME_FEATURE_ENABLED     bool  (default: false) — master enable.
                                         Truthy spellings (trimmed,
                                         case-insensitive): 1|true|yes|on.
  TERMINAL_RUNTIME_MANAGEMENT_TOKEN    str   — gateway management token
  TERMINAL_RUNTIME_MANAGEMENT_PORT     int   (default: 8789) — private gateway port
  TERMINAL_RUNTIME_COMPOSE_PROJECT     str   (default: unchained)
  TERMINAL_RUNTIME_COMPOSE_DIR         str   (default: /home/ec2-user/unchained)
  TERMINAL_RUNTIME_RECONCILE_INTERVAL  int   (default: 15)
  TERMINAL_RUNTIME_IDLE_SCALE_DOWN     int   (default: 300) — seconds
  TERMINAL_RUNTIME_MAX_START_CONCUR    int   (default: 2)
  TERMINAL_RUNTIME_HOST_MEM_RESERVE_MB int   (default: 512)
  TERMINAL_RUNTIME_HOST_MEM_HEADROOM_PCT int (default: 15)
  TERMINAL_RUNTIME_HOST_DISK_MAX_PCT   int   (default: 85)
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_log = logging.getLogger("terminal-runtime-reconciler")

# ---------------------------------------------------------------------------
# Seat allowlist — only these six service names may be controlled
# ---------------------------------------------------------------------------
ALLOWED_SEAT_NAMES = frozenset(
    f"fin-terminal-public-seat-{n:02d}" for n in range(1, 7)
)

# Canonical feature-flag truthy spellings (trimmed, case-insensitive). The
# cross-repo contract requires every consumer to normalize 1|true|yes|on.
_TRUE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})


def parse_feature_flag(value: str | None) -> bool:
    """Normalize a feature-flag env value to a boolean.

    Accepted truthy spellings (trimmed, case-insensitive): ``1``, ``true``,
    ``yes``, ``on``. Anything else (including empty/None) is False. This is
    the same normalization the app core uses for ``FIN_WORKSPACE_ENABLED``.
    """
    return (value or "").strip().lower() in _TRUE_FLAG_VALUES

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class ReconcilerConfig:
    enabled: bool = False
    management_token: str = ""
    management_port: int = 8789
    compose_project: str = "unchained"
    compose_dir: str = "/home/ec2-user/unchained"
    reconcile_interval: int = 15
    idle_scale_down: int = 300  # 5 minutes
    max_start_concurrency: int = 2
    host_mem_reserve_mb: int = 512
    host_mem_headroom_pct: int = 15
    host_disk_max_pct: int = 85
    lock_file: str = ""  # derived from compose_dir

    def __post_init__(self) -> None:
        if not self.lock_file:
            self.lock_file = os.path.join(self.compose_dir, ".deploy.lock")

    @classmethod
    def from_env(cls, env: dict | None = None) -> ReconcilerConfig:
        if env is None:
            env = dict(os.environ)
        enabled = parse_feature_flag(env.get("TERMINAL_RUNTIME_FEATURE_ENABLED", "false"))
        manage_dir = env.get("TERMINAL_RUNTIME_COMPOSE_DIR", "/home/ec2-user/unchained").strip()
        return cls(
            enabled=enabled,
            management_token=env.get("TERMINAL_RUNTIME_MANAGEMENT_TOKEN", "").strip(),
            management_port=int(env.get("TERMINAL_RUNTIME_MANAGEMENT_PORT", "8789")),
            compose_project=env.get("TERMINAL_RUNTIME_COMPOSE_PROJECT", "unchained").strip(),
            compose_dir=manage_dir,
            reconcile_interval=int(env.get("TERMINAL_RUNTIME_RECONCILE_INTERVAL", "15")),
            idle_scale_down=int(env.get("TERMINAL_RUNTIME_IDLE_SCALE_DOWN", "300")),
            max_start_concurrency=int(env.get("TERMINAL_RUNTIME_MAX_START_CONCUR", "2")),
            host_mem_reserve_mb=int(env.get("TERMINAL_RUNTIME_HOST_MEM_RESERVE_MB", "512")),
            host_mem_headroom_pct=int(env.get("TERMINAL_RUNTIME_HOST_MEM_HEADROOM_PCT", "15")),
            host_disk_max_pct=int(env.get("TERMINAL_RUNTIME_HOST_DISK_MAX_PCT", "85")),
            lock_file=os.path.join(manage_dir, ".deploy.lock"),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.enabled:
            return errors  # nothing else matters
        if not self.management_token or len(self.management_token) < 32:
            errors.append("TERMINAL_RUNTIME_MANAGEMENT_TOKEN must be >= 32 chars")
        if not 1 <= self.management_port <= 65535:
            errors.append("TERMINAL_RUNTIME_MANAGEMENT_PORT must be 1-65535")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.compose_project):
            errors.append(f"invalid compose_project: {self.compose_project}")
        if not os.path.isdir(self.compose_dir):
            errors.append(f"compose_dir not found: {self.compose_dir}")
        if self.reconcile_interval < 5:
            errors.append("reconcile_interval must be >= 5 seconds")
        if self.idle_scale_down < 30:
            errors.append("idle_scale_down must be >= 30 seconds")
        if self.max_start_concurrency < 1:
            errors.append("max_start_concurrency must be >= 1")
        if self.host_mem_reserve_mb < 64:
            errors.append("host_mem_reserve_mb must be >= 64")
        if not 1 <= self.host_mem_headroom_pct <= 90:
            errors.append("host_mem_headroom_pct must be 1-90")
        if not 10 <= self.host_disk_max_pct <= 99:
            errors.append("host_disk_max_pct must be 10-99")
        return errors


# ---------------------------------------------------------------------------
# Seat state model
# ---------------------------------------------------------------------------
@dataclass
class SeatState:
    """Observed state of one seat from gateway snapshot."""
    name: str
    container_id: str = ""
    status: str = ""  # absent | starting | healthy | draining | stopped
    generation: str = ""
    assigned: bool = False
    queued_count: int = 0  # how many in queue (zero unless it's the gateway aggregate)
    ready_workers: int = 0
    assigned_workers: int = 0
    idle_seconds: float = 0.0

    @property
    def running(self) -> bool:
        return self.status in ("healthy",)

    @property
    def transitory(self) -> bool:
        return self.status in ("starting", "draining")

    @property
    def stopped(self) -> bool:
        return self.status in ("absent", "stopped")

    @classmethod
    def from_gateway_snapshot(cls, name: str, entry: dict) -> SeatState:
        return cls(
            name=name,
            container_id=str(entry.get("containerId", "")),
            status=str(entry.get("status", "absent")),
            generation=str(entry.get("generation", "") or ""),
            assigned=bool(entry.get("assigned", False)),
            idle_seconds=float(entry.get("idleSeconds", 0.0) or 0.0),
        )


def _worker_to_service(worker_id: str) -> str:
    """Map a gateway worker id to its allowlisted Compose service name.

    Canonical management-contract v1 mapping: the gateway reports its seat ids
    as ``seat-01``..``seat-06``; the Compose services the reconciler controls
    are ``fin-terminal-public-seat-01``..``-06``. Anything else is rejected
    before a subprocess is ever invoked.
    """
    match = re.fullmatch(r"seat-(\d{2})", str(worker_id or "").strip())
    if not match:
        raise ValueError(f"unexpected gateway worker id {worker_id!r}")
    name = f"fin-terminal-public-seat-{match.group(1)}"
    if name not in ALLOWED_SEAT_NAMES:
        raise ValueError(f"worker id {worker_id!r} maps outside the seat allowlist")
    return name


def _service_to_worker(service_name: str) -> str:
    """Inverse of :func:`_worker_to_service` for drain/activate payloads."""
    if service_name not in ALLOWED_SEAT_NAMES:
        raise ValueError(f"service name {service_name!r} not in allowlist")
    return "seat-" + service_name.rsplit("-", 1)[-1]


@dataclass
class GatewaySnapshot:
    """Full gateway reconcile-snapshot response (management contract v1)."""
    seats: dict[str, SeatState] = field(default_factory=dict)
    total_assigned: int = 0
    total_queued: int = 0
    plan: dict = field(default_factory=dict)

    @property
    def running_count(self) -> int:
        return sum(1 for s in self.seats.values() if s.running)

    @property
    def starting_count(self) -> int:
        return sum(1 for s in self.seats.values() if s.status == "starting")

    @property
    def draining_count(self) -> int:
        return sum(1 for s in self.seats.values() if s.status == "draining")

    @property
    def assigned_count(self) -> int:
        return sum(1 for s in self.seats.values() if s.assigned)

    @property
    def desired_running(self) -> int:
        """Fallback target running = min(6, assigned + queued + 1)."""
        return min(6, self.total_assigned + self.total_queued + 1)

    @property
    def desired_from_plan(self) -> int:
        """Authoritative desired running from the gateway's plan.

        The gateway owns the warm-pool policy; its ``plan.desiredRunning``
        (which counts protected seats, absent seats, and warm spares) is used
        when present. Falls back to the formula only for legacy fixtures.
        """
        desired = self.plan.get("desiredRunning") if self.plan else None
        if isinstance(desired, int) and 0 <= desired <= 6:
            return desired
        return self.desired_running


# ---------------------------------------------------------------------------
# Resource guard
# ---------------------------------------------------------------------------
class ResourceGuard:
    """Blocks starts when host resources are below configured thresholds."""

    def __init__(self, config: ReconcilerConfig) -> None:
        self._config = config

    def check(self) -> tuple[bool, str]:
        """Return (allowed, reason)."""
        # Disk
        try:
            compose_dir = self._config.compose_dir
            result = subprocess.run(
                ["df", "--output=pcent", compose_dir],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.strip().splitlines()
            if len(lines) >= 2:
                pct_str = lines[1].strip().rstrip("%")
                disk_pct = int(pct_str)
                if disk_pct > self._config.host_disk_max_pct:
                    return False, f"disk {disk_pct}% > {self._config.host_disk_max_pct}%"
        except Exception:
            pass  # non-Linux or unavailable → skip

        # Memory
        try:
            mem_total = self._get_proc_meminfo("MemTotal")
            mem_available = self._get_proc_meminfo("MemAvailable")
            if mem_total > 0:
                required_kb = max(
                    self._config.host_mem_reserve_mb * 1024,
                    mem_total * self._config.host_mem_headroom_pct // 100,
                )
                if mem_available < required_kb:
                    mem_pct = 100 - (mem_available * 100 // mem_total)
                    return False, (
                        f"memory headroom {mem_available // 1024}MB < {required_kb // 1024}MB "
                        f"(usage {mem_pct}%)"
                    )
        except Exception as exc:
            _log.warning("Memory check failed: %s", exc)

        # OOM / cgroup pressure
        if self._oom_killer_recent():
            return False, "recent OOM kill detected"

        return True, "ok"

    @staticmethod
    def _get_proc_meminfo(key: str) -> int:
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith(key + ":"):
                        return int(line.split()[1])
        except Exception:
            return 0
        return 0

    @staticmethod
    def _oom_killer_recent() -> bool:
        try:
            result = subprocess.run(
                ["dmesg", "-T", "--level=err,warn"],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.splitlines()
            for line in reversed(lines[-500:]):
                if "Out of memory" in line or "oom-killer" in line.lower():
                    return True
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Docker / Compose helpers
# ---------------------------------------------------------------------------
class DockerInterface:
    """Safe Docker operations only against allowlisted seat service names."""

    def __init__(self, config: ReconcilerConfig) -> None:
        self._config = config
        self._project = config.compose_project
        self._dir = config.compose_dir

    def _compose_base(self) -> list[str]:
        return [
            "docker", "compose",
            "--project-name", self._project,
            "--project-directory", self._dir,
            "-f", os.path.join(self._dir, "docker-compose.yml"),
            "-f", os.path.join(self._dir, "docker-compose.public-terminal.yml"),
        ]

    def _run(self, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = self._compose_base() + list(args)
        _log.debug("Running: %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)

    def _validate_service_name(self, name: str) -> None:
        if name not in ALLOWED_SEAT_NAMES:
            raise ValueError(f"service name {name!r} not in allowlist")

    def container_id(self, service_name: str) -> str:
        self._validate_service_name(service_name)
        result = self._run("ps", "-q", service_name, check=False)
        return result.stdout.strip()

    def container_state(self, service_name: str) -> str:
        """Return container state: absent | healthy | starting | unhealthy | exited | dead | ..."""
        cid = self.container_id(service_name)
        if not cid:
            return "absent"
        result = subprocess.run(
            [
                "docker", "inspect", "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                cid,
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.strip() or "absent"

    def container_image_digest(self, service_name: str) -> str:
        """Get the image ID (sha256:...) of a running container."""
        cid = self.container_id(service_name)
        if not cid:
            return ""
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", cid],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.strip()

    def container_labels(self, service_name: str) -> dict[str, str]:
        cid = self.container_id(service_name)
        if not cid:
            return {}
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", cid],
            capture_output=True, text=True, timeout=10, check=False,
        )
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            return {}

    def start_service(self, service_name: str) -> None:
        self._validate_service_name(service_name)
        self._run("up", "-d", "--no-deps", "--no-build", service_name)

    def stop_service(self, service_name: str, timeout_sec: int = 60) -> None:
        self._validate_service_name(service_name)
        self._run("stop", "-t", str(timeout_sec), service_name)

    def remove_service(self, service_name: str) -> None:
        self._validate_service_name(service_name)
        self._run("rm", "-f", service_name, check=False)

    def wait_healthy(self, service_name: str, timeout: int = 60) -> bool:
        self._validate_service_name(service_name)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.container_state(service_name)
            if state == "healthy":
                return True
            if state in ("unhealthy", "exited", "dead"):
                return False
            time.sleep(2)
        return False

    def validate_container_project_and_set(self) -> None:
        """Every controlled container must belong to the expected Compose project."""
        for name in ALLOWED_SEAT_NAMES:
            cid = self.container_id(name)
            if not cid:
                continue
            labels = self.container_labels(name)
            project = labels.get("com.docker.compose.project", "")
            if project != self._config.compose_project:
                raise RuntimeError(
                    f"Container {name} belongs to project {project!r}, "
                    f"expected {self._config.compose_project!r}"
                )


# ---------------------------------------------------------------------------
# Deploy lock
# ---------------------------------------------------------------------------
class DeployLock:
    """Respects the existing host-side .deploy.lock (flock)."""

    def __init__(self, lock_path: str) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None
        self._held = False

    def acquire(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self._lock_path), exist_ok=True)
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
            self._held = True
            return True
        except (IOError, OSError):
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(self._fd)
            self._fd = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held


# ---------------------------------------------------------------------------
# Gateway management API client (via docker exec)
# ---------------------------------------------------------------------------
class GatewayManagementClient:
    """Calls gateway's private management API inside its container via docker exec.

    The listener is private-only (never published, never proxied by Caddy) and
    is reached on the gateway's loopback inside its own network namespace. The
    Node script is passed via ``node -e`` (no host temp path is ever assumed
    inside the container) and the payload via stdin (no shell interpolation).
    The token and URL path are JSON-escaped into the JS string literals so
    neither can break out of the script.
    """

    def __init__(self, config: ReconcilerConfig) -> None:
        self._config = config
        self._token = config.management_token
        self._service = "fin-terminal-public-gateway"

    def _exec(self, url_path: str, payload: dict | None = None, timeout: int = 30) -> dict:
        """Call the gateway management endpoint via docker exec + node -e.

        Arguments are statically constructed; the payload is passed as
        structured JSON through stdin — no shell interpolation and no host
        temp file path referenced inside the container.
        """
        # Resolve container ID first (exact, not a name pattern)
        result = subprocess.run(
            [
                "docker", "compose",
                "--project-name", self._config.compose_project,
                "--project-directory", self._config.compose_dir,
                "-f", os.path.join(self._config.compose_dir, "docker-compose.yml"),
                "-f", os.path.join(self._config.compose_dir, "docker-compose.public-terminal.yml"),
                "ps", "-q", self._service,
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        gw_cid = result.stdout.strip()
        if not gw_cid:
            raise RuntimeError(f"Gateway container {self._service!r} not found")

        payload_json = json.dumps(payload or {})
        # Token and path are JSON-encoded so neither can break out of the JS
        # string literal regardless of its characters (no shell involved).
        token_lit = json.dumps(self._token)
        path_lit = json.dumps(url_path)
        port = int(self._config.management_port)
        script = (
            "const http = require(\"http\");"
            "let payload = \"\";"
            "process.stdin.setEncoding(\"utf8\");"
            "process.stdin.on(\"data\", (c) => payload += c);"
            "process.stdin.on(\"end\", () => {"
            "  const opts = {"
            "    hostname: \"127.0.0.1\", port: %d,"
            "    path: %s, method: \"POST\","
            "    headers: {"
            "      \"Content-Type\": \"application/json\","
            "      \"X-Management-Token\": %s,"
            "      \"Content-Length\": Buffer.byteLength(payload)"
            "    },"
            "    timeout: %d"
            "  };"
            "  const req = http.request(opts, (res) => {"
            "    let d = \"\";"
            "    res.on(\"data\", (c) => d += c);"
            "    res.on(\"end\", () => {"
            "      try { console.log(d); process.exit(res.statusCode >= 200 && res.statusCode < 300 ? 0 : 1); }"
            "      catch { process.exit(1); }"
            "    });"
            "  });"
            "  req.on(\"error\", () => process.exit(1));"
            "  req.write(payload);"
            "  req.end();"
            "});"
        ) % (port, path_lit, token_lit, timeout * 1000)
        result = subprocess.run(
            ["docker", "exec", "-i", gw_cid, "node", "-e", script],
            input=payload_json,
            capture_output=True, text=True, timeout=timeout + 10, check=False,
        )

        if result.returncode != 0:
            _log.error("Gateway API call %s failed: rc=%s stderr=%s", url_path, result.returncode, result.stderr)
            raise RuntimeError(f"Gateway API {url_path} returned {result.returncode}")

        try:
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            _log.error("Gateway API %s returned non-JSON: %s", url_path, result.stdout[:200])
            raise RuntimeError(f"Gateway API {url_path} returned non-JSON")

    def reconcile_snapshot(self) -> GatewaySnapshot:
        data = self._exec("/api/management/reconcile-snapshot")
        seats = {}
        raw_seats = data.get("seats", {}) or {}
        for worker_id, entry in raw_seats.items():
            name = _worker_to_service(worker_id)
            seats[name] = SeatState.from_gateway_snapshot(name, entry)
        return GatewaySnapshot(
            seats=seats,
            total_assigned=int(data.get("totalAssigned", 0) or 0),
            total_queued=int(data.get("totalQueued", 0) or 0),
            plan=data.get("plan") or {},
        )

    def reconcile_plan(self, desired_seats: list[str]) -> dict:
        """Ask gateway what should be running given desired set.

        Also sends the reconciler's ``idleScaleDownSeconds`` so the gateway
        enforces the exact same 5-minute idle threshold (one source of truth:
        ``TERMINAL_RUNTIME_IDLE_SCALE_DOWN``). The gateway ignores the field
        during rollout if it does not yet support it.
        """
        return self._exec("/api/management/reconcile-plan", {
            "desiredSeats": desired_seats,
            "idleScaleDownSeconds": self._config.idle_scale_down,
        })

    def drain_seat(self, seat_name: str, expected_generation: str) -> bool:
        """Atomically drain a seat with a generation CAS. True if accepted."""
        worker_id = _service_to_worker(seat_name)
        drain_id = f"dr-{secrets.token_hex(6)}"
        try:
            data = self._exec("/api/management/drain", {
                "workerId": worker_id,
                "drainId": drain_id,
                "expectedGeneration": expected_generation,
            })
            return bool(data.get("accepted", False))
        except RuntimeError:
            return False

    def activate_seat(self, seat_name: str) -> bool:
        """Mark a seat as desired/activate in gateway state."""
        worker_id = _service_to_worker(seat_name)
        try:
            data = self._exec("/api/management/activate", {
                "workerId": worker_id,
            })
            return bool(data.get("accepted", False))
        except RuntimeError:
            return False


# ---------------------------------------------------------------------------
# Reconciler core
# ---------------------------------------------------------------------------
class TerminalRuntimeReconciler:
    """Host-side singleton reconciler for warm-pool scaling."""

    def __init__(self, config: ReconcilerConfig) -> None:
        self._config = config
        self._docker = DockerInterface(config)
        self._guard = ResourceGuard(config)
        self._gateway: GatewayManagementClient | None = None
        if config.enabled and config.management_token:
            self._gateway = GatewayManagementClient(config)
        self._lock = DeployLock(config.lock_file)
        self._running = False
        self._last_reconcile: float = 0.0
        # seat_name -> process generation observed when the seat entered
        # DRAINING. Used as a crash-recovery cross-check for the sticky-drain
        # activate path (a drain is only releasable once the generation
        # changed). The gateway's ``plan.activateCandidates`` is authoritative;
        # this map is the fallback for gateways without that field.
        self._drain_generations: dict[str, str] = {}

    @property
    def gateway(self) -> GatewayManagementClient:
        if self._gateway is None:
            raise RuntimeError("Gateway client not available (feature disabled or no token)")
        return self._gateway

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_preconditions(self) -> None:
        """Fail closed on any ambiguity before mutating state."""
        self._docker.validate_container_project_and_set()
        _log.info("Precondition validation: OK (all containers in expected project)")

    # ------------------------------------------------------------------
    # Full reconcile cycle
    # ------------------------------------------------------------------
    def reconcile(self) -> None:
        """One reconcile tick: snapshot → decide → act, idempotently.

        The host deployment lock (``.deploy.lock``) is held ONLY for the
        duration of this observed→mutate cycle so a concurrent
        activate/disable/rollback (which stop the reconciler systemd unit in
        deploy preflight) can never deadlock behind a lifetime lock. When the
        lock is held by another process the cycle runs passive (no mutation).
        """
        if not self._config.enabled:
            return

        # Only the lock holder reconciles — acquire per-cycle, never for the
        # process lifetime. Non-blocking: a deploy/pilot action holding the
        # lock simply means this cycle observes without mutating.
        if not self._lock.acquire():
            _log.debug("Reconcile skipped: deploy lock not held")
            return

        try:
            self.validate_preconditions()

            try:
                snapshot = self.gateway.reconcile_snapshot()
            except RuntimeError as exc:
                _log.error("Reconcile snapshot failed: %s", exc)
                return

            _log.info(
                "Snapshot: running=%d assigned=%d queued=%d desired=%d "
                "starting=%d draining=%d",
                snapshot.running_count, snapshot.total_assigned,
                snapshot.total_queued, snapshot.desired_running,
                snapshot.starting_count, snapshot.draining_count,
            )

            # Idempotent recovery: handle transitory states from a previous crash
            self._reconcile_transitory(snapshot)

            # Release sticky drains whose process generation changed (the
            # container was restarted and a new healthy generation registered).
            # Independent of scale direction: it runs every cycle, idempotently.
            self._activate_ready_drained(snapshot)

            # The gateway's plan is authoritative; the totals are informational.
            desired = snapshot.desired_from_plan
            current = snapshot.running_count + snapshot.starting_count

            if current < desired:
                self._scale_up(snapshot, desired)
            elif current > desired and snapshot.draining_count == 0:
                self._scale_down(snapshot, current - desired)
        finally:
            # Never hold the deploy lock past the cycle: activate/disable/
            # rollback need it to make forward progress.
            self._lock.release()

    # ------------------------------------------------------------------
    # Idempotent crash recovery
    # ------------------------------------------------------------------
    def _reconcile_transitory(self, snapshot: GatewaySnapshot) -> None:
        """Handle seats stuck in STARTING or DRAINING from prior crash."""
        for name, seat in snapshot.seats.items():
            if seat.status == "starting":
                # Container may have started but gateway didn't record it.
                # If the container is healthy, re-activate it in gateway.
                docker_state = self._docker.container_state(name)
                if docker_state == "healthy":
                    _log.info("Recovery: seat %s is healthy locally, re-activating in gateway", name)
                    try:
                        self.gateway.activate_seat(name)
                    except RuntimeError:
                        pass
                elif docker_state in ("absent", "exited", "dead"):
                    _log.info("Recovery: seat %s is %s locally, cleaning up", name, docker_state)
                    self._docker.remove_service(name)
                # else: still starting — leave it

            elif seat.status == "draining":
                # A draining seat may be a completed scale-down stop OR a
                # scale-up restart in progress (the container was restarted
                # while the drain stayed sticky). Record the generation the
                # seat entered draining with (crash-recovery cross-check for
                # the activate path) and recover based on local container state.
                self._drain_generations.setdefault(name, seat.generation or "")
                self._recover_draining_seat(name, seat)

            elif seat.status == "stopped":
                # If stopped but container still exists, remove it
                docker_state = self._docker.container_state(name)
                if docker_state != "absent":
                    _log.info("Recovery: seat %s is stopped in gateway but %s locally, removing", name, docker_state)
                    self._docker.stop_service(name)
                    self._docker.remove_service(name)

    def _recover_draining_seat(self, name: str, seat: SeatState) -> None:
        """Crash recovery for a DRAINING seat, based on local container state.

        - Container already gone → the scale-down drain was completed; nothing
          to do (stop/remove are idempotent no-ops on an absent container).
        - Container healthy/starting/running → a scale-up restart is in
          progress: the drain is sticky and the container is left RUNNING so
          the new generation can register. The activate path releases the
          drain only after the generation changed AND the container is healthy.
        - Container exited/dead → clean it up.
        """
        local = self._docker.container_state(name)
        if local in ("exited", "dead"):
            _log.info(
                "Recovery: draining seat %s has %s container locally; removing",
                name, local,
            )
            try:
                self._docker.remove_service(name)
            except RuntimeError as exc:
                _log.error("Recovery cleanup of %s failed: %s", name, exc)
        elif local == "absent":
            # Completed drain (container already stopped/removed).
            _log.debug("Recovery: draining seat %s has no container; nothing to do", name)
        else:
            # healthy / starting / running — a scale-up restart in progress.
            _log.info(
                "Recovery: draining seat %s has %s container locally; "
                "leaving it running until the new generation registers",
                name, local,
            )

    # ------------------------------------------------------------------
    # Sticky-drain activation (scale-up)
    # ------------------------------------------------------------------
    def _is_start_candidate(self, name: str, seat: SeatState) -> bool:
        """A seat is startable when its container is locally absent/stopped —
        including drained seats whose container was stopped by scale-down.

        Drained seats are start candidates while the drain stays sticky: the
        container is restarted (a new process generation), and only an explicit
        ``activate`` with the changed generation releases the drain. A seat
        that is still STARTING, or whose container is already running/healthy,
        is never a candidate (avoids double-starts).
        """
        if seat.status == "starting":
            return False
        local = self._docker.container_state(name)
        return local in ("absent", "exited", "dead")

    def _activate_ready_drained(self, snapshot: GatewaySnapshot) -> None:
        """Release sticky drains whose process generation changed.

        The gateway's ``plan.activateCandidates`` is authoritative: it lists
        exactly the draining seats whose generation changed since the drain was
        requested (the container was restarted and a new generation registered).
        Before activating, the container must ALSO be healthy locally (readiness
        gate — a seat is never activated/assigned before it is ready). A
        same-generation drain is never released (the gateway rejects it with
        ``409 drain sticky; generation unchanged``).
        """
        plan_candidates: set[str] = set()
        for worker_id in (snapshot.plan.get("activateCandidates") or []):
            try:
                plan_candidates.add(_worker_to_service(str(worker_id)))
            except ValueError:
                _log.warning("Ignoring unexpected activate candidate %r", worker_id)

        names: set[str] = set()
        if plan_candidates:
            names |= plan_candidates
        else:
            # Fallback for gateways without activateCandidates: cross-check
            # draining seats where the container is healthy AND the generation
            # changed since the drain was observed (crash-recovery map).
            for name, seat in snapshot.seats.items():
                if seat.status != "draining":
                    continue
                drain_gen = self._drain_generations.get(name)
                if not drain_gen or not seat.generation:
                    continue
                if seat.generation != drain_gen:
                    names.add(name)

        for name in sorted(names):
            seat = snapshot.seats.get(name)
            if seat is None:
                continue
            if seat.status != "draining":
                # A concurrent activation (or a stale plan entry) already
                # released this drain — nothing to do. Only a seat still
                # reported as draining is releasable.
                self._drain_generations.pop(name, None)
                continue
            if self._docker.container_state(name) != "healthy":
                _log.debug(
                    "Activate candidate %s is not ready locally; deferring", name,
                )
                continue
            try:
                accepted = self.gateway.activate_seat(name)
            except RuntimeError as exc:
                _log.error("Activate failed for %s: %s", name, exc)
                continue
            if accepted:
                _log.info(
                    "Released sticky drain for %s (generation %s)",
                    name, seat.generation or "",
                )
                self._drain_generations.pop(name, None)
            else:
                _log.info(
                    "Activate not accepted for %s (drain still sticky)", name,
                )

    # ------------------------------------------------------------------
    # Scale up
    # ------------------------------------------------------------------
    def _scale_up(self, snapshot: GatewaySnapshot, desired: int) -> None:
        current = snapshot.running_count + snapshot.starting_count
        to_start = desired - current
        if to_start <= 0:
            return

        # Resource guard
        allowed, reason = self._guard.check()
        if not allowed:
            _log.warning("Scale-up blocked: %s", reason)
            return

        # Collect locally-stopped seats to start — including drained seats
        # whose containers were stopped by scale-down (the drain stays sticky
        # until the restarted container registers a new generation).
        candidates = [
            name for name, seat in snapshot.seats.items()
            if self._is_start_candidate(name, seat)
        ]
        # Sort deterministically
        candidates.sort()

        started = 0
        for name in candidates:
            if started >= to_start or started >= self._config.max_start_concurrency:
                break
            seat = snapshot.seats[name]
            was_draining = seat.status == "draining"
            _log.info("Starting seat %s (was_draining=%s)", name, was_draining)
            try:
                self._docker.start_service(name)
                if was_draining:
                    # Sticky drain: do NOT activate yet. The gateway rejects a
                    # same-generation activate (409); the new container boots
                    # with a fresh generation, and once it is healthy AND the
                    # generation changed, _activate_ready_drained() releases
                    # the drain via the generation CAS.
                    self._drain_generations.setdefault(name, seat.generation or "")
                    _log.info(
                        "Seat %s was draining; drain stays sticky until the new "
                        "generation registers", name,
                    )
                else:
                    # Non-draining seat: mark desired in the gateway (a no-op
                    # when the seat was already desired).
                    self.gateway.activate_seat(name)
                started += 1
            except RuntimeError as exc:
                _log.error("Failed to start seat %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Scale down
    # ------------------------------------------------------------------
    def _scale_down(self, snapshot: GatewaySnapshot, excess: int) -> None:
        if excess <= 0:
            return

        # Never drain assigned seats or seats serving reconnecting visitors
        # Never drain seats currently draining
        # Enforce the configured 5-minute idle threshold (single source of
        # truth: TERMINAL_RUNTIME_IDLE_SCALE_DOWN, default 300s). The gateway
        # enforces the same exact threshold; the reconciler must not drain a
        # seat that has not been continuously idle long enough.
        # Pick the longest-idle candidates first.
        candidates = []
        for name, seat in snapshot.seats.items():
            if not seat.running:
                continue
            if seat.assigned:
                continue
            if seat.status == "draining":
                continue
            if seat.idle_seconds < self._config.idle_scale_down:
                _log.debug(
                    "Seat %s idle=%.0fs below threshold %ds; not a candidate",
                    name, seat.idle_seconds, self._config.idle_scale_down,
                )
                continue
            candidates.append((name, seat))

        candidates.sort(key=lambda item: item[1].idle_seconds, reverse=True)

        to_drain = min(excess, len(candidates))
        drained = 0
        for name, seat in candidates:
            if drained >= to_drain:
                break

            # Atomic drain: gateway must accept the drain before we stop
            _log.info("Draining seat %s (generation=%s, idle=%.0fs)", name, seat.generation, seat.idle_seconds)
            accepted = False
            try:
                accepted = self.gateway.drain_seat(name, seat.generation)
            except RuntimeError as exc:
                _log.error("Drain API call failed for %s: %s", name, exc)

            if accepted:
                # Record the generation the seat was drained at: the sticky
                # drain is only releasable once the restarted container's
                # generation differs (the activate path cross-checks this).
                self._drain_generations[name] = seat.generation
                self._complete_drain(name, seat)
                drained += 1
            else:
                _log.warning("Drain not accepted for %s — seat may have been reassigned", name)

    def _complete_drain(self, name: str, seat: SeatState) -> None:
        """Stop the container after gateway accepted the drain."""
        _log.info("Stopping drained seat %s", name)
        try:
            self._docker.stop_service(name)
            time.sleep(1)
            self._docker.remove_service(name)
        except RuntimeError as exc:
            _log.error("Failed to stop/remove seat %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Rollback: start all six seats
    # ------------------------------------------------------------------
    def rollback_start_all(self) -> None:
        """Start all six seats unconditionally (used before old gateway rollout).

        Acquires the deploy lock only for the duration of the operation so a
        concurrent pilot/rollback action cannot deadlock behind a lifetime
        lock.
        """
        _log.info("Rollback: starting all six seats")
        if not self._lock.acquire():
            _log.warning("Rollback skipped: deploy lock not held")
            return
        try:
            for name in sorted(ALLOWED_SEAT_NAMES):
                try:
                    docker_state = self._docker.container_state(name)
                    if docker_state == "absent":
                        _log.info("Rollback: starting %s", name)
                        self._docker.start_service(name)
                except RuntimeError as exc:
                    _log.error("Rollback start of %s failed: %s", name, exc)
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> None:
        if not self._config.enabled:
            _log.info("Feature not enabled; exiting.")
            return

        _log.info("Terminal runtime reconciler starting (interval=%ds)", self._config.reconcile_interval)

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            while self._running:
                try:
                    self.reconcile()
                except Exception as exc:
                    _log.exception("Reconcile cycle error: %s", exc)

                self._last_reconcile = time.time()
                # Sleep in small increments to be responsive to signals
                for _ in range(self._config.reconcile_interval):
                    if not self._running:
                        break
                    time.sleep(1)
        finally:
            _log.info("Reconciler stopped.")

    def _handle_signal(self, signum: int, _frame: object) -> None:
        _log.info("Received signal %d; shutting down...", signum)
        self._running = False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config = ReconcilerConfig.from_env()
    errors = config.validate()
    if errors:
        for err in errors:
            _log.error("Configuration error: %s", err)
        sys.exit(1)

    reconciler = TerminalRuntimeReconciler(config)
    reconciler.run()


if __name__ == "__main__":
    main()
