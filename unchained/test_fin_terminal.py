"""Authorization and deployment contracts for the financial terminal."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web_app.handlers import fin_terminal
from web_app.routes import ROUTE_SPECS


class _Request:
    headers: dict[str, str] = {}


class FinTerminalAuthTests(unittest.IsolatedAsyncioTestCase):
    def _core(self, auth_info):
        return SimpleNamespace(
            _authenticate=lambda _request: auth_info,
            FIN_TERMINAL_ALLOWED_EMAILS={"Admin@Example.com", "OPERATOR@example.com"},
        )

    async def test_authentication_is_required(self):
        with patch.object(fin_terminal, "_core", return_value=self._core(None)):
            response = await fin_terminal.handle_fin_terminal_auth(_Request())

        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_pending_and_unlisted_users_are_denied(self):
        pending = {
            "user_id": "u-pending",
            "email": "admin@example.com",
            "status": "pending",
        }
        unlisted = {
            "user_id": "u-unlisted",
            "email": "someone@example.com",
            "status": "approved",
        }

        with patch.object(fin_terminal, "_core", return_value=self._core(pending)):
            pending_response = await fin_terminal.handle_fin_terminal_auth(_Request())
        with patch.object(fin_terminal, "_core", return_value=self._core(unlisted)):
            unlisted_response = await fin_terminal.handle_fin_terminal_auth(_Request())

        self.assertEqual(pending_response.status, 403)
        self.assertEqual(unlisted_response.status, 403)

    async def test_approved_allowlisted_user_gets_an_opaque_principal(self):
        auth_info = {
            "user_id": "u-sensitive-database-id",
            "email": "Operator@Example.com",
            "status": "approved",
        }

        with patch.object(fin_terminal, "_core", return_value=self._core(auth_info)):
            response = await fin_terminal.handle_fin_terminal_auth(_Request())

        principal = response.headers["X-Fin-Terminal-User"]
        self.assertEqual(response.status, 204)
        self.assertRegex(principal, r"^ft-[0-9a-f]{32}$")
        self.assertNotIn(auth_info["user_id"], principal)
        self.assertNotIn(auth_info["email"].lower(), principal)


class FinTerminalDeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.compose = cls.repo_root.joinpath("docker-compose.yml").read_text()
        cls.caddy = cls.repo_root.joinpath("Caddyfile").read_text()
        cls.deploy = cls.repo_root.joinpath("deploy.sh").read_text()
        cls.secrets_helper = cls.repo_root.joinpath(
            "deploy", "ensure_fin_terminal_secrets.py"
        ).read_text()

    def test_internal_auth_route_is_registered_and_publicly_denied(self):
        self.assertIn(
            (
                "GET",
                "/internal/fin-terminal/auth",
                "web_app.handlers.fin_terminal:handle_fin_terminal_auth",
            ),
            ROUTE_SPECS,
        )
        self.assertIn("handle /internal/*", self.caddy)
        self.assertIn('respond "Not found" 404', self.caddy)

    def test_caddy_strips_client_headers_then_runs_forward_auth(self):
        route = self.caddy.split(
            "handle_path /unbrowser/fin-terminal/*", 1
        )[1].split("# MCP SSE stream", 1)[0]
        strip_user = route.index("request_header -X-Fin-Terminal-User")
        forward_auth = route.index("forward_auth web:8080")

        self.assertLess(strip_user, forward_auth)
        self.assertIn("request_header -X-Fin-Terminal-Proxy-Token", route)
        self.assertIn("uri /internal/fin-terminal/auth", route)
        self.assertIn("copy_headers X-Fin-Terminal-User", route)
        self.assertIn(
            "header_up X-Fin-Terminal-Proxy-Token {$FIN_TERMINAL_PROXY_TOKEN}",
            route,
        )
        self.assertNotIn("sampling {", self.caddy)

    def test_caddy_runtime_is_pinned_and_force_recreated(self):
        self.assertIn(
            "caddy:2.11.4@sha256:844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9",
            self.compose,
        )
        self.assertIn("--force-recreate caddy", self.deploy)
        self.assertIn("caddy reload \\\n        --config /etc/caddy/Caddyfile </dev/null", self.deploy)
        self.assertIn('new_container" == "$old_container', self.deploy)
        self.assertIn('"$actual_image_id" != "$desired_image_id"', self.deploy)
        self.assertIn(
            'cp -p -- "$remote_dir/docker-compose.yml" "$backup_dir/docker-compose.yml"',
            self.deploy,
        )
        self.assertIn('"https://$health_host/unbrowser/fin-terminal/"', self.deploy)
        self.assertIn('[[ "$terminal_status" == "401" ]]', self.deploy)

    def test_compose_pins_and_hardens_the_terminal(self):
        service = self.compose.split("\n  fin-terminal:\n", 1)[1].split(
            "\n  unbrowser-egress:\n", 1
        )[0]

        self.assertIn(
            "781a656391cca0b783111568a84c64307c20382b",
            service,
        )
        self.assertIn("deepseek/deepseek-v4-flash-0731", service)
        self.assertIn(
            "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:?OPENROUTER_API_KEY_required}",
            service,
        )
        self.assertNotIn("FIN_TERMINAL_OPENROUTER_API_KEY", service)
        self.assertIn("PUBLIC_BASE_PATH: /unbrowser/fin-terminal/", service)
        self.assertIn("read_only: true", service)
        self.assertIn("no-new-privileges:true", service)
        self.assertIn("pids_limit: 128", service)
        self.assertIn("mem_limit: 1g", service)
        self.assertIn("- fin_terminal_egress", service)
        self.assertIn("- unbrowser_mcp", service)
        self.assertNotIn("- app", service)

    def test_demo_service_is_a_self_resetting_public_kiosk(self):
        service = self.compose.split("\n  fin-terminal-demo:\n", 1)[1].split(
            "\n  unbrowser-egress:\n", 1
        )[0]

        self.assertIn(
            "781a656391cca0b783111568a84c64307c20382b",
            service,
        )
        self.assertIn("PUBLIC_BASE_PATH: /unbrowser/fin-terminal-demo/", service)
        self.assertIn("PUBLIC_DEMO=1", service)
        self.assertIn("DEMO_IDLE_SECONDS=300", service)
        self.assertIn("read_only: true", service)
        self.assertIn("no-new-privileges:true", service)
        self.assertIn("- fin_terminal_egress", service)
        self.assertNotIn("volumes:", service)

    def test_demo_caddy_route_injects_guest_without_auth(self):
        route = self.caddy.split(
            "handle_path /unbrowser/fin-terminal-demo/*", 1
        )[1].split("handle_path /unbrowser/fin-terminal/*", 1)[0]

        self.assertIn("request_header -X-Fin-Terminal-User", route)
        self.assertIn("request_header -X-Fin-Terminal-Proxy-Token", route)
        self.assertIn("request_header -X-Real-IP", route)
        self.assertNotIn("forward_auth", route)
        self.assertNotIn("rate_limit", route)
        self.assertIn("header_up X-Fin-Terminal-User guest", route)
        self.assertIn(
            "header_up X-Fin-Terminal-Proxy-Token {$FIN_TERMINAL_PROXY_TOKEN}",
            route,
        )
        self.assertIn("header_up X-Real-IP {http.request.remote.host}", route)

    def test_deploy_tracks_the_demo_service_and_route(self):
        self.assertIn("fin-terminal-demo", self.deploy)
        self.assertIn(
            "unbrowser/fin-terminal-demo/",
            self.deploy,
        )
        self.assertIn("grep -qx fin-terminal-demo", self.deploy)

    def test_deploy_lifecycle_tracks_the_terminal(self):
        self.assertIn(
            "unbrowser-mcp fin-terminal fin-terminal-demo web",
            self.deploy,
        )
        self.assertIn(
            "unbrowser-mcp fin-terminal fin-terminal-demo scheduler trial-agent",
            self.deploy,
        )
        self.assertIn("caddy fin-terminal fin-terminal-demo mcp private-core", self.deploy)
        self.assertIn("ensure_remote_fin_terminal_secrets", self.deploy)
        self.assertIn("secrets.token_hex(32)", self.secrets_helper)
        self.assertIn("proxy_token != openrouter_key", self.secrets_helper)
        self.assertIn(
            'docker compose up -d --no-deps --no-build --force-recreate "$service"',
            self.deploy,
        )


if __name__ == "__main__":
    unittest.main()
