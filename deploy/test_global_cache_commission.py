"""Tests for the global cache runner commission script and workflow.

Covers the review contract for shadow commissioning:
  A) Action gating: only enable/disable/status, validated SHAs, lock guard.
  B) Enable path pins the digest image, verifies the OCI revision label,
     starts the runner, waits for health, and proves commissioning with
     aggregate counts only (never titles, URLs, contents, or secrets).
  C) Commissioning requires 7 sources, >=4 successes across >=3 hosts,
     scheduler advancement, pinned dry-run policy, zero dispatch records,
     and zero cache entries (dispatch off, no publication in shadow mode).
  D) Fail-closed disable keeps the data volume; status never mutates.
  E) Workflow requires explicit confirmation, main-only ref, production
     environment, stale-main guards, and verified SSH.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _script_text() -> str:
    return (_repo_root() / "deploy" / "commission_global_cache_runner.sh").read_text(encoding="utf-8")


def _workflow_text() -> str:
    return (_repo_root() / ".github" / "workflows" / "commission-global-cache.yml").read_text(encoding="utf-8")


EXPECTED_SOURCE_IDS = [
    "nasdaq-trade-halts",
    "nasdaq-corporate-actions",
    "sec-current-filings",
    "federal-reserve-monetary",
    "bea-news",
    "ftc-press-releases",
    "doj-news",
]


class CommissionScriptStructure(unittest.TestCase):
    def test_action_gate_and_sha_validation(self) -> None:
        text = _script_text()
        self.assertIn('enable | disable | status', text)
        self.assertIn("EXPECTED_INFRA_SHA must be a 40-character lowercase Git SHA", text)
        self.assertIn("EXPECTED_RUNNER_SHA must be a 40-character lowercase Git SHA", text)
        self.assertIn("deployment lock is already held", text)

    def test_enable_pins_digest_image_and_verifies_revision(self) -> None:
        text = _script_text()
        self.assertIn("EXPECTED_IMAGE must be a digest-pinned reference", text)
        self.assertIn("never from the host .env", text)
        self.assertIn('org.opencontainers.image.revision', text)
        self.assertIn("runner image revision does not match the reviewed app revision", text)
        self.assertIn("pinned runner image did not persist to production .env", text)
        self.assertIn("set_env_value FIN_TERMINAL_GLOBAL_CACHE_ENABLED true", text)

    def test_enable_verifies_pulled_digest_not_just_label(self) -> None:
        text = _script_text()
        self.assertIn("pulled runner image digest does not match the pinned reference", text)
        self.assertIn("RepoDigests", text)

    def test_pull_retries_before_failing_closed(self) -> None:
        text = _script_text()
        self.assertIn("could not pull the pinned runner image after 3 attempts", text)

    def test_env_reader_is_scoped_to_non_secrets(self) -> None:
        text = _script_text()
        self.assertIn("get_nonsensitive_env_value", text)
        self.assertIn("never be used for tokens, keys, or other credentials", text)
        self.assertNotRegex(text, r"(?<!nonsensitive_)get_env_value")

    def test_probe_cleanup_precedes_install(self) -> None:
        text = _script_text()
        probe_block = text.split("commission_probe() {")[1].split("\n}\n")[0]
        pre_clean = probe_block.find("rm -f /tmp/gc-health-probe.mjs")
        install = probe_block.find("docker cp")
        self.assertNotEqual(pre_clean, -1)
        self.assertNotEqual(install, -1)
        self.assertLess(pre_clean, install)

    def test_health_wait_before_commissioning(self) -> None:
        text = _script_text()
        self.assertIn("timed out waiting for global-cache runner health", text)
        self.assertIn("wait_for_health", text)

    def test_commission_contract(self) -> None:
        text = _script_text()
        for source_id in EXPECTED_SOURCE_IDS:
            self.assertIn(f'"{source_id}"', text)
        self.assertIn("fewer than 4 fresh successes", text)
        self.assertIn("fewer than 3 distinct hosts", text)
        self.assertIn("scheduler has not advanced yet", text)
        self.assertIn("unexpected dispatch records (dispatch must stay off)", text)
        self.assertIn("unexpected cache entries (no publication in shadow mode)", text)
        self.assertIn("COMMISSION OK", text)

    def test_probe_failure_carries_bounded_detail(self) -> None:
        text = _script_text()
        self.assertIn("detail", text)
        self.assertIn("head -c 500", text)
        self.assertIn("probe execution failed: {probe.get('detail', '')}", text)
        self.assertNotIn('|| echo \'{"probeFailed":true}\'', text)

    def test_probe_prints_aggregates_only(self) -> None:
        text = _script_text()
        probe = text.split("cat >\"$probe_path\" <<'JS'")[1].split("\nJS")[0]
        self.assertNotIn("console.log(state", probe)
        self.assertNotIn("decisions", probe)
        self.assertNotIn("candidates", probe)
        self.assertNotIn("OPENROUTER", probe)
        self.assertNotIn("TOKEN", probe)
        for field in ("sourceCount", "successCount", "hostCount", "schedulerAdvanced",
                       "policyVerified", "dispatchRecords", "cacheEntries"):
            self.assertIn(field, probe)

    def test_disable_is_fail_closed_and_keeps_volume(self) -> None:
        text = _script_text()
        disable_block = text.split("disable_runner() {")[1].split("\n}\n")[0]
        self.assertIn("set_env_value FIN_TERMINAL_GLOBAL_CACHE_ENABLED false", disable_block)
        self.assertIn("stop fin-terminal-global-cache", disable_block)
        self.assertNotIn(" down ", disable_block)
        self.assertNotIn("volume rm", disable_block)

    def test_status_never_mutates(self) -> None:
        text = _script_text()
        status_block = text.split("status)")[1].split(";;")[0]
        self.assertNotIn("set_env_value", status_block)
        self.assertNotIn(" up -d", status_block)


class CommissionWorkflowStructure(unittest.TestCase):
    def test_confirmation_and_guards(self) -> None:
        text = _workflow_text()
        self.assertIn("- status", text)
        self.assertIn("- enable", text)
        self.assertIn("- disable", text)
        self.assertIn("if: github.ref == 'refs/heads/main'", text)
        self.assertIn("name: production", text)
        self.assertIn("ENABLE GLOBAL CACHE", text)
        self.assertIn("DISABLE GLOBAL CACHE", text)
        self.assertIn("Main advanced before the commission action started", text)
        self.assertIn("Fail-closed disable also failed", text)
        self.assertIn('rm -rf -- "$RUNNER_TEMP/unchained-cache-commission-ssh"', text)

    def test_pins_come_from_repo_not_inputs(self) -> None:
        text = _workflow_text()
        self.assertIn("FIN_TERMINAL_GLOBAL_CACHE_SOURCE_REVISION", text)
        self.assertIn("FIN_TERMINAL_GLOBAL_CACHE_IMAGE", text)
        self.assertIn("image is not digest-pinned", text)
        self.assertNotIn("image_tag", text)


if __name__ == "__main__":
    unittest.main()
