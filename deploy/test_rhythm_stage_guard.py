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


class RhythmStageGuardTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
