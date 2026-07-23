"""Public contracts for Google Search Console HTML-file verification."""

from __future__ import annotations

import asyncio
import os
import unittest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web
from web_routes import ROUTE_SPECS


_VERIFICATION_PATH = "/google333e7a6c98af8946.html"
_VERIFICATION_BODY = "google-site-verification: google333e7a6c98af8946.html"
_REQUESTED_VERIFICATION_PATH = "/google83c650022d8db556.html"
_REQUESTED_VERIFICATION_BODY = "google-site-verification: google83c650022d8db556.html"


class TestSearchConsoleVerification(unittest.TestCase):
    def test_requested_verification_route_is_registered(self):
        self.assertIn(
            ("GET", _REQUESTED_VERIFICATION_PATH, "handle_google_verification"),
            ROUTE_SPECS,
        )

    def test_requested_verification_file_has_exact_public_contract(self):
        response = asyncio.run(web.handle_google_verification(None))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, _REQUESTED_VERIFICATION_BODY)
        self.assertEqual(response.content_type, "text/plain")
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")

    def test_current_verification_route_is_registered(self):
        self.assertIn(
            ("GET", _VERIFICATION_PATH, "handle_google_verification_current"),
            ROUTE_SPECS,
        )

    def test_current_verification_file_has_exact_public_contract(self):
        response = asyncio.run(web.handle_google_verification_current(None))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, _VERIFICATION_BODY)
        self.assertEqual(response.content_type, "text/plain")
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")


if __name__ == "__main__":
    unittest.main()
