"""Callback validation and admission-policy tests."""

import unittest

from mcp_oauth_policy import (
    DEFAULT_POLICIES,
    RedirectURIError,
    client_callback_status,
    match_policy,
    validate_redirect_uri,
)

ISSUER = "https://unchainedsky.com"

CLAUDE = {
    "scheme": "https",
    "host": "claude.ai",
    "port": 443,
    "path": "/api/mcp/auth_callback",
    "match": "exact",
    "allow_query": 0,
}


class ValidateRedirectURITests(unittest.TestCase):
    def check_ok(self, uri, **kwargs):
        self.assertEqual(
            validate_redirect_uri(uri, issuer=ISSUER, **kwargs), uri
        )

    def check_bad(self, uri, needle="", **kwargs):
        with self.assertRaises(RedirectURIError) as ctx:
            validate_redirect_uri(uri, issuer=ISSUER, **kwargs)
        if needle:
            self.assertIn(needle, str(ctx.exception))

    def test_accepts_https(self):
        self.check_ok("https://claude.ai/api/mcp/auth_callback")

    def test_accepts_https_with_port(self):
        self.check_ok("https://example.test:8443/cb")

    def test_rejects_plain_http(self):
        self.check_bad("http://example.test/cb", "https")

    def test_rejects_http_when_loopback_not_allowed(self):
        self.check_bad("http://127.0.0.1:33418/cb", "https")

    def test_allows_loopback_when_operator_registers(self):
        self.check_ok("http://127.0.0.1:33418/cb", allow_loopback=True)
        self.check_ok("http://localhost:8080/cb", allow_loopback=True)

    def test_rejects_non_loopback_http_even_when_allowed(self):
        self.check_bad("http://example.test/cb", "https", allow_loopback=True)

    def test_rejects_userinfo(self):
        self.check_bad("https://claude.ai@evil.test/cb", "userinfo")

    def test_rejects_fragment(self):
        self.check_bad("https://example.test/cb#frag", "fragment")

    def test_rejects_empty_fragment(self):
        self.check_bad("https://example.test/cb#", "fragment")

    def test_rejects_backslash(self):
        self.check_bad("https://example.test\\@evil.test/cb", "backslash")

    def test_rejects_control_characters(self):
        self.check_bad("https://example.test/cb\x02", "control")

    def test_rejects_non_ascii_host(self):
        self.check_bad("https://exämple.test/cb", "ASCII")

    def test_rejects_punycode(self):
        self.check_bad("https://xn--exmple-cua.test/cb", "punycode")

    def test_rejects_trailing_dot_host(self):
        self.check_bad("https://example.test./cb", "dot")

    def test_rejects_issuer_host(self):
        self.check_bad("https://unchainedsky.com/cb", "Unchained")

    def test_rejects_issuer_subdomain(self):
        self.check_bad("https://api.unchainedsky.com/cb", "Unchained")

    def test_rejects_bad_port(self):
        self.check_bad("https://example.test:99999/cb", "port")

    def test_rejects_encoded_separator(self):
        self.check_bad("https://example.test/cb%2fnext", "encoded")

    def test_rejects_dot_segments(self):
        self.check_bad("https://example.test/a/../cb", "dot segments")

    def test_rejects_query_when_disallowed(self):
        self.check_bad(
            "https://example.test/cb?next=/x", "query", allow_query=False
        )

    def test_allows_query_when_permitted(self):
        self.check_ok("https://example.test/cb?next=/x", allow_query=True)

    def test_rejects_percent_encoded_host(self):
        self.check_bad("https://%75nchainedsky.com/cb", "percent")

    def test_rejects_encoded_dot_segments(self):
        self.check_bad("https://example.test/allowed/%2e%2e/cb", "encoded")

    def test_rejects_double_encoded_values(self):
        self.check_bad("https://example.test/cb%252fnext", "encoded")

    def test_rejects_single_dot_segment(self):
        self.check_bad("https://example.test/./cb", "dot segments")

    def test_rejects_empty(self):
        self.check_bad("", "empty")

    def test_rejects_too_long(self):
        self.check_bad("https://example.test/" + "a" * 2100, "too long")

    def test_rejects_relative(self):
        self.check_bad("//example.test/cb", "https")


class MatchPolicyTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(match_policy("https://claude.ai/api/mcp/auth_callback", CLAUDE))

    def test_host_is_case_insensitive(self):
        self.assertTrue(match_policy("https://CLAUDE.AI/api/mcp/auth_callback", CLAUDE))

    def test_default_port_matches_explicit_443(self):
        self.assertTrue(match_policy("https://claude.ai:443/api/mcp/auth_callback", CLAUDE))

    def test_wrong_path(self):
        self.assertFalse(match_policy("https://claude.ai/api/mcp/other", CLAUDE))

    def test_wrong_host(self):
        self.assertFalse(match_policy("https://claude.ai.evil.test/api/mcp/auth_callback", CLAUDE))

    def test_wrong_scheme(self):
        self.assertFalse(match_policy("http://claude.ai/api/mcp/auth_callback", CLAUDE))

    def test_wrong_port(self):
        self.assertFalse(match_policy("https://claude.ai:8443/api/mcp/auth_callback", CLAUDE))

    def test_query_not_allowed(self):
        self.assertFalse(
            match_policy("https://claude.ai/api/mcp/auth_callback?x=1", CLAUDE)
        )

    def test_query_allowed_when_policy_says_so(self):
        policy = dict(CLAUDE, allow_query=1)
        self.assertTrue(
            match_policy("https://claude.ai/api/mcp/auth_callback?x=1", policy)
        )

    def test_prefix_match(self):
        policy = dict(CLAUDE, path="/api/", match="prefix")
        self.assertTrue(match_policy("https://claude.ai/api/mcp/auth_callback", policy))
        self.assertTrue(match_policy("https://claude.ai/api/", policy))

    def test_prefix_respects_segment_boundary(self):
        policy = dict(CLAUDE, path="/oauth", match="prefix")
        self.assertTrue(match_policy("https://claude.ai/oauth/cb", policy))
        self.assertFalse(match_policy("https://claude.ai/oauthx/cb", policy))

    def test_malformed_uri_does_not_raise(self):
        self.assertFalse(match_policy("not a url", CLAUDE))


class ClientCallbackStatusTests(unittest.TestCase):
    def test_manual_is_approved_by_operator(self):
        client = {"source": "manual", "redirect_uris": ["https://anywhere.test/cb"]}
        self.assertEqual(client_callback_status(client, DEFAULT_POLICIES), "manual")

    def test_dcr_matching_policy(self):
        client = {
            "source": "dcr",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        }
        self.assertEqual(client_callback_status(client, DEFAULT_POLICIES), "policy")

    def test_dcr_with_one_unapproved_uri_is_blocked(self):
        client = {
            "source": "dcr",
            "redirect_uris": [
                "https://claude.ai/api/mcp/auth_callback",
                "https://evil.test/cb",
            ],
        }
        self.assertEqual(client_callback_status(client, DEFAULT_POLICIES), "blocked")

    def test_dcr_with_no_uris_is_blocked(self):
        self.assertEqual(
            client_callback_status({"source": "dcr", "redirect_uris": []}, DEFAULT_POLICIES),
            "blocked",
        )

    def test_withdrawing_the_policy_blocks_the_client(self):
        client = {
            "source": "dcr",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        }
        self.assertEqual(client_callback_status(client, []), "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
