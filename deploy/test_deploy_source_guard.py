"""Tests for the fail-closed production deployment source guard."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "deploy" / "deploy_source_guard.sh"


class DeploySourceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        base = Path(self._temp.name)
        self.remote = base / "origin.git"
        self.repo = base / "repo"

        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(self.repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        self._git("config", "user.name", "Deploy Guard Test")
        self._git("config", "user.email", "deploy-guard@example.invalid")
        (self.repo / ".gitignore").write_text("/private-core/\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
        self._git("add", ".gitignore", "tracked.txt")
        self._git("commit", "-m", "initial")
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-u", "origin", "main")
        self.revision = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _guard(self, revision: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; verify_deploy_source "$2" "$3"',
                "deploy-source-guard-test",
                str(GUARD),
                str(self.repo),
                revision,
            ],
            capture_output=True,
            text=True,
        )

    def test_accepts_clean_current_main_with_explicit_revision(self) -> None:
        result = self._guard(self.revision)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_allows_ignored_private_core_checkout(self) -> None:
        private_file = self.repo / "private-core" / "unchained" / "cdp.py"
        private_file.parent.mkdir(parents=True)
        private_file.write_text("private overlay input\n", encoding="utf-8")

        result = self._guard(self.revision)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_requires_explicit_lowercase_revision(self) -> None:
        for revision in ("", "abc123", self.revision.upper()):
            with self.subTest(revision=revision):
                result = self._guard(revision)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DEPLOY_REVISION must be set", result.stderr)

    def test_rejects_modified_tracked_file(self) -> None:
        (self.repo / "tracked.txt").write_text("local hotfix\n", encoding="utf-8")

        result = self._guard(self.revision)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree is dirty", result.stderr)
        self.assertIn("tracked.txt", result.stderr)

    def test_rejects_untracked_file(self) -> None:
        (self.repo / "untracked.txt").write_text("not reviewed\n", encoding="utf-8")

        result = self._guard(self.revision)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree is dirty", result.stderr)
        self.assertIn("untracked.txt", result.stderr)

    def test_rejects_revision_that_does_not_match_head(self) -> None:
        result = self._guard("0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the deployment worktree HEAD", result.stderr)

    def test_rejects_clean_head_that_is_not_current_origin_main(self) -> None:
        (self.repo / "tracked.txt").write_text("local commit\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "local only")
        local_revision = self._git("rev-parse", "HEAD").stdout.strip()

        result = self._guard(local_revision)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not the current origin/main revision", result.stderr)

    def test_refreshes_origin_main_before_comparing(self) -> None:
        tracking_before = self._git(
            "rev-parse", "refs/remotes/origin/main"
        ).stdout.strip()
        peer = Path(self._temp.name) / "peer"
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.remote), str(peer)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Peer Test"],
            cwd=peer,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "peer@example.invalid"],
            cwd=peer,
            check=True,
        )
        (peer / "tracked.txt").write_text("new remote main\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=peer, check=True)
        subprocess.run(
            ["git", "commit", "-m", "advance main"],
            cwd=peer,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=peer,
            check=True,
            capture_output=True,
            text=True,
        )

        result = self._guard(self.revision)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not the current origin/main revision", result.stderr)
        tracking_after = self._git(
            "rev-parse", "refs/remotes/origin/main"
        ).stdout.strip()
        self.assertEqual(tracking_after, tracking_before)

    def test_deploy_invokes_guard_before_private_overlay(self) -> None:
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        guard_call = deploy.index(
            'verify_deploy_source "$SCRIPT_DIR" "$DEPLOY_REVISION" || exit 1'
        )
        overlay = deploy.index("# Auto-install private core overlay when available.")

        self.assertLess(guard_call, overlay)
        self.assertIn('DEPLOY_REVISION="${DEPLOY_REVISION-}"', deploy)
        self.assertNotIn('DEPLOY_REVISION="${DEPLOY_REVISION:-$(git', deploy)
        self.assertIn("/private-core/", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("DEPLOY_REVISION: ${{ github.sha }}", workflow)


if __name__ == "__main__":
    unittest.main()
