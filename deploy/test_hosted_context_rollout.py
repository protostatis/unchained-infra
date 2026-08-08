"""Tests for the production hosted-context rollout gate."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from validate_hosted_context_rollout import validate_hosted_context_rollout


class HostedContextRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, content: str) -> None:
        self.env_path.write_text(content, encoding="utf-8")

    def test_requires_explicit_canonical_context_budget(self):
        self._write("HOSTED_MAX_INPUT_CHARS=200000\n")

        with self.assertRaisesRegex(
            ValueError,
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS",
        ):
            validate_hosted_context_rollout(self.env_path)

    def test_rejects_ambiguous_or_unsafe_context_budgets(self):
        for value, message in (
            ("", "non-empty"),
            ("abc", "must be an integer"),
            ("5000", "between 10000 and 400000"),
            ("400001", "between 10000 and 400000"),
        ):
            with self.subTest(value=value):
                self._write(f"HOSTED_MAX_INTERNAL_CONTEXT_CHARS={value}\n")
                with self.assertRaisesRegex(ValueError, message):
                    validate_hosted_context_rollout(self.env_path)

        self._write(
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS=200000\n"
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS=400000\n"
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate_hosted_context_rollout(self.env_path)

    def test_accepts_an_explicit_reviewed_context_budget(self):
        self._write("HOSTED_MAX_INTERNAL_CONTEXT_CHARS=400000\n")

        self.assertEqual(validate_hosted_context_rollout(self.env_path), 400_000)

    def test_deploy_invokes_gate_before_config_promotion(self):
        deploy_script = Path(__file__).resolve().parent.parent / "deploy.sh"
        source = deploy_script.read_text(encoding="utf-8")

        self.assertIn("HOSTED_CONTEXT_ROLLOUT_TOOL", source)
        self.assertIn("validate_staged_hosted_context_rollout", source)
        self.assertLess(
            source.rindex("validate_staged_hosted_context_rollout"),
            source.rindex("promote_staged_config"),
        )


if __name__ == "__main__":
    unittest.main()
