"""Authorization and deployment contracts for the financial terminal."""

from __future__ import annotations

import os
import subprocess
import tempfile
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


class CaddyConfigPreflightTests(unittest.TestCase):
    deployment_id = "a" * 24

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.helper = cls.repo_root / "deploy" / "caddy_config_preflight.sh"

    def _layout(self, root: Path) -> tuple[Path, Path]:
        remote_dir = root / "remote"
        stage_dir = remote_dir / ".deploy-staging" / self.deployment_id
        stage_dir.mkdir(parents=True)
        remote_dir.joinpath("Caddyfile").write_text("old live config\n")
        remote_dir.joinpath(".env").write_text(
            "FIN_TERMINAL_PROXY_TOKEN=old-token\n"
        )
        remote_dir.joinpath("Dockerfile").write_text("FROM old\n")
        remote_dir.joinpath("Dockerfile.unbrowser-mcp").write_text("FROM old\n")
        remote_dir.joinpath("docker-compose.yml").write_text("services: {}\n")
        stage_dir.joinpath("Caddyfile").write_text("candidate config\n")
        stage_dir.joinpath(".env").write_text(
            "FIN_TERMINAL_PROXY_TOKEN=candidate-token\n"
        )
        stage_dir.joinpath("Dockerfile").write_text("FROM candidate\n")
        stage_dir.joinpath("Dockerfile.unbrowser-mcp").write_text(
            "FROM candidate-mcp\n"
        )
        stage_dir.joinpath("docker-compose.yml").write_text("services: {}\n")
        return remote_dir, stage_dir

    def _fake_docker(self, root: Path) -> tuple[Path, Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        log_path = root / "docker.log"
        token_path = root / "docker-token"
        docker_path = fake_bin / "docker"
        docker_path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
log_path="${FAKE_DOCKER_LOG:?}"
token_path="${FAKE_DOCKER_TOKEN:?}"
printf '%q ' "$@" >> "$log_path"
printf '\\n' >> "$log_path"

read_token() {
    local env_file="$1" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            FIN_TERMINAL_PROXY_TOKEN=*)
                printf '%s' "${line#*=}"
                return 0
                ;;
        esac
    done < "$env_file"
}

env_file_arg() {
    local index next
    for ((index = 1; index <= $#; index++)); do
        if [[ "${!index}" == "--env-file" ]]; then
            next=$((index + 1))
            printf '%s' "${!next}"
            return 0
        fi
    done
    return 1
}

case "$1" in
    compose)
        env_file="$(env_file_arg "$@")"
        token="$(read_token "$env_file")"
        printf '{"services":{"caddy":{"image":"example.test/caddy@sha256:expected","environment":{"FIN_TERMINAL_PROXY_TOKEN":"%s"}}}}\\n' "$token"
        ;;
    image)
        [[ "$2" == "pull" ]] || exit 2
        ;;
    run)
        env_file="$(env_file_arg "$@")"
        read_token "$env_file" > "$token_path"
        [[ "${FAKE_DOCKER_VALIDATE_FAIL:-0}" != "1" ]] || exit 42
        ;;
    rm)
        ;;
    *)
        echo "unexpected docker command: $*" >&2
        exit 2
        ;;
esac
""",
            encoding="utf-8",
        )
        docker_path.chmod(0o755)
        return fake_bin, log_path, token_path

    def _run_helper(
        self,
        fake_bin: Path,
        log_path: Path,
        token_path: Path,
        action: str,
        stage_dir: Path,
        remote_dir: Path,
        *,
        validate_fail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["FAKE_DOCKER_LOG"] = str(log_path)
        env["FAKE_DOCKER_TOKEN"] = str(token_path)
        if validate_fail:
            env["FAKE_DOCKER_VALIDATE_FAIL"] = "1"
        return subprocess.run(
            [
                "bash",
                str(self.helper),
                action,
                str(stage_dir),
                str(remote_dir),
                self.deployment_id,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_invalid_candidate_leaves_live_files_and_runtime_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, stage_dir = self._layout(Path(tmpdir))
            fake_bin, log_path, token_path = self._fake_docker(Path(tmpdir))
            live_caddy = remote_dir / "Caddyfile"
            live_env = remote_dir / ".env"
            caddy_inode = live_caddy.stat().st_ino
            caddy_bytes = live_caddy.read_bytes()
            env_bytes = live_env.read_bytes()

            result = self._run_helper(
                fake_bin,
                log_path,
                token_path,
                "validate",
                stage_dir,
                remote_dir,
                validate_fail=True,
            )

            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(live_caddy.stat().st_ino, caddy_inode)
            self.assertEqual(live_caddy.read_bytes(), caddy_bytes)
            self.assertEqual(live_env.read_bytes(), env_bytes)
            self.assertFalse((stage_dir / ".caddy-preflight").exists())

            docker_log = log_path.read_text(encoding="utf-8")
            self.assertIn("image pull", docker_log)
            self.assertIn("run ", docker_log)
            self.assertIn("rm -f", docker_log)
            for forbidden in (" build ", " up ", " exec ", " reload "):
                self.assertNotIn(forbidden, f" {docker_log} ")

    def test_validation_uses_rendered_image_environment_and_isolated_container(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, stage_dir = self._layout(Path(tmpdir))
            fake_bin, log_path, token_path = self._fake_docker(Path(tmpdir))
            live_caddy = remote_dir / "Caddyfile"
            before = live_caddy.read_bytes()

            result = self._run_helper(
                fake_bin,
                log_path,
                token_path,
                "validate",
                stage_dir,
                remote_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(token_path.read_text(encoding="utf-8"), "candidate-token")
            self.assertEqual(live_caddy.read_bytes(), before)
            self.assertFalse((stage_dir / ".caddy-preflight").exists())

            run_line = next(
                line
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("run ")
            )
            self.assertIn("--network none", run_line)
            self.assertIn("--read-only", run_line)
            self.assertIn("--entrypoint caddy", run_line)
            self.assertIn("example.test/caddy@sha256:expected", run_line)
            self.assertIn("validate", run_line)
            self.assertIn(f"src={stage_dir / 'Caddyfile'}", run_line)

    def test_promotion_preserves_live_caddyfile_inode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, stage_dir = self._layout(Path(tmpdir))
            fake_bin, log_path, token_path = self._fake_docker(Path(tmpdir))
            live_caddy = remote_dir / "Caddyfile"
            before_inode = live_caddy.stat().st_ino

            result = self._run_helper(
                fake_bin,
                log_path,
                token_path,
                "promote",
                stage_dir,
                remote_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "fin_terminal_secrets_changed=true")
            self.assertEqual(live_caddy.stat().st_ino, before_inode)
            self.assertEqual(live_caddy.read_text(), "candidate config\n")
            self.assertEqual((remote_dir / ".env").read_text(), "FIN_TERMINAL_PROXY_TOKEN=candidate-token\n")
            self.assertEqual((remote_dir / "Dockerfile").read_text(), "FROM candidate\n")
            self.assertEqual(
                (remote_dir / "Dockerfile.unbrowser-mcp").read_text(),
                "FROM candidate-mcp\n",
            )

    def test_promotion_hardens_an_unchanged_secret_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, stage_dir = self._layout(Path(tmpdir))
            fake_bin, log_path, token_path = self._fake_docker(Path(tmpdir))
            live_env = remote_dir / ".env"
            stage_dir.joinpath(".env").write_text(
                "FIN_TERMINAL_PROXY_TOKEN=old-token\n"
            )
            live_env.chmod(0o644)

            result = self._run_helper(
                fake_bin,
                log_path,
                token_path,
                "promote",
                stage_dir,
                remote_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "fin_terminal_secrets_changed=false")
            self.assertEqual(live_env.stat().st_mode & 0o777, 0o600)


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
        cls.caddy_preflight = cls.repo_root.joinpath(
            "deploy", "caddy_config_preflight.sh"
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
        subdomain = self.caddy.split("unbrowser.unchainedsky.com {", 1)[1]
        route = subdomain.split("handle_path /fin-terminal/*", 1)[1].split(
            "# Public kiosk demo.", 1
        )[0]
        strip_user = route.index("request_header -X-Fin-Terminal-User")
        forward_auth = route.index("forward_auth web:8080")

        self.assertLess(strip_user, forward_auth)
        self.assertIn("request_header -X-Fin-Terminal-Proxy-Token", route)
        self.assertIn("uri /internal/fin-terminal/auth", route)
        self.assertIn("copy_headers X-Fin-Terminal-User", route)
        for header in ("Cookie", "Authorization", "Proxy-Authorization"):
            self.assertGreater(
                route.index(f"request_header -{header}"), forward_auth
            )
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
        self.assertIn('[[ "$legacy_terminal_check" == "308 https://$demo_host/fin-terminal/" ]]', self.deploy)
        self.assertIn('"https://$demo_host/fin-terminal/"', self.deploy)
        self.assertIn('[[ "$terminal_status" == "401" ]]', self.deploy)

    def test_caddyfile_is_staged_and_validated_before_live_mutation(self):
        stage_index = self.deploy.index('echo "==> Staging prospective configuration..."')
        validate_index = self.deploy.index('echo "==> Validating staged Caddyfile..."')
        mutation_index = self.deploy.index("DEPLOY_MUTATED=true", validate_index)
        promote_index = self.deploy.index("promote_staged_config", mutation_index)
        rebuild_index = self.deploy.index("# Build affected services.")
        reload_index = self.deploy.index("==> Reloading Caddy")

        self.assertIn("REMOTE_CONFIG_STAGE=", self.deploy)
        self.assertIn("cleanup_remote_config_stage ||", self.deploy)
        self.assertIn("docker image pull", self.caddy_preflight)
        self.assertIn("docker run --rm", self.caddy_preflight)
        self.assertIn("--network none", self.caddy_preflight)
        self.assertNotIn("docker compose run --rm --no-deps caddy", self.deploy)
        self.assertLess(stage_index, validate_index)
        self.assertLess(validate_index, mutation_index)
        self.assertLess(mutation_index, promote_index)
        self.assertLess(validate_index, rebuild_index)
        self.assertLess(validate_index, reload_index)

    def test_compose_pins_and_hardens_the_terminal(self):
        service = self.compose.split("\n  fin-terminal:\n", 1)[1].split(
            "\n  fin-terminal-demo:\n", 1
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
        self.assertIn("PUBLIC_BASE_PATH: /fin-terminal/", service)
        self.assertIn("PUBLIC_BASE_PATH=/fin-terminal/", service)
        self.assertIn("ALLOWED_ORIGINS=https://unbrowser.unchainedsky.com", service)
        self.assertNotIn("https://unchainedsky.com", service)
        self.assertIn("read_only: true", service)
        self.assertIn("no-new-privileges:true", service)
        self.assertIn("pids_limit: 128", service)
        self.assertIn("mem_limit: 1g", service)
        self.assertIn("- fin_terminal_egress", service)
        self.assertIn("- unbrowser_mcp", service)
        self.assertNotIn("- fin_terminal_demo", service)
        self.assertNotIn("- app", service)

    def test_demo_service_is_a_self_resetting_public_kiosk(self):
        service = self.compose.split("\n  fin-terminal-demo:\n", 1)[1].split(
            "\n  unbrowser-egress:\n", 1
        )[0]

        self.assertIn(
            "781a656391cca0b783111568a84c64307c20382b",
            service,
        )
        self.assertIn("PUBLIC_BASE_PATH: /fin-terminal-demo/", service)
        self.assertIn("PUBLIC_BASE_PATH=/fin-terminal-demo/", service)
        self.assertIn("ALLOWED_ORIGINS=https://unbrowser.unchainedsky.com", service)
        self.assertNotIn("https://unchainedsky.com", service)
        self.assertIn("PUBLIC_DEMO=1", service)
        self.assertIn("DEMO_IDLE_SECONDS=300", service)
        self.assertIn("read_only: true", service)
        self.assertIn("no-new-privileges:true", service)
        self.assertIn(
            "MARKET_PROXY_TOKEN=${FIN_TERMINAL_DEMO_PROXY_TOKEN:?FIN_TERMINAL_DEMO_PROXY_TOKEN_required}",
            service,
        )
        self.assertIn("- fin_terminal_demo_egress", service)
        self.assertIn("- unbrowser_mcp_demo", service)
        self.assertNotIn("- fin_terminal_egress", service)
        self.assertNotIn("- unbrowser_mcp\n", service)
        self.assertNotIn("volumes:", service)

    def test_demo_caddy_site_injects_guest_without_auth(self):
        subdomain = self.caddy.split("unbrowser.unchainedsky.com {", 1)[1]
        route = subdomain.split("handle_path /fin-terminal-demo/*", 1)[1].split(
            "# The root reuses", 1
        )[0]

        self.assertIn("request_header -X-Fin-Terminal-User", route)
        self.assertIn("request_header -X-Fin-Terminal-Proxy-Token", route)
        self.assertIn("request_header -X-Real-IP", route)
        self.assertIn("request_header -Cookie", route)
        self.assertIn("request_header -Authorization", route)
        self.assertIn("request_header -Proxy-Authorization", route)
        self.assertNotIn("forward_auth", route)
        self.assertNotIn("rate_limit", route)
        self.assertIn("header_up X-Fin-Terminal-User guest", route)
        self.assertIn(
            "header_up X-Fin-Terminal-Proxy-Token {$FIN_TERMINAL_DEMO_PROXY_TOKEN}",
            route,
        )
        self.assertIn("header_up X-Real-IP {http.request.remote.host}", route)
        self.assertIn("header_up X-Fin-Terminal-User guest", route)
        self.assertNotIn("relay:8765", route)
        self.assertNotIn("mcp:8766", route)

    def test_demo_site_serves_unbrowser_page_at_root(self):
        route = self.caddy.split("unbrowser.unchainedsky.com {", 1)[1]

        self.assertIn("rewrite * /unbrowser", route)
        self.assertIn(
            "@primary_site_paths path /mcp /mcp/* /first-look /chrome-tax /install /install/*",
            route,
        )
        self.assertIn("handle_path /fin-terminal/*", route)
        self.assertIn("forward_auth web:8080", route)
        self.assertIn("handle_path /fin-terminal-demo/*", route)
        self.assertIn("@fin_terminal_base path /fin-terminal", route)
        self.assertIn(
            "redir @fin_terminal_base https://unbrowser.unchainedsky.com/fin-terminal/{?query} 308",
            route,
        )
        self.assertIn("@fin_terminal_demo_base path /fin-terminal-demo", route)
        self.assertIn(
            "redir @fin_terminal_demo_base https://unbrowser.unchainedsky.com/fin-terminal-demo/{?query} 308",
            route,
        )
        self.assertIn("@unbrowser_outbound path /go/unbrowser-connect", route)
        self.assertIn("handle /web/unbrowser/*", route)
        self.assertIn("handle /web/analytics/*", route)
        self.assertIn("handle /favicon.svg", route)
        self.assertIn('respond "Not found" 404', route)

    def test_demo_is_isolated_from_persistent_terminal_networks(self):
        caddy = self.compose.split("\n  caddy:\n", 1)[1].split("\n  relay:\n", 1)[0]
        mcp = self.compose.split("\n  unbrowser-mcp:\n", 1)[1].split(
            "\n  fin-terminal:\n", 1
        )[0]
        demo = self.compose.split("\n  fin-terminal-demo:\n", 1)[1].split(
            "\n  unbrowser-egress:\n", 1
        )[0]

        self.assertIn("- fin_terminal", caddy)
        self.assertIn("- fin_terminal_demo", caddy)
        self.assertIn("- unbrowser_mcp", mcp)
        self.assertIn("- unbrowser_mcp_demo", mcp)
        self.assertIn("- fin_terminal_demo", demo)
        self.assertIn("- fin_terminal_demo_egress", demo)
        self.assertIn("- unbrowser_mcp_demo", demo)
        self.assertNotIn("- fin_terminal\n", demo)
        self.assertNotIn("- fin_terminal_egress", demo)
        self.assertNotIn("- unbrowser_mcp\n", demo)

    def test_legacy_terminal_routes_redirect_to_separate_canonical_paths(self):
        canonical_demo = "https://unbrowser.unchainedsky.com/fin-terminal-demo/"
        canonical_terminal = "https://unbrowser.unchainedsky.com/fin-terminal/"
        legacy_demo = "@legacy_fin_terminal_demo path_regexp legacy_fin_terminal_demo"
        legacy_alias = "@legacy_fin_terminal_demo_alias path_regexp legacy_fin_terminal_demo_alias"

        self.assertIn(legacy_demo, self.caddy)
        self.assertIn(legacy_alias, self.caddy)
        self.assertIn(
            "^/unbrowser/fin-terminal-demo(?:/(.*))?$",
            self.caddy,
        )
        self.assertIn(
            "^/unbrowser/fin-terminal/demo(?:/(.*))?$",
            self.caddy,
        )
        self.assertIn(
            "^/unbrowser/fin-terminal(?:/(.*))?$",
            self.caddy,
        )
        self.assertIn(
            f"redir @legacy_fin_terminal_demo {canonical_demo}{{re.legacy_fin_terminal_demo.1}}{{?query}} 308",
            self.caddy,
        )
        self.assertIn(
            f"redir @legacy_fin_terminal_demo_alias {canonical_demo}{{re.legacy_fin_terminal_demo_alias.1}}{{?query}} 308",
            self.caddy,
        )
        self.assertIn("@legacy_fin_terminal {", self.caddy)
        self.assertIn(
            "not path /unbrowser/fin-terminal/demo /unbrowser/fin-terminal/demo/*",
            self.caddy,
        )
        self.assertIn(
            f"redir @legacy_fin_terminal {canonical_terminal}{{re.legacy_fin_terminal.1}}{{?query}} 308",
            self.caddy,
        )
        self.assertIn("@legacy_unbrowser_page path /unbrowser /unbrowser/", self.caddy)
        self.assertIn(
            "redir @legacy_unbrowser_page https://unbrowser.unchainedsky.com/{?query} 308",
            self.caddy,
        )

    def test_deploy_tracks_the_demo_service_and_route(self):
        self.assertIn("fin-terminal-demo", self.deploy)
        self.assertIn(
            "FIN_TERMINAL_DEMO_HOST",
            self.deploy,
        )
        self.assertIn('"https://$demo_host/"', self.deploy)
        self.assertIn('"https://$demo_host/fin-terminal-demo/"', self.deploy)
        self.assertIn('"https://$demo_host/fin-terminal/"', self.deploy)
        self.assertIn("/fin-terminal-demo/assets/", self.deploy)
        self.assertIn("unbrowser by Unchained - MCP Browser for LLM Agents", self.deploy)
        self.assertIn('"https://$demo_host/fin-terminal"', self.deploy)
        self.assertIn('[[ "$terminal_base_check" != "308 https://$demo_host/fin-terminal/" ]]', self.deploy)
        self.assertIn('"https://$demo_host/fin-terminal-demo"', self.deploy)
        self.assertIn('[[ "$demo_base_check" != "308 https://$demo_host/fin-terminal-demo/" ]]', self.deploy)
        self.assertIn('"https://$health_host/unbrowser/fin-terminal-demo/"', self.deploy)
        self.assertIn('[[ "$legacy_demo_check" != "308 https://$demo_host/fin-terminal-demo/" ]]', self.deploy)
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
        self.assertIn("ensure_staged_fin_terminal_secrets", self.deploy)
        self.assertIn("FIN_TERMINAL_SECRETS_CHANGED", self.deploy)
        self.assertIn("secrets.token_hex(32)", self.secrets_helper)
        self.assertIn("terminal_token == openrouter_key", self.secrets_helper)
        self.assertIn("demo_token == terminal_token", self.secrets_helper)
        self.assertIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", self.compose)
        self.assertIn(
            'docker compose up -d --no-deps --no-build --force-recreate "$service"',
            self.deploy,
        )


if __name__ == "__main__":
    unittest.main()
