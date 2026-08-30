"""Session-isolating Streamable HTTP broker for hosted Unbrowser MCP.

``mcp-proxy`` intentionally keeps one stdio child alive for its entire
process lifetime.  That is useful for a local, single-user MCP client, but is
unsafe when one HTTP proxy serves multiple clients: Unbrowser retains cookies,
DOM state, and JavaScript state in that child process.

This broker gives every external MCP session a dedicated, loopback-only
``mcp-proxy -> unbrowser --mcp`` process pair.  The public
``Mcp-Session-Id`` is an opaque broker-generated bearer identifier; the
upstream proxy's session ID is never exposed to callers.  A worker is killed
on MCP DELETE, failed/disconnected requests, idle expiry, maximum lifetime, or
broker shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


LOGGER = logging.getLogger("unbrowser_mcp_router")

MCP_SESSION_HEADER = "mcp-session-id"
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
MAX_SESSION_ID_LENGTH = 512
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "last-event-id",
        "mcp-protocol-version",
        "user-agent",
    }
)
PROCESS_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "PYTHONUNBUFFERED",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class SessionCapacityError(RuntimeError):
    """Raised when the broker has no safe worker capacity remaining."""


class WorkerStartupError(RuntimeError):
    """Raised when a dedicated MCP worker cannot be started."""


@dataclass(frozen=True)
class RouterConfig:
    """Bounded lifecycle settings for dedicated Unbrowser worker processes."""

    max_sessions: int = 8
    idle_timeout_seconds: float = 120.0
    max_session_seconds: float = 900.0
    startup_timeout_seconds: float = 10.0
    worker_port_start: int = 18700
    session_root: Path = Path("/tmp/unbrowser-mcp-sessions")
    mcp_proxy_command: str = "mcp-proxy"
    unbrowser_command: str = "unbrowser"

    @classmethod
    def from_environment(cls) -> "RouterConfig":
        max_sessions = _positive_int("UNBROWSER_MCP_MAX_SESSIONS", 8, upper=64)
        worker_port_start = _positive_int(
            "UNBROWSER_MCP_WORKER_PORT_START", 18700, upper=65535
        )
        if worker_port_start < 1024 or worker_port_start + max_sessions - 1 > 65535:
            raise ValueError("UNBROWSER_MCP_WORKER_PORT_START does not leave a valid worker port range")
        return cls(
            max_sessions=max_sessions,
            idle_timeout_seconds=_positive_float(
                "UNBROWSER_MCP_IDLE_TIMEOUT_SECONDS", 120.0, upper=86_400.0
            ),
            max_session_seconds=_positive_float(
                "UNBROWSER_MCP_MAX_SESSION_SECONDS", 900.0, upper=86_400.0
            ),
            startup_timeout_seconds=_positive_float(
                "UNBROWSER_MCP_WORKER_STARTUP_TIMEOUT_SECONDS", 10.0, upper=120.0
            ),
            worker_port_start=worker_port_start,
            session_root=Path(
                os.environ.get("UNBROWSER_MCP_SESSION_ROOT", "/tmp/unbrowser-mcp-sessions")
            ),
            mcp_proxy_command=os.environ.get("UNBROWSER_MCP_PROXY_BIN", "mcp-proxy"),
            unbrowser_command=os.environ.get("UNBROWSER_MCP_BIN", "unbrowser"),
        )


@dataclass
class Worker:
    """One loopback mcp-proxy process and its private Unbrowser child."""

    port: int
    process: Any
    state_dir: Path
    created_at: float
    last_active_at: float
    public_session_id: str | None = None
    upstream_session_id: str | None = None
    active_requests: int = 0
    closing: bool = False
    stopped: bool = False


WorkerLauncher = Callable[[int, Path, dict[str, str]], Awaitable[Any]]
WorkerReadinessProbe = Callable[[Any, int], Awaitable[None]]
WorkerTerminator = Callable[[Any], Awaitable[None]]


class WorkerPool:
    """Own and route a bounded collection of one-session MCP workers."""

    def __init__(
        self,
        config: RouterConfig,
        upstream_client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        launcher: WorkerLauncher | None = None,
        readiness_probe: WorkerReadinessProbe | None = None,
        terminator: WorkerTerminator | None = None,
    ) -> None:
        self.config = config
        self._upstream_client = upstream_client
        self._clock = clock
        self._launcher = launcher or self._launch_worker
        self._readiness_probe = readiness_probe or self._wait_until_ready
        self._terminator = terminator or self._terminate_process
        self._lock = asyncio.Lock()
        self._sessions: dict[str, Worker] = {}
        self._workers: dict[int, Worker] = {}
        self._leased_ports: set[int] = set()
        self._closed = False
        self._reaper_task: asyncio.Task[None] | None = None

    async def start_background_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reap_loop())

    async def start_worker(self) -> Worker:
        """Reserve capacity, then start and health-check an isolated worker."""
        async with self._lock:
            if self._closed:
                raise WorkerStartupError("unbrowser MCP router is shutting down")
            if len(self._leased_ports) >= self.config.max_sessions:
                raise SessionCapacityError("unbrowser MCP session capacity is full")
            port = self._next_free_port_locked()
            self._leased_ports.add(port)

        state_dir: Path | None = None
        worker: Worker | None = None
        try:
            state_dir = self._create_state_dir()
            process = await self._launcher(port, state_dir, self._worker_environment(state_dir))
            now = self._clock()
            worker = Worker(
                port=port,
                process=process,
                state_dir=state_dir,
                created_at=now,
                last_active_at=now,
            )
            async with self._lock:
                self._workers[port] = worker
            await self._readiness_probe(process, port)
            return worker
        except asyncio.CancelledError:
            if worker is not None:
                await self._stop_worker(worker)
            else:
                await self._release_reservation(port, state_dir)
            raise
        except Exception as exc:  # noqa: BLE001 - never expose worker internals to callers.
            if worker is not None:
                await self._stop_worker(worker)
            else:
                await self._release_reservation(port, state_dir)
            if isinstance(exc, WorkerStartupError):
                raise
            raise WorkerStartupError("could not start an isolated unbrowser session") from exc

    async def bind_session(self, worker: Worker, upstream_session_id: str) -> Worker:
        """Associate an opaque public session ID with a successfully initialized worker."""
        if not _valid_session_id(upstream_session_id):
            raise WorkerStartupError("unbrowser worker did not return a valid MCP session ID")
        async with self._lock:
            if worker.closing or worker.stopped or self._closed:
                raise WorkerStartupError("unbrowser worker was closed during initialization")
            public_session_id = self._new_public_session_id_locked()
            worker.public_session_id = public_session_id
            worker.upstream_session_id = upstream_session_id
            worker.last_active_at = self._clock()
            self._sessions[public_session_id] = worker
            return worker

    async def acquire(self, public_session_id: str, *, closing: bool = False) -> Worker | None:
        """Reserve a worker for one proxied request, optionally closing after it."""
        async with self._lock:
            worker = self._sessions.get(public_session_id)
            if worker is None or worker.closing or worker.stopped:
                return None
            worker.active_requests += 1
            worker.last_active_at = self._clock()
            if closing:
                self._detach_locked(worker)
            return worker

    async def begin_unbound_request(self, worker: Worker) -> None:
        """Mark the initial initialize request active before it receives an ID."""
        async with self._lock:
            if worker.closing or worker.stopped:
                raise WorkerStartupError("unbrowser worker stopped during initialization")
            worker.active_requests += 1
            worker.last_active_at = self._clock()

    async def finish(self, worker: Worker, *, close: bool = False) -> None:
        """Release an active request and stop the worker when its session ends."""
        should_stop = False
        async with self._lock:
            worker.active_requests = max(0, worker.active_requests - 1)
            worker.last_active_at = self._clock()
            if close:
                self._detach_locked(worker)
                should_stop = True
        if should_stop:
            await self._stop_worker(worker)

    async def abort(self, worker: Worker) -> None:
        """Discard an incomplete or failed initialization without leaving state behind."""
        async with self._lock:
            self._detach_locked(worker)
        await self._stop_worker(worker)

    async def reap_expired(self) -> int:
        """Stop inactive workers that exceeded their idle or absolute lifetime."""
        now = self._clock()
        expired: list[Worker] = []
        async with self._lock:
            for worker in tuple(self._sessions.values()):
                if worker.closing or worker.active_requests:
                    continue
                idle = now - worker.last_active_at
                lifetime = now - worker.created_at
                if (
                    idle >= self.config.idle_timeout_seconds
                    or lifetime >= self.config.max_session_seconds
                ):
                    self._detach_locked(worker)
                    expired.append(worker)
        for worker in expired:
            await self._stop_worker(worker)
        return len(expired)

    async def snapshot(self) -> dict[str, int]:
        """Return non-sensitive health information only; never expose session IDs."""
        async with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "capacity": self.config.max_sessions,
            }

    async def close(self) -> None:
        """Terminate every child process and remove all per-session filesystem state."""
        reaper = self._reaper_task
        self._reaper_task = None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper

        async with self._lock:
            self._closed = True
            workers = list(self._workers.values())
            for worker in workers:
                self._detach_locked(worker)
        for worker in workers:
            await self._stop_worker(worker)

    async def _reap_loop(self) -> None:
        # Keep the two-minute default close to its advertised expiry without
        # continuously waking the service under normal load.
        interval = min(10.0, max(1.0, self.config.idle_timeout_seconds / 4))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_expired()
            except Exception:  # noqa: BLE001 - leave the broker available if reaping has a transient error.
                LOGGER.exception("unbrowser MCP session reaper failed")

    def _next_free_port_locked(self) -> int:
        for port in range(
            self.config.worker_port_start,
            self.config.worker_port_start + self.config.max_sessions,
        ):
            if port not in self._leased_ports:
                return port
        raise SessionCapacityError("unbrowser MCP session capacity is full")

    def _new_public_session_id_locked(self) -> str:
        while True:
            public_session_id = secrets.token_urlsafe(32)
            if public_session_id not in self._sessions:
                return public_session_id

    def _create_state_dir(self) -> Path:
        self.config.session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="session-", dir=self.config.session_root))
        path.chmod(0o700)
        for name in ("cache", "config", "data", "tmp"):
            child = path / name
            child.mkdir(mode=0o700)
        return path

    def _worker_environment(self, state_dir: Path) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in PROCESS_ENV_NAMES or key.startswith("UNBROWSER_")
        }
        environment.update(
            {
                "HOME": str(state_dir),
                "TMPDIR": str(state_dir / "tmp"),
                "XDG_CACHE_HOME": str(state_dir / "cache"),
                "XDG_CONFIG_HOME": str(state_dir / "config"),
                "XDG_DATA_HOME": str(state_dir / "data"),
            }
        )
        return environment

    async def _launch_worker(self, port: int, _state_dir: Path, environment: dict[str, str]) -> Any:
        try:
            return await asyncio.create_subprocess_exec(
                self.config.mcp_proxy_command,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--pass-environment",
                "--",
                self.config.unbrowser_command,
                "--mcp",
                stdout=asyncio.subprocess.DEVNULL,
                # Preserve child diagnostics in the sidecar log. stdout is
                # still suppressed because the MCP transport is stdio-backed;
                # stderr is the only useful signal when a worker exits before
                # returning an MCP response.
                stderr=None,
                env=environment,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise WorkerStartupError("unbrowser MCP worker executable is unavailable") from exc

    async def _wait_until_ready(self, process: Any, port: int) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.startup_timeout_seconds
        url = f"http://127.0.0.1:{port}/status"
        while asyncio.get_running_loop().time() < deadline:
            if getattr(process, "returncode", None) is not None:
                raise WorkerStartupError("unbrowser MCP worker exited during startup")
            try:
                response = await self._upstream_client.get(url, timeout=0.5)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
        raise WorkerStartupError("unbrowser MCP worker did not become ready")

    async def _terminate_process(self, process: Any) -> None:
        if getattr(process, "returncode", None) is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError, AttributeError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError, AttributeError):
                process.kill()
        with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort child cleanup.
            await process.wait()

    async def _release_reservation(self, port: int, state_dir: Path | None) -> None:
        async with self._lock:
            self._leased_ports.discard(port)
        if state_dir is not None:
            shutil.rmtree(state_dir, ignore_errors=True)

    async def _stop_worker(self, worker: Worker) -> None:
        async with self._lock:
            if worker.stopped:
                return
            worker.stopped = True
        try:
            await self._terminator(worker.process)
        finally:
            async with self._lock:
                self._sessions.pop(worker.public_session_id or "", None)
                self._workers.pop(worker.port, None)
                self._leased_ports.discard(worker.port)
            shutil.rmtree(worker.state_dir, ignore_errors=True)

    def _detach_locked(self, worker: Worker) -> None:
        worker.closing = True
        if worker.public_session_id:
            self._sessions.pop(worker.public_session_id, None)


class MCPHTTPRouter:
    """Translate one public HTTP MCP session into one dedicated local worker."""

    def __init__(self, pool: WorkerPool, upstream_client: httpx.AsyncClient) -> None:
        self.pool = pool
        self._upstream_client = upstream_client

    async def handle_mcp(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Allow": "GET, POST, DELETE, OPTIONS"})
        if request.method not in {"GET", "POST", "DELETE"}:
            return JSONResponse({"error": "unsupported MCP method"}, status_code=405)

        body = await request.body()
        if len(body) > MAX_REQUEST_BODY_BYTES:
            return JSONResponse({"error": "MCP request is too large"}, status_code=413)

        public_session_id = request.headers.get(MCP_SESSION_HEADER, "")
        if public_session_id and not _valid_session_id(public_session_id):
            return JSONResponse({"error": "invalid MCP session ID"}, status_code=400)

        worker: Worker | None = None
        created = False
        close_after_response = request.method == "DELETE"
        try:
            if public_session_id:
                worker = await self.pool.acquire(
                    public_session_id,
                    closing=close_after_response,
                )
                if worker is None:
                    return JSONResponse({"error": "unknown or expired MCP session"}, status_code=404)
            else:
                if request.method != "POST" or not _is_initialize_request(body):
                    return JSONResponse(
                        {"error": "start an MCP session with an initialize request"},
                        status_code=400,
                    )
                worker = await self.pool.start_worker()
                created = True
                await self.pool.begin_unbound_request(worker)

            upstream_response = await self._forward(worker, request, body)
            if created:
                upstream_session_id = upstream_response.headers.get(MCP_SESSION_HEADER, "")
                if not _valid_session_id(upstream_session_id):
                    await upstream_response.aclose()
                    await self.pool.abort(worker)
                    return JSONResponse(
                        {"error": "unbrowser MCP session initialization failed"},
                        status_code=502,
                    )
                await self.pool.bind_session(worker, upstream_session_id)

            return self._proxied_response(
                upstream_response,
                worker,
                close_after_response=close_after_response,
            )
        except asyncio.CancelledError:
            if worker is not None:
                await self.pool.finish(worker, close=True)
            raise
        except SessionCapacityError:
            return JSONResponse({"error": "unbrowser MCP session capacity is full"}, status_code=429)
        except WorkerStartupError as exc:
            if worker is not None:
                await self.pool.finish(worker, close=True)
            LOGGER.warning("unbrowser MCP worker startup failed: %s", str(exc)[:240])
            return JSONResponse({"error": "unbrowser MCP is temporarily unavailable"}, status_code=503)
        except httpx.HTTPError as exc:
            if worker is not None:
                await self.pool.finish(worker, close=True)
            LOGGER.warning("unbrowser MCP worker request failed: %s", str(exc)[:240])
            return JSONResponse({"error": "unbrowser MCP worker is unavailable"}, status_code=502)
        except Exception:  # noqa: BLE001 - do not surface process or session details.
            if worker is not None:
                await self.pool.finish(worker, close=True)
            LOGGER.exception("unbrowser MCP request failed")
            return JSONResponse({"error": "unbrowser MCP request failed"}, status_code=502)

    async def handle_status(self, _request: Request) -> Response:
        return JSONResponse({"status": "ok", **await self.pool.snapshot()})

    async def _forward(self, worker: Worker, request: Request, body: bytes) -> httpx.Response:
        if not worker.upstream_session_id and worker.public_session_id:
            raise WorkerStartupError("unbrowser worker has no upstream MCP session")
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in FORWARDED_REQUEST_HEADERS
        }
        if worker.upstream_session_id:
            headers[MCP_SESSION_HEADER] = worker.upstream_session_id
        url = f"http://127.0.0.1:{worker.port}/mcp"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        upstream_request = self._upstream_client.build_request(
            request.method,
            url,
            content=body,
            headers=headers,
        )
        return await self._upstream_client.send(upstream_request, stream=True)

    def _proxied_response(
        self,
        upstream_response: httpx.Response,
        worker: Worker,
        *,
        close_after_response: bool,
    ) -> StreamingResponse:
        headers = {
            name: value
            for name, value in upstream_response.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != MCP_SESSION_HEADER
        }
        if worker.public_session_id:
            headers[MCP_SESSION_HEADER] = worker.public_session_id

        async def stream() -> AsyncIterator[bytes]:
            completed = False
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
                completed = True
            finally:
                await upstream_response.aclose()
                await self.pool.finish(
                    worker,
                    close=close_after_response or not completed,
                )

        return StreamingResponse(
            stream(),
            status_code=upstream_response.status_code,
            headers=headers,
        )


def create_app(config: RouterConfig | None = None) -> Starlette:
    """Create the public ASGI app used by the Docker service."""
    settings = config or RouterConfig.from_environment()
    upstream_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        trust_env=False,
    )
    pool = WorkerPool(settings, upstream_client)
    router = MCPHTTPRouter(pool, upstream_client)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await pool.start_background_reaper()
        try:
            yield
        finally:
            await pool.close()
            await upstream_client.aclose()

    app = Starlette(
        routes=[
            Route("/mcp", router.handle_mcp, methods=["GET", "POST", "DELETE", "OPTIONS"]),
            Route("/mcp/", router.handle_mcp, methods=["GET", "POST", "DELETE", "OPTIONS"]),
            Route("/status", router.handle_status, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.state.unbrowser_mcp_router = router
    return app


def _is_initialize_request(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("method") == "initialize"


def _valid_session_id(value: str) -> bool:
    return bool(value) and len(value) <= MAX_SESSION_ID_LENGTH and not any(
        character.isspace() or ord(character) < 32 for character in value
    )


def _positive_int(name: str, default: int, *, upper: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1 or value > upper:
        raise ValueError(f"{name} must be between 1 and {upper}")
    return value


def _positive_float(name: str, default: float, *, upper: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0 or value > upper:
        raise ValueError(f"{name} must be greater than zero and no more than {upper:g}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Session-isolating hosted Unbrowser MCP router")
    parser.add_argument("--host", default=os.environ.get("UNBROWSER_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNBROWSER_MCP_PORT", "8767")))
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
