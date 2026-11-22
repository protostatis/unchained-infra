"""The OAuth master switch must gate credentials, not just endpoints."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class OAuthFlagGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        os.environ["UNCHAINED_DB_PATH"] = self.db_path
        self._prev_enabled = os.environ.get("MCP_OAUTH_ENABLED")

        import mcp_server

        self.mod = mcp_server
        mcp_server._OAUTH_STORE.clear()

    def tearDown(self):
        if self._prev_enabled is None:
            os.environ.pop("MCP_OAUTH_ENABLED", None)
        else:
            os.environ["MCP_OAUTH_ENABLED"] = self._prev_enabled
        self.tmp.cleanup()

    def _request(self, authorization):
        return SimpleNamespace(headers={"authorization": authorization})

    def test_oauth_token_rejected_when_feature_is_off(self):
        os.environ["MCP_OAUTH_ENABLED"] = "0"
        with patch.object(
            self.mod, "get_http_request", return_value=self._request("Bearer uco_at_x")
        ):
            principal, error = self.mod._principal_or_error()
        self.assertIsNone(principal)
        self.assertIn("disabled", error)

    def test_oauth_token_attempted_when_feature_is_on(self):
        os.environ["MCP_OAUTH_ENABLED"] = "1"
        with patch.object(
            self.mod, "get_http_request", return_value=self._request("Bearer uco_at_x")
        ):
            principal, error = self.mod._principal_or_error()
        self.assertIsNone(principal)
        # Unknown token: it reached the OAuth path and failed there.
        self.assertIn("no longer valid", error)

    def test_missing_header_message_is_unchanged(self):
        os.environ["MCP_OAUTH_ENABLED"] = "1"
        with patch.object(self.mod, "get_http_request", return_value=self._request("")):
            principal, error = self.mod._principal_or_error()
        self.assertIsNone(principal)
        self.assertIn("Authorization: Bearer <api_key> header is required.", error)

    def test_bogus_api_key_message_is_unchanged(self):
        os.environ["MCP_OAUTH_ENABLED"] = "0"
        with patch.object(
            self.mod, "get_http_request", return_value=self._request("Bearer uc_nope")
        ):
            principal, error = self.mod._principal_or_error()
        self.assertIsNone(principal)
        self.assertEqual(error, "Invalid API key.")

    def test_no_request_context_is_reported_as_missing_header(self):
        with patch.object(self.mod, "get_http_request", side_effect=RuntimeError("no request")):
            principal, error = self.mod._principal_or_error()
        self.assertIsNone(principal)
        self.assertIn("required", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
