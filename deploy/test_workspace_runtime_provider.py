"""Tests for the host-side workspace runtime provider.

Covers:
  - configuration validation (token, pinned image, listen address)
  - capability self-report and the fail-closed accountRuntime gate
  - slug allowlist (injection rejection)
  - wake provisioning order: network → checkpoint file → container → control
    plane attach (no Docker socket in containers)
  - sleep / status lifecycle
  - flush export to the control plane (S2S, control token)
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


def make_cfg(**overrides) -> provider.ProviderConfig:
    cfg = provider.ProviderConfig()
    cfg.token = "t" * 40
    cfg.listen = "127.0.0.1:8793"
    cfg.app_image = "unbrowser-fin-terminal:8a95cb75bd01a3288b0c859dc07540f7d9fa4d8b"
    cfg.app_capable = True
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


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


class HealthTests(unittest.TestCase):
    def test_health_reports_capabilities_from_config(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=True))
        h = p.health()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["capabilities"]["accountRuntime"])
        self.assertTrue(h["capabilities"]["checkpointFile"])

    def test_health_fails_closed_when_app_not_capable(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=False))
        h = p.health()
        self.assertFalse(h["capabilities"]["accountRuntime"],
                         "the control plane must refuse activation until the "
                         "pinned app runtime support is validated")

    def test_health_reports_pinned_image(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        self.assertIn("8a95cb75bd01a3288b0c859dc07540f7d9fa4d8b", p.health()["image"])


class SlugAllowlistTests(unittest.TestCase):
    def test_invalid_slugs_rejected(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        for bad in ("", "rm -rf /", "a" * 23, "G" * 24, "a" * 25, "..", "x" * 24):
            self.assertIsNone(p.status(bad))
            self.assertIsNone(p.wake(bad, {"k": 1}))
            self.assertIsNone(p.sleep(bad))


class WakeProvisioningTests(unittest.TestCase):
    def test_wake_refused_when_app_not_capable(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg(app_capable=False))
        with mock.patch.object(p.docker, "image_present", return_value=True):
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)

    def test_wake_requires_checkpoint_dict(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True):
            self.assertIsNone(p.wake(SLUG, None))
            self.assertIsNone(p.wake(SLUG, "not-a-dict"))

    def test_wake_refused_when_pinned_image_missing(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=False):
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)

    def test_wake_provisions_network_checkpoint_container_attach(self):
        """wake must run in the exact order: network → checkpoint file →
        container → control-plane attach; the checkpoint JSON travels on
        stdin of a one-off helper — never a host temp file."""
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        calls: list[str] = []
        with mock.patch.object(p.docker, "image_present", return_value=True) as m_img, \
             mock.patch.object(p.docker, "ensure_network",
                               side_effect=lambda slug: calls.append("network") or True), \
             mock.patch.object(p.docker, "write_checkpoint_file",
                               side_effect=lambda slug, img, chk: calls.append("checkpoint") or True), \
             mock.patch.object(p.docker, "start_runtime",
                               side_effect=lambda cfg, slug, tok: calls.append("container") or True), \
             mock.patch.object(p.docker, "connect_control_plane",
                               side_effect=lambda slug, ctrl: calls.append("attach")), \
             mock.patch.object(p.docker, "container_state", return_value="running"):
            result = p.wake(SLUG, {"k": 1, "holdings": []}, control_token="ct-1")
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["network", "checkpoint", "container", "attach"])
        m_img.assert_called_once_with(p.cfg.app_image)

    def test_wake_passes_control_token_to_container(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        captured: dict = {}
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p.docker, "ensure_network", return_value=True), \
             mock.patch.object(p.docker, "write_checkpoint_file", return_value=True), \
             mock.patch.object(
                 p.docker, "start_runtime",
                 side_effect=lambda cfg, slug, tok: captured.update(
                     {"cfg": cfg, "slug": slug, "token": tok}
                 ) or True,
             ), \
             mock.patch.object(p.docker, "connect_control_plane"), \
             mock.patch.object(p.docker, "container_state", return_value="running"):
            p.wake(SLUG, {"k": 1}, control_token="ctrl-token-abc")
        self.assertEqual(captured["token"], "ctrl-token-abc")
        self.assertEqual(captured["slug"], SLUG)

    def test_network_create_failure_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p.docker, "ensure_network", return_value=False), \
             mock.patch.object(p.docker, "write_checkpoint_file") as m_write:
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)
        m_write.assert_not_called()

    def test_container_start_failure_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "image_present", return_value=True), \
             mock.patch.object(p.docker, "ensure_network", return_value=True), \
             mock.patch.object(p.docker, "write_checkpoint_file", return_value=True), \
             mock.patch.object(p.docker, "start_runtime", return_value=False), \
             mock.patch.object(p.docker, "connect_control_plane") as m_conn:
            result = p.wake(SLUG, {"k": 1})
        self.assertIsNone(result)
        m_conn.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def test_sleep_stops_container_and_disconnects_control_plane(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="running") as m_state, \
             mock.patch.object(p.docker, "stop_runtime") as m_stop:
            result = p.sleep(SLUG)
        self.assertIsNotNone(result)
        m_stop.assert_called_once_with(SLUG, p.cfg.control_container)
        self.assertEqual(result["state"], "running")

    def test_status_reports_container_state(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "container_state", return_value="running"):
            status = p.status(SLUG)
        self.assertEqual(status["slug"], SLUG)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["container"], f"fin-workspace-{SLUG}")


class FlushTests(unittest.TestCase):
    def test_flush_posts_checkpoint_to_control_plane(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(
            p.docker, "read_checkpoint_file", return_value={"holdings": [1]},
        ), mock.patch(
            "urllib.request.urlopen",
        ) as m_open:
            m_open.return_value.__enter__.return_value.status = 200
            m_open.return_value.__enter__.return_value.read.return_value = (
                b'{"ok": true, "snapshot_id": "fsn-1"}'
            )
            result = p.flush(SLUG, "http://fin-terminal-workspace-control:8790", "ct-1")
        self.assertTrue(result["ok"])
        req = m_open.call_args[0][0]
        self.assertIn("/internal/financial-workspace/runtime/flush", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer ct-1")
        body = json.loads(req.data)
        self.assertEqual(body["slug"], SLUG)
        self.assertEqual(body["checkpoint"], {"holdings": [1]})

    def test_flush_without_checkpoint_file_fails_closed(self):
        p = provider.WorkspaceRuntimeProvider(make_cfg())
        with mock.patch.object(p.docker, "read_checkpoint_file", return_value=None):
            result = p.flush(SLUG, "http://control:8790", "ct-1")
        self.assertFalse(result["ok"])

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
        # The only -v mount is the per-account data volume.
        self.assertIn('"-v", f"{self.volume_name(slug)}:/data"', source)
        self.assertEqual(source.count('"/var/run/docker.sock"'), 0)

    def test_runtime_has_hardening_flags(self):
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        for flag in ('"--cap-drop", "ALL"',
                     '"--security-opt", "no-new-privileges:true"',
                     '"--read-only"',
                     '"--pids-limit"',
                     '"--memory"',
                     '"--tmpfs"'):
            self.assertIn(flag, source, f"missing hardening flag {flag}")

    def test_no_published_ports_for_runtime(self):
        source = (DEPLOY_DIR / "workspace_runtime_provider.py").read_text()
        self.assertNotIn('"-p",', source)
        self.assertNotIn('"--publish"', source)


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
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")

    def test_unknown_endpoint_404(self):
        import urllib.request

        req = urllib.request.Request(
            self._url("/v1/accounts/aaaaaaaaaaaaaaaaaaaaaaaa/status"),
            headers={"X-Workspace-Runtime-Token": self.cfg.token},
        )
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
