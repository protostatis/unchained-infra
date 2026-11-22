"""Consent-handler tests: the step that actually grants access.

The protocol flow is covered in ``test_mcp_oauth_flow.py`` by approving through
the store.  This file drives the handler that a real browser hits, so a broken
session check, CSRF check, or agent check fails a test instead of silently
falling back to the store.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from unittest.mock import AsyncMock

from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

from mcp_oauth_store import fingerprint_key
from web_app.handlers import mcp_oauth

USER = {
    "user_id": "user-1",
    "email": "protostatis.dev@gmail.com",
    "auth_method": "session",
    "status": "approved",
}
AGENTS = [
    {
        "agent_id": "claude-deadbeef-facebook",
        "profile": "facebook",
        "key": "uc_live_a",
        "connected": True,
    }
]


def _post(fields, *, cookie="csrf-value", csrf_field="csrf-value"):
    """A POST request whose parsed form is the given fields."""
    headers = CIMultiDict({"Cookie": f"{mcp_oauth._CSRF_COOKIE}={cookie}"})
    request = make_mocked_request("POST", "/oauth/consent", headers=headers)
    request.post = AsyncMock(
        return_value={
            "consent_version": mcp_oauth.CONSENT_VERSION,
            **fields,
            "csrf": csrf_field,
        }
    )
    return request


class ConsentHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UNCHAINED_DB_PATH"] = os.path.join(self.tmp.name, "auth.db")
        os.environ["MCP_OAUTH_ENABLED"] = "1"
        os.environ["MCP_OAUTH_ISSUER"] = "https://unchainedsky.com"
        mcp_oauth._STORE.clear()
        self.store = mcp_oauth._store()
        self.store.save_client(
            client_id="client-1",
            client_name="Test Connector",
            redirect_uris=["https://connector.test/cb"],
            source="manual",
        )
        self.request_id = self.store.create_request(
            client_id="client-1",
            redirect_uri="https://connector.test/cb",
            state="state-1",
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource="https://unchainedsky.com/mcp",
        )

    def tearDown(self):
        os.environ.pop("MCP_OAUTH_ENABLED", None)
        self.tmp.cleanup()

    def _run(self, coro):
        return asyncio.run(coro)

    def _approve(self, request, *, user=USER, agents=AGENTS):
        with patch.object(mcp_oauth, "_signed_in", return_value=user), patch.object(
            mcp_oauth, "_agent_options", return_value=agents
        ):
            return self._run(mcp_oauth.handle_mcp_oauth_consent_post(request))

    def _get(self, request_id, *, user=USER, agents=AGENTS):
        request = make_mocked_request("GET", f"/oauth/consent?request={request_id}")
        with patch.object(mcp_oauth, "_signed_in", return_value=user), patch.object(
            mcp_oauth, "_agent_options", return_value=agents
        ):
            response = self._run(mcp_oauth.handle_mcp_oauth_consent(request))
        return response

    def _dcr_client(self, client_id="dcr-1", uris=("https://claude.ai/api/mcp/auth_callback",)):
        self.store.save_client(
            client_id=client_id,
            client_name="Claude",
            redirect_uris=list(uris),
            source="dcr",
        )
        return self.store.create_request(
            client_id=client_id,
            redirect_uri=uris[0],
            state="s",
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource="https://unchainedsky.com/mcp",
        )

    # -- consent presentation ---------------------------------------------

    def test_consent_shows_full_callback_and_host(self):
        response = self._get(self.request_id)
        self.assertEqual(response.status, 200)
        body = response.text
        self.assertIn("https://connector.test/cb", body)
        self.assertIn("<strong>connector.test</strong>", body)

    def test_manual_client_is_labelled_operator_registered(self):
        response = self._get(self.request_id)
        self.assertIn("registered by an Unchained administrator", response.text)

    def test_policy_client_is_labelled_by_destination(self):
        request_id = self._dcr_client()
        response = self._get(request_id)
        self.assertEqual(response.status, 200)
        self.assertIn("matches Unchained&#x27;s allowed destinations", response.text)

    def test_policy_client_shows_unverified_warning(self):
        request_id = self._dcr_client()
        response = self._get(request_id)
        self.assertIn("Unverified connector", response.text)

    def test_consent_shows_the_signed_in_account(self):
        response = self._get(self.request_id)
        self.assertIn("Signed in as", response.text)
        self.assertIn("protostatis.dev@gmail.com", response.text)
        self.assertIn("Switch account", response.text)

    def test_consent_discloses_profile_copying(self):
        response = self._get(self.request_id)
        self.assertIn("Google account email", response.text)
        self.assertIn("launch a copy", response.text)
        self.assertIn("original profile is not modified", response.text)
        self.assertIn("assistant provider", response.text)

    def test_consent_form_carries_the_current_consent_version(self):
        response = self._get(self.request_id)
        self.assertIn(
            f'name="consent_version" value="{mcp_oauth.CONSENT_VERSION}"',
            response.text,
        )

    def test_stale_consent_version_is_refused(self):
        request = _post(
            {
                "request": self.request_id,
                "decision": "approve",
                "agent_id": "claude-deadbeef-facebook",
                "consent_version": "1",
            }
        )
        response = self._approve(request)
        self.assertEqual(response.status, 400)
        self.assertIn("out of date", response.text)

    def test_switch_account_clears_the_session_and_returns_to_the_same_request(self):
        request = _post(
            {"request": self.request_id, "decision": "switch", "agent_id": ""}
        )

        class FakeCore:
            def _clear_session_cookie(self, response, request):
                response.del_cookie("uc_session", path="/")

        with patch.object(mcp_oauth, "_signed_in", return_value=USER), patch.object(
            mcp_oauth, "_core", return_value=FakeCore()
        ):
            response = self._run(mcp_oauth.handle_mcp_oauth_consent_post(request))

        self.assertEqual(response.status, 302)
        self.assertIn(f"/oauth/consent?request={self.request_id}", response.headers["Location"])
        cookie = response.cookies.get("uc_session")
        self.assertIsNotNone(cookie)
        self.assertIn(cookie["max-age"], ("0", 0))

    def test_manual_client_has_no_unverified_warning(self):
        response = self._get(self.request_id)
        self.assertNotIn("Unverified connector", response.text)

    def test_browser_requires_an_explicit_choice(self):
        agents = [
            {"agent_id": "claude-deadbeef-facebook", "profile": "facebook", "key": "uc_live_a", "connected": True},
            {"agent_id": "claude-deadbeef-work", "profile": "work", "key": "uc_live_a", "connected": True},
        ]
        response = self._get(self.request_id, agents=agents)
        self.assertEqual(response.status, 200)
        self.assertNotIn(" checked", response.text)
        self.assertEqual(response.text.count("name='agent_id'"), 2)

    def test_blocked_client_gets_no_allow_form(self):
        request_id = self._dcr_client(uris=("https://evil.test/cb",))
        response = self._get(request_id)
        self.assertEqual(response.status, 403)
        self.assertNotIn("name='agent_id'", response.text)

    def test_browser_labels_do_not_headline_the_internal_id(self):
        response = self._get(self.request_id)
        # The profile name is the headline; the id is secondary text.
        self.assertIn("<strong>facebook</strong>", response.text)
        self.assertIn("claude-deadbeef-facebook", response.text)

    # -- rejection paths ---------------------------------------------------

    def test_missing_session_is_rejected(self):
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"}
        )
        response = self._approve(request, user=None)
        self.assertEqual(response.status, 401)
        self.assertIsNone(self.store.get_grant("anything"))
        self.assertEqual(
            self.store.get_request(self.request_id)["status"], "pending"
        )

    def test_bearer_key_cannot_approve(self):
        """Only a browser session may mint a grant, never an API key."""
        from types import SimpleNamespace

        fake_core = SimpleNamespace(
            _authenticate=lambda request: {
                "auth_method": "bearer",
                "user_id": "user-1",
                "status": "approved",
            }
        )
        with patch.object(mcp_oauth, "_core", return_value=fake_core):
            self.assertIsNone(mcp_oauth._signed_in(object()))

    def test_admin_pending_user_cannot_approve(self):
        from types import SimpleNamespace

        fake_core = SimpleNamespace(
            _authenticate=lambda request: {
                "auth_method": "session",
                "user_id": "user-1",
                "status": "pending",
            }
        )
        with patch.object(mcp_oauth, "_core", return_value=fake_core):
            self.assertIsNone(mcp_oauth._signed_in(object()))

    def test_session_user_can_approve(self):
        from types import SimpleNamespace

        fake_core = SimpleNamespace(
            _authenticate=lambda request: {
                "auth_method": "session",
                "user_id": "user-1",
                "status": "approved",
            }
        )
        with patch.object(mcp_oauth, "_core", return_value=fake_core):
            self.assertEqual(mcp_oauth._signed_in(object())["user_id"], "user-1")

    def test_wrong_csrf_is_rejected(self):
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"},
            csrf_field="attacker",
        )
        response = self._approve(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(
            self.store.get_request(self.request_id)["status"], "pending"
        )

    def test_missing_csrf_cookie_is_rejected(self):
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"},
            cookie="",
        )
        response = self._approve(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(
            self.store.get_request(self.request_id)["status"], "pending"
        )

    def test_agent_outside_the_list_is_rejected(self):
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-work"}
        )
        response = self._approve(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(
            self.store.get_request(self.request_id)["status"], "pending"
        )

    def test_expired_request_is_rejected(self):
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_requests SET expires_at = 1 WHERE id = ?",
                (self.request_id,),
            )
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"}
        )
        response = self._approve(request)
        self.assertEqual(response.status, 400)

    # -- approval ----------------------------------------------------------

    def test_approve_creates_pinned_grant_and_ticket(self):
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"}
        )
        response = self._approve(request)
        self.assertEqual(response.status, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("https://unchainedsky.com/oauth/complete?ticket="))

        ticket = location.split("ticket=", 1)[1]
        payload = self.store.complete_request(ticket)
        self.assertEqual(payload["state"], "state-1")
        self.assertEqual(payload["redirect_uri"], "https://connector.test/cb")

        grant = self.store.get_grant(payload["grant_id"])
        self.assertEqual(grant["agent_id"], "claude-deadbeef-facebook")
        self.assertEqual(grant["user_id"], "user-1")
        self.assertEqual(grant["key_fingerprint"], fingerprint_key("uc_live_a"))
        self.assertIn("mcp:provision", grant["scopes"])

        # The code carries the approved scopes, which is what the token
        # exchange and therefore the principal will see.
        code = self.store.get_authorization_code(payload["code"])
        self.assertIn("mcp:provision", code["scopes"])

    def test_approve_is_single_use(self):
        request = _post(
            {"request": self.request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"}
        )
        self.assertEqual(self._approve(request).status, 302)
        again = self._approve(request)
        self.assertEqual(again.status, 400)

    def test_post_rejected_when_policy_withdrawn_after_get(self):
        request_id = self._dcr_client()
        policy_id = next(
            p["id"] for p in self.store.list_policies() if p["host"] == "claude.ai"
        )
        self.store.remove_policy(policy_id)
        request = _post(
            {"request": request_id, "decision": "approve",
             "agent_id": "claude-deadbeef-facebook"}
        )
        response = self._approve(request)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.store.get_request(request_id)["status"], "pending")

    # -- denial ------------------------------------------------------------

    def test_deny_redirects_with_access_denied(self):
        request = _post(
            {"request": self.request_id, "decision": "deny", "agent_id": ""}
        )
        response = self._approve(request)
        self.assertEqual(response.status, 302)
        location = response.headers["Location"]
        self.assertIn("error=access_denied", location)
        self.assertIn("state=state-1", location)
        self.assertEqual(
            self.store.get_request(self.request_id)["status"], "denied"
        )

    # -- feature switch ----------------------------------------------------

    def test_handler_is_404_when_feature_is_off(self):
        os.environ["MCP_OAUTH_ENABLED"] = "0"
        request = make_mocked_request("GET", f"/oauth/consent?request={self.request_id}")
        response = self._run(mcp_oauth.handle_mcp_oauth_consent(request))
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
