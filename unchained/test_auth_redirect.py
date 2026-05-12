"""Tests for external auth redirect URI validation."""

from __future__ import annotations

import unittest

from web_app.handlers.auth_admin import _validate_redirect_uri


class TestAuthRedirectUri(unittest.TestCase):
    def test_production_redirect_allowlist_includes_external_apps(self):
        for uri in [
            "https://analytics.unchainedsky.com/auth/callback",
            "https://searchagentsky.com/auth/callback",
            "https://search.unchainedsky.com/auth/callback",
        ]:
            with self.subTest(uri=uri):
                self.assertTrue(_validate_redirect_uri(uri))

    def test_redirect_allowlist_rejects_wrong_origin_or_path(self):
        for uri in [
            "https://analytics.unchainedsky.com/auth/other",
            "https://analytics.evil.example/auth/callback",
            "http://analytics.unchainedsky.com/auth/callback",
            "https://analytics.unchainedsky.com.evil.example/auth/callback",
        ]:
            with self.subTest(uri=uri):
                self.assertFalse(_validate_redirect_uri(uri))


if __name__ == "__main__":
    unittest.main()
