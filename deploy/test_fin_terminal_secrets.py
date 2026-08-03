"""Tests for deployment-host fin-terminal credential preparation."""

from __future__ import annotations

from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from ensure_fin_terminal_secrets import (
    PUBLIC_EXTERNAL_NAMES,
    PUBLIC_TOKEN_NAMES,
    RETIRED_TOKEN_NAMES,
    TOKEN_NAMES,
    _env_value,
    ensure_fin_terminal_secrets,
    install_public_turnstile_values,
)


class FinTerminalSecretsTests(unittest.TestCase):
    site_key = "0x4AAAAAAAAAAAAAAAAAAAAAAA"
    turnstile_secret = "0x4BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, content: str) -> None:
        self.env_path.write_text(content, encoding="utf-8")

    def _values(self) -> dict[str, str]:
        lines = self.env_path.read_text(encoding="utf-8").splitlines()
        return {
            "OPENROUTER_API_KEY": _env_value(lines, "OPENROUTER_API_KEY"),
            **{name: _env_value(lines, name) for name in TOKEN_NAMES},
            **{name: _env_value(lines, name) for name in PUBLIC_EXTERNAL_NAMES},
        }

    @staticmethod
    def _valid_tokens() -> dict[str, str]:
        return {
            name: chr(ord("a") + index) * 64
            for index, name in enumerate(TOKEN_NAMES)
        }

    @staticmethod
    def _retired_token_value() -> str:
        return "z" * 64

    def _write_valid(self, *, extra: str = "") -> dict[str, str]:
        tokens = self._valid_tokens()
        content = ["OPENROUTER_API_KEY=provider-secret", extra]
        content.extend(f"{name}={value}" for name, value in tokens.items())
        self._write("\n".join(part for part in content if part) + "\n")
        return tokens

    def test_requires_existing_openrouter_key(self):
        self._write("ADMIN_EMAILS=admin@example.com\n")

        with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
            ensure_fin_terminal_secrets(self.env_path)

    def test_generates_independent_256_bit_credentials(self):
        self._write("OPENROUTER_API_KEY=provider-secret\nADMIN_EMAILS=admin@example.com\n")

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        values = self._values()

        self.assertEqual(values["OPENROUTER_API_KEY"], "provider-secret")
        generated = [values[name] for name in TOKEN_NAMES]
        self.assertEqual(len(generated), len(TOKEN_NAMES))
        for token in generated:
            self.assertRegex(token, r"^[0-9a-f]{64}$")
            self.assertNotEqual(token, values["OPENROUTER_API_KEY"])
        self.assertEqual(len(set(generated)), len(TOKEN_NAMES))
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        # Retired token must not appear in output
        for retired_name in RETIRED_TOKEN_NAMES:
            self.assertNotIn(retired_name, values)

    def test_scrub_retired_token_does_not_trigger_rotation_and_preserves_rollback_semantics(self):
        """Scrubbing retired token must be idempotent and not signal rotation."""
        retired_value = self._retired_token_value()
        tokens = self._write_valid(
            extra=f"FIN_TERMINAL_DEMO_PROXY_TOKEN={retired_value}\n"
        )

        # First run: scrub the retired token, retain active tokens unchanged.
        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))
        content = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", content)

        # Second run: must remain idempotent (already scrubbed).
        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))
        content2 = self.env_path.read_text(encoding="utf-8")
        self.assertEqual(content, content2)

        # Re-add the retired token (simulating rollback restoring old .env)
        content_with_retired = content.rstrip("\n") + "\n"
        content_with_retired += f"FIN_TERMINAL_DEMO_PROXY_TOKEN={retired_value}\n"
        self._write(content_with_retired)

        # Third run: scrub again, must still not signal rotation.
        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))
        content3 = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", content3)
        # Active tokens must be preserved through the scrub cycle
        for name in TOKEN_NAMES:
            self.assertIn(f"{name}={tokens[name]}", content3)

    def test_replaces_provider_key_reused_as_proxy_token(self):
        self._write(
            "OPENROUTER_API_KEY='shared-secret-value-that-is-long-enough'\n"
            "FIN_TERMINAL_PROXY_TOKEN='shared-secret-value-that-is-long-enough'\n"
        )

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        values = self._values()

        self.assertEqual(
            values["OPENROUTER_API_KEY"],
            "shared-secret-value-that-is-long-enough",
        )
        self.assertNotEqual(
            values["FIN_TERMINAL_PROXY_TOKEN"],
            values["OPENROUTER_API_KEY"],
        )
        self.assertEqual(len({values[name] for name in TOKEN_NAMES}), len(TOKEN_NAMES))

    def test_replaces_short_proxy_token(self):
        self._write(
            "OPENROUTER_API_KEY=provider-secret\n"
            f"FIN_TERMINAL_PROXY_TOKEN={'a' * 63}\n"
        )

        self.assertTrue(ensure_fin_terminal_secrets(self.env_path))
        self.assertRegex(
            self._values()["FIN_TERMINAL_PROXY_TOKEN"],
            r"^[0-9a-f]{64}$",
        )

    def test_scrubs_retired_demo_token_silently(self):
        retired_value = self._retired_token_value()
        tokens = self._write_valid(
            extra=f"FIN_TERMINAL_DEMO_PROXY_TOKEN={retired_value}\n"
        )

        # Scrubbing the retired token must not be reported as a credential
        # rotation when it is the only change. All active tokens are already
        # valid so no generation occurs.
        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))

        content = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("FIN_TERMINAL_DEMO_PROXY_TOKEN", content)
        self.assertNotIn(retired_value, content)
        for name, value in tokens.items():
            self.assertIn(f"{name}={value}", content)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_retains_existing_independent_credentials(self):
        tokens = self._write_valid()

        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))
        values = self._values()
        self.assertEqual({name: values[name] for name in TOKEN_NAMES}, tokens)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_rejects_invalid_public_enabled_value(self):
        self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=1")

        with self.assertRaisesRegex(ValueError, "must be true or false"):
            ensure_fin_terminal_secrets(self.env_path)

    def test_first_deployment_materializes_disabled_public_flag(self):
        self._write_valid()

        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))

        content = self.env_path.read_text(encoding="utf-8")
        self.assertEqual(content.count("FIN_TERMINAL_PUBLIC_ENABLED="), 1)
        self.assertIn("FIN_TERMINAL_PUBLIC_ENABLED=false\n", content)

    def test_rejects_duplicate_public_enabled_definitions(self):
        self._write_valid(
            extra=(
                "FIN_TERMINAL_PUBLIC_ENABLED=false\n"
                "FIN_TERMINAL_PUBLIC_ENABLED=true"
            )
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            ensure_fin_terminal_secrets(self.env_path)

        self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=TRUE")
        with self.assertRaisesRegex(ValueError, "must be true or false"):
            ensure_fin_terminal_secrets(self.env_path)

    def test_enabled_public_route_requires_external_values(self):
        self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=true")

        with self.assertRaisesRegex(ValueError, "external values"):
            ensure_fin_terminal_secrets(self.env_path)

    def test_enabled_public_route_rejects_malformed_external_values(self):
        self._write_valid(
            extra=(
                "FIN_TERMINAL_PUBLIC_ENABLED=true\n"
                "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY=site-key\n"
                "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET=turnstile-secret"
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid Turnstile site key"):
            ensure_fin_terminal_secrets(self.env_path)

    def test_does_not_rotate_public_credentials_while_route_is_enabled(self):
        self.assertEqual(
            PUBLIC_TOKEN_NAMES,
            {
                "FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY",
                "FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN",
                "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN",
            },
        )
        for invalid_name in PUBLIC_TOKEN_NAMES:
            with self.subTest(invalid_name=invalid_name):
                tokens = self._valid_tokens()
                tokens[invalid_name] = "short"
                lines = [
                    "OPENROUTER_API_KEY=provider-secret",
                    "FIN_TERMINAL_PUBLIC_ENABLED=true",
                    f"FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY={self.site_key}",
                    f"FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET={self.turnstile_secret}",
                ]
                lines.extend(f"{name}={value}" for name, value in tokens.items())
                self._write("\n".join(lines) + "\n")

                with self.assertRaisesRegex(ValueError, "disable the public route"):
                    ensure_fin_terminal_secrets(self.env_path)

    def test_enabled_public_route_accepts_trial_agent_provider_key(self):
        tokens = self._write_valid(
            extra=(
                "FIN_TERMINAL_PUBLIC_ENABLED=true\n"
                f"FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY={self.site_key}\n"
                f"FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET={self.turnstile_secret}"
            )
        )

        self.assertFalse(ensure_fin_terminal_secrets(self.env_path))
        values = self._values()
        self.assertEqual(values["OPENROUTER_API_KEY"], "provider-secret")
        self.assertEqual({name: values[name] for name in TOKEN_NAMES}, tokens)

    def test_installs_external_turnstile_values_while_disabled(self):
        tokens = self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=false")

        self.assertTrue(
            install_public_turnstile_values(
                self.env_path,
                self.site_key,
                self.turnstile_secret,
            )
        )

        values = self._values()
        self.assertEqual(
            values["FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY"],
            self.site_key,
        )
        self.assertEqual(
            values["FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET"],
            self.turnstile_secret,
        )
        self.assertEqual({name: values[name] for name in TOKEN_NAMES}, tokens)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_external_turnstile_install_is_idempotent_and_removes_duplicates(self):
        self._write_valid(
            extra=(
                "FIN_TERMINAL_PUBLIC_ENABLED=false\n"
                "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY=0x4CCCCCCCCCCCCCCCCCCCCCCCC\n"
                "FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET=0x4DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD\n"
                "FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY=0x4EEEEEEEEEEEEEEEEEEEEEEEE"
            )
        )

        self.assertTrue(
            install_public_turnstile_values(
                self.env_path,
                self.site_key,
                self.turnstile_secret,
            )
        )
        installed = self.env_path.read_text(encoding="utf-8")
        self.assertEqual(installed.count("FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY="), 1)
        self.assertEqual(installed.count("FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET="), 1)

        self.assertFalse(
            install_public_turnstile_values(
                self.env_path,
                self.site_key,
                self.turnstile_secret,
            )
        )
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), installed)

    def test_external_turnstile_install_rejects_bad_values_without_mutation(self):
        self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=false")
        original = self.env_path.read_bytes()

        for site_key, secret in (
            ("too-short", self.turnstile_secret),
            (self.site_key, "contains a space and is invalid"),
            (self.site_key + "\nINJECTED=value", self.turnstile_secret),
            ("x" * 257, self.turnstile_secret),
        ):
            with self.subTest(site_key=site_key[:20], secret=secret[:20]):
                with self.assertRaisesRegex(ValueError, "invalid Turnstile"):
                    install_public_turnstile_values(self.env_path, site_key, secret)
                self.assertEqual(self.env_path.read_bytes(), original)

    def test_external_turnstile_rotation_requires_disabled_route(self):
        self._write_valid(
            extra=(
                "FIN_TERMINAL_PUBLIC_ENABLED=true\n"
                f"FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY={self.site_key}\n"
                f"FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET={self.turnstile_secret}"
            )
        )
        original = self.env_path.read_bytes()

        self.assertFalse(
            install_public_turnstile_values(
                self.env_path,
                self.site_key,
                self.turnstile_secret,
            )
        )
        with self.assertRaisesRegex(ValueError, "disable the public route"):
            install_public_turnstile_values(
                self.env_path,
                "0x4CCCCCCCCCCCCCCCCCCCCCCCC",
                self.turnstile_secret,
            )
        self.assertEqual(self.env_path.read_bytes(), original)

    def test_external_turnstile_install_rejects_symlink(self):
        real_env = Path(self.temp_dir.name) / "real.env"
        real_env.write_text("FIN_TERMINAL_PUBLIC_ENABLED=false\n", encoding="utf-8")
        self.env_path.symlink_to(real_env)

        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            install_public_turnstile_values(
                self.env_path,
                self.site_key,
                self.turnstile_secret,
            )
        self.assertEqual(
            real_env.read_text(encoding="utf-8"),
            "FIN_TERMINAL_PUBLIC_ENABLED=false\n",
        )

    def test_cli_accepts_nul_framing_without_printing_values(self):
        self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=false")
        helper = Path(__file__).with_name("ensure_fin_terminal_secrets.py")
        payload = f"{self.site_key}\0{self.turnstile_secret}\0".encode("ascii")

        result = subprocess.run(
            [sys.executable, str(helper), "--install-public-turnstile", str(self.env_path)],
            input=payload,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode().strip(), "turnstile_changed=true")
        combined = result.stdout + result.stderr
        self.assertNotIn(self.site_key.encode(), combined)
        self.assertNotIn(self.turnstile_secret.encode(), combined)

    def test_cli_rejects_malformed_framing_without_mutation_or_echo(self):
        self._write_valid(extra="FIN_TERMINAL_PUBLIC_ENABLED=false")
        helper = Path(__file__).with_name("ensure_fin_terminal_secrets.py")
        original = self.env_path.read_bytes()
        malformed = f"{self.site_key}\0{self.turnstile_secret}".encode("ascii")

        result = subprocess.run(
            [sys.executable, str(helper), "--install-public-turnstile", str(self.env_path)],
            input=malformed,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.env_path.read_bytes(), original)
        combined = result.stdout + result.stderr
        self.assertNotIn(self.site_key.encode(), combined)
        self.assertNotIn(self.turnstile_secret.encode(), combined)

    def test_cli_reports_internal_credential_status(self):
        self._write_valid()
        helper = Path(__file__).with_name("ensure_fin_terminal_secrets.py")

        result = subprocess.run(
            [sys.executable, str(helper), "--ensure-status", str(self.env_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "fin_terminal_credentials_changed=false",
        )


if __name__ == "__main__":
    unittest.main()
