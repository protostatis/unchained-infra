"""Tests for provider-key encryption storage."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest


class TestProviderKeyCrypto(unittest.TestCase):
    def setUp(self):
        self._old_db = os.environ.get("UNCHAINED_DB_PATH")
        self._old_secret = os.environ.get("JWT_SECRET")
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["UNCHAINED_DB_PATH"] = self._tmp.name
        os.environ["JWT_SECRET"] = "test-jwt-secret"
        sys.modules.pop("signup_agent", None)
        self.signup_agent = importlib.import_module("signup_agent")

    def tearDown(self):
        sys.modules.pop("signup_agent", None)
        if self._old_db is None:
            os.environ.pop("UNCHAINED_DB_PATH", None)
        else:
            os.environ["UNCHAINED_DB_PATH"] = self._old_db
        if self._old_secret is None:
            os.environ.pop("JWT_SECRET", None)
        else:
            os.environ["JWT_SECRET"] = self._old_secret
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_store_provider_key_uses_random_row_salt(self):
        key = "AIzaSyTEST_PROVIDER_KEY_1234567890abc"
        self.signup_agent.store_provider_key("u-crypto", "gemini", key)

        with self.signup_agent._auth._conn() as conn:
            row = conn.execute(
                "SELECT encrypted_key, salt FROM provider_keys WHERE user_id = ? AND provider = ?",
                ("u-crypto", "gemini"),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], key)
        self.assertTrue(row[1], "salt should be stored per row")
        self.assertEqual(self.signup_agent.get_provider_key("u-crypto", "gemini"), key)

    def test_legacy_row_is_migrated_on_read(self):
        key = "AIzaSyLEGACY_PROVIDER_KEY_1234567890"
        encrypted = self.signup_agent._fallback_fernet.encrypt(key.encode()).decode()
        now = time.time()
        with self.signup_agent._auth._conn() as conn:
            conn.execute(
                """
                INSERT INTO provider_keys (user_id, provider, encrypted_key, salt, created_at, last_verified_at, active)
                VALUES (?, ?, ?, NULL, ?, ?, 1)
                """,
                ("u-legacy", "gemini", encrypted, now, now),
            )

        self.assertEqual(self.signup_agent.get_provider_key("u-legacy", "gemini"), key)

        with self.signup_agent._auth._conn() as conn:
            row = conn.execute(
                "SELECT salt FROM provider_keys WHERE user_id = ? AND provider = ?",
                ("u-legacy", "gemini"),
            ).fetchone()

        self.assertTrue(row[0], "legacy row should be rewritten with a random salt")


if __name__ == "__main__":
    unittest.main()
