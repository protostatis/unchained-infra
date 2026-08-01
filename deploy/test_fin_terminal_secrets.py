"""Tests for deployment-host fin-terminal credential preparation."""

from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest

from ensure_fin_terminal_secrets import ensure_fin_terminal_secrets, _env_value


class FinTerminalSecretsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, content: str) -> None:
        self.env_path.write_text(content, encoding="utf-8")

    def _values(self) -> tuple[str, str, str]:
        lines = self.env_path.read_text(encoding="utf-8").splitlines()
        return (
            _env_value(lines, "OPENROUTER_API_KEY"),
            _env_value(lines, "FIN_TERMINAL_PROXY_TOKEN"),
            _env_value(lines, "FIN_TERMINAL_DEMO_PROXY_TOKEN"),
        )

    def test_requires_existing_openrouter_key(self):
        self._write("ADMIN_EMAILS=admin@example.com\n")

        with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
            ensure_fin_terminal_secrets(self.env_path)

    def test_generates_independent_256_bit_proxy_tokens(self):
        self._write("OPENROUTER_API_KEY=provider-secret\nADMIN_EMAILS=admin@example.com\n")

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        openrouter_key, terminal_token, demo_token = self._values()

        self.assertEqual(openrouter_key, "provider-secret")
        self.assertRegex(terminal_token, r"^[0-9a-f]{64}$")
        self.assertRegex(demo_token, r"^[0-9a-f]{64}$")
        self.assertNotEqual(terminal_token, openrouter_key)
        self.assertNotEqual(demo_token, openrouter_key)
        self.assertNotEqual(terminal_token, demo_token)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_replaces_provider_key_reused_as_proxy_token(self):
        self._write(
            "OPENROUTER_API_KEY='shared-secret-value-that-is-long-enough'\n"
            "FIN_TERMINAL_PROXY_TOKEN='shared-secret-value-that-is-long-enough'\n"
        )

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        openrouter_key, terminal_token, demo_token = self._values()

        self.assertEqual(openrouter_key, "shared-secret-value-that-is-long-enough")
        self.assertNotEqual(terminal_token, openrouter_key)
        self.assertNotEqual(demo_token, openrouter_key)
        self.assertNotEqual(terminal_token, demo_token)

    def test_replaces_short_proxy_token(self):
        self._write(
            "OPENROUTER_API_KEY=provider-secret\n"
            f"FIN_TERMINAL_PROXY_TOKEN={'a' * 63}\n"
        )

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        self.assertRegex(self._values()[1], r"^[0-9a-f]{64}$")

    def test_replaces_demo_token_reused_from_persistent_terminal(self):
        existing = "a" * 64
        self._write(
            "OPENROUTER_API_KEY=provider-secret\n"
            f"FIN_TERMINAL_PROXY_TOKEN={existing}\n"
            f"FIN_TERMINAL_DEMO_PROXY_TOKEN={existing}\n"
        )

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        _, terminal_token, demo_token = self._values()

        self.assertEqual(terminal_token, existing)
        self.assertRegex(demo_token, r"^[0-9a-f]{64}$")
        self.assertNotEqual(demo_token, terminal_token)

    def test_retains_existing_independent_proxy_tokens(self):
        terminal_token = "a" * 64
        demo_token = "b" * 64
        self._write(
            "OPENROUTER_API_KEY=provider-secret\n"
            f"FIN_TERMINAL_PROXY_TOKEN={terminal_token}\n"
            f"FIN_TERMINAL_DEMO_PROXY_TOKEN={demo_token}\n"
        )

        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))
        self.assertEqual(self._values()[1:], (terminal_token, demo_token))
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
