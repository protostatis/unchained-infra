"""Tests for the open-core boundary guard's rhythm rules.

The guard is what keeps "only private-core serves rhythm" a checkable invariant.
A rule that cannot fail is theatre, so each rule is exercised on a fixture that
violates it, and once more on a fixture that respects it.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


GUARD = Path(__file__).resolve().parent / "check_private_imports.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("check_private_imports", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposeRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = load_guard()
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _compose(self, text: str) -> Path:
        path = self.base / "docker-compose.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_private_core_only_is_accepted(self) -> None:
        compose = self._compose(
            "services:\n"
            "  private-core:\n"
            "    environment:\n"
            "      - RHYTHM_ROOT=/app/rhythm\n"
            "  web:\n"
            "    environment:\n"
            "      - TOKEN=x\n"
        )
        self.assertEqual(self.guard.compose_violations(compose), [])

    def test_quoted_service_key_is_attributed_to_that_service(self) -> None:
        # A quoted key is still a service key; an unquoted-only regex would keep
        # the previous service in scope and attribute this to private-core.
        compose = self._compose(
            "services:\n"
            "  private-core:\n"
            "    environment:\n"
            "      - RHYTHM_ROOT=/app/rhythm\n"
            '  "relay":\n'
            "    environment:\n"
            "      - RHYTHM_ROOT=/app/rhythm\n"
        )
        violations = self.guard.compose_violations(compose)
        self.assertTrue(violations)
        self.assertIn("relay", violations[0])

    def test_inline_comment_on_service_key_is_attributed(self) -> None:
        compose = self._compose(
            "services:\n"
            "  private-core:  # the only rhythm consumer\n"
            "    environment:\n"
            "      - RHYTHM_ROOT=/app/rhythm\n"
            "  relay:  # also a service\n"
            "    environment:\n"
            "      - RHYTHM_ROOT=/app/rhythm\n"
        )
        violations = self.guard.compose_violations(compose)
        self.assertTrue(violations)
        self.assertIn("relay", violations[0])

    def test_top_level_rhythm_root_key_is_rejected(self) -> None:
        # An unindented key is also the shape of a section header; it must not
        # be skipped just because it sits at column zero.
        compose = self._compose(
            "RHYTHM_ROOT: /app/rhythm\n"
            "services:\n"
            "  private-core:\n"
            "    environment: []\n"
        )
        violations = self.guard.compose_violations(compose)
        self.assertEqual(len(violations), 1)
        self.assertIn("outside any service block", violations[0])

    def test_rhythm_root_outside_a_service_is_rejected(self) -> None:
        # An anchor can be merged into any service, so placement outside a
        # service block cannot be attributed to private-core.
        compose = self._compose(
            "x-rhythm: &rhythm\n"
            "  environment:\n"
            "    - RHYTHM_ROOT=/app/rhythm\n"
            "services:\n"
            "  private-core:\n"
            "    <<: *rhythm\n"
        )
        violations = self.guard.compose_violations(compose)
        self.assertEqual(len(violations), 1)
        self.assertIn("outside any service block", violations[0])

    def test_missing_compose_file_fails_closed(self) -> None:
        violations = self.guard.compose_violations(self.base / "absent.yml")
        self.assertEqual(len(violations), 1)
        self.assertIn("not found", violations[0])


class RhythmRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = load_guard()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.src = self.root / "unchained"
        self.src.mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_public_module_referencing_the_runtime_is_rejected(self) -> None:
        (self.src / "web.py").write_text(
            'ROOT = "/app/rhythm"  # RHYTHM_ROOT\n', encoding="utf-8"
        )
        violations = self.guard.rhythm_runtime_violations(self.src, self.root)
        self.assertEqual(len(violations), 1)
        self.assertIn("web.py", violations[0])

    def test_private_core_engine_may_resolve_the_runtime(self) -> None:
        (self.src / "private_core_engine.py").write_text(
            'ROOT = "/app/rhythm"  # RHYTHM_ROOT\n', encoding="utf-8"
        )
        self.assertEqual(
            self.guard.rhythm_runtime_violations(self.src, self.root), []
        )

    def test_tests_and_caches_are_ignored(self) -> None:
        (self.src / "test_rhythm.py").write_text(
            'ROOT = "/app/rhythm"  # RHYTHM_ROOT\n', encoding="utf-8"
        )
        cache = self.src / "__pycache__"
        cache.mkdir()
        (cache / "web.py").write_text("RHYTHM_ROOT = 1\n", encoding="utf-8")
        self.assertEqual(
            self.guard.rhythm_runtime_violations(self.src, self.root), []
        )


class ImportRuleTests(unittest.TestCase):
    def test_rhythm_is_forbidden_for_public_boundary_modules(self) -> None:
        self.assertIn("rhythm", load_guard().FORBIDDEN)
        self.assertIn("jev_client", load_guard().FORBIDDEN)

    def test_imports_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "module.py"
            path.write_text("import rhythm\nfrom cdp import CDP\n", encoding="utf-8")
            imported = load_guard().imports_for(path)
        self.assertTrue({"rhythm", "cdp"} <= imported)


if __name__ == "__main__":
    unittest.main()
