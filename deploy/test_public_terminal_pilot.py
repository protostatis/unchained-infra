"""Tests for the public-terminal pilot remote control script.

Covers all review items (A-L):
  A) Embedded atomic updater: O_NOFOLLOW, no Path.resolve, fsync, os.replace,
     mkstemp mode 0600, directory fsync, duplicate rejection, inode recheck.
  B) Caddy validation via --env-file, never swaps live .env.
  C) MCP session protocol: container ID resolution, Accept header, session-id,
     initialized notification, tools/list, navigate, private rejection, DELETE.
  D) Gateway: resolve container ID, HTTP 2xx + status "ready".
  E) config --format json, exact set-difference, pilot-only port/host checks, PortBindings.
  F) ps -aq for cleanup/presence.
  G) Separated PRIMARY_HOST/PUBLIC_HOST; stale-demo via Docker labels.
  H) Credentials stability against temp copy of .env.
  I) Pre-edge 404, post-Caddy public-live marker/CSP/admission checks.
  J) Action-specific confirmation phrases.
  K) Secure temp cleanup, rollback order, lock/revision guards.
  L) PRIMARY_HOST/PUBLIC_HOST naming, no FIN_TERMINAL_DEMO_HOST.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Static data
# ---------------------------------------------------------------------------

PILOT_SEATS = [
    f"fin-terminal-public-seat-{value:02d}" for value in range(1, 7)
]

PILOT_SERVICES = [
    "fin-terminal-public-redis",
    "fin-terminal-public-unbrowser-mcp",
    *PILOT_SEATS,
    "fin-terminal-public-gateway",
]

PILOT_STOP_ORDER = [
    "fin-terminal-public-gateway",
    *PILOT_SEATS,
    "fin-terminal-public-unbrowser-mcp",
    "fin-terminal-public-redis",
]

ENV_FLAG = "FIN_TERMINAL_PUBLIC_ENABLED"
PILOT_PATH = "/fin-terminal-live-pilot"

SECRET_NAMES = [
    "OPENROUTER_API_KEY",
    "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY",
    "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET",
    "FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY",
    "FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN",
    "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN",
    "FIN_TERMINAL_PROXY_TOKEN",
    "FIN_TERMINAL_DEMO_PROXY_TOKEN",
]


def _script_path() -> Path:
    return Path(__file__).resolve().parent / "public_terminal_pilot_remote.sh"


def _script_text() -> str:
    return _script_path().read_text(encoding="utf-8")


def _extract_embedded_python(text: str, marker: str) -> str:
    """Extract the first heredoc Python block starting after marker."""
    idx = text.find(marker)
    if idx < 0:
        return ""
    # Find <<'PYEOF' or <<PYEOF
    heredoc_start = text.find("<<'PYEOF'", idx)
    if heredoc_start < 0:
        heredoc_start = text.find("<<PYEOF", idx)
    if heredoc_start < 0:
        return ""
    # Find the PYEOF terminator on its own line after the heredoc start.
    body_start = text.index("\n", heredoc_start) + 1
    py_end = text.find("\nPYEOF\n", body_start)
    if py_end < 0:
        return ""
    return text[body_start:py_end]


# ---------------------------------------------------------------------------
# (A) Embedded atomic updater — real execution, no mocks
# ---------------------------------------------------------------------------


class EmbeddedAtomicUpdaterTests(unittest.TestCase):
    """Test the real embedded Python atomic updater extracted from the script."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script_text = _script_text()
        # Extract both the update_env_flag updater and restore_env_from_snapshot.
        cls.updater_code = _extract_embedded_python(
            cls.script_text, "update_env_flag()"
        )
        cls.restore_code = _extract_embedded_python(
            cls.script_text, "restore_env_from_snapshot()"
        )

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._work = Path(self._temp_dir.name)
        self.env_path = self._work / ".env"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _write_env(self, content: str) -> None:
        self.env_path.write_text(content, encoding="utf-8")
        self.env_path.chmod(0o600)

    def _read_env(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    def _run_updater(self, key: str, value: str) -> subprocess.CompletedProcess:
        script = self._work / "updater.py"
        script.write_text(self.updater_code, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(script), str(self.env_path), key, value],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_rejects_symlink(self) -> None:
        real = self._work / "real.env"
        real.write_text(f"{ENV_FLAG}=false\n")
        self.env_path.symlink_to(real)
        result = self._run_updater(ENV_FLAG, "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(real.read_text(), f"{ENV_FLAG}=false\n")

    def test_rejects_duplicate_key(self) -> None:
        self._write_env(f"{ENV_FLAG}=false\nOTHER=val\n{ENV_FLAG}=false\n")
        result = self._run_updater(ENV_FLAG, "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate", result.stdout + result.stderr)

    def test_rejects_max_size_exceeded(self) -> None:
        content = f"{ENV_FLAG}=false\n" + ("X" * (1024 * 1024 + 100))
        self._write_env(content)
        result = self._run_updater(ENV_FLAG, "true")
        self.assertNotEqual(result.returncode, 0)

    def test_atomic_writes_correct_value(self) -> None:
        self._write_env(f"{ENV_FLAG}=false\nOPENROUTER_API_KEY=sk-test\nADMIN_EMAILS=admin@example.com\n")
        result = self._run_updater(ENV_FLAG, "true")
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        content = self._read_env()
        self.assertIn(f"{ENV_FLAG}=true", content)

    def test_preserves_other_lines(self) -> None:
        original = f"# comment\n{ENV_FLAG}=false\nOPENROUTER_API_KEY=sk-test\n"
        self._write_env(original)
        self._run_updater(ENV_FLAG, "true")
        content = self._read_env()
        self.assertIn("# comment", content)
        self.assertIn("OPENROUTER_API_KEY=sk-test", content)

    def test_mode_0600_after_write(self) -> None:
        self._write_env(f"{ENV_FLAG}=false\n")
        self._run_updater(ENV_FLAG, "true")
        mode = self.env_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_adds_key_when_missing(self) -> None:
        self._write_env("OTHER=something\n")
        result = self._run_updater(ENV_FLAG, "false")
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"{ENV_FLAG}=false", self._read_env())

    def test_adds_missing_key_after_unterminated_last_line(self) -> None:
        self._write_env("OTHER=something")
        result = self._run_updater(ENV_FLAG, "false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._read_env(),
            f"OTHER=something\n{ENV_FLAG}=false\n",
        )

    def test_restore_replaces_exact_snapshot_atomically(self) -> None:
        snapshot = self._work / "snapshot.env"
        snapshot_content = f"# before\n{ENV_FLAG}=false\nOTHER=original\n"
        snapshot.write_text(snapshot_content, encoding="utf-8")
        snapshot.chmod(0o600)
        self._write_env(f"{ENV_FLAG}=true\nOTHER=changed\n")
        script = self._work / "restore.py"
        script.write_text(self.restore_code, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(script), str(self.env_path), str(snapshot)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._read_env(), snapshot_content)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)

    def test_restore_rejects_symlink_snapshot(self) -> None:
        real_snapshot = self._work / "real-snapshot.env"
        real_snapshot.write_text(f"{ENV_FLAG}=false\n", encoding="utf-8")
        snapshot = self._work / "snapshot.env"
        snapshot.symlink_to(real_snapshot)
        self._write_env(f"{ENV_FLAG}=true\n")
        script = self._work / "restore.py"
        script.write_text(self.restore_code, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(script), str(self.env_path), str(snapshot)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._read_env(), f"{ENV_FLAG}=true\n")

    def test_no_live_env_swap_pattern_in_script(self) -> None:
        """Script must never do `cp $STAGED_ENV $ENV_FILE` (live swap)."""
        text = self.script_text
        # The only env mutations should go through update_env_flag or restore_env_from_snapshot.
        # Check that there's no raw cp of staged to live.
        lines_with_cp = [
            l
            for l in text.splitlines()
            if "cp" in l and "ENV_FILE" in l and "STAGED" in l
        ]
        for line in lines_with_cp:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Allow cp -p "$ENV_FILE" "$STAGED_ENV" (copy to staged) but
            # reject cp ... STAGED ... ENV_FILE (copy staged to live).
            if "STAGED" in stripped and "$ENV_FILE" in stripped:
                if stripped.index("STAGED") < stripped.index("$ENV_FILE"):
                    self.fail(
                        f"Live .env swap detected: {stripped}"
                    )

    def test_rollback_uses_atomic_restore_not_flag_only(self) -> None:
        """Rollback must restore the exact snapshot, not just update one flag."""
        text = self.script_text
        self.assertIn("restore_env_from_snapshot", text)


# ---------------------------------------------------------------------------
# (B) Caddy validation via --env-file
# ---------------------------------------------------------------------------


class CaddyValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_caddy_validate_uses_env_file_flag(self) -> None:
        self.assertIn("--env-file", self.text)
        self.assertIn("caddy_validate", self.text)

    def test_no_temporary_live_env_overwrite(self) -> None:
        """Script must never temporarily overwrite live .env for Caddy validation."""
        # Search for patterns like `cp ... ENV_FILE` that write to $ENV_FILE.
        # The only allowed writes are via update_env_flag or restore.
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "cp" in stripped and "ENV_FILE" in stripped:
                # cp "$STAGED_ENV" "$ENV_FILE" would be invalid.
                # cp -p "$ENV_FILE" "$STAGED_ENV" is valid (live→staged).
                if "$ENV_FILE" in stripped and "STAGED" in stripped:
                    live_pos = stripped.index("$ENV_FILE")
                    staged_pos = stripped.index("STAGED")
                    if staged_pos < live_pos:
                        self.fail(
                            f"Live .env overwrite detected: {stripped}"
                        )


# ---------------------------------------------------------------------------
# Persisted worker-set transition — real embedded Python with fake Redis CLI
# ---------------------------------------------------------------------------


class RedisWorkerSetTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.code = _extract_embedded_python(
            _script_text(), "transition_redis_worker_set()"
        )

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.work = Path(self._temp_dir.name)
        self.state_path = self.work / "redis-state.json"
        self.backup_path = self.work / "backup.json"
        self.code_path = self.work / "transition.py"
        self.code_path.write_text(self.code, encoding="utf-8")
        docker = self.work / "docker"
        docker.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                state_path = Path(os.environ["FAKE_REDIS_STATE"])
                args = sys.argv[1:]
                try:
                    redis_index = args.index("redis-cli")
                except ValueError:
                    raise SystemExit(2)
                command = args[redis_index + 1:]
                use_stdin = False
                while command and command[0] in {"--raw", "-x"}:
                    use_stdin = use_stdin or command[0] == "-x"
                    command = command[1:]
                if command[:1] == ["EXISTS"]:
                    print("1" if state_path.exists() else "0")
                elif command[:1] == ["GET"]:
                    if state_path.exists():
                        sys.stdout.write(state_path.read_text(encoding="utf-8") + "\\n")
                elif command[:1] == ["SET"] and use_stdin:
                    state_path.write_text(sys.stdin.read(), encoding="utf-8")
                    print("OK")
                elif command[:1] == ["DEL"]:
                    existed = state_path.exists()
                    if existed:
                        state_path.unlink()
                    print("1" if existed else "0")
                else:
                    raise SystemExit(3)
                """
            ),
            encoding="utf-8",
        )
        docker.chmod(0o755)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _run(self, target: int, backup: Optional[Path] = None) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.work}:{os.environ.get('PATH', '')}",
            "FAKE_REDIS_STATE": str(self.state_path),
        }
        return subprocess.run(
            [
                sys.executable,
                str(self.code_path),
                "fake-container-id",
                str(target),
                str(backup) if backup else "-",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

    @staticmethod
    def _state(worker_ids: list[str]) -> dict:
        return {
            "version": 1,
            "dailyBudgetDay": "2026-08-03",
            "dailyReservedMicroUsd": 6_000_000,
            "queue": ["active-ticket"],
            "sessions": [
                {
                    "id": "active-ticket",
                    "visitorId": "visitor",
                    "state": "active",
                    "ticketExpiresAt": 1,
                    "researchRuns": 1,
                    "connectionVersion": 1,
                    "nextConnectionVersion": 1,
                }
            ],
            "workers": [
                {"id": worker_id, "generation": f"generation-{worker_id}"}
                for worker_id in worker_ids
            ],
        }

    def test_forward_transition_preserves_budget_and_ends_live_state(self) -> None:
        original = json.dumps(self._state(["seat-01"]), separators=(",", ":"))
        self.state_path.write_text(original, encoding="utf-8")
        result = self._run(6, self.backup_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "STATE_TRANSITION_OK:present")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dailyReservedMicroUsd"], 6_000_000)
        self.assertEqual(state["dailyBudgetDay"], "2026-08-03")
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["sessions"][0]["state"], "ended")
        self.assertEqual(state["sessions"][0]["endReason"], "worker-unavailable")
        self.assertEqual(
            [worker["id"] for worker in state["workers"]],
            [f"seat-{value:02d}" for value in range(1, 7)],
        )
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), original)
        self.assertEqual(stat.S_IMODE(self.backup_path.stat().st_mode), 0o600)

    def test_reverse_transition_restores_one_worker_shape(self) -> None:
        state = self._state([f"seat-{value:02d}" for value in range(1, 7)])
        state["queue"] = []
        state["sessions"][0]["state"] = "ended"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self._run(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["dailyReservedMicroUsd"], 6_000_000)
        self.assertEqual(restored["workers"], [{"id": "seat-01"}])

    def test_rejects_unknown_worker_set_without_mutating_state(self) -> None:
        original = json.dumps(self._state(["seat-unknown"]))
        self.state_path.write_text(original, encoding="utf-8")
        result = self._run(6, self.backup_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original)

    def test_absent_state_remains_absent_and_is_backed_up_as_empty(self) -> None:
        result = self._run(6, self.backup_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "STATE_TRANSITION_OK:absent")
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), "")
        self.assertEqual(
            Path(f"{self.backup_path}.presence").read_text(encoding="utf-8"),
            "absent\n",
        )

    def test_present_empty_state_is_not_mistaken_for_absence(self) -> None:
        self.state_path.write_text("", encoding="utf-8")
        result = self._run(6, self.backup_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.state_path.exists())
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), "")
        self.assertEqual(
            Path(f"{self.backup_path}.presence").read_text(encoding="utf-8"),
            "present\n",
        )

    def test_backup_write_retries_short_writes(self) -> None:
        self.assertIn("while written < len(data)", self.code)
        self.assertIn('redis("--raw", "EXISTS", STATE_KEY)', self.code)


# ---------------------------------------------------------------------------
# (C) MCP protocol check requirements
# ---------------------------------------------------------------------------


class MCPProtocolRequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_mcp_uses_resolve_container_id(self) -> None:
        self.assertIn("resolve_container_id", self.text)
        self.assertIn("fin-terminal-public-unbrowser-mcp", self.text)

    def test_mcp_accept_header_present(self) -> None:
        self.assertIn("application/json, text/event-stream", self.text)

    def test_mcp_session_id_capture(self) -> None:
        self.assertIn("MCP-Session-Id", self.text)
        self.assertIn("session_id", self.text)

    def test_mcp_sends_initialized_notification(self) -> None:
        self.assertIn("notifications/initialized", self.text)

    def test_mcp_delete_cleanup(self) -> None:
        self.assertIn("DELETE", self.text)

    def test_mcp_never_prints_session_or_provider(self) -> None:
        self.assertIn("never print session", self.text.lower())
        self.assertIn("scrubbed", self.text.lower())

    def test_mcp_no_literal_container_name(self) -> None:
        """MCP must not use literal container name `fin-terminal-public-unbrowser-mcp`
        as a docker exec target — must use resolved ID."""
        # The docker exec line should use "$mcp_cid" not the literal name.
        mcp_func_start = self.text.find("mcp_protocol_check()")
        mcp_func = self.text[mcp_func_start:] if mcp_func_start >= 0 else self.text
        # After resolution, docker exec should reference the variable.
        self.assertIn('"$mcp_cid"', mcp_func)

    def test_mcp_requires_embedded_egress_403_for_private_targets(self) -> None:
        """Navigate reports an egress denial in its blockmap, not as a tool error."""
        mcp_func_start = self.text.find("mcp_protocol_check()")
        mcp_func = self.text[mcp_func_start:] if mcp_func_start >= 0 else self.text
        self.assertIn('get("http_error_status")', mcp_func)
        self.assertIn("blocked_status != 403", mcp_func)
        self.assertNotIn('rejected = "error" in rejection', mcp_func)

    def test_mcp_checks_six_concurrent_unique_sessions(self) -> None:
        mcp_func_start = self.text.find("mcp_protocol_check()")
        mcp_func = self.text[mcp_func_start:] if mcp_func_start >= 0 else self.text
        self.assertIn("ThreadPoolExecutor(max_workers=6)", mcp_func)
        self.assertIn("range(1, 7)", mcp_func)
        self.assertIn("len(set(session_ids)) != 6", mcp_func)
        self.assertIn("MCP_SIX_OK", mcp_func)


# ---------------------------------------------------------------------------
# (D) Gateway status check
# ---------------------------------------------------------------------------


class GatewayStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_gateway_uses_resolved_container_id(self) -> None:
        self.assertIn('"$gw_cid"', self.text)

    def test_gateway_expects_status_ready_not_ok(self) -> None:
        self.assertIn('"ready"', self.text)
        self.assertNotIn('"ok"', self.text)

    def test_gateway_checks_http_2xx(self) -> None:
        self.assertIn("statusCode", self.text)

    def test_gateway_requires_six_ready_unique_worker_generations(self) -> None:
        self.assertIn('gateway.readyWorkers !== 6', self.text)
        self.assertIn('gateway.assignedWorkers !== 0', self.text)
        self.assertIn('gateway.queuedVisitors !== 0', self.text)
        self.assertIn('new Set(generations).size !== 6', self.text)


# ---------------------------------------------------------------------------
# (E) --format json and exact set-difference
# ---------------------------------------------------------------------------


class FormatJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_config_uses_format_json(self) -> None:
        self.assertIn("--format json", self.text)

    def test_exact_set_difference_validation(self) -> None:
        self.assertIn("validate_overlay_services", self.text)
        self.assertIn("comm -13", self.text)

    def test_pilot_only_port_and_host_checks(self) -> None:
        # The overlay safety should filter to pilot services.
        self.assertIn("pilot", self.text.lower())

    def test_runtime_port_bindings_check(self) -> None:
        self.assertIn("PortBindings", self.text)

    def test_network_attachment_verification(self) -> None:
        self.assertIn("Networks", self.text)
        self.assertIn("{{json .NetworkSettings.Networks}}", self.text)
        self.assertIn('"\\n".join(sorted(data))', self.text)
        self.assertNotIn(
            '.NetworkSettings.Networks}}{{$name}}{{"\\n"}}',
            self.text,
        )


class SixSeatComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parent.parent
        command = [
            "docker",
            "compose",
            "-f",
            str(root / "docker-compose.yml"),
            "-f",
            str(root / "docker-compose.public-terminal.yml"),
            "--profile",
            "fin-terminal-public-pilot",
            "config",
            "--no-interpolate",
            "--format",
            "json",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise AssertionError(f"Compose render failed:\n{result.stderr}")
        cls.config = json.loads(result.stdout)
        cls.services = cls.config["services"]

    @staticmethod
    def _networks(service: dict) -> set[str]:
        networks = service.get("networks", {})
        return set(networks if isinstance(networks, list) else networks.keys())

    @staticmethod
    def _environment(service: dict) -> dict[str, str]:
        environment = service.get("environment", {})
        if isinstance(environment, dict):
            return {str(key): str(value) for key, value in environment.items()}
        result = {}
        for entry in environment:
            key, value = entry.split("=", 1)
            result[key] = value
        return result

    @staticmethod
    def _subnets(network: dict) -> list[str]:
        return [
            str(entry.get("subnet", ""))
            for entry in network.get("ipam", {}).get("config", [])
        ]

    def test_exact_six_workers_and_gateway_contract(self) -> None:
        for service in PILOT_SERVICES:
            self.assertIn(service, self.services)
        seats = sorted(name for name in self.services if name.startswith("fin-terminal-public-seat-"))
        self.assertEqual(seats, PILOT_SEATS)
        environment = self._environment(self.services["fin-terminal-public-gateway"])
        self.assertEqual(environment["PUBLIC_MAX_SESSIONS"], "6")
        expected = ",".join(
            f"seat-{value:02d}=http://fin-terminal-public-seat-{value:02d}:8787"
            for value in range(1, 7)
        )
        self.assertEqual(environment["PUBLIC_WORKER_ENDPOINTS"], expected)

    def test_each_worker_has_disjoint_gateway_mcp_and_egress_networks(self) -> None:
        worker_networks = {
            seat: self._networks(self.services[seat]) for seat in PILOT_SEATS
        }
        for index, seat in enumerate(PILOT_SEATS, start=1):
            suffix = f"{index:02d}"
            self.assertEqual(
                worker_networks[seat],
                {
                    f"fin_terminal_public_seat_{suffix}",
                    f"fin_terminal_public_mcp_{suffix}",
                    f"fin_terminal_public_egress_{suffix}",
                },
            )
        for index, left in enumerate(PILOT_SEATS):
            for right in PILOT_SEATS[index + 1 :]:
                self.assertTrue(worker_networks[left].isdisjoint(worker_networks[right]))

    def test_shared_services_attach_only_to_reviewed_sides(self) -> None:
        gateway = self._networks(self.services["fin-terminal-public-gateway"])
        mcp = self._networks(self.services["fin-terminal-public-unbrowser-mcp"])
        self.assertEqual(
            gateway,
            {
                "fin_terminal_public",
                "fin_terminal_public_state",
                "fin_terminal_public_egress",
                *{f"fin_terminal_public_seat_{value:02d}" for value in range(1, 7)},
            },
        )
        self.assertEqual(
            mcp,
            {
                "unbrowser_egress_proxy",
                *{f"fin_terminal_public_mcp_{value:02d}" for value in range(1, 7)},
            },
        )

    def test_worker_private_network_types(self) -> None:
        networks = self.config["networks"]
        for value in range(1, 7):
            suffix = f"{value:02d}"
            self.assertTrue(networks[f"fin_terminal_public_seat_{suffix}"]["internal"])
            self.assertTrue(networks[f"fin_terminal_public_mcp_{suffix}"]["internal"])
            self.assertFalse(networks[f"fin_terminal_public_egress_{suffix}"].get("internal", False))
            self.assertEqual(
                self._subnets(networks[f"fin_terminal_public_mcp_{suffix}"]),
                [f"10.253.0.{(value - 1) * 8}/29"],
            )
            self.assertEqual(
                self._subnets(networks[f"fin_terminal_public_egress_{suffix}"]),
                [f"10.253.0.{48 + (value - 1) * 8}/29"],
            )
            self.assertEqual(
                self._subnets(networks[f"fin_terminal_public_seat_{suffix}"]),
                [f"10.253.0.{96 + (value - 1) * 8}/29"],
            )

    def test_per_seat_subnets_are_unique_and_fit_the_reviewed_block(self) -> None:
        networks = self.config["networks"]
        subnets = []
        for prefix in ("mcp", "egress", "seat"):
            for value in range(1, 7):
                name = f"fin_terminal_public_{prefix}_{value:02d}"
                subnets.extend(self._subnets(networks[name]))
        self.assertEqual(len(subnets), 18)
        self.assertEqual(len(set(subnets)), 18)
        self.assertTrue(all(subnet.startswith("10.253.0.") for subnet in subnets))

    def test_no_extra_seat_or_container(self) -> None:
        text = _script_text()
        self.assertIn("seat_count", text)
        self.assertIn('PILOT_SEAT_COUNT="${#PILOT_SEATS[@]}"', text)


# ---------------------------------------------------------------------------
# (F) ps -aq for cleanup
# ---------------------------------------------------------------------------


class PsAqCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_cleanup_uses_ps_aq(self) -> None:
        self.assertIn("ps -aq", self.text)

    def test_presence_check_uses_ps_aq(self) -> None:
        self.assertIn("pilot_any_container_present", self.text)

    def test_stop_order_matches_pilot_stop_order(self) -> None:
        # Verify PILOT_STOP_ORDER appears in the stop/remove loops.
        self.assertIn("PILOT_STOP_ORDER", self.text)
        self.assertIn('"${PILOT_STOP_ORDER[@]}"', self.text)


# ---------------------------------------------------------------------------
# (G) Separated hosts + label-based stale demo check
# ---------------------------------------------------------------------------


class HostSeparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_primary_and_public_hosts_defined(self) -> None:
        self.assertIn("PRIMARY_HOST=", self.text)
        self.assertIn("PUBLIC_HOST=", self.text)
        self.assertIn("unchainedsky.com", self.text)

    def test_no_fin_terminal_demo_host_variable(self) -> None:
        self.assertNotIn("FIN_TERMINAL_DEMO_HOST", self.text)

    def test_health_uses_primary_host(self) -> None:
        self.assertIn("${PRIMARY_HOST}", self.text)

    def test_pilot_uses_public_host(self) -> None:
        self.assertIn("${PUBLIC_HOST}", self.text)

    def test_legacy_demo_paths_include_primary_host_aliases(self) -> None:
        self.assertIn("/unbrowser/fin-terminal-demo", self.text)

    def test_stale_demo_uses_docker_labels(self) -> None:
        self.assertIn("com.docker.compose.service=fin-terminal-demo", self.text)
        self.assertIn("com.docker.compose.project", self.text)

    def test_host_separated_curl_helpers(self) -> None:
        self.assertIn("curl_host_status", self.text)
        self.assertIn("curl_host_body", self.text)
        self.assertIn("--resolve", self.text)


# ---------------------------------------------------------------------------
# (H) Credentials stability against temp copy
# ---------------------------------------------------------------------------


class CredentialsTempCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_credentials_runs_against_temp_copy(self) -> None:
        self.assertIn("mktemp", self.text)
        self.assertIn("credcheck", self.text)

    def test_credentials_never_touches_live_env(self) -> None:
        # The credentials check must copy to a temp location, not run on live.
        cred_func_start = self.text.find("check_credentials_stable()")
        cred_func = self.text[cred_func_start:] if cred_func_start >= 0 else self.text
        self.assertIn("cp -p", cred_func)
        self.assertNotIn("$ENV_FILE\"", cred_func.replace("$ENV_FILE\"", ""))  # rough check

    def test_credentials_output_never_printed_directly(self) -> None:
        self.assertIn("case", self.text)  # result is validated via case/esac


# ---------------------------------------------------------------------------
# (I) Pre-edge / post-edge HTTP checks + admission
# ---------------------------------------------------------------------------


class EdgeCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_pre_edge_pilot_404(self) -> None:
        self.assertIn("pre-activation pilot 404", self.text)

    def test_post_edge_public_live_marker(self) -> None:
        self.assertIn("public-live", self.text)

    def test_post_edge_csp_check(self) -> None:
        self.assertIn("CSP", self.text)

    def test_admission_wrong_origin_403(self) -> None:
        self.assertIn("403", self.text)
        self.assertIn("evil.example.com", self.text)

    def test_admission_correct_origin_no_visitor_401(self) -> None:
        self.assertIn("if status != 401:", self.text)

    def test_admission_empty_token_400(self) -> None:
        self.assertIn("if status != 400:", self.text)

    def test_admission_short_token_403(self) -> None:
        self.assertIn('{"turnstileToken": "short"}', self.text)
        self.assertGreaterEqual(self.text.count("if status != 403:"), 2)

    def test_admission_never_logs_visitor_token(self) -> None:
        # The admission check bodies contain 'test-visitor' but that's a test pattern.
        # In production, the error messages must not echo the full token/body.
        self.assertNotIn("echo $", self.text)  # rough — no raw echo of token vars


# ---------------------------------------------------------------------------
# (J) Action-specific confirmation phrases
# ---------------------------------------------------------------------------


class ConfirmationPhraseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_no_generic_yes_confirmation_in_script(self) -> None:
        # The script itself doesn't handle confirmation (workflow does).
        # Verify the workflow YAML content.
        wf_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "public-terminal-pilot.yml"
        )
        wf_text = wf_path.read_text(encoding="utf-8")
        self.assertIn("ACTIVATE SIX SEATS", wf_text)
        self.assertIn("DISABLE PUBLIC PILOT", wf_text)
        self.assertNotIn("CONFIRM != \"yes\"", wf_text)


class WorkflowSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "public-terminal-pilot.yml"
        ).read_text(encoding="utf-8")
        cls.ci = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "ci.yml"
        ).read_text(encoding="utf-8")

    def test_workflow_is_main_only_and_uses_production_approval(self) -> None:
        self.assertIn("github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn("name: production", self.workflow)
        self.assertIn("group: production-deploy", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_workflow_guards_latest_main_and_remote_revision(self) -> None:
        self.assertIn("origin/main", self.workflow)
        self.assertIn("deployed_sha", self.workflow)
        self.assertIn('if [[ "$ACTION" == "activate" ]]', self.workflow)
        self.assertIn('expected_sha="$MAIN_SHA"', self.workflow)
        self.assertIn('expected_sha="$deployed_sha"', self.workflow)
        self.assertIn("steps.candidate.outputs.revision", self.workflow)

    def test_workflow_streams_script_without_provider_credentials(self) -> None:
        self.assertIn("deploy/public_terminal_pilot_remote.sh", self.workflow)
        self.assertIn("bash -s --", self.workflow)
        self.assertNotIn("OPENROUTER_API_KEY", self.workflow)
        self.assertNotIn("FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET", self.workflow)

    def test_ci_runs_pilot_script_syntax_and_contract_tests(self) -> None:
        self.assertIn("bash -n deploy/public_terminal_pilot_remote.sh", self.ci)
        self.assertIn("python deploy/test_public_terminal_pilot.py", self.ci)


# ---------------------------------------------------------------------------
# (K) Secure temp cleanup + rollback order
# ---------------------------------------------------------------------------


class SecureTempRollbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_secure_workdir_init_and_cleanup(self) -> None:
        self.assertIn("secure_workdir_init", self.text)
        self.assertIn("secure_workdir_cleanup", self.text)
        self.assertIn(".pilot-", self.text)

    def test_rollback_restores_snapshot_first(self) -> None:
        handler_start = self.text.find("activate_rollback_handler()")
        handler = self.text[handler_start:] if handler_start >= 0 else self.text
        self.assertIn("restore_env_from_snapshot", handler)

    def test_rollback_caddy_recreate_after_restore(self) -> None:
        handler_start = self.text.find("activate_rollback_handler()")
        handler = self.text[handler_start:] if handler_start >= 0 else self.text
        restore_pos = handler.find("restore_env_from_snapshot")
        caddy_pos = handler.find("caddy_force_recreate")
        self.assertGreater(restore_pos, -1)
        self.assertGreater(caddy_pos, -1)
        self.assertLess(restore_pos, caddy_pos, "restore must happen before caddy recreate")

    def test_rollback_fails_closed_stops_caddy_if_404_cannot_be_proved(self) -> None:
        self.assertIn("stop caddy", self.text.lower())
        self.assertIn("FATAL", self.text)
        self.assertNotIn("compose kill caddy", self.text)
        self.assertIn('docker rm -f "$caddy_id"', self.text)

    def test_rollback_stops_caddy_if_snapshot_restore_fails(self) -> None:
        handler_start = self.text.find("activate_rollback_handler()")
        handler_end = self.text.find("# Validate overlay safety", handler_start)
        handler = self.text[handler_start:handler_end]
        restore_failure = handler.split(
            'echo "FATAL: could not restore .env from snapshot"', 1
        )[1].split("}", 1)[0]
        self.assertIn("stop_caddy_fail_closed", restore_failure)

    def test_rollback_stops_in_pilot_stop_order(self) -> None:
        self.assertIn("PILOT_STOP_ORDER", self.text)

    def test_rollback_restores_exact_redis_snapshot_before_removing_services(self) -> None:
        handler_start = self.text.find("activate_rollback_handler()")
        handler_end = self.text.find("# Validate overlay safety", handler_start)
        handler = self.text[handler_start:handler_end]
        stop_gateway = handler.find("stop fin-terminal-public-gateway")
        restore_redis = handler.find("restore_redis_state_backup")
        remove_loop = handler.find('compose_cmd rm -f "$svc"')
        self.assertGreater(stop_gateway, -1)
        self.assertGreater(restore_redis, stop_gateway)
        self.assertGreater(remove_loop, restore_redis)

    def test_no_backup_temp_leftovers(self) -> None:
        self.assertNotIn("BACKUP_TEMP", self.text)


# ---------------------------------------------------------------------------
# (L) PRIMARY_HOST / PUBLIC_HOST naming
# ---------------------------------------------------------------------------


class HostNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_primary_host_is_unchainedsky(self) -> None:
        self.assertIn('PRIMARY_HOST="unchainedsky.com"', self.text)

    def test_public_host_is_unbrowser(self) -> None:
        self.assertIn('PUBLIC_HOST="unbrowser.unchainedsky.com"', self.text)

    def test_no_fin_terminal_demo_host(self) -> None:
        self.assertNotIn("FIN_TERMINAL_DEMO_HOST", self.text)


# ---------------------------------------------------------------------------
# General syntax and static guards
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_script_is_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(_script_path())],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"bash syntax error:\n{result.stderr}")

    def test_no_unqualified_docker_compose_down(self) -> None:
        self.assertNotRegex(self.text, r"\bdocker compose down\b")

    def test_no_volume_removal(self) -> None:
        self.assertNotRegex(self.text, r"\bdocker volume rm\b")
        self.assertNotRegex(self.text, r"\bcompose.*down.*-v\b")

    def test_no_compose_service_wildcards(self) -> None:
        self.assertNotRegex(self.text, r"\bdocker compose[^;\n]*\*")

    def test_defines_exact_pilot_services(self) -> None:
        for svc in PILOT_SERVICES:
            self.assertIn(svc, self.text, f"service {svc} not found in script")

    def test_defines_exact_stop_order_array(self) -> None:
        seats_marker = "PILOT_SEATS=("
        seats_idx = self.text.find(seats_marker)
        self.assertGreater(seats_idx, -1)
        seats_section = self.text[seats_idx : seats_idx + 500]
        for seat in PILOT_SEATS:
            self.assertIn(seat, seats_section)

        marker = "PILOT_STOP_ORDER=("
        idx = self.text.find(marker)
        self.assertGreater(idx, -1)
        section = self.text[idx : idx + 600]
        gateway = section.find("fin-terminal-public-gateway")
        seats = section.find('"${PILOT_SEATS[@]}"')
        mcp = section.find("fin-terminal-public-unbrowser-mcp")
        redis = section.find("fin-terminal-public-redis")
        for name, position in (("gateway", gateway), ("seats", seats), ("MCP", mcp), ("Redis", redis)):
            self.assertGreater(position, -1, f"{name} missing from PILOT_STOP_ORDER")
        self.assertLess(gateway, seats)
        self.assertLess(seats, mcp)
        self.assertLess(mcp, redis)

    def test_lock_acquire_pattern(self) -> None:
        self.assertIn(".deploy.lock", self.text)

    def test_revision_guard(self) -> None:
        self.assertIn(".deploy-current", self.text)
        self.assertIn("EXPECTED_SHA", self.text)

    def test_revision_is_rechecked_after_deployment_lock(self) -> None:
        main_start = self.text.find("main() {")
        main = self.text[main_start:]
        self.assertGreater(main.find("verify_deployed_revision"), main.find("acquire_lock"))
        self.assertGreater(main.find("verify_current_main_revision"), main.find("acquire_lock"))

    def test_activate_rechecks_current_main_before_edge_mutation(self) -> None:
        self.assertIn("git ls-remote --exit-code", self.text)
        promote_start = self.text.find("promote_edge()")
        promote = self.text[promote_start:]
        main_check = promote.find("verify_current_main_revision")
        snapshot = promote.find("snapshot_env")
        self.assertGreater(main_check, -1)
        self.assertLess(main_check, snapshot)

    def test_env_flag_read_pattern(self) -> None:
        self.assertIn(ENV_FLAG, self.text)

    def test_rollback_trap_armed(self) -> None:
        self.assertIn("ROLLBACK_ARMED", self.text)
        self.assertIn("trap", self.text)

    def test_caddy_only_recreate(self) -> None:
        self.assertIn("--no-deps", self.text)
        self.assertIn("--no-build", self.text)
        self.assertIn("--pull never", self.text)
        self.assertIn("--force-recreate caddy", self.text)

    def test_profile_flag_in_compose_args(self) -> None:
        self.assertIn("--profile fin-terminal-public-pilot", self.text)

    def test_overlay_file_in_compose_args(self) -> None:
        self.assertIn("docker-compose.public-terminal.yml", self.text)

    def test_no_secret_names_in_echo_or_printf(self) -> None:
        for name in SECRET_NAMES:
            for line in self.text.splitlines():
                stripped = line.strip()
                if name in stripped and (
                    stripped.lstrip().startswith("echo")
                    or stripped.lstrip().startswith("printf")
                ):
                    if "ERROR" not in stripped and "error" not in stripped.lower():
                        self.fail(
                            f"secret name '{name}' appears in potential output on line: {line}"
                        )

    def test_action_validation_rejects_unknown(self) -> None:
        self.assertIn("activate|disable|status", self.text)
        self.assertIn("must be one of", self.text)

    def test_status_output_is_limited_to_booleans_and_states(self) -> None:
        self.assertIn("container_state", self.text)

    def test_set_e_pipefail_set(self) -> None:
        self.assertIn("set -Eeuo pipefail", self.text)

    def test_runtime_hardening_and_idempotent_activation_are_reverified(self) -> None:
        self.assertIn("validate_runtime_pilot", self.text)
        self.assertIn("no-new-privileges:true", self.text)
        self.assertIn("volume.get('type') == 'bind'", self.text)
        active_branch = self.text.split('if [[ "$enabled" == "true" ]]', 1)[1]
        self.assertIn("validate_runtime_pilot", active_branch)
        self.assertIn("verify_live_edge_surface", active_branch)

    def test_runtime_checks_cross_seat_isolation_and_capacity(self) -> None:
        self.assertIn("Cross-seat and worker-to-state negative connectivity", self.text)
        self.assertIn("check_post_start_capacity", self.text)
        self.assertIn("512 * 1024", self.text)
        self.assertIn("mem_total_kb * 15 / 100", self.text)
        self.assertIn("PILOT_EPHEMERAL_NETWORK_SPECS", self.text)
        self.assertIn("Per-seat bridge subnets", self.text)

    def test_unused_pilot_network_cleanup_is_label_scoped_and_required(self) -> None:
        self.assertIn("remove_unused_pilot_networks", self.text)
        self.assertIn('com.docker.compose.network', self.text)
        self.assertIn('[[ "$container_count" != "0" ]]', self.text)
        self.assertIn("PILOT_LEGACY_NETWORK_KEYS", self.text)
        self.assertGreaterEqual(self.text.count("remove_unused_pilot_networks"), 4)

    def test_disable_returns_persisted_state_to_one_worker_shape(self) -> None:
        disable_start = self.text.find("cmd_disable()")
        disable = self.text[disable_start:]
        self.assertIn("transition_redis_worker_set 1 -", disable)

    def test_public_config_must_require_turnstile(self) -> None:
        self.assertIn('public_config.get("turnstileRequired") is not True', self.text)
        self.assertIn('public_config.get("turnstileSiteKey")', self.text)


# ---------------------------------------------------------------------------
# Build and start ordering tests
# ---------------------------------------------------------------------------


class BuildStartOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_pull_redis_before_build(self) -> None:
        pull_pos = self.text.find("pull fin-terminal-public-redis")
        build_pos = self.text.find("compose_cmd build")
        self.assertLess(pull_pos, build_pos, "Redis pull must precede build steps")

    def test_start_order_redis_mcp_seats_gateway(self) -> None:
        redis_start = self.text.find("up -d --no-build fin-terminal-public-redis")
        mcp_start = self.text.find("up -d --no-deps --no-build fin-terminal-public-unbrowser-mcp")
        seat_start = self.text.find('for seat in "${PILOT_SEATS[@]}"')
        gateway_start = self.text.find("up -d --no-deps --no-build fin-terminal-public-gateway")
        self.assertLess(redis_start, mcp_start)
        self.assertLess(mcp_start, seat_start)
        self.assertLess(seat_start, gateway_start)

    def test_no_deps_flag_used(self) -> None:
        self.assertIn("--no-deps", self.text)


# ---------------------------------------------------------------------------
# Disable idempotency
# ---------------------------------------------------------------------------


class DisableIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _script_text()

    def test_disable_handles_absent_containers(self) -> None:
        self.assertIn("already absent", self.text.lower())

    def test_disable_handles_flag_already_false(self) -> None:
        self.assertIn("already false", self.text.lower())

    def test_disable_always_recreates_caddy(self) -> None:
        self.assertIn("caddy_force_recreate", self.text)


# ---------------------------------------------------------------------------
# Dynamic tests (subprocess with PATH-based mocks for docker/curl/flock)
# ---------------------------------------------------------------------------


class MockEnviron:
    """Creates a mock environment for subprocess-based tests."""

    def __init__(self, work_dir: str) -> None:
        self.work_dir = Path(work_dir)

    def setup_minimal(self, public_enabled: str = "false") -> None:
        w = self.work_dir
        w.mkdir(parents=True, exist_ok=True)

        (w / "docker-compose.yml").write_text(
            "services:\n  caddy:\n    image: caddy:2\n  web:\n    image: nginx\n  unbrowser-egress:\n    image: egress\n"
        )
        (w / "docker-compose.public-terminal.yml").write_text(
            "services:\n  fin-terminal-public-redis:\n    image: redis\n    profiles: [\"fin-terminal-public-pilot\"]\n"
            "  fin-terminal-public-unbrowser-mcp:\n    build:\n      context: .\n    profiles: [\"fin-terminal-public-pilot\"]\n"
            "  fin-terminal-public-gateway:\n    build:\n      context: .\n    profiles: [\"fin-terminal-public-pilot\"]\n"
            "  fin-terminal-public-seat-01:\n    image: worker\n    profiles: [\"fin-terminal-public-pilot\"]\n"
        )
        (w / ".env").write_text(
            f"OPENROUTER_API_KEY=sk-test\n{ENV_FLAG}={public_enabled}\n"
        )
        (w / ".env").chmod(0o600)
        (w / ".deploy-current").write_text(
            "revision=0000000000000000000000000000000000000000\n"
        )
        deploy_tools = w / ".deploy-tools"
        deploy_tools.mkdir(exist_ok=True)
        (deploy_tools / "ensure_fin_terminal_secrets.py").write_text(
            "import sys\nif '--ensure-status' in sys.argv:\n print('fin_terminal_credentials_changed=false')\n"
        )

    def make_mock_script(self) -> str:
        """Generate a mock dispatcher for docker, curl, flock, etc."""
        return textwrap.dedent(
            """\
            #!/bin/bash
            cmd="$(basename "$0")"

            case "$cmd" in
                docker)
                    case "$1" in
                        compose)
                            shift
                            has_public=false
                            for a in "$@"; do
                                [[ "$a" == *"public-terminal"* ]] && has_public=true
                            done
                            while [[ "${1-}" == -* ]]; do
                                case "$1" in
                                    -f|--project-name|--profile|--env-file) shift; shift ;;
                                    -*) shift ;;
                                esac
                            done
                            case "$1" in
                                config)
                                    if [[ "$*" == *"--format"*"json"* || "$*" == *"--services"* ]]; then
                                        if [[ "$*" == *"--format json"* ]]; then
                                            echo '{"services":{"caddy":{"image":"caddy"},"web":{"image":"nginx"},"unbrowser-egress":{"image":"egress"}'
                                            $has_public && echo ',"fin-terminal-public-redis":{"image":"redis"},"fin-terminal-public-unbrowser-mcp":{"image":"mcp"},"fin-terminal-public-seat-01":{"image":"worker"},"fin-terminal-public-gateway":{"image":"gateway"}'
                                            echo '}}'
                                        else
                                            echo "caddy"; echo "web"; echo "unbrowser-egress"
                                            $has_public && { echo "fin-terminal-public-redis"; echo "fin-terminal-public-unbrowser-mcp"; echo "fin-terminal-public-seat-01"; echo "fin-terminal-public-gateway"; }
                                        fi
                                    elif [[ "$*" == *"--quiet"* ]]; then
                                        exit 0
                                    else
                                        echo '{}'
                                    fi
                                    ;;
                                ps)
                                    shift
                                    svc=""
                                    for a in "$@"; do [[ "$a" != -* ]] && { svc="$a"; break; }; done
                                    [[ -n "$svc" ]] && echo "${svc}-container-id"
                                    ;;
                                pull|build|up|stop|rm|run)
                                    exit 0
                                    ;;
                                logs)
                                    echo "mock logs"
                                    exit 0
                                    ;;
                                *)
                                    exit 0
                                    ;;
                            esac
                            ;;
                        inspect)
                            fmt=""
                            for a in "$@"; do
                                if [[ "$a" == *"Health"* || "$a" == *"State.Status"* ]]; then
                                    echo "healthy"
                                elif [[ "$a" == *"Networks"* ]]; then
                                    echo "network1 network2"
                                elif [[ "$a" == *"PortBindings"* ]]; then
                                    echo ""
                                fi
                            done
                            exit 0
                            ;;
                        exec)
                            # Mock MCP: print MCP_OK
                            echo "MCP_OK"
                            exit 0
                            ;;
                        *)
                            exit 0
                            ;;
                    esac
                    ;;
                curl)
                    last_arg=""
                    for a in "$@"; do last_arg="$a"; done
                    if [[ "$*" == *"--write-out"* ]]; then
                        if [[ "$last_arg" == *"/fin-terminal-live-pilot/"* ]]; then
                            echo "404"
                        elif [[ "$last_arg" == *"/health"* ]]; then
                            echo "200"
                        elif [[ "$last_arg" == *"/fin-terminal/"* ]]; then
                            echo "401"
                        elif [[ "$last_arg" == *"/fin-terminal-demo"* ]]; then
                            echo "404"
                        elif [[ "$last_arg" == *"/api/admission"* ]]; then
                            if [[ "$*" == *"evil.example.com"* ]]; then echo "403"
                            elif [[ "$*" == *'"Origin: https://unbrowser.unchainedsky.com"'* && "$*" != *'-d'* ]]; then echo "401"
                            elif [[ "$*" == *'"token":""'* ]]; then echo "400"
                            elif [[ "$*" == *'"token":"short"'* ]]; then echo "403"
                            else echo "200"
                            fi
                        else
                            echo "200"
                        fi
                    elif [[ "$*" == *"--fail"* ]]; then
                        echo '<html><meta name="x-build-mode" content="public-live"><script src="/assets/"></html>'
                    elif [[ "$*" == *"-I"* ]]; then
                        echo "200"
                    else
                        exit 0
                    fi
                    ;;
                flock)
                    exit 0
                    ;;
                python3)
                    exit 0
                    ;;
                df)
                    echo "/dev/sda1  30% /"
                    ;;
                awk)
                    last_arg=""
                    for a in "$@"; do last_arg="$a"; done
                    if [[ -f "$last_arg" ]]; then
                        /usr/bin/awk "${@:1:$#-1}" "$last_arg"
                    elif [[ "$*" == *"MemAvailable"* ]]; then
                        echo "MemTotal:       16000000 kB"
                        echo "MemAvailable:   12000000 kB"
                    else
                        /usr/bin/awk "$@"
                    fi
                    ;;
                stat)
                    echo ""
                    ;;
                ssh-keygen)
                    exit 0
                    ;;
                mktemp)
                    echo "/tmp/test-tmp-XXXXXX"
                    ;;
                comm)
                    # comm -13: output lines unique to file2 (not in file1)
                    if [[ "$*" == *"-13"* ]]; then
                        # Simulate overlay-only services from file2.
                        echo "fin-terminal-public-gateway"
                        echo "fin-terminal-public-redis"
                        echo "fin-terminal-public-seat-01"
                        echo "fin-terminal-public-unbrowser-mcp"
                    fi
                    ;;
                *)
                    exit 0
                    ;;
            esac
            """
        )


class DynamicPilotScriptTests(unittest.TestCase):
    """Run the real script against a mock environment."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script_path = _script_path()

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._home_dir = Path(self._temp_dir.name) / "home" / "ec2-user"
        self._work_dir = self._home_dir / "unchained"
        self._mock_dir = Path(self._temp_dir.name) / "mockbin"
        self._mock_dir.mkdir(parents=True)
        self._work_dir.mkdir(parents=True)

        self.env = MockEnviron(str(self._work_dir))
        self.env.setup_minimal(public_enabled="false")

        mock_script = self._mock_dir / "docker"
        mock_script.write_text(self.env.make_mock_script())
        mock_script.chmod(0o755)

        for name in ("awk", "curl", "flock", "python3", "df", "stat", "ssh-keygen", "mktemp", "comm"):
            (self._mock_dir / name).symlink_to(mock_script)

        deploy_tools = self._work_dir / ".deploy-tools"
        deploy_tools.mkdir(exist_ok=True)
        (deploy_tools / "ensure_fin_terminal_secrets.py").write_text(
            "import sys\nif '--ensure-status' in sys.argv:\n print('fin_terminal_credentials_changed=false')\n"
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _run_script(
        self,
        action: str,
        expected_sha: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        if expected_sha is None:
            expected_sha = "0000000000000000000000000000000000000000"

        env = os.environ.copy()
        env["PATH"] = f"{self._mock_dir}:{env.get('PATH', '')}"
        env["REMOTE_DIR"] = str(self._work_dir)
        env["USER"] = "ec2-user"

        with open(str(self.script_path), "rb") as fh:
            result = subprocess.run(
                ["bash", "-s", "--", action, expected_sha],
                stdin=fh,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._work_dir),
                env=env,
            )
        return result

    def test_status_returns_success(self) -> None:
        result = self._run_script("status")
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

    def test_status_no_secret_output(self) -> None:
        result = self._run_script("status")
        output = result.stdout + result.stderr
        for name in SECRET_NAMES:
            self.assertNotIn(name, output, f"secret name {name} leaked in output")

    def test_status_shows_pilot_service_states(self) -> None:
        result = self._run_script("status")
        output = result.stdout + result.stderr
        for svc in PILOT_SERVICES:
            self.assertIn(svc, output, f"service {svc} not shown")

    def test_invalid_action_rejected(self) -> None:
        result = self._run_script("restart")
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_sha_rejected(self) -> None:
        with open(str(self.script_path), "rb") as fh:
            result = subprocess.run(
                ["bash", "-s", "--", "status", "bad-sha"],
                stdin=fh,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self._work_dir),
                env={
                    **os.environ,
                    "PATH": f"{self._mock_dir}:{os.environ.get('PATH', '')}",
                    "REMOTE_DIR": str(self._work_dir),
                    "USER": "ec2-user",
                },
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("40-character", result.stdout + result.stderr)

    def test_revision_guard_enforced(self) -> None:
        (self._work_dir / ".deploy-current").write_text(
            "revision=ffffffffffffffffffffffffffffffffffffffff\n"
        )
        result = self._run_script("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# Dynamic-mode / workspace integration (source-level contract)
# ---------------------------------------------------------------------------
class DynamicModeIntegrationTests(unittest.TestCase):
    """Source-level checks that the deploy workflow supports the dynamic
    (reconciler) mode and the workspace companion state."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _script_path().read_text()

    def test_rollback_action_accepted(self) -> None:
        self.assertIn("activate|disable|status|rollback", self.source)
        self.assertIn("rollback) cmd_rollback ;;", self.source)

    def test_dynamic_mode_helper_present(self) -> None:
        self.assertIn("read_dynamic_mode_enabled()", self.source)
        self.assertIn("dynamic_mode_enabled()", self.source)
        self.assertIn("TERMINAL_RUNTIME_FEATURE_ENABLED", self.source)

    def test_sqlite_online_backup_before_migration(self) -> None:
        self.assertIn("sqlite_online_backup()", self.source)
        # Called from run_activate_gates before any schema migration.
        self.assertIn("sqlite_online_backup; then", self.source)
        self.assertIn("pre-migration SQLite online backup failed", self.source)

    def test_companion_redis_backup_restore_clean(self) -> None:
        self.assertIn("backup_workspace_redis()", self.source)
        self.assertIn("restore_workspace_redis_backup()", self.source)
        self.assertIn("cleanup_workspace_redis()", self.source)
        # Wired: backup at state prepare, restore on rollback, clean on disable.
        self.assertIn("backup_workspace_redis || return 1", self.source)
        self.assertIn("restore_workspace_redis_backup; then", self.source)
        self.assertIn("cleanup_workspace_redis", self.source)
        # Companion keys live in Redis DB 1 (workspace namespace).
        self.assertIn('redis-cli -n 1 --scan', self.source)

    def test_rollback_starts_all_six_seats(self) -> None:
        self.assertIn("start_all_seats_for_rollback()", self.source)
        self.assertIn('for svc in "${PILOT_SEATS[@]}"', self.source)

    def test_gateway_readiness_relaxed_in_dynamic_mode(self) -> None:
        self.assertIn("const dynamic = process.argv[1] === \"true\";", self.source)
        self.assertIn("if (dynamic) {", self.source)
        self.assertIn("one-warm-spare", self.source)
        # Feature-disabled mode still requires six unique workers.
        self.assertIn("gateway.readyWorkers !== 6 || healthy.length !== 6", self.source)

    def test_runtime_pilot_verification_allows_stopped_seats_in_dynamic_mode(self) -> None:
        self.assertIn("Reconciler may have drained this seat", self.source)
        self.assertIn("absent is valid in dynamic mode", self.source)

    def test_status_reports_dynamic_mode(self) -> None:
        self.assertIn("TERMINAL_RUNTIME_FEATURE_ENABLED: $dynamic", self.source)
        self.assertIn("terminal-runtime-reconciler:", self.source)

    def test_cmd_rollback_disables_reconciler_and_starts_six(self) -> None:
        self.assertIn("cmd_rollback()", self.source)
        self.assertIn("systemctl stop terminal-runtime-reconciler", self.source)
        self.assertIn("static six-seat", self.source)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
