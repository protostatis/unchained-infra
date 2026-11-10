"""Tests for the fail-closed rhythm staging guard.

A production deploy must either ship a complete rhythm source or refuse to run:
shipping a partial copy silently keeps the previously baked rhythm in the image
while a new private core is overlaid on top of it.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "deploy" / "rhythm_stage_guard.sh"

REQUIRED = (
    "rhythm/__init__.py",
    "rhythm/tools.py",
    "rhythm_binding.py",
    "schema_registry.py",
    "schema_db.py",
    "state_machine.py",
    "catcher_agent.js",
    "prime_interceptor.js",
    "executor_binding.js",
)


class _RhythmFixture:
    """Shared temp-tree helpers; not a TestCase so tests are not collected twice."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        base = Path(self._temp.name)
        self.src = base / "rhythm-src"
        self.dst = base / "build-context" / "rhythm"
        self.src.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_source(self, files=REQUIRED) -> None:
        for relative in files:
            path = self.src / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {relative}\n", encoding="utf-8")

    def _stage(self, require: str = "0") -> subprocess.CompletedProcess:
        script = (
            f'source "{GUARD}"\n'
            f'stage_rhythm_source "{self.src}" "{self.dst}" {require}\n'
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        )


class RhythmStageGuardTests(_RhythmFixture, unittest.TestCase):
    def test_complete_source_is_staged(self) -> None:
        self._write_source()

        result = self._stage()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.dst / "rhythm" / "tools.py").is_file())
        self.assertTrue((self.dst / "catcher_agent.js").is_file())
        self.assertFalse((self.dst / ".empty").exists())

    def test_missing_source_is_fatal_when_required(self) -> None:
        result = self._stage(require="1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing rhythm file", result.stderr)
        self.assertFalse((self.dst / ".empty").exists())
        self.assertFalse((self.dst / "rhythm").exists())

    def test_partial_source_is_fatal_when_required(self) -> None:
        # A directory that exists but lacks tools.py is the real hazard: the
        # old check only tested for a directory and copied a partial tree.
        self._write_source([f for f in REQUIRED if f != "rhythm/tools.py"])

        result = self._stage(require="1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("rhythm/tools.py", result.stderr)

    def test_missing_source_degrades_when_optional(self) -> None:
        result = self._stage(require="0")

        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.dst / ".empty").is_file())
        self.assertFalse((self.dst / "rhythm").exists())

    def test_partial_source_degrades_when_optional(self) -> None:
        self._write_source(["rhythm/__init__.py"])

        result = self._stage(require="0")

        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.dst / ".empty").is_file())

    def test_staging_replaces_a_stale_destination(self) -> None:
        self.dst.mkdir(parents=True)
        (self.dst / "stale.py").write_text("old\n", encoding="utf-8")
        self._write_source()

        result = self._stage()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dst / "stale.py").exists())
        self.assertTrue((self.dst / "rhythm" / "tools.py").is_file())

    def test_filesystem_failure_is_distinct_from_a_missing_source(self) -> None:
        # A complete source that cannot be written must not be reported as
        # "incomplete", and must not be mistaken for the optional path.
        self._write_source()
        blocker = self.dst.parent
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory\n", encoding="utf-8")

        result = self._stage()

        self.assertEqual(result.returncode, 3, result.stderr)


class RhythmStageCallerTests(unittest.TestCase):
    """deploy.sh runs under `set -e`, so the caller must capture the status.

    These exercise the exact idiom deploy.sh uses: an unguarded call to a
    function that returns nonzero would terminate the script before the branch
    could run, which silently turned the optional path into a hard failure.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        base = Path(self._temp.name)
        self.src = base / "rhythm-src"
        self.dst = base / "build-context" / "rhythm"
        self.src.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _run_caller(self, require: str) -> subprocess.CompletedProcess:
        # Mirrors deploy.sh: errexit, status capture, and the case dispatch.
        script = (
            "set -euo pipefail\n"
            f'source "{GUARD}"\n'
            "status=0\n"
            f'stage_rhythm_source "{self.src}" "{self.dst}" {require} || status=$?\n'
            'case "$status" in\n'
            '  0) echo staged ;;\n'
            '  1) echo "incomplete" >&2; exit 1 ;;\n'
            '  2) echo skipped ;;\n'
            '  *) echo "filesystem" >&2; exit 1 ;;\n'
            "esac\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_optional_missing_source_degrades_instead_of_aborting(self) -> None:
        result = self._run_caller(require="0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "skipped")

    def test_required_missing_source_aborts_the_caller(self) -> None:
        result = self._run_caller(require="1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("incomplete", result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class RhythmRevisionTests(_RhythmFixture, unittest.TestCase):
    """The staged identity file that makes a rhythm change diffable."""

    def _write_revision(self) -> subprocess.CompletedProcess:
        script = (
            f'source "{GUARD}"\n'
            f'write_rhythm_revision "{self.src}" "{self.dst}"\n'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def _revision(self) -> str:
        return (self.dst / "REVISION").read_text(encoding="utf-8").strip()

    def _digest(self) -> str:
        return self._revision().split("sha256:", 1)[1]

    def test_revision_records_source_and_content_digest(self) -> None:
        self._write_source()
        self.assertEqual(self._stage(require="1").returncode, 0)

        result = self._write_revision()

        self.assertEqual(result.returncode, 0, result.stderr)
        revision, _, digest = self._revision().partition(" sha256:")
        self.assertEqual(revision, "unknown")  # no git metadata in the fixture
        self.assertRegex(digest, r"^[0-9a-f]{32}$")

    def test_digest_is_stable_for_identical_content(self) -> None:
        self._write_source()
        self._stage(require="1")
        self._write_revision()
        first = self._revision()

        self._stage(require="1")
        self._write_revision()

        self.assertEqual(self._revision(), first)

    def test_content_change_changes_the_digest(self) -> None:
        self._write_source()
        self._stage(require="1")
        self._write_revision()
        before = self._digest()

        (self.src / "rhythm" / "tools.py").write_text("# edited\n", encoding="utf-8")
        self._stage(require="1")
        self._write_revision()

        self.assertNotEqual(self._digest(), before)

    def test_unstaged_tree_is_a_failure(self) -> None:
        # Nothing was staged, so there is no identity to assert and shipping
        # one would describe files that are not in the build context.
        self._write_source()

        result = self._write_revision()

        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.dst / "REVISION").exists())

    def test_git_revision_is_recorded_and_dirty_is_flagged(self) -> None:
        self._write_source()
        self._stage(require="1")
        git = [
            "git", "-C", str(self.src),
            "-c", "user.email=deploy@test", "-c", "user.name=deploy",
        ]
        subprocess.run(["git", "-C", str(self.src), "init", "-q", "."], check=True)
        subprocess.run(["git", "-C", str(self.src), "add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "fixture"], check=True)
        head = subprocess.run(
            ["git", "-C", str(self.src), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        self._write_revision()
        self.assertTrue(self._revision().startswith(f"{head} sha256:"), self._revision())

        (self.src / "rhythm" / "tools.py").write_text("# uncommitted\n", encoding="utf-8")
        self._stage(require="1")
        self._write_revision()

        self.assertTrue(
            self._revision().startswith(f"{head}-dirty sha256:"), self._revision()
        )


DEPLOY = ROOT / "deploy.sh"


class DeployCallerPolicyTests(unittest.TestCase):
    """Wiring checks on deploy.sh itself.

    A correct guard is still bypassable by its caller: an unguarded call under
    `set -e` aborts the script before the status can be interpreted, and an
    optional-by-default source reopens the silent staleness this path exists to
    prevent. These are text-level assertions because the behaviour they protect
    only shows up during a real deploy.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DEPLOY.read_text(encoding="utf-8")
        # Assertions on a call are compared after whitespace normalisation so a
        # rewrap does not fail the test without a behaviour change.
        cls.normalized = " ".join(cls.text.split())
        cls.lines = cls.text.splitlines()

    def test_rhythm_is_required_by_default(self) -> None:
        self.assertIn('REQUIRE_RHYTHM="${REQUIRE_RHYTHM:-1}"', self.normalized)

    def test_staging_status_is_captured_under_errexit(self) -> None:
        self.assertIn(
            'stage_rhythm_source "$RHYTHM_SRC" "$RHYTHM_DST" "$REQUIRE_RHYTHM" '
            "|| rhythm_stage_status=$?",
            self.normalized,
        )

    def test_no_unguarded_staging_call(self) -> None:
        bare = [
            line
            for line in self.lines
            if line.strip().startswith("stage_rhythm_source ")
            and "|| rhythm_stage_status=$?" not in line
        ]
        self.assertEqual(bare, [], f"unguarded staging call: {bare}")

    def test_staged_rhythm_gets_a_diffable_identity(self) -> None:
        # rhythm/REVISION is the only rhythm path in the deployed-path list, so
        # it is what makes a rhythm change visible to the classifier (and what
        # stops an unchanged rhythm from rebuilding private-core every deploy).
        self.assertIn(
            'write_rhythm_revision "$RHYTHM_SRC" "$RHYTHM_DST"', self.normalized
        )
        self.assertIn("rhythm/REVISION", self.normalized)

    def test_revision_is_uploaded_with_the_tree(self) -> None:
        self.assertIn('"$RHYTHM_DST/REVISION"', self.text)

    def test_running_revision_is_asserted_after_deploy(self) -> None:
        # A rhythm that never made it into the image would otherwise still
        # report as healthy. The expected value travels as an argument to the
        # verification script rather than being re-read from a host file, so a
        # missing file cannot silently skip the check.
        self.assertIn(
            'cat "${RHYTHM_ROOT:-/app/rhythm}/REVISION"', self.normalized
        )
        self.assertIn('"$FIN_TERMINAL_PUBLIC_HOST" "$RHYTHM_REVISION"', self.normalized)
        self.assertIn('rhythm_revision="${5:-}"', self.normalized)
        self.assertIn('if [[ -n "$rhythm_revision" ]]; then', self.normalized)

    def test_rhythm_staging_no_longer_forces_a_rebuild(self) -> None:
        # The classifier owns this decision now (rhythm/ -> private-core, see
        # test_compose_service_diff.py); forcing it here rebuilds private-core
        # on every deploy whether or not rhythm changed. The block is cut at a
        # line-anchored "fi": splitting on the bare substring would stop inside
        # the word "classifier" and assert nothing.
        staged_block = self.text.split("if $RHYTHM_STAGED; then", 1)[1].split(
            "\nfi\n", 1
        )[0]
        self.assertIn("Rhythm staged", staged_block)
        self.assertNotIn("add_services", staged_block)


if __name__ == "__main__":
    unittest.main()
