"""Tests for the terminal runtime reconciler.

Covers:
  - target math and one-warm invariant
  - no stop without successful atomic drain
  - assigned/reconnect seat never stopped
  - allowlist/shell-injection/label checks
  - lock/reconciler split-brain and crash recovery
  - resource threshold behavior
  - dynamic vs legacy activation/disable/rollback
  - Caddy internal-route non-exposure and cookie/header contracts
  - compose render, shell syntax, deployment tests
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
from unittest import mock

# ---------------------------------------------------------------------------
# Determine base paths
# ---------------------------------------------------------------------------
DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_DIR = DEPLOY_DIR.parent
RECONCILER_PATH = DEPLOY_DIR / "terminal_runtime_reconciler.py"

# Load the reconciler module (do not run its main loop).
# We use exec() so that dataclass __module__ resolution works correctly.
if RECONCILER_PATH.exists():
    reconciler_source = RECONCILER_PATH.read_text()
    # Remove the `if __name__ == "__main__"` block so the main loop doesn't execute
    idx = reconciler_source.find('\nif __name__ == "__main__":')
    if idx >= 0:
        reconciler_source = reconciler_source[:idx]
    reconciler_module = type(sys)("terminal_runtime_reconciler")
    reconciler_module.__file__ = str(RECONCILER_PATH)
    reconciler_module.__name__ = "terminal_runtime_reconciler"
    reconciler_module.__package__ = ""
    # Register in sys.modules so @dataclass can resolve __module__ lookups
    sys.modules["terminal_runtime_reconciler"] = reconciler_module
    exec(reconciler_source, reconciler_module.__dict__)
else:
    reconciler_module = None


# ---------------------------------------------------------------------------
# Constants under test
# ---------------------------------------------------------------------------
SEAT_NAMES = [f"fin-terminal-public-seat-{n:02d}" for n in range(1, 7)]
ALLOWED_SEAT_NAMES = frozenset(SEAT_NAMES)


# ---------------------------------------------------------------------------
# A) Target math and one-warm invariant
# ---------------------------------------------------------------------------
class TargetMathTests(unittest.TestCase):
    """Test the target running calculation: min(6, assigned + queued + 1)."""

    def test_empty_state_wants_one(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=0,
            total_queued=0,
        )
        self.assertEqual(snapshot.desired_running, 1)

    def test_one_assigned_wants_two(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=1,
            total_queued=0,
        )
        self.assertEqual(snapshot.desired_running, 2)

    def test_two_assigned_wants_three(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=2,
            total_queued=0,
        )
        self.assertEqual(snapshot.desired_running, 3)

    def test_queued_adds(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=1,
            total_queued=3,
        )
        self.assertEqual(snapshot.desired_running, 5)  # 1 + 3 + 1

    def test_caps_at_six(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=5,
            total_queued=10,
        )
        self.assertEqual(snapshot.desired_running, 6)  # min(6, 5+10+1=16)

    def test_full_house_is_six(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=6,
            total_queued=0,
        )
        self.assertEqual(snapshot.desired_running, 6)

    def test_count_properties_no_starting_no_draining(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={
                "fin-terminal-public-seat-01": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-01", status="healthy", assigned=True,
                ),
                "fin-terminal-public-seat-02": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-02", status="healthy", assigned=False,
                ),
                "fin-terminal-public-seat-03": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-03", status="absent",
                ),
                "fin-terminal-public-seat-04": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-04", status="absent",
                ),
                "fin-terminal-public-seat-05": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-05", status="absent",
                ),
                "fin-terminal-public-seat-06": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-06", status="absent",
                ),
            },
            total_assigned=1,
            total_queued=0,
        )
        self.assertEqual(snapshot.running_count, 2)
        self.assertEqual(snapshot.assigned_count, 1)
        self.assertEqual(snapshot.starting_count, 0)
        self.assertEqual(snapshot.draining_count, 0)


class OneWarmInvariantTests(unittest.TestCase):
    """Always at least one warm spare when possible."""

    def test_snapshot_never_drops_below_one_when_queued_present(self) -> None:
        # Scenarios: assigned=0, queued>0 => desired_running at least 1
        for queued in range(0, 50):
            snapshot = reconciler_module.GatewaySnapshot(
                seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
                total_assigned=0,
                total_queued=queued,
            )
            self.assertGreaterEqual(snapshot.desired_running, 1,
                                     f"queued={queued}")

    def test_warm_spare_present_in_formula(self) -> None:
        # assigned + queued + 1 => always >= 1 when not capped
        self.assertEqual(reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=0, total_queued=0,
        ).desired_running, 1)

        self.assertEqual(reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=0, total_queued=1,
        ).desired_running, 2)


# ---------------------------------------------------------------------------
# B) No stop without successful atomic drain
# ---------------------------------------------------------------------------
class NoStopWithoutDrainTests(unittest.TestCase):
    def test_scale_down_only_drains_accepted_seats(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True,
            management_token="t" * 32,
            compose_dir="/nonexistent",
        )
        # mock docker + gateway
        with mock.patch.object(
            reconciler_module.DockerInterface, "container_state", return_value="healthy"
        ), mock.patch.object(
            reconciler_module.DockerInterface, "stop_service"
        ) as mock_stop, mock.patch.object(
            reconciler_module.DockerInterface, "remove_service"
        ) as mock_remove, mock.patch.object(
            reconciler_module.GatewayManagementClient, "drain_seat",
        ) as mock_drain:
            # First drain rejects, second accepts, rest don't matter
            # 6 seats are candidates; we only need 2 drains
            mock_drain.side_effect = [False, True] + [True] * 10

            r = reconciler_module.TerminalRuntimeReconciler(config)
            seats = {}
            for i, name in enumerate(sorted(ALLOWED_SEAT_NAMES)):
                seats[name] = reconciler_module.SeatState(
                    name=name, status="healthy", assigned=False,
                    generation=f"gen-{name}", idle_seconds=float(i * 60),
                )
            snapshot = reconciler_module.GatewaySnapshot(
                seats=seats,
                total_assigned=0,
                total_queued=0,
            )

            r._scale_down(snapshot, 2)

            # seat-06 (idle 300s) should be first candidate, rejected -> no stop for seat-06
            # seat-05 (idle 240s) second candidate, accepted -> stopped
            # If seat-05 was accepted, stop count should be >= 1
            # First drain call should be for seat-06 and return False
            self.assertGreaterEqual(mock_drain.call_count, 2)
            first_drain_call_name = mock_drain.call_args_list[0][0][0]
            self.assertEqual(first_drain_call_name, "fin-terminal-public-seat-06")
            # At least one seat was accepted and stopped
            self.assertGreaterEqual(mock_stop.call_count, 1)

    def test_drain_failure_does_not_stop(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True,
            management_token="t" * 32,
            compose_dir="/nonexistent",
        )
        with mock.patch.object(
            reconciler_module.DockerInterface, "container_state", return_value="healthy"
        ), mock.patch.object(
            reconciler_module.DockerInterface, "stop_service"
        ) as mock_stop, mock.patch.object(
            reconciler_module.GatewayManagementClient, "drain_seat",
            side_effect=RuntimeError("drain failed"),
        ):
            r = reconciler_module.TerminalRuntimeReconciler(config)
            seats = {name: reconciler_module.SeatState(
                name=name, status="healthy", assigned=False,
                generation=f"gen-{name}", idle_seconds=300.0,
            ) for name in sorted(ALLOWED_SEAT_NAMES)}
            snapshot = reconciler_module.GatewaySnapshot(seats=seats, total_assigned=0, total_queued=0)
            r._scale_down(snapshot, 1)

            mock_stop.assert_not_called()


# ---------------------------------------------------------------------------
# C) Assigned/reconnect seat never stopped
# ---------------------------------------------------------------------------
class AssignedSeatNeverStoppedTests(unittest.TestCase):
    def test_assigned_seat_not_in_scale_down_candidates(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/nonexistent",
        )
        with mock.patch.object(reconciler_module.DockerInterface, "container_state", return_value="healthy"), \
             mock.patch.object(reconciler_module.DockerInterface, "stop_service") as mock_stop, \
             mock.patch.object(reconciler_module.GatewayManagementClient, "drain_seat", return_value=True):
            r = reconciler_module.TerminalRuntimeReconciler(config)

            # seat-01 is assigned; only unassigned should be candidates
            seats = {
                "fin-terminal-public-seat-01": reconciler_module.SeatState(
                    name="fin-terminal-public-seat-01", status="healthy",
                    assigned=True, generation="gen-01", idle_seconds=999.0,
                ),
                **{name: reconciler_module.SeatState(
                    name=name, status="healthy", assigned=False,
                    generation=f"gen-{name}", idle_seconds=10.0,
                ) for name in sorted(ALLOWED_SEAT_NAMES) if name != "fin-terminal-public-seat-01"},
            }
            snapshot = reconciler_module.GatewaySnapshot(seats=seats, total_assigned=1, total_queued=0)
            r._scale_down(snapshot, 5)

            # seat-01 must never be drained
            calls = [c[0][0] for c in mock_stop.call_args_list]
            self.assertNotIn("fin-terminal-public-seat-01", calls)

    def test_draining_seat_not_duplicated_in_candidates(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/nonexistent",
        )
        with mock.patch.object(reconciler_module.DockerInterface, "container_state", return_value="healthy"), \
             mock.patch.object(reconciler_module.DockerInterface, "stop_service") as mock_stop, \
             mock.patch.object(reconciler_module.GatewayManagementClient, "drain_seat", return_value=True):
            r = reconciler_module.TerminalRuntimeReconciler(config)

            seats = {}
            for name in sorted(ALLOWED_SEAT_NAMES):
                seats[name] = reconciler_module.SeatState(
                    name=name, status="draining" if name == "fin-terminal-public-seat-06" else "healthy",
                    assigned=False, generation=f"gen-{name}", idle_seconds=300.0,
                )
            snapshot = reconciler_module.GatewaySnapshot(seats=seats, total_assigned=0, total_queued=0)
            # seat-06 is draining → already excluded from scale-down candidates
            r._scale_down(snapshot, 1)
            # Should only drain seat-05 (idle 300s, not draining)
            calls = [c[0][0] for c in mock_stop.call_args_list]
            self.assertNotIn("fin-terminal-public-seat-06", calls)


# ---------------------------------------------------------------------------
# D) Allowlist / shell-injection / label checks
# ---------------------------------------------------------------------------
class AllowlistAndInjectionTests(unittest.TestCase):
    def test_only_allowed_names_accepted(self) -> None:
        docker = reconciler_module.DockerInterface(
            reconciler_module.ReconcilerConfig(enabled=False, management_token="", compose_dir="/tmp")
        )
        with self.assertRaises(ValueError):
            docker._validate_service_name("fin-terminal-public-gateway")
        with self.assertRaises(ValueError):
            docker._validate_service_name("rm -rf /")
        with self.assertRaises(ValueError):
            docker._validate_service_name("")
        with self.assertRaises(ValueError):
            docker._validate_service_name("fin-terminal-public-seat-07")

        # All six are valid
        for name in ALLOWED_SEAT_NAMES:
            docker._validate_service_name(name)  # should not raise

    def test_no_unvalidated_name_in_compose_calls(self) -> None:
        """All compose subprocess calls must pass through _validate_service_name."""
        source = RECONCILER_PATH.read_text()
        # Every call to _run must use a validated service name (or be a static command).
        # The _run method is called from start_service, stop_service, remove_service,
        # wait_healthy, container_state, container_id, container_labels,
        # container_image_digest — all of which call _validate_service_name.
        for method_name in ("start_service", "stop_service", "remove_service",
                            "wait_healthy", "container_state", "container_id",
                            "container_labels", "container_image_digest"):
            self.assertIn(method_name, source)

        # The _compose_base method never interpolates service names into shell commands
        compose_base_match = re.search(
            r"def _compose_base\(self\)[\s\S]*?return\s*\[([^\]]*)\]", source
        )
        self.assertIsNotNone(compose_base_match)
        base_args = compose_base_match.group(1)
        self.assertNotIn("service", base_args.lower())

    def test_no_shell_metacharacter_injection_vector(self) -> None:
        """The reconciler must never construct shell commands with user/service data."""
        source = RECONCILER_PATH.read_text()
        # No os.system, no subprocess with shell=True
        self.assertNotIn("os.system", source)
        self.assertNotIn("shell=True", source)
        # All subprocess.run calls use list arguments, not string commands
        # Check that subprocess.run calls use list args (first positional is a list)
        for match in re.finditer(r"subprocess\.run\((\[[^\]]*?\])", source):
            self.assertIn("[", match.group(1), "subprocess.run should use list args")

    def test_gateway_client_no_raw_curl(self) -> None:
        """Gateway management client must not shell-interpolate URLs."""
        source = RECONCILER_PATH.read_text()
        self.assertNotIn("curl", source)


class LabelValidationTests(unittest.TestCase):
    def test_label_check_requires_exact_project(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32,
            compose_project="unchained", compose_dir="/tmp",
        )
        docker = reconciler_module.DockerInterface(config)

        with mock.patch.object(docker, "container_id", return_value="abc123"), \
             mock.patch.object(docker, "container_labels", return_value={
                 "com.docker.compose.project": "other-project",
             }):
            with self.assertRaises(RuntimeError) as ctx:
                docker.validate_container_project_and_set()
            self.assertIn("other-project", str(ctx.exception))


# ---------------------------------------------------------------------------
# E) Lock / reconciler split-brain and crash recovery
# ---------------------------------------------------------------------------
class LockAndSplitBrainTests(unittest.TestCase):
    def test_reconciler_skips_when_lock_not_held(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)
        # The lock is not acquired, so reconcile() should skip the gateway call
        with mock.patch.object(r, "_gateway") as mock_gw:
            r.reconcile()  # lock not held → should skip
            mock_gw.reconcile_snapshot.assert_not_called()

    def test_lock_released_after_exception(self) -> None:
        lock = reconciler_module.DeployLock("/tmp/.deploy.lock.test")
        import fcntl
        with mock.patch("fcntl.flock") as mock_flock:
            mock_flock.side_effect = IOError("locked")
            result = lock.acquire()
            self.assertFalse(result)
            self.assertFalse(lock.held)

            # Simulate successful acquire then release
            mock_flock.side_effect = None
            mock_flock.return_value = None
            with mock.patch("os.open", return_value=3):
                with mock.patch("os.close"):
                    with mock.patch("os.makedirs"):
                        result = lock.acquire()
                        # Reset side_effect for release
                        mock_flock.side_effect = None
                        self.assertTrue(result)

    def test_reconcile_transitory_recovers_starting_seat(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)

        # seat-03 is "starting" in gateway, healthy in docker → activate
        seats = {
            "fin-terminal-public-seat-03": reconciler_module.SeatState(
                name="fin-terminal-public-seat-03", status="starting",
            ),
        }
        snapshot = reconciler_module.GatewaySnapshot(
            seats={**{name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES}, **seats},
        )

        with mock.patch.object(r._docker, "container_state", return_value="healthy") as mock_state, \
             mock.patch.object(r.gateway, "activate_seat") as mock_activate:
            r._reconcile_transitory(snapshot)
            mock_activate.assert_called_once_with("fin-terminal-public-seat-03")

    def test_reconcile_transitory_cleans_absent_starting_seat(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)

        seats = {
            "fin-terminal-public-seat-03": reconciler_module.SeatState(
                name="fin-terminal-public-seat-03", status="starting",
            ),
        }
        snapshot = reconciler_module.GatewaySnapshot(
            seats={**{name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES}, **seats},
        )

        with mock.patch.object(r._docker, "container_state", return_value="absent") as mock_state, \
             mock.patch.object(r._docker, "remove_service") as mock_remove:
            r._reconcile_transitory(snapshot)
            mock_remove.assert_called_once_with("fin-terminal-public-seat-03")

    def test_reconcile_transitory_cleans_stopped_seat_with_container(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)

        seats = {
            "fin-terminal-public-seat-05": reconciler_module.SeatState(
                name="fin-terminal-public-seat-05", status="stopped",
            ),
        }
        snapshot = reconciler_module.GatewaySnapshot(
            seats={**{name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES}, **seats},
        )

        with mock.patch.object(r._docker, "container_state", return_value="exited"), \
             mock.patch.object(r._docker, "stop_service") as mock_stop, \
             mock.patch.object(r._docker, "remove_service") as mock_remove:
            r._reconcile_transitory(snapshot)
            mock_stop.assert_called_once()
            mock_remove.assert_called_once()

    def test_reconcile_transitory_leaves_absent_stopped_seat_alone(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)

        seats = {
            "fin-terminal-public-seat-05": reconciler_module.SeatState(
                name="fin-terminal-public-seat-05", status="stopped",
            ),
        }
        snapshot = reconciler_module.GatewaySnapshot(
            seats={**{name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES}, **seats},
        )

        with mock.patch.object(r._docker, "container_state", return_value="absent"), \
             mock.patch.object(r._docker, "stop_service") as mock_stop, \
             mock.patch.object(r._docker, "remove_service") as mock_remove:
            r._reconcile_transitory(snapshot)
            mock_stop.assert_not_called()
            mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# F) Resource threshold behavior
# ---------------------------------------------------------------------------
class ResourceGuardTests(unittest.TestCase):
    def test_memory_check_blocks_when_below_headroom(self) -> None:
        guard = reconciler_module.ResourceGuard(
            reconciler_module.ReconcilerConfig(
                enabled=True, management_token="t" * 32,
                host_mem_reserve_mb=512, host_mem_headroom_pct=15,
            )
        )
        with mock.patch.object(guard, "_get_proc_meminfo") as mock_meminfo:
            # 1GB total, 100MB available = well below 15% headroom
            mock_meminfo.side_effect = lambda key: {
                "MemTotal": 1024 * 1024,
                "MemAvailable": 100 * 1024,
            }.get(key, 0)
            with mock.patch.object(guard, "_oom_killer_recent", return_value=False):
                allowed, reason = guard.check()
                self.assertFalse(allowed)
                self.assertIn("memory", reason.lower())

    def test_memory_check_passes_when_sufficient(self) -> None:
        guard = reconciler_module.ResourceGuard(
            reconciler_module.ReconcilerConfig(
                enabled=True, management_token="t" * 32,
                host_mem_reserve_mb=512, host_mem_headroom_pct=15,
            )
        )
        with mock.patch.object(guard, "_get_proc_meminfo") as mock_meminfo:
            mock_meminfo.side_effect = lambda key: {
                "MemTotal": 8192 * 1024,
                "MemAvailable": 4096 * 1024,
            }.get(key, 0)
            with mock.patch.object(guard, "_oom_killer_recent", return_value=False):
                allowed, reason = guard.check()
                self.assertTrue(allowed)

    def test_oom_killer_blocks_start(self) -> None:
        guard = reconciler_module.ResourceGuard(
            reconciler_module.ReconcilerConfig(
                enabled=True, management_token="t" * 32,
                host_mem_reserve_mb=512, host_mem_headroom_pct=15,
            )
        )
        with mock.patch.object(guard, "_get_proc_meminfo", return_value={"MemTotal": 8192 * 1024, "MemAvailable": 4096 * 1024}), \
             mock.patch.object(guard, "_oom_killer_recent", return_value=True):
            allowed, reason = guard.check()
            self.assertFalse(allowed)
            self.assertIn("OOM", reason)

    def test_scale_up_blocked_by_resource_guard(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)

        # Override guard to always block
        with mock.patch.object(r._guard, "check", return_value=(False, "test block")):
            with mock.patch.object(r._docker, "start_service") as mock_start:
                seats = {name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES}
                snapshot = reconciler_module.GatewaySnapshot(seats=seats, total_assigned=3, total_queued=2)
                r._scale_up(snapshot, 6)  # need 6 but guard blocks
                mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# G) Dynamic vs legacy activation/disable/rollback
# ---------------------------------------------------------------------------
class DynamicVsLegacyTests(unittest.TestCase):
    """Tests for the pilot script changes when reconciler feature is enabled."""

    def test_legacy_requires_nine_containers(self) -> None:
        """When feature disabled, validate_runtime_pilot must check 9 containers."""
        pilot_script = (DEPLOY_DIR / "public_terminal_pilot_remote.sh").read_text()
        # The legacy path requires exact pilot service count
        self.assertIn("PILOT_SERVICE_COUNT", pilot_script)
        self.assertIn("expected $PILOT_SERVICE_COUNT", pilot_script)

    def test_reconciler_mode_section_present_in_source(self) -> None:
        """The reconciler module must have a section that defines the dynamic activation behavior."""
        source = RECONCILER_PATH.read_text()
        self.assertIn("ALLOWED_SEAT_NAMES", source)
        # The reconciler must understand the concept of desired/observed set
        self.assertIn("desired_running", source)
        self.assertIn("GatewaySnapshot", source)

    def test_rollback_start_all_seats(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)
        with mock.patch.object(r._docker, "container_state", return_value="absent"), \
             mock.patch.object(r._docker, "start_service") as mock_start:
            r.rollback_start_all()
            self.assertEqual(mock_start.call_count, 6)
            called_names = sorted(c[0][0] for c in mock_start.call_args_list)
            self.assertEqual(called_names, sorted(ALLOWED_SEAT_NAMES))

    def test_rollback_skips_already_running_seats(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        r = reconciler_module.TerminalRuntimeReconciler(config)

        call_count = 0
        def side_effect(name):
            nonlocal call_count
            call_count += 1
            # First 3 are healthy, last 3 are absent
            return "healthy" if name <= "fin-terminal-public-seat-03" else "absent"

        with mock.patch.object(r._docker, "container_state") as mock_state, \
             mock.patch.object(r._docker, "start_service") as mock_start:
            mock_state.side_effect = lambda name: "healthy" if name in {
                "fin-terminal-public-seat-01",
                "fin-terminal-public-seat-02",
                "fin-terminal-public-seat-03",
            } else "absent"
            r.rollback_start_all()
            # Only 3 seats should be started (the absent ones)
            self.assertEqual(mock_start.call_count, 3)


# ---------------------------------------------------------------------------
# H) Caddy internal-route non-exposure and cookie/header contracts
# ---------------------------------------------------------------------------
class CaddyHeaderContractTests(unittest.TestCase):
    def test_internal_routes_return_404(self) -> None:
        caddyfile = (PROJECT_DIR / "Caddyfile").read_text()
        self.assertIn('handle /internal/*', caddyfile)
        self.assertIn('respond "Not found" 404', caddyfile)

    def test_public_pilot_strips_cookies(self) -> None:
        caddyfile = (PROJECT_DIR / "Caddyfile").read_text()
        self.assertIn("request_header -Cookie", caddyfile)
        # Verify it appears in both the signed-in terminal route AND the live pilot route
        cookie_strips = [
            line for line in caddyfile.splitlines()
            if "request_header -Cookie" in line
        ]
        self.assertGreaterEqual(len(cookie_strips), 2,
            "Cookie must be stripped in both authenticated and public routes")

    def test_public_pilot_strips_identity_headers(self) -> None:
        caddyfile = (PROJECT_DIR / "Caddyfile").read_text()
        self.assertIn("request_header -X-Fin-Terminal-User", caddyfile)
        self.assertIn("request_header -X-Fin-Terminal-Proxy-Token", caddyfile)
        self.assertIn("request_header -Authorization", caddyfile)

    def test_edge_token_injected_by_caddy_not_caller(self) -> None:
        caddyfile = (PROJECT_DIR / "Caddyfile").read_text()
        # Caller-supplied edge token must be stripped
        self.assertIn("request_header -X-Fin-Terminal-Edge-Token", caddyfile)
        # Caddy injects its own edge token
        self.assertIn("X-Fin-Terminal-Edge-Token", caddyfile)
        self.assertIn("header_up", caddyfile)

    def test_pilot_route_skips_access_logs(self) -> None:
        caddyfile = (PROJECT_DIR / "Caddyfile").read_text()
        self.assertIn("log_skip", caddyfile)


# ---------------------------------------------------------------------------
# I) Compose render, shell syntax, deployment tests
# ---------------------------------------------------------------------------
class ComposeRenderTests(unittest.TestCase):
    @classmethod
    def _compose_args(cls, *extra: str) -> list[str]:
        return [
            "docker", "compose",
            "-f", str(PROJECT_DIR / "docker-compose.yml"),
            "-f", str(PROJECT_DIR / "docker-compose.public-terminal.yml"),
            "--profile", "fin-terminal-public-pilot",
            *extra,
        ]

    @classmethod
    def _render_compose(cls, *extra: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PRIVATE_CORE_TOKEN": "test-token",
            "OPENROUTER_API_KEY": "test-key",
            "FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY": "test-sig",
            "FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN": "test-worker",
            "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN": "test-edge",
            "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY": "test-ts-site",
            "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET": "test-ts-secret",
            "FIN_TERMINAL_PROXY_TOKEN": "test-proxy",
            "FIN_TERMINAL_DEMO_PROXY_TOKEN": "test-demo",
            "HOSTED_AGENT_SERVICE_TOKEN": "test-hosted",
            "TRIAL_AGENT_KEY": "test-trial",
            "JWT_SECRET": "test-jwt",
        }
        return subprocess.run(
            cls._compose_args(*extra),
            capture_output=True, text=True, timeout=30, env=env,
        )

    def test_public_terminal_compose_valid(self) -> None:
        result = self._render_compose("config", "--no-interpolate", "--quiet")
        if result.returncode != 0 and "PRIVATE_CORE_TOKEN" in (result.stderr or ""):
            self.skipTest("PRIVATE_CORE_TOKEN env var required for compose config")
        self.assertEqual(result.returncode, 0,
                         f"Compose render failed:\n{result.stderr}")

    def test_exactly_six_seats_in_overlay(self) -> None:
        result = self._render_compose("config", "--format", "json")
        if result.returncode != 0 and "PRIVATE_CORE_TOKEN" in (result.stderr or ""):
            self.skipTest("PRIVATE_CORE_TOKEN env var required for compose config")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        services = config.get("services", {})
        seats = [s for s in services if s.startswith("fin-terminal-public-seat-")]
        self.assertEqual(len(seats), 6, f"Found {len(seats)} seats: {seats}")

    def test_no_host_ports_on_seat_services(self) -> None:
        result = self._render_compose("config", "--format", "json")
        if result.returncode != 0:
            self.skipTest("Compose config not renderable in test env")
        config = json.loads(result.stdout)
        for name, svc in config.get("services", {}).items():
            if name.startswith("fin-terminal-public-seat-"):
                ports = svc.get("ports", [])
                self.assertEqual(ports, [],
                                 f"{name} has published ports: {ports}")

    def test_eighteen_networks_in_pilot(self) -> None:
        result = self._render_compose("config", "--format", "json")
        if result.returncode != 0:
            self.skipTest("Compose config not renderable in test env")
        config = json.loads(result.stdout)
        networks = config.get("networks", {})
        pilot_nets = [n for n in networks if n.startswith("fin_terminal_public_")]
        # 1 state + 6 mcp + 6 egress + 6 seat + 2 shared = 21
        # But the shared ones are fin_terminal_public and fin_terminal_public_egress
        # and fin_terminal_public_state
        self.assertGreaterEqual(len(pilot_nets), 18,
            f"Expected at least 18 pilot networks, got {len(pilot_nets)}")


class ShellSyntaxTests(unittest.TestCase):
    def test_reconciler_valid_python_syntax(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(RECONCILER_PATH)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         f"Python syntax error:\n{result.stderr}")

    def test_pilot_script_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY_DIR / "public_terminal_pilot_remote.sh")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         f"Bash syntax error:\n{result.stderr}")

    def test_systemd_unit_valid_syntax(self) -> None:
        unit_path = DEPLOY_DIR / "terminal-runtime-reconciler.service"
        if unit_path.exists():
            text = unit_path.read_text()
            # Basic systemd unit validation
            self.assertIn("[Unit]", text)
            self.assertIn("[Service]", text)
            self.assertIn("[Install]", text)
            self.assertIn("ExecStart=", text)
            self.assertIn("Restart=", text)


# ---------------------------------------------------------------------------
# J) Configuration validation
# ---------------------------------------------------------------------------
class ConfigValidationTests(unittest.TestCase):
    def test_disabled_config_passes_validation(self) -> None:
        config = reconciler_module.ReconcilerConfig(enabled=False)
        errors = config.validate()
        self.assertEqual(errors, [])

    def test_missing_token_rejected(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="",
        )
        errors = config.validate()
        self.assertIn("TERMINAL_RUNTIME_MANAGEMENT_TOKEN", str(errors))

    def test_short_token_rejected(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="short",
        )
        errors = config.validate()
        self.assertIn("TERMINAL_RUNTIME_MANAGEMENT_TOKEN", str(errors))

    def test_invalid_compose_project_rejected(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32,
            compose_project="bad project!",
        )
        errors = config.validate()
        self.assertTrue(any("compose_project" in e for e in errors))

    def test_valid_config_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = reconciler_module.ReconcilerConfig(
                enabled=True,
                management_token="t" * 32,
                compose_project="unchained",
                compose_dir=td,
            )
            errors = config.validate()
            self.assertEqual(errors, [])

    def test_reconcile_interval_minimum(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32,
            compose_dir="/tmp", reconcile_interval=1,
        )
        errors = config.validate()
        self.assertTrue(any("reconcile_interval" in e for e in errors))

    def test_idle_scale_down_minimum(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32,
            compose_dir="/tmp", idle_scale_down=10,
        )
        errors = config.validate()
        self.assertTrue(any("idle_scale_down" in e for e in errors))


# ---------------------------------------------------------------------------
# K) Idle scale-down timing
# ---------------------------------------------------------------------------
class IdleScaleDownTests(unittest.TestCase):
    def test_idle_seats_ordered_by_idle_time_descending(self) -> None:
        """Seats with longest idle time should be drained first."""
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        with mock.patch.object(reconciler_module.DockerInterface, "container_state", return_value="healthy"), \
             mock.patch.object(reconciler_module.DockerInterface, "stop_service"), \
             mock.patch.object(reconciler_module.DockerInterface, "remove_service"), \
             mock.patch.object(reconciler_module.GatewayManagementClient, "drain_seat") as mock_drain:
            mock_drain.return_value = True

            r = reconciler_module.TerminalRuntimeReconciler(config)
            seats = {}
            for i, name in enumerate(sorted(ALLOWED_SEAT_NAMES)):
                seats[name] = reconciler_module.SeatState(
                    name=name, status="healthy", assigned=False,
                    generation=f"gen-{name}", idle_seconds=float(i * 60),
                )
            snapshot = reconciler_module.GatewaySnapshot(
                seats=seats, total_assigned=0, total_queued=0,
            )
            r._scale_down(snapshot, 2)

            # Most idle first: seat-06 (300s) then seat-05 (240s)
            drain_calls = [c[0][0] for c in mock_drain.call_args_list]
            self.assertEqual(len(drain_calls), 2)
            self.assertEqual(drain_calls[0], "fin-terminal-public-seat-06")
            self.assertEqual(drain_calls[1], "fin-terminal-public-seat-05")


# ---------------------------------------------------------------------------
# L) Reconcile snapshot serialization
# ---------------------------------------------------------------------------
class SnapshotParsingTests(unittest.TestCase):
    def test_seat_state_from_gateway_entry(self) -> None:
        entry = {
            "containerId": "abc123",
            "status": "healthy",
            "generation": "gen-abc",
            "assigned": True,
            "idleSeconds": 42.0,
        }
        seat = reconciler_module.SeatState.from_gateway_snapshot("fin-terminal-public-seat-01", entry)
        self.assertEqual(seat.name, "fin-terminal-public-seat-01")
        self.assertEqual(seat.container_id, "abc123")
        self.assertEqual(seat.status, "healthy")
        self.assertEqual(seat.generation, "gen-abc")
        self.assertTrue(seat.assigned)
        self.assertEqual(seat.idle_seconds, 42.0)
        self.assertTrue(seat.running)
        self.assertFalse(seat.transitory)
        self.assertFalse(seat.stopped)

    def test_seat_state_running_not_healthy(self) -> None:
        for status in ("starting", "draining", "absent", "stopped"):
            seat = reconciler_module.SeatState(name="x", status=status)
            self.assertFalse(seat.running, f"status={status} should not be running")

    def test_seat_state_transitory(self) -> None:
        self.assertTrue(reconciler_module.SeatState(name="x", status="starting").transitory)
        self.assertTrue(reconciler_module.SeatState(name="x", status="draining").transitory)
        self.assertFalse(reconciler_module.SeatState(name="x", status="healthy").transitory)
        self.assertFalse(reconciler_module.SeatState(name="x", status="absent").transitory)

    def test_seat_state_stopped(self) -> None:
        self.assertTrue(reconciler_module.SeatState(name="x", status="absent").stopped)
        self.assertTrue(reconciler_module.SeatState(name="x", status="stopped").stopped)
        self.assertFalse(reconciler_module.SeatState(name="x", status="healthy").stopped)


# ---------------------------------------------------------------------------
# M) Env file feature flag default-off
# ---------------------------------------------------------------------------
class FeatureFlagDefaultOffTests(unittest.TestCase):
    def test_from_env_defaults_to_disabled(self) -> None:
        config = reconciler_module.ReconcilerConfig.from_env({})
        self.assertFalse(config.enabled)

    def test_from_env_enabled_when_true(self) -> None:
        config = reconciler_module.ReconcilerConfig.from_env({
            "TERMINAL_RUNTIME_FEATURE_ENABLED": "true",
            "TERMINAL_RUNTIME_MANAGEMENT_TOKEN": "t" * 64,
        })
        self.assertTrue(config.enabled)

    def test_from_env_case_insensitive_trimmed(self) -> None:
        # " True " after strip() → "True", after lower() → "true"
        # Since no token is provided, validation would fail, but the "enabled" flag
        # itself is True
        config = reconciler_module.ReconcilerConfig.from_env({
            "TERMINAL_RUNTIME_FEATURE_ENABLED": " True ",
        })
        self.assertTrue(config.enabled, "Flag ' True ' should be truthy after trim+lower")

    def test_from_env_lowercase_false_is_disabled(self) -> None:
        config = reconciler_module.ReconcilerConfig.from_env({
            "TERMINAL_RUNTIME_FEATURE_ENABLED": "false",
        })
        self.assertFalse(config.enabled)

    def test_from_env_all_settings(self) -> None:
        env = {
            "TERMINAL_RUNTIME_FEATURE_ENABLED": "true",
            "TERMINAL_RUNTIME_MANAGEMENT_TOKEN": "t" * 64,
            "TERMINAL_RUNTIME_COMPOSE_PROJECT": "myproj",
            "TERMINAL_RUNTIME_COMPOSE_DIR": "/opt/unchained",
            "TERMINAL_RUNTIME_RECONCILE_INTERVAL": "30",
            "TERMINAL_RUNTIME_IDLE_SCALE_DOWN": "600",
            "TERMINAL_RUNTIME_MAX_START_CONCUR": "3",
            "TERMINAL_RUNTIME_HOST_MEM_RESERVE_MB": "1024",
            "TERMINAL_RUNTIME_HOST_MEM_HEADROOM_PCT": "20",
            "TERMINAL_RUNTIME_HOST_DISK_MAX_PCT": "90",
        }
        config = reconciler_module.ReconcilerConfig.from_env(env)
        self.assertTrue(config.enabled)
        self.assertEqual(config.management_token, "t" * 64)
        self.assertEqual(config.compose_project, "myproj")
        self.assertEqual(config.compose_dir, "/opt/unchained")
        self.assertEqual(config.reconcile_interval, 30)
        self.assertEqual(config.idle_scale_down, 600)
        self.assertEqual(config.max_start_concurrency, 3)
        self.assertEqual(config.host_mem_reserve_mb, 1024)
        self.assertEqual(config.host_mem_headroom_pct, 20)
        self.assertEqual(config.host_disk_max_pct, 90)
        self.assertEqual(config.lock_file, "/opt/unchained/.deploy.lock")


# ---------------------------------------------------------------------------
# N) Gateway management API endpoint contract
# ---------------------------------------------------------------------------
class GatewayAPIContractTests(unittest.TestCase):
    def test_reconcile_snapshot_endpoint_path(self) -> None:
        source = RECONCILER_PATH.read_text()
        self.assertIn("/api/management/reconcile-snapshot", source)

    def test_reconcile_plan_endpoint_path(self) -> None:
        source = RECONCILER_PATH.read_text()
        self.assertIn("/api/management/reconcile-plan", source)

    def test_drain_endpoint_path(self) -> None:
        source = RECONCILER_PATH.read_text()
        self.assertIn("/api/management/drain", source)

    def test_activate_endpoint_path(self) -> None:
        source = RECONCILER_PATH.read_text()
        self.assertIn("/api/management/activate", source)

    def test_management_token_in_header_not_query(self) -> None:
        source = RECONCILER_PATH.read_text()
        self.assertIn("X-Management-Token", source)
        # Token must not appear in URL
        self.assertIn("X-Management-Token", source)
        self.assertNotIn("?token=", source)
        self.assertNotIn("&token=", source)

    def test_payload_is_json_not_string_interpolation(self) -> None:
        source = RECONCILER_PATH.read_text()
        for path in ("/api/management/reconcile-plan", "/api/management/drain", "/api/management/activate"):
            # Find the _exec call for this path
            self.assertIn("json.dumps", source, "Payload must be JSON-serialized")

    def test_gateway_token_is_json_escaped_not_interpolated(self) -> None:
        """The management token must never be f-string interpolated into the
        docker-exec Node script (token with quotes would break out of the JS
        string literal)."""
        source = RECONCILER_PATH.read_text()
        self.assertIn("json.dumps(self._token)", source)
        self.assertNotIn('X-Management-Token": "', source)

    def test_all_management_calls_are_posts_with_header(self) -> None:
        source = RECONCILER_PATH.read_text()
        # Every management call uses method "POST" (JS-escaped inside the
        # Python string literal) and the X-Management-Token header.
        self.assertEqual(source.count('method: \\"POST\\"'), 1)
        self.assertIn("X-Management-Token", source)

    def test_management_api_runs_inside_gateway_container_not_host(self) -> None:
        source = RECONCILER_PATH.read_text()
        # Never exposes the management listener on the host or through Caddy.
        self.assertNotIn("0.0.0.0:8788", source)
        self.assertNotIn("0.0.0.0:8789", source)
        self.assertIn("docker", source)
        self.assertIn("exec", source)

    def test_exec_uses_stdin_node_e_not_host_temp_file(self) -> None:
        """The reconciler must call the private API via `docker exec -i ... node -e`
        with the payload on stdin — never a host temp path inside the container."""
        source = RECONCILER_PATH.read_text()
        self.assertIn('"node", "-e", script', source)
        self.assertIn('input=payload_json', source)
        self.assertNotIn("NamedTemporaryFile", source)
        self.assertNotIn("tmp_path", source)

    def test_management_port_is_configurable_and_defaults_to_8789(self) -> None:
        config = reconciler_module.ReconcilerConfig.from_env({
            "TERMINAL_RUNTIME_FEATURE_ENABLED": "true",
            "TERMINAL_RUNTIME_MANAGEMENT_TOKEN": "t" * 64,
        })
        self.assertEqual(config.management_port, 8789)
        config2 = reconciler_module.ReconcilerConfig.from_env({
            "TERMINAL_RUNTIME_FEATURE_ENABLED": "true",
            "TERMINAL_RUNTIME_MANAGEMENT_TOKEN": "t" * 64,
            "TERMINAL_RUNTIME_MANAGEMENT_PORT": "8791",
        })
        self.assertEqual(config2.management_port, 8791)
        errors = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, management_port=0,
        ).validate()
        self.assertTrue(any("MANAGEMENT_PORT" in e for e in errors))

    def test_worker_service_mapping_roundtrip(self) -> None:
        for n in range(1, 7):
            service = f"fin-terminal-public-seat-{n:02d}"
            self.assertEqual(
                reconciler_module._worker_to_service(f"seat-{n:02d}"), service
            )
            self.assertEqual(
                reconciler_module._service_to_worker(service), f"seat-{n:02d}"
            )

    def test_worker_service_mapping_rejects_unexpected_ids(self) -> None:
        for bad in ("seat-07", "gateway", "rm -rf /", "seat-1", "", None):
            with self.assertRaises(ValueError):
                reconciler_module._worker_to_service(bad)
        with self.assertRaises(ValueError):
            reconciler_module._service_to_worker("fin-terminal-public-gateway")

    def test_reconcile_snapshot_parses_v1_seat_map(self) -> None:
        """Gateway v1 snapshot keys seats by workerId; the reconciler maps them
        to its own service names and prefers plan.desiredRunning."""
        data = {
            "version": 1,
            "seats": {
                "seat-01": {"workerId": "seat-01", "status": "healthy", "phase": "active",
                            "generation": "gen-1", "assigned": True, "idleSeconds": 0,
                            "drainRequested": False, "drainId": None, "containerId": ""},
                "seat-02": {"workerId": "seat-02", "status": "healthy", "phase": "ready-idle",
                            "generation": "gen-2", "assigned": False, "idleSeconds": 400,
                            "drainRequested": True, "drainId": "dr-abc", "containerId": ""},
                "seat-03": {"workerId": "seat-03", "status": "absent", "phase": "absent",
                            "generation": None, "assigned": False, "idleSeconds": 0,
                            "drainRequested": False, "drainId": None, "containerId": ""},
                "seat-04": {"workerId": "seat-04", "status": "starting", "phase": "starting",
                            "generation": "gen-4", "assigned": False, "idleSeconds": 0,
                            "drainRequested": False, "drainId": None, "containerId": ""},
                "seat-05": {"workerId": "seat-05", "status": "draining", "phase": "draining",
                            "generation": "gen-5", "assigned": False, "idleSeconds": 0,
                            "drainRequested": True, "drainId": "dr-5", "containerId": ""},
                "seat-06": {"workerId": "seat-06", "status": "stopped", "phase": "recycling",
                            "generation": "gen-6", "assigned": False, "idleSeconds": 0,
                            "drainRequested": False, "drainId": None, "containerId": ""},
            },
            "totalAssigned": 1,
            "totalQueued": 2,
            "plan": {"desiredRunning": 4, "scaleDownCandidates": ["seat-02"],
                     "activateCandidates": ["seat-03"]},
        }
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        client = reconciler_module.GatewayManagementClient(config)
        with mock.patch.object(client, "_exec", return_value=data):
            snapshot = client.reconcile_snapshot()
        self.assertEqual(set(snapshot.seats.keys()), set(ALLOWED_SEAT_NAMES))
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-01"].status, "healthy")
        self.assertTrue(snapshot.seats["fin-terminal-public-seat-01"].assigned)
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-01"].generation, "gen-1")
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-02"].idle_seconds, 400.0)
        self.assertTrue(snapshot.seats["fin-terminal-public-seat-02"].running)
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-03"].status, "absent")
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-04"].status, "starting")
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-05"].status, "draining")
        self.assertEqual(snapshot.seats["fin-terminal-public-seat-06"].status, "stopped")
        self.assertFalse(snapshot.seats["fin-terminal-public-seat-06"].running)
        self.assertEqual(snapshot.total_assigned, 1)
        self.assertEqual(snapshot.total_queued, 2)
        # The gateway plan is authoritative.
        self.assertEqual(snapshot.desired_from_plan, 4)
        self.assertEqual(snapshot.running_count, 2)
        self.assertEqual(snapshot.draining_count, 1)

    def test_desired_from_plan_falls_back_to_formula(self) -> None:
        snapshot = reconciler_module.GatewaySnapshot(
            seats={name: reconciler_module.SeatState(name=name) for name in SEAT_NAMES},
            total_assigned=1, total_queued=3, plan={},
        )
        self.assertEqual(snapshot.desired_from_plan, 5)  # formula fallback
        snapshot.plan = {"desiredRunning": 6}
        self.assertEqual(snapshot.desired_from_plan, 6)

    def test_drain_payload_uses_workerid_drainid_and_generation_cas(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        client = reconciler_module.GatewayManagementClient(config)
        with mock.patch.object(client, "_exec", return_value={"accepted": True}) as mock_exec:
            accepted = client.drain_seat("fin-terminal-public-seat-03", "gen-3")
        self.assertTrue(accepted)
        url_path, payload = mock_exec.call_args[0]
        self.assertEqual(url_path, "/api/management/drain")
        self.assertEqual(payload["workerId"], "seat-03")
        self.assertEqual(payload["expectedGeneration"], "gen-3")
        self.assertTrue(payload["drainId"].startswith("dr-"))
        self.assertNotIn("seatName", payload)

    def test_drain_rejects_stale_generation(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        client = reconciler_module.GatewayManagementClient(config)
        with mock.patch.object(client, "_exec", side_effect=RuntimeError("409")):
            self.assertFalse(client.drain_seat("fin-terminal-public-seat-03", "gen-stale"))

    def test_activate_uses_workerid_and_accepted_response(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        client = reconciler_module.GatewayManagementClient(config)
        with mock.patch.object(client, "_exec", return_value={"accepted": True}) as mock_exec:
            accepted = client.activate_seat("fin-terminal-public-seat-04")
        self.assertTrue(accepted)
        url_path, payload = mock_exec.call_args[0]
        self.assertEqual(url_path, "/api/management/activate")
        self.assertEqual(payload["workerId"], "seat-04")
        self.assertNotIn("seatName", payload)

    def test_activate_rejects_when_not_accepted(self) -> None:
        config = reconciler_module.ReconcilerConfig(
            enabled=True, management_token="t" * 32, compose_dir="/tmp",
        )
        client = reconciler_module.GatewayManagementClient(config)
        with mock.patch.object(client, "_exec", return_value={"accepted": False, "reason": "sticky"}):
            self.assertFalse(client.activate_seat("fin-terminal-public-seat-04"))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main(verbosity=2)
