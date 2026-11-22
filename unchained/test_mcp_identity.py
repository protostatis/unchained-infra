"""Tests for credential -> principal -> target-agent resolution."""

import os
import tempfile
import unittest

from auth import Auth
from mcp_identity import (
    IdentityError,
    Principal,
    KIND_API_KEY,
    KIND_OAUTH,
    oauth_tool_target_error,
    resolve_principal,
    target_agent,
)
from mcp_oauth_store import OAuthStore, agent_id_for_key, fingerprint_key


class _FakeAuth:
    def __init__(self, keys_by_user, status="approved"):
        self._keys = keys_by_user
        self.status = status
        self.missing_users = set()

    def validate_key(self, key):
        for user_id, keys in self._keys.items():
            if key in keys:
                return {"user_id": user_id, "key": key}
        return None

    def get_keys_for_user(self, user_id):
        return list(self._keys.get(user_id, []))

    def find_user_by_id(self, user_id):
        if user_id in self.missing_users or self.status is None:
            return None
        return {"user_id": user_id, "status": self.status}


class ResolvePrincipalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self.store = OAuthStore(self.db_path)
        self.auth = _FakeAuth({"user-1": ["uc_live_a"]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_header(self):
        self.assertIsNone(resolve_principal("", auth=self.auth, store=self.store))

    def test_non_bearer_header(self):
        self.assertIsNone(
            resolve_principal("Basic abc", auth=self.auth, store=self.store)
        )

    def test_api_key_principal(self):
        principal = resolve_principal(
            "Bearer uc_live_a", auth=self.auth, store=self.store
        )
        self.assertEqual(principal.kind, KIND_API_KEY)
        self.assertEqual(principal.user_id, "user-1")
        self.assertEqual(principal.api_key, "uc_live_a")

    def test_unknown_key(self):
        self.assertIsNone(
            resolve_principal("Bearer uc_nope", auth=self.auth, store=self.store)
        )

    def test_oauth_token_principal_is_pinned(self):
        self.store.save_client(
            client_id="c1",
            client_name="C",
            redirect_uris=["https://c.test/cb"],
            source="manual",
        )
        request_id = self.store.create_request(
            client_id="c1",
            redirect_uri="https://c.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        grant_id, _ = self.store.approve_request(
            request_id=request_id,
            user_id="user-1",
            agent_id="claude-deadbeef-facebook",
            key_hash="deadbeef",
            key_fingerprint=fingerprint_key("uc_live_a"),
            scopes=["mcp:browser"],
            resource=None,
        )
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="c1", scopes=["mcp:browser"], resource=None
        )
        principal = resolve_principal(
            f"Bearer {issued['access_token']}", auth=self.auth, store=self.store
        )
        self.assertEqual(principal.kind, KIND_OAUTH)
        self.assertEqual(principal.agent_id, "claude-deadbeef-facebook")
        self.assertEqual(principal.grant_id, grant_id)

    def _issue_token(self, key_fingerprint, key_hash="deadbeef"):
        self.store.save_client(
            client_id="c1",
            client_name="C",
            redirect_uris=["https://c.test/cb"],
            source="manual",
        )
        request_id = self.store.create_request(
            client_id="c1",
            redirect_uri="https://c.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        grant_id, _ = self.store.approve_request(
            request_id=request_id,
            user_id="user-1",
            agent_id="claude-deadbeef",
            key_hash=key_hash,
            key_fingerprint=key_fingerprint,
            scopes=["mcp:browser"],
            resource=None,
        )
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="c1", scopes=["mcp:browser"], resource=None
        )
        return grant_id, issued["access_token"]

    def test_revoked_grant_resolves_to_nothing(self):
        grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        self.store.revoke_grant(grant_id)
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )

    def test_revoked_api_key_kills_the_connector_token(self):
        """A live access token must die with the key the grant was pinned to."""
        _grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        self.assertIsNotNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )
        self.auth._keys["user-1"] = []  # key revoked
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )

    def test_rejected_account_token_resolves_to_nothing(self):
        _grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        self.auth.status = "rejected"
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )

    def test_pending_account_token_resolves_to_nothing(self):
        _grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        self.auth.status = "pending"
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )

    def test_legacy_key_without_user_row_still_resolves(self):
        _grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        # The key still validates, but the account row is genuinely absent.
        self.auth.missing_users.add("user-1")
        principal = resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        self.assertIsNotNone(principal)

    def test_ineligible_request_revokes_the_grant(self):
        grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        self.auth.status = "rejected"
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )
        self.assertIsNone(self.store.get_grant(grant_id))

    def test_revoked_key_request_revokes_the_grant(self):
        grant_id, token = self._issue_token(fingerprint_key("uc_live_a"))
        self.auth._keys["user-1"] = []
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )
        self.assertIsNone(self.store.get_grant(grant_id))

    def test_grant_for_unknown_key_resolves_to_nothing(self):
        _grant_id, token = self._issue_token("f" * 64)
        self.assertIsNone(
            resolve_principal(f"Bearer {token}", auth=self.auth, store=self.store)
        )


class GrantStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OAuthStore(os.path.join(self.tmp.name, "auth.db"))
        self.auth = _FakeAuth({"user-1": ["uc_live_a"]})
        self.store.save_client(
            client_id="c1",
            client_name="C",
            redirect_uris=["https://c.test/cb"],
            source="manual",
        )
        request_id = self.store.create_request(
            client_id="c1",
            redirect_uri="https://c.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        self.grant_id, _ = self.store.approve_request(
            request_id=request_id,
            user_id="user-1",
            agent_id="claude-deadbeef",
            key_hash="deadbeef",
            key_fingerprint=fingerprint_key("uc_live_a"),
            scopes=["mcp:browser"],
            resource=None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid(self):
        from mcp_identity import GRANT_VALID, grant_state

        self.assertEqual(grant_state(self.auth, self.store, self.grant_id), GRANT_VALID)

    def test_account_rejected(self):
        from mcp_identity import GRANT_ACCOUNT, grant_state

        self.auth.status = "rejected"
        self.assertEqual(
            grant_state(self.auth, self.store, self.grant_id), GRANT_ACCOUNT
        )

    def test_key_revoked(self):
        from mcp_identity import GRANT_KEY, grant_state

        self.auth._keys["user-1"] = []
        self.assertEqual(grant_state(self.auth, self.store, self.grant_id), GRANT_KEY)

    def test_unknown_grant(self):
        from mcp_identity import GRANT_GONE, grant_state

        self.assertEqual(grant_state(self.auth, self.store, "nope"), GRANT_GONE)

    def test_store_failure_is_unknown_not_ineligible(self):
        from mcp_identity import GRANT_UNKNOWN, grant_state

        class Broken:
            def get_grant(self, grant_id):
                raise RuntimeError("db down")

        self.assertEqual(grant_state(self.auth, Broken(), self.grant_id), GRANT_UNKNOWN)


class TargetAgentTests(unittest.TestCase):
    def setUp(self):
        self.key = "uc_live_a"
        self.user_id = "user-1"
        self.auth = _FakeAuth({self.user_id: [self.key]})
        self.principal = Principal(
            kind=KIND_API_KEY,
            user_id=self.user_id,
            caller_tag="tag",
            api_key=self.key,
        )

    def test_api_key_default_agent(self):
        self.assertEqual(
            target_agent(self.principal, "", auth=self.auth), agent_id_for_key(self.key)
        )

    def test_api_key_profile_name(self):
        self.assertEqual(
            target_agent(self.principal, "facebook", auth=self.auth),
            agent_id_for_key(self.key, "facebook"),
        )

    def test_api_key_owned_full_id(self):
        agent_id = agent_id_for_key(self.key, "facebook")
        self.assertEqual(
            target_agent(self.principal, agent_id, auth=self.auth), agent_id
        )

    def test_api_key_foreign_full_id_rejected(self):
        other = agent_id_for_key("uc_live_other")
        with self.assertRaises(IdentityError):
            target_agent(self.principal, other, auth=self.auth)

    def test_oauth_default_uses_pinned_agent(self):
        oauth = Principal(
            kind=KIND_OAUTH,
            user_id=self.user_id,
            caller_tag="tag",
            agent_id="claude-deadbeef-facebook",
            grant_id="g1",
            scopes=("mcp:browser",),
        )
        self.assertEqual(target_agent(oauth, ""), "claude-deadbeef-facebook")
        self.assertEqual(target_agent(oauth, "auto"), "claude-deadbeef-facebook")
        self.assertEqual(
            target_agent(oauth, "claude-deadbeef-facebook"), "claude-deadbeef-facebook"
        )

    def test_oauth_profile_name_matches_pinned(self):
        oauth = Principal(
            kind=KIND_OAUTH,
            user_id=self.user_id,
            caller_tag="tag",
            agent_id="claude-deadbeef-facebook",
            grant_id="g1",
            scopes=("mcp:browser",),
        )
        self.assertEqual(
            target_agent(oauth, "facebook"), "claude-deadbeef-facebook"
        )

    def test_oauth_other_agent_rejected(self):
        oauth = Principal(
            kind=KIND_OAUTH,
            user_id=self.user_id,
            caller_tag="tag",
            agent_id="claude-deadbeef-facebook",
            grant_id="g1",
            scopes=("mcp:browser",),
        )
        with self.assertRaises(IdentityError):
            target_agent(oauth, "claude-deadbeef-work")
        with self.assertRaises(IdentityError):
            target_agent(oauth, agent_id_for_key(self.key, "work"))

    def test_oauth_error_names_the_requested_profile(self):
        oauth = Principal(
            kind=KIND_OAUTH,
            user_id=self.user_id,
            caller_tag="tag",
            agent_id="claude-deadbeef-facebook",
            grant_id="g1",
            scopes=("mcp:browser",),
        )
        with self.assertRaises(IdentityError) as ctx:
            target_agent(oauth, "work")
        message = str(ctx.exception)
        self.assertIn("facebook", message)
        self.assertIn("work", message)
        self.assertNotIn("Default browser", message)

    def test_oauth_without_pinned_agent_rejected(self):
        oauth = Principal(
            kind=KIND_OAUTH,
            user_id=self.user_id,
            caller_tag="tag",
            agent_id=None,
            grant_id="g1",
        )
        with self.assertRaises(IdentityError):
            target_agent(oauth, "")


class ToolScopeTests(unittest.TestCase):
    """An OAuth grant covers one browser, never a provisioned Chrome."""

    def setUp(self):
        self.oauth = Principal(
            kind=KIND_OAUTH,
            user_id="user-1",
            caller_tag="tag",
            agent_id="claude-deadbeef",
            grant_id="g1",
            scopes=("mcp:browser",),
        )
        self.api_key = Principal(
            kind=KIND_API_KEY,
            user_id="user-1",
            caller_tag="tag",
            api_key="uc_live_a",
        )

    def test_oauth_cannot_list_provisioned_tabs(self):
        self.assertIsNotNone(
            oauth_tool_target_error(self.oauth, "list_provisioned_tabs", {})
        )

    def test_oauth_cannot_list_chrome_profiles(self):
        self.assertIsNotNone(
            oauth_tool_target_error(self.oauth, "list_chrome_profiles", {})
        )

    def test_oauth_cannot_launch_provisioned_chrome(self):
        self.assertIsNotNone(
            oauth_tool_target_error(
                self.oauth, "cdp_provision_launch", {"profile_path": "/x"}
            )
        )

    def test_oauth_cannot_target_prov_tab(self):
        self.assertIsNotNone(
            oauth_tool_target_error(
                self.oauth, "js_eval", {"tab_id": "prov-dc31-ABC", "expr": "1"}
            )
        )

    def test_oauth_can_target_normal_tab(self):
        self.assertIsNone(
            oauth_tool_target_error(self.oauth, "js_eval", {"tab_id": "ABC123"})
        )
        self.assertIsNone(oauth_tool_target_error(self.oauth, "js_eval", {}))

    def test_api_key_keeps_provisioning_access(self):
        for name in ("list_provisioned_tabs", "cdp_provision_launch",
                     "list_chrome_profiles"):
            self.assertIsNone(oauth_tool_target_error(self.api_key, name, {}))
        self.assertIsNone(
            oauth_tool_target_error(self.api_key, "js_eval", {"tab_id": "prov-x-1"})
        )


class RealAuthIntegrationTests(unittest.TestCase):
    """The ownership rule must match the relay's, using the real Auth store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self.auth = Auth(self.db_path)
        user = self.auth.get_or_create_user("owner@example.test")
        self.user_id = user["user_id"]
        self.key = self.auth.create_key(self.user_id)
        self.principal = Principal(
            kind=KIND_API_KEY,
            user_id=self.user_id,
            caller_tag="tag",
            api_key=self.key,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_owned_agent_from_secondary_key_is_allowed(self):
        # Historical behaviour: any active key of the user may address an
        # agent connected with a different one of their keys.
        other_key = self.auth.create_key(self.user_id)
        other_agent = agent_id_for_key(other_key, "work")
        self.assertEqual(
            target_agent(self.principal, other_agent, auth=self.auth), other_agent
        )

    def test_revoked_key_agent_is_rejected(self):
        other_key = self.auth.create_key(self.user_id)
        other_agent = agent_id_for_key(other_key, "work")
        self.auth.revoke_key(other_key)
        with self.assertRaises(IdentityError):
            target_agent(self.principal, other_agent, auth=self.auth)


if __name__ == "__main__":
    unittest.main(verbosity=2)
