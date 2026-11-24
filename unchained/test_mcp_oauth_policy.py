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


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.uri = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-abc"
        self.policy = dict(
            CLAUDE,
            host="oauth-redirect.googleusercontent.com",
            path="/r/user_bound_custom-mcp-abc",
            binding_id="user_bound_custom-mcp-abc",
        )
        self.binding = {
            "binding_id": "user_bound_custom-mcp-abc",
            "owner_user_id": "u-1",
            "status": "active",
        }

    def check(self, user="u-1", bindings=None, policies=None):
        from mcp_oauth_policy import callback_owner_state

        return callback_owner_state(
            self.uri,
            policies if policies is not None else [self.policy],
            bindings if bindings is not None else {self.binding["binding_id"]: self.binding},
            user,
        )

    def test_owner_can_authorize(self):
        allowed, reason = self.check()
        self.assertTrue(allowed)
        self.assertEqual(reason, "bound")

    def test_other_account_cannot_authorize(self):
        self.assertEqual(self.check(user="u-2"), (False, "owner_mismatch"))

    def test_missing_binding_fails_closed(self):
        self.assertEqual(self.check(bindings={}), (False, "missing"))

    def test_inactive_binding_fails_closed(self):
        self.assertEqual(
            self.check(bindings={self.binding["binding_id"]: dict(self.binding, status="blocked")}),
            (False, "inactive"),
        )

    def test_personal_callback_without_binding_fails_closed(self):
        policy = dict(self.policy)
        policy.pop("binding_id")
        self.assertEqual(self.check(policies=[policy]), (False, "unbound_personal"))

    def test_overlapping_bound_and_unbound_policies_fail_closed(self):
        loose = dict(self.policy, binding_id=None)
        self.assertEqual(self.check(policies=[self.policy, loose]), (False, "conflict"))

    def test_static_callback_allows_any_account(self):
        from mcp_oauth_policy import callback_owner_state

        allowed, reason = callback_owner_state(
            "https://claude.ai/api/mcp/auth_callback",
            [dict(CLAUDE)],
            {},
            "u-9",
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "static")

    def test_inactive_binding_makes_the_client_blocked(self):
        from mcp_oauth_policy import client_callback_status

        client = {"source": "dcr", "redirect_uris": [self.uri]}
        bindings = {self.binding["binding_id"]: dict(self.binding, status="blocked")}
        self.assertEqual(client_callback_status(client, [self.policy], bindings), "blocked")


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
