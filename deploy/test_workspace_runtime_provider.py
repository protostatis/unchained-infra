"""Tests for the host-side workspace runtime provider.

Covers:
  - configuration validation (token, pinned image, listen address, proxy
    token, OpenRouter key, MCP URL)
  - capability health tied to a REAL image-contract probe (not a manual
    boolean alone)
  - slug allowlist (injection rejection)
  - wake provisioning order: private network → egress network → checkpoint
    file → container (two networks) → shared control+MCP attach
  - allowlisted runtime env only (no --env-file leak)
  - label/name validation before any destructive mutation
  - sleep/delete lifecycle: detach shared services, remove per-account
    networks, never touch a foreign container
  - flush: export from the RUNNING runtime (proxy+control tokens), then S2S;
    the checkpoint file is used only when durably acknowledged
  - HTTP API contract (token header required, endpoint allowlist)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DEPLOY_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(DEPLOY_DIR))

import workspace_runtime_provider as provider  # noqa: E402

SLUG = "a" * 24  # valid 24-hex slug
IMAGE = "unbrowser-fin-terminal:e937377b945ed84d721ebd06e22510b5f805e19d"


def make_cfg(**overrides) -> provider.ProviderConfig:
    cfg = provider.ProviderConfig()
    cfg.token = "t" * 40
    cfg.listen = "127.0.0.1:8793"
    cfg.app_image = IMAGE
    cfg.app_capable = True
    cfg.proxy_token = "p" * 40
    cfg.openrouter_api_key = "k" * 24
    cfg.state_dir = tempfile.mkdtemp(prefix="fin-ws-provider-test-")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def ok_probe() -> dict:
    return {
        "probed": True,
        "ok": True,
        "buildMode": "live",
        "basePath": "/fin-terminal/",
        "exportPath": "/internal/financial-workspace/checkpoint-export",
    }


class ProviderConfigTests(unittest.TestCase):
    def test_valid_config_passes(self):
        self.assertEqual(make_cfg().errors(), [])

    def test_missing_token_rejected(self):
        cfg = make_cfg(token="")
        errors = cfg.errors()
        self.assertTrue(any("TOKEN" in e for e in errors))

    def test_short_token_rejected(self):
        cfg = make_cfg(token="short")
        errors = cfg.errors()
        self.assertTrue(any(">= 32" in e for e in errors))

    def test_missing_app_image_rejected(self):
        cfg = make_cfg(app_image="")
        errors = cfg.errors()
        self.assertTrue(any("APP_IMAGE" in e for e in errors))

    def test_missing_proxy_token_rejected(self):
        cfg = make_cfg(proxy_token="")
        errors = cfg.errors()
        self.assertTrue(any("PROXY_TOKEN" in e for e in errors))

    def test_missing_openrouter_key_rejected(self):
        cfg = make_cfg(openrouter_api_key="")
        errors = cfg.errors()
        self.assertTrue(any("OPENROUTER_API_KEY" in e for e in errors))

    def test_invalid_model_provider_rejected(self):
        cfg = make_cfg(model_provider="anthropic")
        errors = cfg.errors()
        self.assertTrue(any("MODEL_PROVIDER" in e for e in errors))

    def test_invalid_listen_rejected(self):
        cfg = make_cfg(listen="not-a-port")
        errors = cfg.errors()
        self.assertTrue(any("LISTEN" in e for e in errors))

    def test_invalid_control_container_rejected(self):
        cfg = make_cfg(control_container="bad name; rm -rf")
        errors = cfg.errors()
        self.assertTrue(any("control container" in e for e in errors))

    def test_checkpoint_path_must_be_absolute(self):
        cfg = make_cfg(checkpoint_file="relative.json")
        errors = cfg.errors()
        self.assertTrue(any("absolute" in e for e in errors))


class ParseFeatureFlagTests(unittest.TestCase):
    def test_contract_truthy(self):
        for value in ("1", "true", "yes", "on", " True ", "ON"):
            self.assertTrue(provider.parse_feature_flag(value), value)

    def test_contract_falsy(self):
        for value in ("0", "false", "no", "off", "", None, "2"):
            self.assertFalse(provider.parse_feature_flag(value), value)


class WorkerGenerationEpochTests(unittest.TestCase):
    def test_matches_app_worker_generation_epoch(self):
        # Deterministic reference values produced by the app's
        # workerGenerationEpoch (server/workspace-checkpoint-export.ts).
        self.assertEqual(provider.worker_generation_epoch("gen-abc123"), 1555693016)
        self.assertEqual(provider.worker_generation_epoch("gen-opaque-001"), 1548768263)
        self.assertEqual(provider.worker_generation_epoch("x" * 40), 68951451)


class HealthTests(unittest.TestCase):
    def test_capability_requires_probe_pass_and_prerequisite_flag(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=True))
        with mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            h = p.health()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["capabilities"]["accountRuntime"])
        self.assertTrue(h["capabilities"]["checkpointFile"])
        self.assertTrue(h["imageContract"]["ok"])

    def test_capability_fails_closed_when_probe_fails_even_with_flag(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=True))
        with mock.patch.object(
            p, "probe_image_contract",
            return_value={"probed": True, "ok": False, "error": "build mode mismatch"},
        ):
            h = p.health()
        self.assertFalse(h["capabilities"]["accountRuntime"])
        self.assertFalse(h["capabilities"]["checkpointFile"])

    def test_capability_fails_closed_without_prerequisite_flag(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=False))
        with mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            h = p.health()
        self.assertFalse(h["capabilities"]["accountRuntime"],
                         "the manual boolean alone must never turn the capability on")

    def test_health_reports_pinned_image(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            self.assertIn("e937377b945ed84d721ebd06e22510b5f805e19d", p.health()["image"])


class ImageContractProbeTests(unittest.TestCase):
    def test_probe_parses_ok_contract(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        script_output = json.dumps({
            "indexPresent": True,
            "buildModeLive": True,
            "basePathFinTerminal": True,
            "runtimeModeJsPresent": True,
            "privateWorkspaceMode": True,
            "exportModulePresent": True,
            "exportPath": "/internal/financial-workspace/checkpoint-export",
        })
        with mock.patch.object(
            p.docker, "image_present", return_value=True
        ), mock.patch.object(
            p.docker, "probe_image_contract",
            return_value={
                "probed": True, "ok": True, "buildMode": "live",
                "basePath": "/fin-terminal/",
                "exportPath": "/internal/financial-workspace/checkpoint-export",
            },
        ):
            result = p.probe_image_contract()
        self.assertTrue(result["ok"])
        self.assertEqual(result["buildMode"], "live")
        self.assertEqual(result["basePath"], "/fin-terminal/")

    def test_probe_rejects_wrong_export_path(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(
            p.docker, "image_present", return_value=True
        ), mock.patch.object(
            p.docker, "probe_image_contract",
            return_value={
                "probed": True, "ok": False,
                "exportPath": "/internal/wrong/export",
            },
        ):
            result = p.probe_image_contract()
        self.assertFalse(result["ok"])

    def test_probe_caches_per_image(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(
            p.docker, "image_present", return_value=True
        ), mock.patch.object(p.docker, "probe_image_contract", return_value=ok_probe()) as m:
            p.probe_image_contract()
            p.probe_image_contract()
        m.assert_called_once()


class SlugAllowlistTests(unittest.TestCase):
    def test_invalid_slugs_rejected(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            for bad in ("", "rm -rf /", "a" * 23, "G" * 24, "a" * 25, "..", "x" * 24):
                self.assertIsNone(p.status(bad))
                self.assertIsNone(p.wake(bad, {"k": 1}))
                self.assertIsNone(p.sleep(bad))
                self.assertIsNone(p.delete(bad))


class WakeProvisioningTests(unittest.TestCase):
    def test_wake_refused_when_app_not_capable(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=False))
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)

    def test_wake_refused_when_probe_fails(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(
                 p, "probe_image_contract",
                 return_value={"probed": True, "ok": False, "error": "bad image"},
             ):
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)

    def test_wake_requires_checkpoint_dict(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            self.assertIsNone(p.wake(SLUG, None))
            self.assertIsNone(p.wake(SLUG, "not-a-dict"))

    def test_wake_refused_when_pinned_image_missing(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=False), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()):
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)

    def test_wake_provisions_network_egress_checkpoint_container_attach(self):
        """wake must run in the exact order: private network → egress network →
        checkpoint file → container (both networks) → shared control+MCP attach;
        the checkpoint JSON travels on stdin — never a host temp file."""
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        calls: list[str] = []
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()), \
             mock.patch.object(p.docker, "ensure_network",
                               side_effect=lambda slug, img: calls.append("network") or True), \
             mock.patch.object(p.docker, "ensure_egress_network",
                               side_effect=lambda slug, img: calls.append("egress") or True), \
             mock.patch.object(p.docker, "write_checkpoint_file",
                               side_effect=lambda slug, img, chk: calls.append("checkpoint") or True), \
             mock.patch.object(p.docker, "start_runtime",
                               side_effect=lambda cfg, slug, tok, gen: calls.append("container") or True), \
             mock.patch.object(p.docker, "connect_shared_services",
                               side_effect=lambda slug, ctrl, mcp: calls.append("attach") or True), \
             mock.patch.object(p.docker, "container_state", return_value="running"):
            result = p.wake(SLUG, {"k": 1, "holdings": []}, control_token="ct-1")
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["network", "egress", "checkpoint", "container", "attach"])

    def test_wake_passes_control_token_and_generation_to_container(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        captured: dict = {}
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()), \
             mock.patch.object(p.docker, "ensure_network", return_value=True), \
             mock.patch.object(p.docker, "ensure_egress_network", return_value=True), \
             mock.patch.object(p.docker, "write_checkpoint_file", return_value=True), \
             mock.patch.object(
                 p.docker, "start_runtime",
                 side_effect=lambda cfg, slug, tok, gen: captured.update(
                     {"cfg": cfg, "slug": slug, "token": tok, "gen": gen}
                 ) or True,
             ), \
             mock.patch.object(p.docker, "connect_shared_services", return_value=True), \
             mock.patch.object(p.docker, "container_state", return_value="running"):
            p.wake(SLUG, {"k": 1}, control_token="ctrl-token-abc")
        self.assertEqual(captured["token"], "ctrl-token-abc")
        self.assertEqual(captured["slug"], SLUG)
        self.assertTrue(captured["gen"].startswith("gen-"))

    def test_shared_attach_failure_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()), \
             mock.patch.object(p.docker, "ensure_network", return_value=True), \
             mock.patch.object(p.docker, "ensure_egress_network", return_value=True), \
             mock.patch.object(p.docker, "write_checkpoint_file", return_value=True), \
             mock.patch.object(p.docker, "start_runtime", return_value=True), \
             mock.patch.object(p.docker, "connect_shared_services", return_value=False):
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)

    def test_network_create_failure_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()), \
             mock.patch.object(p.docker, "ensure_network", return_value=False), \
             mock.patch.object(p.docker, "write_checkpoint_file") as m_write:
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)
        m_write.assert_not_called()

    def test_container_start_failure_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p, "probe_image_contract", return_value=ok_probe()), \
             mock.patch.object(p.docker, "ensure_network", return_value=True), \
             mock.patch.object(p.docker, "ensure_egress_network", return_value=True), \
             mock.patch.object(p.docker, "write_checkpoint_file", return_value=True), \
             mock.patch.object(p.docker, "start_runtime", return_value=False), \
             mock.patch.object(p.docker, "connect_shared_services") as m_conn:
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)
        m_conn.assert_not_called()


class RuntimeEnvAllowlistTests(unittest.TestCase):
    def test_start_runtime_passes_only_allowlisted_env(self):
        """The provider must hand the container exactly the app's
        private-workspace contract — never a broad env-file leak."""
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        self.assertNotIn("--env-file", source)
        self.assertNotIn("env_file", source)
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        cmd: list = []
        with mock.patch.object(p.docker, "container_state", return_value="absent"), \
             mock.patch.object(
                 p.docker, "_run",
                 side_effect=lambda *a, **k: cmd.extend(a) or subprocess_run_stub(),
             ):
            p.docker.start_runtime(p.cfg, SLUG, "control-token-xyz", "gen-1")
        joined = " ".join(cmd)
        # Required contract env present.
        for envvar in (
            "TERMINAL_RUNTIME_MODE=private-workspace",
            "FINANCIAL_WORKSPACE_CHECKPOINTS=1",
            "FIN_WORKSPACE_CHECKPOINT_FILE=/data/checkpoint.json",
            "FIN_WORKSPACE_SESSION_ID=" + SLUG,
            "TERMINAL_RUNTIME_WORKER_GENERATION=gen-1",
            "MARKET_PROXY_TOKEN=",
            "ALLOWED_ORIGINS=https://unbrowser.unchainedsky.com",
            "UNBROWSER_MCP_URL=",
            "UNBROWSER_MCP_REQUIRED=1",
            "MARKET_MODEL_PROVIDER=openrouter",
            "OPENROUTER_API_KEY=",
            "PUBLIC_BASE_PATH=/fin-terminal/",
        ):
            self.assertIn(envvar, joined, f"missing allowlisted env {envvar}")
        # Two networks: private (internal) + per-account egress.
        self.assertIn("--network", joined)
        self.assertEqual(joined.count("--network"), 2)
        self.assertIn(f"fin_ws_{SLUG}", joined)
        self.assertIn(f"fin_ws_{SLUG}_egress", joined)
        # No sibling runtime network is ever shared.
        self.assertNotIn("fin_ws_" + "b" * 24, joined)
        # Labels stamped for identity validation.
        self.assertIn("com.unchained.fin-workspace.slug", joined)

    def test_sibling_runtimes_never_share_a_network(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        slug2 = "b" * 24
        self.assertNotEqual(p.docker.network_name(SLUG), p.docker.network_name(slug2))
        self.assertNotEqual(p.docker.egress_network_name(SLUG), p.docker.egress_network_name(slug2))


def subprocess_run_stub():
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    return _P()


class LifecycleTests(unittest.TestCase):
    def test_sleep_stops_container_detaches_shared_and_removes_networks(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        calls: list[str] = []
        with mock.patch.object(p.docker, "container_state", return_value="running"), \
             mock.patch.object(p.docker, "assert_owned_container",
                               side_effect=lambda *a, **k: calls.append("verify")), \
             mock.patch.object(p.docker, "_run",
                               side_effect=lambda *a, **k: calls.append(a[0]) or subprocess_run_stub()), \
             mock.patch.object(p.docker, "disconnect_shared_services",
                               side_effect=lambda slug, ctrl, mcp: calls.append("disconnect")), \
             mock.patch.object(p.docker, "remove_network",
                               side_effect=lambda name: calls.append(f"rmnet:{name}")):
            result = p.sleep(SLUG)
        self.assertIsNotNone(result)
        # Verify identity before any destructive mutation.
        self.assertTrue(calls.index("verify") < calls.index("stop"))
        self.assertIn("disconnect", calls)
        self.assertIn(f"rmnet:fin_ws_{SLUG}", calls)
        self.assertIn(f"rmnet:fin_ws_{SLUG}_egress", calls)

    def test_sleep_refuses_foreign_container_via_label_check(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="running"), \
             mock.patch.object(
                 p.docker, "assert_owned_container",
                 side_effect=RuntimeError("label slug mismatch"),
             ), \
             mock.patch.object(p.docker, "_run") as m_run:
            with self.assertRaises(RuntimeError):
                p.sleep(SLUG)
        m_run.assert_not_called()

    def test_status_reports_container_state_networks_and_generation(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="running"), \
             mock.patch.object(p.docker, "container_labels",
                               return_value={
                                   "com.unchained.fin-workspace.slug": SLUG,
                                   "com.unchained.fin-workspace.generation": "gen-9",
                               }), \
             mock.patch.object(p.docker, "container_networks",
                               return_value=[f"fin_ws_{SLUG}", f"fin_ws_{SLUG}_egress"]):
            status = p.status(SLUG)
        self.assertEqual(status["slug"], SLUG)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["generation"], "gen-9")
        self.assertIn(f"fin_ws_{SLUG}", status["networks"])

    def test_delete_removes_runtime_and_volume(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="absent"), \
             mock.patch.object(p.docker, "remove_runtime_data") as m_rm:
            result = p.delete(SLUG)
        self.assertTrue(result["deleted"])
        m_rm.assert_called_once_with(SLUG)


class FlushTests(unittest.TestCase):
    def test_flush_exports_from_running_runtime_then_persists(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        p._generations[SLUG] = "gen-abc123"
        with mock.patch.object(p.docker, "container_state", return_value="running"), \
             mock.patch.object(p.docker, "assert_owned_container"), \
             mock.patch.object(
                 p, "_export_from_runtime",
                 return_value={"holdings": [1], "updated": True},
             ) as m_export, \
             mock.patch.object(
                 p, "_persist_to_control_plane",
                 return_value={"ok": True, "snapshot_id": "fsn-1"},
             ) as m_persist:
            result = p.flush(SLUG, "http://fin-terminal-workspace-control:8790", "ct-1")
        self.assertTrue(result["ok"])
        m_export.assert_called_once_with(SLUG, "ct-1")
        m_persist.assert_called_once_with(
            SLUG, {"holdings": [1], "updated": True},
            "http://fin-terminal-workspace-control:8790", "ct-1",
        )
        self.assertIn(SLUG, p._durable_hashes)

    def test_flush_exports_using_docker_exec_proxy_and_control_headers_and_epoch_generation(self):
        """The provider is host-side and the runtime is on an internal network:
        the export must run INSIDE the runtime via docker exec, with the proxy
        + control tokens and the epoch generation on stdin (no shell
        interpolation, no host resolution of the internal container name)."""
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        p._generations[SLUG] = "gen-abc123"
        with mock.patch.object(p.docker, "container_state", return_value="running"), \
             mock.patch.object(p.docker, "assert_owned_container"), \
             mock.patch.object(p.docker, "container_labels", return_value={}), \
             mock.patch("subprocess.run") as m_run:
            m_run.return_value.returncode = 0
            m_run.return_value.stdout = '{"checkpoint": {"holdings": [1]}}'
            m_run.return_value.stderr = ""
            checkpoint = p._export_from_runtime(SLUG, "ct-1")
        self.assertEqual(checkpoint, {"holdings": [1]})
        args, kwargs = m_run.call_args
        cmd = args[0]
        self.assertEqual(cmd[:4], ["docker", "exec", "-i", f"fin-workspace-{SLUG}"])
        self.assertIn("node", cmd)
        script = cmd[-1]
        self.assertIn("/internal/financial-workspace/checkpoint-export", script)
        self.assertIn("X-Fin-Terminal-Proxy-Token", script)
        self.assertIn(p.cfg.proxy_token, script)
        self.assertIn("ct-1", script)
        self.assertIn(str(provider.worker_generation_epoch("gen-abc123")), kwargs["input"])
        self.assertNotIn("fin-workspace-" + SLUG + ":8787", kwargs["input"])

    def test_flush_without_running_runtime_uses_durably_acknowledged_file(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        checkpoint = {"holdings": [1]}
        p._durable_hashes[SLUG] = provider.sha256_of(checkpoint)
        with mock.patch.object(p.docker, "container_state", return_value="absent"), \
             mock.patch.object(p.docker, "read_checkpoint_file", return_value=checkpoint), \
             mock.patch.object(
                 p, "_persist_to_control_plane",
                 return_value={"ok": True, "snapshot_id": "fsn-1"},
             ) as m_persist:
            result = p.flush(SLUG, "http://fin-terminal-workspace-control:8790", "ct-1")
        self.assertTrue(result["ok"])
        m_persist.assert_called_once()

    def test_flush_file_fallback_refuses_unacknowledged_file(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="absent"), \
             mock.patch.object(p.docker, "read_checkpoint_file", return_value={"dirty": True}), \
             mock.patch.object(p, "_persist_to_control_plane") as m_persist:
            result = p.flush(SLUG, "http://fin-terminal-workspace-control:8790", "ct-1")
        self.assertFalse(result["ok"])
        m_persist.assert_not_called()

    def test_flush_runtime_export_failure_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="running"), \
             mock.patch.object(p.docker, "assert_owned_container"), \
             mock.patch.object(p, "_export_from_runtime", return_value=None), \
             mock.patch.object(p, "_persist_to_control_plane") as m_persist:
            result = p.flush(SLUG, "http://fin-terminal-workspace-control:8790", "ct-1")
        self.assertFalse(result["ok"])
        m_persist.assert_not_called()

    def test_flush_invalid_slug_rejected(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        result = p.flush("bad slug!", "http://control:8790", "ct-1")
        self.assertFalse(result["ok"])


class DockerSecurityTests(unittest.TestCase):
    def test_no_shell_interpolation_in_docker_calls(self):
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        self.assertNotIn("os.system", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("curl", source)

    def test_runtime_never_gets_docker_socket(self):
        """The account runtime container must never receive the Docker socket
        mount; Docker authority stays host-side."""
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        self.assertNotIn("/var/run/docker.sock", source)
        self.assertEqual(source.count('"/var/run/docker.sock"'), 0)

    def test_runtime_has_hardening_flags(self):
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        for flag in ('"--cap-drop", "ALL"',
                     '"--security-opt", "no-new-privileges:true"',
                     '"--read-only"',
                     '"--pids-limit"',
                     '"--memory"',
                     '"--tmpfs"',
                     '"--init"'):
            self.assertIn(flag, source, f"missing hardening flag {flag}")

    def test_no_published_ports_for_runtime(self):
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        self.assertNotIn('"-p",', source)
        self.assertNotIn('"--publish"', source)

    def test_private_network_is_internal_and_egress_is_not(self):
        """fin_ws_<slug> is internal: true; the per-account egress network is
        deliberately NOT internal so model/MCP traffic can egress."""
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        self.assertIn('"network", "create",\n            "--internal"', source)
        # The egress network create must NOT pass --internal.
        egress_snippet = source[source.index("def ensure_egress_network"):source.index("def remove_network")]
        self.assertNotIn("--internal", egress_snippet)


class HTTPContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg()
        cls.provider = provider.WorkspaceRuntimeProvider(cls.cfg)
        cls.server = provider.ThreadingHTTPServer(("127.0.0.1", 0), provider.ProviderHandler)
        provider.ProviderHandler.provider = cls.provider
        provider.ProviderHandler.token = cls.cfg.token
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_health_requires_token(self):
        import urllib.request

        with self.assertRaises(Exception):
            urllib.request.urlopen(self._url("/v1/health"), timeout=5)

        req = urllib.request.Request(
            self._url("/v1/health"),
            headers={"X-Workspace-Runtime-Token": self.cfg.token},
        )
        with mock.patch.object(self.provider, "probe_image_contract", return_value=ok_probe()):
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["capabilities"]["accountRuntime"])

    def test_probe_endpoint_200_with_token(self):
        import urllib.request

        req = urllib.request.Request(
            self._url("/v1/probe"),
            headers={"X-Workspace-Runtime-Token": self.cfg.token},
        )
        with mock.patch.object(self.provider, "probe_image_contract", return_value=ok_probe()):
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
        self.assertTrue(data["ok"])

    def test_unknown_endpoint_404(self):
        import urllib.request

        req = urllib.request.Request(
            self._url("/v1/accounts/aaaaaaaaaaaaaaaaaaaaaaaa/status"),
            headers={"X-Workspace-Runtime-Token": self.cfg.token},
        )
        with mock.patch.object(self.provider, "probe_image_contract", return_value=ok_probe()):
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        req = urllib.request.Request(
            self._url("/v1/not-a-route"),
            headers={"X-Workspace-Runtime-Token": self.cfg.token},
        )
        with self.assertRaises(Exception) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_wake_endpoint_refused_when_app_not_capable(self):
        import urllib.request

        old = self.provider.cfg.app_capable
        self.provider.cfg.app_capable = False
        try:
            req = urllib.request.Request(
                self._url(f"/v1/accounts/{SLUG}/wake"),
                data=json.dumps({"checkpoint": {"k": 1}}).encode("utf-8"),
                method="POST",
                headers={
                    "X-Workspace-Runtime-Token": self.cfg.token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(Exception) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            self.provider.cfg.app_capable = old

    def test_delete_endpoint_requires_valid_slug(self):
        import urllib.request

        with mock.patch.object(self.provider, "delete", return_value={"slug": SLUG, "deleted": True}):
            req = urllib.request.Request(
                self._url(f"/v1/accounts/{SLUG}/delete"),
                data=b"{}",
                method="POST",
                headers={
                    "X-Workspace-Runtime-Token": self.cfg.token,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
        self.assertTrue(data["deleted"])


def _sha256_of(checkpoint: dict) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(checkpoint, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# Attach the helper to the provider module for use inside FlushTests.
provider.sha256_of = staticmethod(_sha256_of)

if __name__ == "__main__":
    unittest.main(verbosity=2)
