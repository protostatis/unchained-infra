#!/usr/bin/env python3
"""End-to-end container integration harness for the private account workspace runtime.

REQUIRES Docker and the pinned app image (built with PUBLIC_BASE_PATH=/fin-terminal/
and TERMINAL_RUNTIME_MODE=private-workspace). Skips cleanly when unavailable.

Build the image first:
  docker build -t unbrowser-fin-terminal:<sha> \\
    --build-arg PUBLIC_BASE_PATH=/fin-terminal/ \\
    --build-arg VITE_TERMINAL_BUILD_MODE=live <app-repo>

Run:  uv run python -m unittest deploy/e2e_fin_workspace_runtime.py -v

Covers the private account-workspace runtime end-to-end with REAL Docker:
  1. imported checkpoint boots (real agent session, production mode)
  2. authenticated HTTP assets load under /fin-terminal/ (strip proxy)
  3. WebSocket connects with the injected principal + proxy token
  4. current authoritative state exports (proxy + control tokens)
  5. provider flush persists a new snapshot to the control plane
  6. sleep occurs only after a durable flush (container + networks removed)
  7. a second account cannot reach the first account's runtime/network/data
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
REPO_DIR = DEPLOY_DIR.parent
UNCHAINED_DIR = REPO_DIR / "unchained"

sys.path.insert(0, str(DEPLOY_DIR))
sys.path.insert(0, str(UNCHAINED_DIR))

import workspace_runtime_provider as provider  # noqa: E402

IMAGE = os.environ.get(
    "FIN_WORKSPACE_RUNTIME_APP_IMAGE",
    "unbrowser-fin-terminal:e937377b945ed84d721ebd06e22510b5f805e19d",
)

_IMAGE_RE = re.compile(r"^[A-Za-z0-9._:/@-]+$")
_RAND = os.environ.get("E2E_RAND", f"{os.getpid():x}")


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
        return True
    except Exception:
        return False


def _run_docker(*args: str, input_data: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], input=input_data, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def _env_cleanup(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _valid_checkpoint(user_id: str, symbol: str) -> dict:
    """A valid FinancialTerminalCheckpoint v1 (the app's import contract) that
    a real claim would have stored as the workspace's latest snapshot."""
    now = int(time.time() * 1000)
    return {
        "version": 1,
        "id": f"fcp-e2e-{user_id}",
        "source": {"sessionId": f"sess-{user_id}", "generation": 1, "sourceRevision": "gen-x"},
        "createdAt": now,
        "expiresAt": now + 86_400_000,
        "eventLog": [
            {"at": now, "type": "command", "data": {"name": "market", "args": symbol}},
            {"at": now + 1, "type": "navigate", "data": {"screen": "MARKET", "symbol": symbol}},
        ],
        "context": {"screen": "MARKET", "symbol": symbol, "watchlist": [symbol]},
        "canvases": [],
        "continuationSummary": f"Continue from a saved checkpoint: {symbol}.",
    }


def _seed_workspace(fw, user_id: str, email: str, symbol: str) -> None:
    chk = fw.create_checkpoint(
        request_id=f"req-e2e-{user_id}", session_id="sess", worker_id="worker",
        checkpoint=json.dumps({"holdings": [{"ticker": symbol, "qty": 1}]}).encode(),
    )
    claim = fw.initiate_claim(
        chk["handoff_id"], chk["handoff_secret"], browser_nonce="n", audience="github",
    )
    fw.bind_oauth_state(claim["claim_id"], "st", audience="github")
    fw.accept_claim(
        claim["claim_id"], claim["claim_secret"],
        final_account_user_id=user_id, final_account_email=email,
        browser_nonce="n", oauth_state="st",
    )
    # The workspace's latest snapshot must be a VALID app checkpoint v1 (the
    # control plane stores the app's exported v1 payloads as snapshots).
    fw.import_flushed_checkpoint(user_id, _valid_checkpoint(user_id, symbol))


class ControlPlaneFlushStub:
    """Host-side stub of the control plane's S2S flush endpoint, backed by the
    REAL unchained FinancialWorkspace (import_flushed_checkpoint)."""

    def __init__(self, fw):
        self.fw = fw
        self.received: list[dict] = []
        self.token = "c" * 40

        class _Handler(BaseHTTPRequestHandler):
            stub = self

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length else b"{}"
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {self.stub.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return
                self.stub.received.append(payload)
                slug = str(payload.get("slug", ""))
                checkpoint = payload.get("checkpoint")
                result = None
                if slug and isinstance(checkpoint, dict):
                    # slug -> user reverse map (mirrors the real handler).
                    for row in self.stub.fw._iter_workspace_user_ids():
                        if provider_workspace_slug(row[0]) == slug:
                            result = self.stub.fw.import_flushed_checkpoint(row[0], checkpoint)
                            break
                if result is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                resp = json.dumps(
                    {"ok": True, "snapshot_id": result["snapshot_id"], "version": result["version"]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, fmt, *args):
                del fmt, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "ControlPlaneFlushStub":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def provider_workspace_slug(user_id: str) -> str:
    import hashlib
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:24]


@unittest.skipUnless(_docker_available(), "Docker is not available")
@unittest.skipUnless(
    bool(subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0),
    f"app image {IMAGE} not present — build it first",
)
class E2EPrivateWorkspaceRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._started = time.time()
        cls._env_saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
            "FIN_WORKSPACE_CONTROL_TOKEN",
            "FIN_WORKSPACE_CONTROL_URL",
        )}
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "c" * 40

        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "auth.db")
        from checkpoint_store import LocalCheckpointStore
        from financial_workspace import FinancialWorkspace
        cls.fw = FinancialWorkspace(cls.db_path, LocalCheckpointStore())
        _seed_workspace(cls.fw, "u-e2e-1", "one@example.com", "AAPL")
        _seed_workspace(cls.fw, "u-e2e-2", "two@example.com", "TSLA")
        cls.slug1 = provider_workspace_slug("u-e2e-1")
        cls.slug2 = provider_workspace_slug("u-e2e-2")

        # Control-plane flush stub (host-side, real FinancialWorkspace).
        cls.control_stub = ControlPlaneFlushStub(cls.fw).start()

        # Host-side runtime provider with real Docker.
        cfg = provider.ProviderConfig()
        cfg.token = "t" * 40
        cfg.listen = "127.0.0.1:8793"
        cfg.app_image = IMAGE
        cfg.app_capable = True
        cfg.proxy_token = "p" * 40
        cfg.openrouter_api_key = "k" * 24
        cfg.state_dir = os.path.join(cls.tmp.name, "state")
        cfg.control_container = f"e2e-control-{_RAND}"
        cfg.mcp_container = f"e2e-mcp-{_RAND}"
        cfg.mcp_url = f"http://{cfg.mcp_container}:8767/mcp"
        cls.cfg = cfg
        cls.provider = provider.WorkspaceRuntimeProvider(cfg)

        # Provider HTTP API (so the control-plane functions runtime_provider_*
        # reach the SAME real provider instance end-to-end).
        cls.provider_server = provider.ThreadingHTTPServer(
            ("127.0.0.1", 0), provider.ProviderHandler
        )
        provider.ProviderHandler.provider = cls.provider
        provider.ProviderHandler.token = cfg.token
        cls.provider_port = cls.provider_server.server_address[1]
        cls.provider_thread = threading.Thread(
            target=cls.provider_server.serve_forever, daemon=True
        )
        cls.provider_thread.start()
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = (
            f"http://127.0.0.1:{cls.provider_port}"
        )
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = cfg.token
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "c" * 40
        # The provider's flush/wake callbacks reach the control plane through
        # the shared service name; point them at the local stub for the E2E.
        os.environ["FIN_WORKSPACE_CONTROL_URL"] = (
            f"http://127.0.0.1:{cls.control_stub.port}"
        )

        # Shared stub containers the provider attaches to each per-account
        # network (control plane name + MCP name must be running).
        cls.shared_containers = []
        _run_docker(
            "run", "-d", "--name", cfg.control_container, "--restart", "no",
            IMAGE, "node", "-e", "setTimeout(()=>{},3600000)",
        )
        cls.shared_containers.append(cfg.control_container)
        _run_docker(
            "run", "-d", "--name", cfg.mcp_container, "--restart", "no",
            IMAGE, "node", "-e",
            "require('http').createServer((q,r)=>{r.end('ok')}).listen(8767);setTimeout(()=>{},3600000)",
        )
        cls.shared_containers.append(cfg.mcp_container)
        for name in cls.shared_containers:
            _wait_container_running(name)

    @classmethod
    def tearDownClass(cls):
        for slug in (cls.slug1, cls.slug2):
            try:
                cls.provider.delete(slug)
            except Exception:
                pass
        for name in cls.shared_containers:
            _run_docker("rm", "-f", name)
        cls.control_stub.stop()
        cls.provider_server.shutdown()
        cls.provider_server.server_close()
        cls.provider_thread.join(timeout=5)
        _env_cleanup(cls._env_saved)
        cls.tmp.cleanup()

    # ------------------------------------------------------------------
    def _runtime_ready(self, slug: str, timeout: float = 240.0) -> dict:
        name = f"fin-workspace-{slug}"
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            result = _run_docker("exec", name, "node", "-e",
                                 "fetch('http://127.0.0.1:8787/api/ready').then(r=>r.json()).then(d=>process.stdout.write(JSON.stringify(d))).catch(e=>process.exit(1))",
                                 timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                try:
                    last = json.loads(result.stdout)
                except json.JSONDecodeError:
                    last = {}
                if last.get("status") == "ready":
                    return last
            time.sleep(3)
        logs = _run_docker("logs", "--tail", "40", name).stdout
        self.fail(f"runtime {slug} never became ready; last={last}; logs:\n{logs}")

    # ------------------------------------------------------------------
    def test_01_image_contract_probe(self):
        probe = self.provider.probe_image_contract()
        self.assertTrue(probe["ok"], f"image contract probe failed: {probe}")
        self.assertEqual(probe["buildMode"], "live")
        self.assertEqual(probe["basePath"], "/fin-terminal/")
        self.assertEqual(probe["exportPath"], "/internal/financial-workspace/checkpoint-export")

    def test_02_imported_checkpoint_boots(self):
        checkpoint = self.fw.get_workspace_runtime_checkpoint("u-e2e-1")
        self.assertIsNotNone(checkpoint)
        status = self.provider.wake(self.slug1, checkpoint, control_token="c" * 40)
        self.assertIsNotNone(status)
        self.assertEqual(status["state"], "running")
        ready = self._runtime_ready(self.slug1)
        self.assertTrue(ready.get("privateWorkspace"))
        self.assertEqual(ready.get("sessionId"), self.slug1)

    def test_03_authenticated_assets_load_under_fin_terminal_prefix(self):
        """A strip proxy (mirroring Caddy's /fin-terminal strip + the control
        plane's /terminal marker) must serve the app's index and its absolute
        /fin-terminal/assets/* bundle from the running runtime."""
        name = f"fin-workspace-{self.slug1}"
        proxy = f"e2e-proxy-{_RAND}"
        _run_docker(
            "run", "-d", "--name", proxy, "--restart", "no",
            "--network", self.provider.docker.network_name(self.slug1),
            IMAGE, "node", "-e",
            "const http=require('http');"
            "http.createServer((q,r)=>{"
            "const tail=q.url.replace(/^\\/fin-terminal/, '') || '/';"
            "const u='http://fin-workspace-%s:8787'+tail;"
            "http.get(u,{headers:{'X-Fin-Terminal-Proxy-Token':'p'.repeat(40),'X-Fin-Terminal-User':'account:%s'}},"
            "(up)=>{let d='';up.on('data',c=>d+=c);up.on('end',()=>{r.writeHead(up.statusCode,{'Content-Type':up.headers['content-type']});r.end(d);});});"
            "}).listen(8788);setTimeout(()=>{},3600000)" % (self.slug1, self.slug1),
        )
        try:
            _wait_container_running(proxy)
            result = _run_docker(
                "run", "--rm", "--network", self.provider.docker.network_name(self.slug1),
                IMAGE, "node", "-e",
                "fetch('http://%s:8788/fin-terminal/').then(r=>r.text()).then(t=>process.stdout.write(t)).catch(e=>{process.exit(1)})" % proxy,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("href=\"/fin-terminal/assets/", result.stdout)
            asset = re.search(r'href="(/fin-terminal/assets/[^"]+)"', result.stdout)
            self.assertIsNotNone(asset, "index must reference a /fin-terminal/ asset")
            asset_result = _run_docker(
                "run", "--rm", "--network", self.provider.docker.network_name(self.slug1),
                IMAGE, "node", "-e",
                "fetch('http://%s:8788%s').then(r=>{if(!r.ok)process.exit(1);return r.text()}).then(t=>process.stdout.write(t)).catch(e=>process.exit(1))" % (proxy, asset.group(1)),
                timeout=30,
            )
            self.assertEqual(asset_result.returncode, 0, "asset must load through the /fin-terminal/ proxy")
            self.assertNotIn("<html", asset_result.stdout[:200].lower())
        finally:
            _run_docker("rm", "-f", proxy)

    def test_04_websocket_connects_with_injected_principal(self):
        """A WebSocket upgrade with the server-derived principal + proxy token
        must be accepted and deliver a frame."""
        name = f"fin-workspace-{self.slug1}"
        script = (
            "const WebSocket=require('/app/node_modules/ws');"
            "const ws=new WebSocket('ws://127.0.0.1:8787/ws',{"
            "headers:{"
            "'Origin':'https://unbrowser.unchainedsky.com',"
            "'X-Fin-Terminal-Proxy-Token':'p'.repeat(40),"
            "'X-Fin-Terminal-User':'account:%s'}});"
            "let got=false;"
            "ws.on('message',(d)=>{"
            "  const m=JSON.parse(d.toString());"
            "  if(m.type==='frame'&&Array.isArray(m.rows)){got=true;process.stdout.write('FRAME');ws.close();}"
            "});"
            "setTimeout(()=>{if(!got){process.stdout.write('TIMEOUT');process.exit(1)}},60000);"
            % self.slug1
        )
        result = _run_docker("exec", name, "node", "-e", script, timeout=90)
        self.assertEqual(result.returncode, 0, f"ws connect failed: {result.stdout} {result.stderr}")
        self.assertIn("FRAME", result.stdout)

    def test_05_websocket_rejected_without_principal(self):
        """A WS upgrade WITHOUT the proxy token must be rejected (fail closed)."""
        name = f"fin-workspace-{self.slug1}"
        script = (
            "const WebSocket=require('/app/node_modules/ws');"
            "const ws=new WebSocket('ws://127.0.0.1:8787/ws');"
            "ws.on('open',()=>{process.stdout.write('OPEN');ws.close();process.exit(0)});"
            "ws.on('unexpected-response',(q,r)=>{process.stdout.write('REJECTED:'+r.statusCode);process.exit(0)});"
            "setTimeout(()=>{process.stdout.write('TIMEOUT');process.exit(0)},15000);"
        )
        result = _run_docker("exec", name, "node", "-e", script, timeout=30)
        self.assertIn("REJECTED:403", result.stdout)

    def test_06_current_state_exports(self):
        """The provider flush source: export the current authoritative
        checkpoint from the running runtime for the exact session/generation."""
        name = f"fin-workspace-{self.slug1}"
        generation = self.provider._generations.get(self.slug1, "")
        self.assertTrue(generation)
        epoch = provider.worker_generation_epoch(generation)
        body = json.dumps({"sessionId": self.slug1, "generation": epoch})
        result = _run_docker(
            "exec", "-i", name, "node", "-e",
            "let d='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>d+=c);"
            "process.stdin.on('end',()=>{"
            "fetch('http://127.0.0.1:8787/internal/financial-workspace/checkpoint-export',{"
            "method:'POST',headers:{'Content-Type':'application/json',"
            "'X-Fin-Terminal-Proxy-Token':'p'.repeat(40),'X-Fin-Terminal-Control-Token':'c'.repeat(40)},"
            "body:d}).then(async r=>{process.stdout.write(JSON.stringify({status:r.status,body:await r.json()}))})"
            ".catch(e=>process.exit(1));});",
            input_data=body,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], 200, f"export failed: {payload}")
        checkpoint = payload["body"]["checkpoint"]
        self.assertEqual(checkpoint["version"], 1)
        self.assertEqual(checkpoint["source"]["sessionId"], self.slug1)

    def test_07_provider_flush_persists_snapshot(self):
        """provider.flush() must export from the RUNNING runtime and persist a
        new snapshot to the control plane (real FinancialWorkspace)."""
        result = self.provider.flush(
            self.slug1, f"http://127.0.0.1:{self.control_stub.port}", "c" * 40
        )
        self.assertTrue(result["ok"], f"flush failed: {result}")
        self.assertEqual(len(self.control_stub.received), 1)
        snapshots = self.fw.get_snapshots_for_workspace(
            self.fw.get_workspace_for_user("u-e2e-1")["workspace_id"]
        )
        self.assertGreaterEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot"]["version"], 1)

    def test_08_sleep_occurs_only_after_durable_flush(self):
        """Durable sleep: a failed flush keeps the runtime awake; a durable
        flush stops the container and removes the per-account networks."""
        name = f"fin-workspace-{self.slug1}"
        self.fw.runtime_wake("u-e2e-1")
        # Fail closed: flush fails → runtime must stay awake.
        with mock.patch(
            "financial_workspace.runtime_provider_flush",
            return_value={"ok": False, "reason": "boom"},
        ):
            result = self.fw.runtime_sleep_durable("u-e2e-1", reason="idle")
        self.assertEqual(result["runtime_state"], "awake")
        state = _run_docker("inspect", "--format", "{{.State.Status}}", name)
        self.assertEqual(state.stdout.strip(), "running")

        # Durable flush → stop + network removal.
        result = self.fw.runtime_sleep_durable("u-e2e-1", reason="idle")
        self.assertEqual(result["runtime_state"], "asleep")
        self.assertTrue(result["flush"]["ok"])
        state = _run_docker("inspect", "--format", "{{.State.Status}}", name, timeout=20)
        self.assertEqual(state.returncode, 1)  # container gone
        for net in (self.provider.docker.network_name(self.slug1), self.provider.docker.egress_network_name(self.slug1)):
            check = _run_docker("network", "inspect", net)
            self.assertNotEqual(check.returncode, 0, f"network {net} should be removed")

    def test_09_second_account_cannot_reach_first(self):
        """Sibling isolation: account2 has its own runtime + network; a probe
        on account2's network cannot resolve or reach account1's runtime, and
        account2's own runtime is reachable only from its own network."""
        name1 = f"fin-workspace-{self.slug1}"
        # Re-wake account1 and boot account2's OWN runtime.
        checkpoint1 = self.fw.get_workspace_runtime_checkpoint("u-e2e-1")
        self.provider.wake(self.slug1, checkpoint1, control_token="c" * 40)
        self._runtime_ready(self.slug1, timeout=240)
        checkpoint2 = self.fw.get_workspace_runtime_checkpoint("u-e2e-2")
        self.assertIsNotNone(checkpoint2)
        status2 = self.provider.wake(self.slug2, checkpoint2, control_token="c" * 40)
        self.assertIsNotNone(status2)
        self.assertEqual(status2["state"], "running")
        name2 = f"fin-workspace-{self.slug2}"
        self._runtime_ready(self.slug2, timeout=240)

        # Account1's network can reach account1 only.
        from_account1 = _run_docker(
            "run", "--rm", "--network", self.provider.docker.network_name(self.slug1),
            IMAGE, "node", "-e",
            "fetch('http://%s:8787/api/ready',{signal:AbortSignal.timeout(3000)})"
            ".then(r=>process.stdout.write('OK:'+r.status)).catch(()=>process.stdout.write('FAIL'))" % name1,
            timeout=30,
        )
        self.assertIn("OK:200", from_account1.stdout)
        # Account2's network must NOT reach account1's runtime.
        from_account2 = _run_docker(
            "run", "--rm", "--network", self.provider.docker.network_name(self.slug2),
            IMAGE, "node", "-e",
            "fetch('http://%s:8787/api/ready',{signal:AbortSignal.timeout(3000)})"
            ".then(()=>process.stdout.write('REACHABLE')).catch(()=>process.stdout.write('ISOLATED'))" % name1,
            timeout=30,
        )
        self.assertEqual(from_account2.returncode, 0, from_account2.stderr)
        self.assertIn("ISOLATED", from_account2.stdout)
        # Account2's own runtime IS reachable from its own network.
        from_account2_self = _run_docker(
            "run", "--rm", "--network", self.provider.docker.network_name(self.slug2),
            IMAGE, "node", "-e",
            "fetch('http://%s:8787/api/ready',{signal:AbortSignal.timeout(3000)})"
            ".then(r=>process.stdout.write('SELFOK:'+r.status)).catch(()=>process.stdout.write('FAIL'))" % name2,
            timeout=30,
        )
        self.assertIn("SELFOK:200", from_account2_self.stdout)
        # Data volumes are per-account and never shared.
        self.assertNotEqual(
            self.provider.docker.volume_name(self.slug1),
            self.provider.docker.volume_name(self.slug2),
        )
        # Sleep account2 (durable) so the suite ends clean.
        self.fw.runtime_wake("u-e2e-2")
        slept = self.fw.runtime_sleep_durable("u-e2e-2", reason="e2e")
        self.assertEqual(slept["runtime_state"], "asleep")
        # Data volumes are per-account.
        self.assertNotEqual(
            self.provider.docker.volume_name(self.slug1),
            self.provider.docker.volume_name(self.slug2),
        )


def _wait_container_running(name: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run_docker("inspect", "--format", "{{.State.Status}}", name)
        if result.stdout.strip() == "running":
            return
        time.sleep(1)
    raise RuntimeError(f"container {name} did not start")


if __name__ == "__main__":
    unittest.main(verbosity=2)
