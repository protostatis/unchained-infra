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
        remote_dir.joinpath("docker-compose.public-terminal.yml").write_text(
            "services: {}\n"
        )
        stage_dir.joinpath("Caddyfile").write_text("candidate config\n")
        stage_dir.joinpath(".env").write_text(
            "FIN_TERMINAL_PROXY_TOKEN=candidate-token\n"
        )
        stage_dir.joinpath("Dockerfile").write_text("FROM candidate\n")
        stage_dir.joinpath("Dockerfile.unbrowser-mcp").write_text(
            "FROM candidate-mcp\n"
        )
        stage_dir.joinpath("docker-compose.yml").write_text("services: {}\n")
        stage_dir.joinpath("docker-compose.public-terminal.yml").write_text(
            "services:\n  public: {}\n"
        )
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
        if env_file="$(env_file_arg "$@")"; then
            token="$(read_token "$env_file")"
            printf '{"services":{"caddy":{"image":"example.test/caddy@sha256:expected","environment":{"FIN_TERMINAL_PROXY_TOKEN":"%s"}}}}\\n' "$token"
        fi
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
            self.assertEqual(result.stdout.strip(), "environment_changed=true")
            self.assertEqual(live_caddy.stat().st_ino, before_inode)
            self.assertEqual(live_caddy.read_text(), "candidate config\n")
            self.assertEqual((remote_dir / ".env").read_text(), "FIN_TERMINAL_PROXY_TOKEN=candidate-token\n")
            self.assertEqual((remote_dir / "Dockerfile").read_text(), "FROM candidate\n")
            self.assertEqual(
                (remote_dir / "Dockerfile.unbrowser-mcp").read_text(),
                "FROM candidate-mcp\n",
            )
            self.assertEqual(
                (remote_dir / "docker-compose.public-terminal.yml").read_text(),
                "services:\n  public: {}\n",
            )

    def test_validation_requires_the_staged_public_terminal_overlay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, stage_dir = self._layout(Path(tmpdir))
            fake_bin, log_path, token_path = self._fake_docker(Path(tmpdir))
            stage_dir.joinpath("docker-compose.public-terminal.yml").unlink()

            result = self._run_helper(
                fake_bin,
                log_path,
                token_path,
                "validate",
                stage_dir,
                remote_dir,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("docker-compose.public-terminal.yml", result.stderr)
            self.assertFalse(log_path.exists())

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
            self.assertEqual(result.stdout.strip(), "environment_changed=false")
            self.assertEqual(live_env.stat().st_mode & 0o777, 0o600)


class FinTerminalDeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.compose = cls.repo_root.joinpath("docker-compose.yml").read_text()
        cls.public_compose = cls.repo_root.joinpath(
            "docker-compose.public-terminal.yml"
        ).read_text()
        cls.unbrowser_mcp_dockerfile = cls.repo_root.joinpath(
            "Dockerfile.unbrowser-mcp"
        ).read_text()
        cls.caddy = cls.repo_root.joinpath("Caddyfile").read_text()
        cls.deploy = cls.repo_root.joinpath("deploy.sh").read_text()
        cls.runtime_context = cls.repo_root.joinpath(
            "deploy", "runtime_context_files.sh"
        ).read_text()
        cls.pilot_doc = cls.repo_root.joinpath(
            "docs", "public-live-terminal-pilot.md"
        ).read_text()
        cls.secrets_helper = cls.repo_root.joinpath(
            "deploy", "ensure_fin_terminal_secrets.py"
        ).read_text()
        cls.caddy_preflight = cls.repo_root.joinpath(
            "deploy", "caddy_config_preflight.sh"
        ).read_text()
        cls.workflow = cls.repo_root.joinpath(
            ".github", "workflows", "ci.yml"
        ).read_text()
        cls.route_doc = cls.repo_root.joinpath(
            "docs", "fin-terminal-route.md"
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
        route = subdomain.split("handle @fin_terminal_singleton {", 1)[1].split(
            "# The static fin-terminal replay demo is retired.", 1
        )[0]
        self.assertIn("uri strip_prefix /fin-terminal", route)
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
        self.assertIn('[[ "$legacy_terminal_check" == "308 https://$public_host/fin-terminal/" ]]', self.deploy)
        self.assertIn('"https://$public_host/fin-terminal/"', self.deploy)
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
        self.assertIn("docker-compose.public-terminal.yml", self.caddy_preflight)
        self.assertIn("config --no-interpolate --quiet", self.caddy_preflight)
        self.assertNotIn("docker compose run --rm --no-deps caddy", self.deploy)
        self.assertLess(stage_index, validate_index)
        self.assertLess(validate_index, mutation_index)
        self.assertLess(mutation_index, promote_index)
        self.assertLess(validate_index, rebuild_index)
        self.assertLess(validate_index, reload_index)

    def test_compose_pins_and_hardens_the_terminal(self):
        service = self.compose.split("\n  fin-terminal:\n", 1)[1].split(
            "\n  unbrowser-egress:\n", 1
        )[0]

        self.assertIn(
            "e937377b945ed84d721ebd06e22510b5f805e19d",
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
        self.assertIn("PUBLIC_DEMO=0", service)
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

    def test_retired_demo_returns_404_tombstones(self):
        """All retired demo URLs must return direct 404 with no-store, no redirects."""
        subdomain = self.caddy.split("unbrowser.unchainedsky.com {", 1)[1]

        # The canonical demo URL must return 404 with Cache-Control: no-store
        self.assertIn("@fin_terminal_demo_retired path /fin-terminal-demo /fin-terminal-demo/*", subdomain)
        self.assertIn("respond \"Not found\" 404", subdomain)
        route = subdomain.split("@fin_terminal_demo_retired path /fin-terminal-demo /fin-terminal-demo/*", 1)[1].split(
            "# Opt-in live-session pilot.", 1
        )[0]
        self.assertIn("no-store", route)
        self.assertNotIn("reverse_proxy", route)
        self.assertNotIn("redir", route)
        self.assertNotIn("forward_auth", route)
        self.assertNotIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", route)

        # Legacy demo redirects on the main host must also return 404
        main_site = self.caddy.split("unchainedsky.com, www.unchainedsky.com, api.unchainedsky.com {", 1)[1].split(
            "unbrowser.unchainedsky.com {", 1
        )[0]
        self.assertIn("@legacy_fin_terminal_demo path /unbrowser/fin-terminal-demo", main_site)
        self.assertIn("respond \"Not found\" 404", main_site)

    def test_public_live_overlay_uses_reviewed_immutable_images(self):
        app_revision = "e937377b945ed84d721ebd06e22510b5f805e19d"
        redis_revision = (
            "redis:7.4.2-alpine@sha256:"
            "02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952"
        )

        self.assertEqual(self.public_compose.count(app_revision), 2)
        self.assertIn(redis_revision, self.public_compose)
        self.assertNotIn("FIN_TERMINAL_PUBLIC_APP_REF", self.public_compose)
        self.assertNotIn("image: redis:7.4.2-alpine\n", self.public_compose)

    def test_public_live_gateway_matches_the_reviewed_runtime_contract(self):
        gateway = self.public_compose.split(
            "\n  fin-terminal-public-gateway:\n", 1
        )[1].split("\n  fin-terminal-public-seat-01:\n", 1)[0]
        worker = self.public_compose.split(
            "x-fin-terminal-public-worker: &fin-terminal-public-worker\n", 1
        )[1].split("\nservices:\n", 1)[0]

        self.assertIn("VITE_TERMINAL_BUILD_MODE: public-live", gateway)
        self.assertIn("TERMINAL_RUNTIME_MODE=public-gateway", gateway)
        self.assertNotIn("PUBLIC_DEMO=", gateway)
        self.assertIn("PUBLIC_BASE_PATH=/fin-terminal-live-pilot/", gateway)
        self.assertIn(
            "PUBLIC_ALLOWED_ORIGIN=https://unbrowser.unchainedsky.com", gateway
        )
        self.assertIn(
            "PUBLIC_EDGE_PROXY_TOKEN=${FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN:",
            gateway,
        )
        self.assertIn("PUBLIC_TURNSTILE_EXPECTED_HOSTNAME=unbrowser.unchainedsky.com", gateway)
        self.assertIn("PUBLIC_MAX_SESSIONS=6", gateway)
        expected_endpoints = ",".join(
            f"seat-{value:02d}=http://fin-terminal-public-seat-{value:02d}:8787"
            for value in range(1, 7)
        )
        self.assertIn(f"PUBLIC_WORKER_ENDPOINTS={expected_endpoints}", gateway)
        for value in range(1, 7):
            suffix = f"{value:02d}"
            self.assertEqual(
                self.public_compose.count(
                    f"\n  fin-terminal-public-seat-{suffix}:\n"
                ),
                1,
            )
            self.assertIn(f"- fin_terminal_public_seat_{suffix}", self.public_compose)
            self.assertIn(f"- fin_terminal_public_mcp_{suffix}", self.public_compose)
            self.assertIn(f"- fin_terminal_public_egress_{suffix}", self.public_compose)
            self.assertIn(
                f"subnet: 10.253.0.{(value - 1) * 8}/29",
                self.public_compose,
            )
            self.assertIn(
                f"subnet: 10.253.0.{48 + (value - 1) * 8}/29",
                self.public_compose,
            )
            self.assertIn(
                f"subnet: 10.253.0.{96 + (value - 1) * 8}/29",
                self.public_compose,
            )
        self.assertNotIn("fin-terminal-public-seat-07", self.public_compose)
        self.assertIn("VITE_TERMINAL_BUILD_MODE: live", worker)
        self.assertIn("PUBLIC_SESSION_WORKER=1", worker)
        self.assertIn("MARKET_RESEARCH_CONCURRENCY=1", worker)
        self.assertIn(
            "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:",
            worker,
        )
        self.assertNotIn("FIN_TERMINAL_PUBLIC_OPENROUTER_API_KEY", worker)
        self.assertIn(
            "UNBROWSER_MCP_URL=http://fin-terminal-public-unbrowser-mcp:8767/mcp",
            worker,
        )
        self.assertNotIn("- unbrowser_mcp", worker)
        self.assertIn("read_only: true", worker)
        self.assertIn("cap_drop:\n    - ALL", worker)
        self.assertNotIn("ports:", self.public_compose)

        public_mcp = self.public_compose.split(
            "\n  fin-terminal-public-unbrowser-mcp:\n", 1
        )[1].split("\n  fin-terminal-public-gateway:\n", 1)[0]
        self.assertIn("dockerfile: Dockerfile.unbrowser-mcp", public_mcp)
        for value in range(1, 7):
            self.assertIn(f"- fin_terminal_public_mcp_{value:02d}", public_mcp)
        self.assertIn("- unbrowser_egress_proxy", public_mcp)
        self.assertIn("read_only: true", public_mcp)
        self.assertIn("cap_drop:\n      - ALL", public_mcp)

    def test_unbrowser_mcp_pins_a_compatible_patched_sdk_version(self):
        self.assertIn("mcp-proxy==0.12.0", self.unbrowser_mcp_dockerfile)
        self.assertIn("mcp==1.29.0", self.unbrowser_mcp_dockerfile)
        self.assertIn("pyunbrowser==0.0.18", self.unbrowser_mcp_dockerfile)

    def test_public_live_edge_route_is_authenticated_and_fail_closed(self):
        subdomain = self.caddy.split("unbrowser.unchainedsky.com {", 1)[1]
        route = subdomain.split("# Opt-in live-session pilot.", 1)[1].split(
            "# The root reuses", 1
        )[0]

        self.assertIn(
            "FIN_TERMINAL_PUBLIC_ENABLED=${FIN_TERMINAL_PUBLIC_ENABLED:-false}",
            self.compose,
        )
        self.assertIn(
            "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN=${FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN:",
            self.compose,
        )
        self.assertEqual(
            route.count("expression `{$FIN_TERMINAL_PUBLIC_ENABLED:false}`"), 2
        )
        self.assertIn("uri strip_prefix /fin-terminal-live-pilot", route)
        self.assertIn("log_skip", route)
        self.assertIn("request_header -X-Fin-Terminal-Edge-Token", route)
        self.assertIn("request_header -X-Real-IP", route)
        self.assertIn("request_header -Cookie", route)
        self.assertIn(
            "header_up X-Fin-Terminal-Edge-Token {$FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN}",
            route,
        )
        self.assertIn("header_up X-Real-IP {remote_host}", route)
        self.assertLess(
            route.index("request_header -X-Fin-Terminal-Edge-Token"),
            route.index("reverse_proxy fin-terminal-public-gateway:8788"),
        )
        self.assertNotIn("forward_auth", route)

    def test_public_live_overlay_is_staged_but_not_auto_started(self):
        self.assertIn('"docker-compose.public-terminal.yml"', self.runtime_context)
        self.assertIn(
            "docker-compose.public-terminal.yml Caddyfile unchained", self.deploy
        )
        self.assertIn(
            '"$remote_dir/docker-compose.public-terminal.yml"', self.deploy
        )
        self.assertNotIn("--profile fin-terminal-public-pilot", self.deploy)
        self.assertIn("profiles: [\"fin-terminal-public-pilot\"]", self.public_compose)
        self.assertNotIn("fin-terminal-public-pilot-10", self.public_compose)
        self.assertIn("FIN_TERMINAL_PUBLIC_ENABLED=false", self.pilot_doc)
        self.assertIn("Exactly six worker seats", self.pilot_doc)
        self.assertIn("unbrowser-fin-terminal/pull/13", self.pilot_doc)
        self.assertIn("not a hard spend", self.pilot_doc)
        self.assertIn("same `OPENROUTER_API_KEY` as the trial agent", self.pilot_doc)
        self.assertIn("Public Terminal Pilot", self.pilot_doc)
        self.assertIn("-f action=activate -f confirm='ACTIVATE SIX SEATS'", self.pilot_doc)
        self.assertIn("-f action=disable -f confirm='DISABLE PUBLIC PILOT'", self.pilot_doc)
        self.assertIn("restores the exact pre-activation", self.pilot_doc)
        self.assertRegex(self.pilot_doc, r"Never run\s+`docker compose down`")

    def test_turnstile_values_are_provisioned_through_protected_staging(self):
        capture_index = self.deploy.index(
            'TURNSTILE_SITE_KEY_INPUT="${FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY-}"'
        )
        unset_index = self.deploy.index(
            "unset FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY "
            "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET"
        )
        child_process_index = self.deploy.index('SCRIPT_DIR="$(cd')
        stage_index = self.deploy.index('echo "==> Staging prospective configuration..."')
        install_index = self.deploy.index(
            'echo "==> Provisioning staged Turnstile values..."',
            stage_index,
        )
        validate_index = self.deploy.index(
            'echo "==> Validating staged fin-terminal production secrets..."',
            install_index,
        )
        mutation_index = self.deploy.index("DEPLOY_MUTATED=true", validate_index)

        self.assertLess(capture_index, unset_index)
        self.assertLess(unset_index, child_process_index)
        self.assertIn(
            "export -n TURNSTILE_SITE_KEY_INPUT TURNSTILE_SECRET_INPUT",
            self.deploy,
        )
        self.assertLess(stage_index, install_index)
        self.assertLess(install_index, validate_index)
        self.assertLess(validate_index, mutation_index)
        self.assertIn("printf '%s\\0%s\\0'", self.deploy)
        self.assertIn("--install-public-turnstile", self.deploy)
        self.assertIn("--ensure-status", self.deploy)
        self.assertIn("turnstile_changed=true", self.deploy)
        self.assertIn("fin_terminal_credentials_changed=true", self.deploy)
        self.assertNotIn(
            'remote_bash "$TURNSTILE_SITE_KEY_INPUT"',
            self.deploy,
        )

        self.assertIn(
            "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY: "
            "${{ vars.FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY }}",
            self.workflow,
        )
        self.assertIn(
            "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET: "
            "${{ secrets.FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET }}",
            self.workflow,
        )
        self.assertIn("GitHub `production` Environment", self.pilot_doc)
        self.assertIn("never command arguments", self.pilot_doc)

    def test_demo_site_serves_unbrowser_page_at_root(self):
        route = self.caddy.split("unbrowser.unchainedsky.com {", 1)[1]

        self.assertIn("rewrite * /unbrowser", route)
        self.assertIn(
            "@primary_site_paths path /mcp /mcp/* /first-look /chrome-tax /install /install/*",
            route,
        )
        # The authenticated singleton is feature-gated behind the workspace
        # canary and strips its prefix before forward_auth.
        self.assertIn("@fin_terminal_singleton {", route)
        self.assertIn("uri strip_prefix /fin-terminal", route)
        self.assertIn("forward_auth web:8080", route)
        self.assertIn("@fin_terminal_base {", route)
        self.assertIn(
            "redir @fin_terminal_base https://unbrowser.unchainedsky.com/fin-terminal/{?query} 308",
            route,
        )
        # Retired demo returns 404 tombstone, never a redirect or upstream proxy
        self.assertIn("@fin_terminal_demo_retired path /fin-terminal-demo", route)
        self.assertIn("respond \"Not found\" 404", route)
        self.assertNotIn("@fin_terminal_demo_base", route)
        self.assertNotIn("reverse_proxy fin-terminal-demo:8788", route)
        self.assertIn("@unbrowser_outbound path /go/unbrowser-connect", route)
        self.assertIn("handle /web/unbrowser/*", route)
        self.assertIn("handle /web/analytics/*", route)
        self.assertIn("handle /favicon.svg", route)
        self.assertIn('respond "Not found" 404', route)

    def test_demo_service_and_network_are_absent(self):
        """fin-terminal-demo service and its dedicated network must not exist."""
        self.assertNotIn("fin-terminal-demo:", self.compose)
        self.assertNotIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", self.compose)
        self.assertNotIn("fin_terminal_demo", self.compose)

    def test_legacy_terminal_routes_redirect_to_separate_canonical_paths(self):
        canonical_terminal = "https://unbrowser.unchainedsky.com/fin-terminal/"

        # Legacy demo routes are retired — must return direct 404, not redirect
        self.assertIn('@legacy_fin_terminal_demo path /unbrowser/fin-terminal-demo', self.caddy)
        self.assertIn('@legacy_fin_terminal_demo_alias path /unbrowser/fin-terminal/demo', self.caddy)
        self.assertIn("respond \"Not found\" 404", self.caddy)
        self.assertNotIn("^/unbrowser/fin-terminal-demo(?:/(.*))?$", self.caddy)
        self.assertNotIn("^/unbrowser/fin-terminal/demo(?:/(.*))?$", self.caddy)

        # Legacy authenticated terminal must still redirect
        self.assertIn("@legacy_fin_terminal {", self.caddy)
        self.assertIn(
            "^/unbrowser/fin-terminal(?:/(.*))?$",
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

    def test_deploy_retires_demo_service_and_verifies_404(self):
        """Deploy must retire fin-terminal-demo and verify 404 on all former URLs."""
        self.assertIn("FIN_TERMINAL_PUBLIC_HOST", self.deploy)
        self.assertNotIn("FIN_TERMINAL_DEMO_HOST", self.deploy)
        self.assertIn('"https://$public_host/"', self.deploy)
        self.assertIn('"https://$public_host/fin-terminal/"', self.deploy)
        self.assertIn("unbrowser by Unchained - MCP Browser for LLM Agents", self.deploy)
        self.assertIn('"https://$public_host/fin-terminal"', self.deploy)
        self.assertIn('[[ "$terminal_base_check" != "308 https://$public_host/fin-terminal/" ]]', self.deploy)

        # Retired URLs checked for 404, not 200-content or 308-redirect.
        # Host-specific --resolve: each URL uses its own host, not a hardcoded one.
        self.assertIn('"$public_host|/fin-terminal-demo/"', self.deploy)
        self.assertIn('"$health_host|/unbrowser/fin-terminal-demo/"', self.deploy)
        self.assertIn('--resolve "$host:443:127.0.0.1"', self.deploy)
        self.assertIn('"$retired_check" != "404"', self.deploy)
        self.assertIn("retired fin-terminal-demo URL", self.deploy)
        # Covers canonical base/slash/ws + both apex legacy aliases with wildcard representatives
        self.assertIn("fin-terminal-demo/ws", self.deploy)
        self.assertIn("unbrowser/fin-terminal/demo/", self.deploy)
        self.assertIn("unbrowser/fin-terminal-demo/", self.deploy)
        self.assertNotIn("/fin-terminal-demo/assets/", self.deploy)
        self.assertNotIn('name="x-build-mode" content="replay"', self.deploy)
        # The retirement function checks for fin-terminal-demo in old/new compose
        self.assertIn("grep -qx fin-terminal-demo", self.deploy)

        # Retirement lifecycle: old container cleanup using backup compose
        self.assertIn("retire_fin_terminal_demo", self.deploy)
        self.assertIn("docker compose --project-directory \"$remote_dir\"", self.deploy)
        # Finds stopped containers too, verifies absence after rm
        self.assertIn("ps -aq fin-terminal-demo", self.deploy)
        self.assertIn("still present after removal", self.deploy)
        # Network removal uses label verification from backup Compose JSON
        self.assertIn("com.docker.compose.project", self.deploy)
        self.assertIn("com.docker.compose.network", self.deploy)
        # Network key queried from JSON and label value verified
        self.assertIn("fin_terminal_demo", self.deploy)

    def test_deploy_lifecycle_tracks_the_terminal(self):
        self.assertIn(
            "unbrowser-mcp fin-terminal web",
            self.deploy,
        )
        self.assertNotIn(
            "fin-terminal-demo",
            self.deploy.split("ALL_RUNTIME_SERVICES=", 1)[1].split("\n", 1)[0],
        )
        self.assertIn("ensure_staged_fin_terminal_secrets", self.deploy)
        self.assertIn("FIN_TERMINAL_SECRETS_CHANGED", self.deploy)
        self.assertIn("secrets.token_hex(32)", self.secrets_helper)
        self.assertIn("PUBLIC_TOKEN_NAMES", self.secrets_helper)
        self.assertIn("public_enabled and name in PUBLIC_TOKEN_NAMES", self.secrets_helper)
        self.assertIn("FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET", self.secrets_helper)
        self.assertNotIn("FIN_TERMINAL_PUBLIC_OPENROUTER_API_KEY", self.secrets_helper)
        self.assertNotIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", self.compose)
        # Retired demo token is scrubbed silently
        self.assertIn("RETIRED_TOKEN_NAMES", self.secrets_helper)
        self.assertIn("retired_token_prefixes", self.secrets_helper)
        self.assertIn(
            'add_services "caddy fin-terminal"', self.deploy
        )
        self.assertIn(
            'docker compose up -d --no-deps --no-build --force-recreate "$service"',
            self.deploy,
        )

    def test_rollback_restores_retained_images_without_rebuilding(self):
        self.assertIn("runtime-images.tsv", self.deploy)
        self.assertIn("unchained-deploy-rollback:${deploy_id}-${service}", self.deploy)
        self.assertIn('docker image tag "$image_id" "$rollback_ref"', self.deploy)
        self.assertIn('docker image tag "$rollback_ref" "$image_ref"', self.deploy)
        self.assertIn('[[ "$restored_id" == "$image_id" ]]', self.deploy)
        self.assertIn("release_remote_rollback_images", self.deploy)
        self.assertNotIn('docker compose build "${runtime_services[@]}"', self.deploy)

    def test_normal_deploy_requires_public_pilot_disabled_before_snapshot(self):
        lock_index = self.deploy.index("acquire_remote_deploy_lock\n")
        guard_index = self.deploy.index("assert_public_pilot_disabled_for_deploy\n")
        snapshot_index = self.deploy.index("snapshot_remote_release\n")
        self.assertLess(lock_index, guard_index)
        self.assertLess(guard_index, snapshot_index)
        self.assertIn(
            "normal deployment is blocked while the public terminal pilot is active",
            self.deploy,
        )

    def test_deploy_rollback_restores_exact_deployment_metadata(self):
        self.assertIn(
            'cp -p -- "$remote_dir/.deploy-current" "$backup_dir/.deploy-current"',
            self.deploy,
        )
        self.assertIn('deploy-current.state', self.deploy)
        self.assertIn(
            'cmp -s "$backup_dir/.deploy-current" "$remote_dir/.deploy-current"',
            self.deploy,
        )
        self.assertIn('rm -f -- "$remote_dir/.deploy-current"', self.deploy)
        metadata_index = self.deploy.rindex("write_deploy_metadata\n")
        committed_index = self.deploy.index("DEPLOY_SUCCEEDED=true", metadata_index)
        release_index = self.deploy.index(
            "release_remote_rollback_images", committed_index
        )
        self.assertLess(metadata_index, committed_index)
        self.assertLess(committed_index, release_index)

    def test_route_doc_lists_retired_demo_404_not_200(self):
        """Retired demo must be documented as returning 404, not conflated with root's 200."""
        self.assertIn("must return `200`", self.route_doc)
        self.assertIn("must return `404` (no-store, no redirect)", self.route_doc)
        self.assertNotIn("both the Unbrowser root and\n`https://unbrowser.unchainedsky.com/fin-terminal-demo/` must return `200`", self.route_doc)


if __name__ == "__main__":
    unittest.main()
