"""Unit tests for resolved Docker Compose service change detection."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from classify_changes import SERVICES, classify_path
from compose_service_diff import changed_services


def _config() -> dict:
    return {
        "name": "unchained",
        "networks": {"app": {"internal": True}},
        "services": {
            "caddy": {
                "image": "caddy:2",
                "depends_on": {"web": {"condition": "service_healthy"}},
                "networks": {"app": None},
            },
            "relay": {"image": "unchained", "networks": {"app": None}},
            "web": {"image": "unchained", "networks": {"app": None}},
        },
    }


class TestChangedServices(unittest.TestCase):
    def test_compose_file_uses_resolved_service_comparison(self):
        self.assertEqual(classify_path("docker-compose.yml"), {"COMPOSE"})

    def test_opt_in_compose_overlay_does_not_restart_default_services(self):
        self.assertEqual(classify_path("docker-compose.public-terminal.yml"), set())

    def test_classifier_knows_fin_terminal_service(self):
        self.assertIn("fin-terminal", SERVICES)
        self.assertNotIn("fin-terminal-demo", SERVICES)

    def test_deployment_tooling_does_not_rebuild_runtime_services(self):
        self.assertEqual(classify_path("deploy.sh"), set())
        self.assertEqual(classify_path("deploy/compose_service_diff.py"), set())

    def test_transcript_module_rebuilds_its_runtime_consumers(self):
        self.assertEqual(
            classify_path("unchained/conversation_transcript.py"),
            {"web", "trial-agent"},
        )

    def test_reports_only_the_service_with_an_effective_change(self):
        old = _config()
        new = copy.deepcopy(old)
        new["services"]["web"]["environment"] = {"FEATURE_FLAG": "on"}

        self.assertEqual(changed_services(old, new), ["web"])

    def test_ignores_caddy_depends_on_only_change(self):
        old = _config()
        new = copy.deepcopy(old)
        new["services"]["caddy"]["depends_on"] = {
            "web": {"condition": "service_started"},
            "relay": {"condition": "service_healthy"},
        }

        self.assertEqual(changed_services(old, new), [])

    def test_caddy_runtime_change_is_reported(self):
        old = _config()
        new = copy.deepcopy(old)
        new["services"]["caddy"]["ports"] = ["443:443"]

        self.assertEqual(changed_services(old, new), ["caddy"])

    def test_caddy_label_change_is_reported(self):
        old = _config()
        new = copy.deepcopy(old)
        new["services"]["caddy"]["labels"] = {
            "com.unchainedsky.caddy.version": "2.11.4"
        }

        self.assertEqual(changed_services(old, new), ["caddy"])

    def test_shared_topology_change_fails_closed(self):
        old = _config()
        new = copy.deepcopy(old)
        new["networks"]["app"]["internal"] = False

        self.assertIsNone(changed_services(old, new))

    def test_service_topology_change_fails_closed(self):
        old = _config()
        new = copy.deepcopy(old)
        new["services"].pop("relay")

        self.assertIsNone(changed_services(old, new))

    def test_empty_service_config_fails_closed(self):
        self.assertIsNone(changed_services({"services": {}}, {"services": {}}))


if __name__ == "__main__":
    unittest.main()
