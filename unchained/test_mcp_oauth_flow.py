"""End-to-end protocol tests for the MCP OAuth authorization server.

Runs the real FastMCP/Starlette app in-process, so discovery, registration,
consent hand-off, code exchange, refresh rotation, revocation and bearer
verification are exercised the way a connector will use them.

Each case runs inside a single ``asyncio.run`` so the app lifespan (which opens
an anyio task group) is entered and exited in the same task.
"""

import asyncio
import base64
import hashlib
import os
import tempfile
import unittest

import httpx
from fastmcp import FastMCP

from auth import Auth
from mcp_oauth_provider import MCPOAuthProvider, install_complete_route
from mcp_oauth_store import OAuthStore

ISSUER = "https://testserver"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT_URI = "https://connector.test/callback"
JSON_HEADERS = {"Accept": "application/json, text/event-stream"}


def _pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def _initialize_payload(request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }


class OAuthFlowTests(unittest.TestCase):
    # -- harness -----------------------------------------------------------

    async def _scenario(self, name):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        os.environ["UNCHAINED_DB_PATH"] = self.db_path
        os.environ["MCP_OAUTH_ENABLED"] = "1"
        os.environ["MCP_OAUTH_ISSUER"] = ISSUER

        import mcp_oauth_provider as provider_module

        provider_module._AUTH.clear()

        self.auth = Auth(self.db_path)
        user = self.auth.get_or_create_user("connector@example.test", name="Connector")
        self.user_id = user["user_id"]
        self.api_key = self.auth.create_key(self.user_id)

        self.store = OAuthStore(self.db_path)
        # The seeded policies cover real connectors; admit the test callback too.
        self.store.add_policy(
            scheme="https",
            host="connector.test",
            port=443,
            path="/callback",
            match="exact",
        )
        self.provider = MCPOAuthProvider(
            self.store, issuer=ISSUER, resource=RESOURCE, dcr_enabled=True
        )

        self.mcp = FastMCP("oauth-test")

        @self.mcp.tool()
        async def ping() -> str:
            """Return pong."""
            return "pong"

        install_complete_route(self.mcp, self.provider)
        self.mcp.auth = self.provider
        self.app = self.mcp.http_app(path="/mcp", json_response=True)

        lifespan = self.app.router.lifespan_context(self.app)
        await lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=ISSUER,
            follow_redirects=False,
            timeout=10.0,
        )
        try:
            await getattr(self, "_case_" + name)()
        finally:
            await self.client.aclose()
            await lifespan.__aexit__(None, None, None)
            os.environ.pop("MCP_OAUTH_ENABLED", None)
            self.tmp.cleanup()

    # -- discovery ---------------------------------------------------------

    async def _case_protected_resource_metadata(self):
        resp = await self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["resource"], RESOURCE)
        servers = [s.rstrip("/") for s in body["authorization_servers"]]
        self.assertIn(ISSUER.rstrip("/"), servers)

    async def _case_authorization_server_metadata(self):
        resp = await self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["issuer"].rstrip("/"), ISSUER)
        self.assertTrue(body["authorization_endpoint"].endswith("/authorize"))
        self.assertTrue(body["token_endpoint"].endswith("/token"))
        self.assertIn("S256", body["code_challenge_methods_supported"])

    # -- authentication at the transport ----------------------------------

    async def _case_mcp_requires_auth_with_discovery_hint(self):
        resp = await self.client.post(
            "/mcp", json=_initialize_payload(), headers=JSON_HEADERS
        )
        self.assertEqual(resp.status_code, 401, resp.text)
        challenge = resp.headers.get("www-authenticate", "")
        self.assertIn("Bearer", challenge)
        self.assertIn("resource_metadata=", challenge)

    async def _case_api_key_still_works(self):
        resp = await self.client.post(
            "/mcp",
            json=_initialize_payload(),
            headers={**JSON_HEADERS, "Authorization": f"Bearer {self.api_key}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("result", resp.json())

    async def _case_bogus_token_is_rejected(self):
        resp = await self.client.post(
            "/mcp",
            json=_initialize_payload(),
            headers={**JSON_HEADERS, "Authorization": "Bearer nope"},
        )
        self.assertEqual(resp.status_code, 401)

    async def _case_oauth_prefix_lookalike_is_rejected(self):
        resp = await self.client.post(
            "/mcp",
            json=_initialize_payload(),
            headers={**JSON_HEADERS, "Authorization": "Bearer uco_at_forged"},
        )
        self.assertEqual(resp.status_code, 401)

    # -- dynamic registration ---------------------------------------------

    async def _register(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Test Connector",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "mcp:browser",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    async def _case_dynamic_registration(self):
        client = await self._register()
        self.assertTrue(client["client_id"])
        stored = self.store.get_client(client["client_id"])
        self.assertEqual(stored["source"], "dcr")
        self.assertEqual(stored["client_name"], "Test Connector")

    async def _case_registration_rejects_http_redirect(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Bad",
                "redirect_uris": ["http://evil.test/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"], "invalid_redirect_uri")

    async def _case_registration_rejects_confidential_client(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Secret Client",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"], "invalid_client_metadata")

    async def _case_registration_rejects_loopback_http(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Native",
                "redirect_uris": ["http://127.0.0.1:33418/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        # No cloud connector uses loopback; operators may still register one.
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"], "invalid_redirect_uri")

    # -- authorization -----------------------------------------------------

    async def _authorize(self, client, *, resource=RESOURCE):
        verifier, challenge = _pkce()
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp:browser",
        }
        if resource:
            params["resource"] = resource
        resp = await self.client.get("/authorize", params=params)
        return resp, verifier

    async def _case_authorize_redirects_to_consent(self):
        client = await self._register()
        resp, _ = await self._authorize(client)
        self.assertEqual(resp.status_code, 302, resp.text)
        location = resp.headers["location"]
        self.assertTrue(location.startswith(f"{ISSUER}/oauth/consent?request="))
        request_id = location.split("request=", 1)[1]
        pending = self.store.pending_request(request_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["state"], "xyz")
        self.assertEqual(pending["redirect_uri"], REDIRECT_URI)

    async def _case_authorize_rejects_foreign_resource(self):
        client = await self._register()
        resp, _ = await self._authorize(client, resource="https://evil.test/mcp")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=invalid_target", resp.headers["location"])
        self.assertIn("state=xyz", resp.headers["location"])

    async def _case_authorize_requires_pkce(self):
        client = await self._register()
        resp = await self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "code_challenge": "short",
                "code_challenge_method": "S256",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=invalid_request", resp.headers["location"])

    async def _case_authorize_rejects_unregistered_redirect(self):
        client = await self._register()
        _verifier, challenge = _pkce()
        resp = await self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": "https://evil.test/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("evil.test", resp.headers.get("location", ""))

    # -- the full connector flow ------------------------------------------

    async def _approve(self, location):
        request_id = location.split("request=", 1)[1]
        grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id=self.user_id,
            agent_id="claude-deadbeef",
            key_hash="deadbeef",
            key_fingerprint=hashlib.sha256(self.api_key.encode()).hexdigest(),
            scopes=["mcp:browser"],
            resource=RESOURCE,
        )
        complete = await self.client.get("/oauth/complete", params={"ticket": ticket})
        self.assertEqual(complete.status_code, 302, complete.text)
        location = complete.headers["location"]
        self.assertTrue(location.startswith(REDIRECT_URI))
        code = location.split("code=", 1)[1].split("&", 1)[0]
        return grant_id, code

    async def _full_flow(self):
        client = await self._register()
        resp, verifier = await self._authorize(client)
        grant_id, code = await self._approve(resp.headers["location"])
        token_resp = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        self.assertEqual(token_resp.status_code, 200, token_resp.text)
        return client, token_resp.json(), code, verifier, grant_id

    async def _case_full_flow_issues_working_token(self):
        client, tokens, _, _, grant_id = await self._full_flow()
        self.assertTrue(tokens["access_token"].startswith("uco_at_"))
        self.assertTrue(tokens["refresh_token"].startswith("uco_rt_"))

        resp = await self.client.post(
            "/mcp",
            json=_initialize_payload(2),
            headers={
                **JSON_HEADERS,
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        row = self.store.load_access_token(tokens["access_token"])
        self.assertEqual(row["grant_id"], grant_id)
        self.assertEqual(row["agent_id"], "claude-deadbeef")
        self.assertEqual(row["user_id"], self.user_id)

    async def _case_token_ignores_client_secret_for_public_clients(self):
        """Public clients need only `client_id`; a stray secret is ignored."""
        client = await self._register()
        resp, verifier = await self._authorize(client)
        _grant_id, code = await self._approve(resp.headers["location"])
        token_resp = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "client_secret": "ignored-because-client-is-public",
            },
        )
        self.assertEqual(token_resp.status_code, 200, token_resp.text)

    async def _case_unknown_client_at_token_is_rejected(self):
        resp = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": "nope",
                "client_id": "does-not-exist",
                "redirect_uri": REDIRECT_URI,
                "code_verifier": "v" * 43,
            },
        )
        self.assertIn(resp.status_code, (400, 401), resp.text)
        self.assertIn(
            resp.json()["error"], ("invalid_client", "unauthorized_client")
        )

    # -- callback admission ------------------------------------------------

    async def _case_seeded_claude_callback_registers(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    async def _case_unapproved_callback_is_refused(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Evil",
                "redirect_uris": ["https://evil.test/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"], "invalid_redirect_uri")

    async def _case_userinfo_callback_is_refused(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Tricky",
                "redirect_uris": ["https://claude.ai@evil.test/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    async def _case_query_callback_is_refused_when_policy_disallows(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Query",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback?next=/x"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    async def _case_one_bad_callback_blocks_the_whole_registration(self):
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Mixed",
                "redirect_uris": [
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://evil.test/callback",
                ],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    async def _case_withdrawing_a_policy_blocks_the_client(self):
        client = await self._register()
        policy_id = next(
            p["id"] for p in self.store.list_policies() if p["host"] == "connector.test"
        )
        self.assertTrue(self.store.remove_policy(policy_id))

        resp, _ = await self._authorize(client)
        # get_client() now reports the client as unknown, so the SDK refuses
        # before any callback redirect can happen.
        self.assertEqual(resp.status_code, 400, resp.text)

    async def _case_withdrawn_policy_cannot_deliver_a_code(self):
        client = await self._register()
        resp, _ = await self._authorize(client)
        request_id = resp.headers["location"].split("request=", 1)[1]
        _grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id=self.user_id,
            agent_id="claude-deadbeef",
            key_hash="deadbeef",
            key_fingerprint=hashlib.sha256(self.api_key.encode()).hexdigest(),
            scopes=["mcp:browser"],
            resource=RESOURCE,
        )
        policy_id = next(
            p["id"] for p in self.store.list_policies() if p["host"] == "connector.test"
        )
        self.store.remove_policy(policy_id)

        complete = await self.client.get("/oauth/complete", params={"ticket": ticket})
        self.assertEqual(complete.status_code, 403, complete.text)
        self.assertNotIn("code=", complete.headers.get("location", ""))

    async def _case_changed_callback_cannot_receive_a_code(self):
        self.store.save_client(
            client_id="manual-1",
            client_name="Manual",
            redirect_uris=[REDIRECT_URI],
            source="manual",
        )
        client = {"client_id": "manual-1"}
        resp, _verifier = await self._authorize(client)
        request_id = resp.headers["location"].split("request=", 1)[1]
        _grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id=self.user_id,
            agent_id="claude-deadbeef",
            key_hash="deadbeef",
            key_fingerprint=hashlib.sha256(self.api_key.encode()).hexdigest(),
            scopes=["mcp:browser"],
            resource=RESOURCE,
        )
        # Operator re-registers the client with a different callback while the
        # approval is still live.
        self.store.save_client(
            client_id="manual-1",
            client_name="Manual",
            redirect_uris=["https://connector.test/other"],
            source="manual",
        )
        complete = await self.client.get("/oauth/complete", params={"ticket": ticket})
        self.assertEqual(complete.status_code, 403, complete.text)
        self.assertNotIn("code=", complete.headers.get("location", ""))

    async def _case_authorize_allocation_is_bounded(self):
        import mcp_oauth_provider as provider_module

        previous = provider_module.AUTHORIZE_MAX_PENDING_PER_CLIENT
        provider_module.AUTHORIZE_MAX_PENDING_PER_CLIENT = 1
        try:
            client = await self._register()
            first, _ = await self._authorize(client)
            self.assertEqual(first.status_code, 302)
            self.assertIn("/oauth/consent", first.headers["location"])

            second, _ = await self._authorize(client)
            self.assertEqual(second.status_code, 302)
            self.assertIn("error=temporarily_unavailable", second.headers["location"])
        finally:
            provider_module.AUTHORIZE_MAX_PENDING_PER_CLIENT = previous

    async def _case_code_cannot_be_reused(self):
        client, _, code, verifier, _ = await self._full_flow()
        replay = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        self._assert_token_error(replay)

    async def _case_wrong_verifier_is_rejected(self):
        client = await self._register()
        resp, _verifier = await self._authorize(client)
        _grant_id, code = await self._approve(resp.headers["location"])
        _other, other_verifier = _pkce()
        wrong = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": other_verifier,
            },
        )
        self._assert_token_error(wrong)

    async def _case_complete_rejects_unknown_ticket(self):
        resp = await self.client.get("/oauth/complete", params={"ticket": "nope"})
        self.assertEqual(resp.status_code, 400)

    async def _case_complete_requires_ticket(self):
        resp = await self.client.get("/oauth/complete")
        self.assertEqual(resp.status_code, 400)

    # -- refresh and revocation -------------------------------------------

    async def _case_refresh_rotation_and_replay(self):
        client, tokens, _, _, _ = await self._full_flow()
        refreshed = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        rotated = refreshed.json()
        self.assertNotEqual(rotated["refresh_token"], tokens["refresh_token"])

        replay = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        self._assert_token_error(replay)

        # Replay revokes the whole family, including the rotated token.
        after = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": rotated["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        self._assert_token_error(after)

    async def _case_revocation_stops_access(self):
        client, tokens, _, _, _ = await self._full_flow()
        resp = await self.client.post(
            "/revoke",
            data={
                "token": tokens["access_token"],
                "client_id": client["client_id"],
                "client_secret": "",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        after = await self.client.post(
            "/mcp",
            json=_initialize_payload(3),
            headers={
                **JSON_HEADERS,
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        self.assertEqual(after.status_code, 401)

    async def _case_grant_revocation_stops_access(self):
        _client, tokens, _, _, grant_id = await self._full_flow()
        self.assertTrue(self.store.revoke_grant(grant_id))
        after = await self.client.post(
            "/mcp",
            json=_initialize_payload(4),
            headers={
                **JSON_HEADERS,
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        self.assertEqual(after.status_code, 401)

    # -- account lifecycle -------------------------------------------------

    def _assert_token_error(self, response, code="invalid_grant"):
        # FastMCP 4 returns 401 for token errors (MCP spec: invalid or expired
        # tokens MUST be 401); the 2.x line returned 400 from the SDK. The
        # error code in the body is the stable contract.
        self.assertIn(response.status_code, (400, 401), response.text)
        self.assertEqual(response.json().get("error"), code)

    async def _reject_user(self):
        with self.auth._conn() as conn:
            conn.execute(
                "UPDATE users SET status = 'rejected' WHERE user_id = ?", (self.user_id,)
            )

    async def _case_rejected_account_loses_mcp_access(self):
        _client, tokens, _, _, grant_id = await self._full_flow()
        await self._reject_user()
        resp = await self.client.post(
            "/mcp",
            json=_initialize_payload(9),
            headers={
                **JSON_HEADERS,
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        self.assertEqual(resp.status_code, 401, resp.text)
        # Request-time enforcement detaches the connector, not just the call.
        self.assertIsNone(self.store.get_grant(grant_id))

    async def _case_rejected_account_cannot_refresh(self):
        client, tokens, _, _, grant_id = await self._full_flow()
        await self._reject_user()
        refreshed = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        self._assert_token_error(refreshed)
        # The connector is detached, not merely paused.
        self.assertIsNone(self.store.get_grant(grant_id))

    async def _case_key_revocation_kills_the_connector(self):
        client, tokens, _, _, grant_id = await self._full_flow()
        self.auth.revoke_key(self.api_key)
        resp = await self.client.post(
            "/mcp",
            json=_initialize_payload(10),
            headers={
                **JSON_HEADERS,
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        self.assertEqual(resp.status_code, 401, resp.text)
        refreshed = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        self._assert_token_error(refreshed)
        self.assertIsNone(self.store.get_grant(grant_id))

    # -- registration switch ----------------------------------------------

    async def _case_dcr_can_be_disabled(self):
        self.provider.dcr_enabled = False
        self.provider.client_registration_options.enabled = False
        resp = await self.client.post(
            "/register",
            json={
                "client_name": "Nope",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertIn(resp.status_code, (400, 404), resp.text)


def _make_test(name):
    def run(self):
        asyncio.run(self._scenario(name))

    run.__name__ = "test_" + name
    return run


for _name in (
    "protected_resource_metadata",
    "authorization_server_metadata",
    "mcp_requires_auth_with_discovery_hint",
    "api_key_still_works",
    "bogus_token_is_rejected",
    "oauth_prefix_lookalike_is_rejected",
    "dynamic_registration",
    "registration_rejects_http_redirect",
    "registration_rejects_confidential_client",
    "registration_rejects_loopback_http",
    "authorize_redirects_to_consent",
    "authorize_rejects_foreign_resource",
    "authorize_requires_pkce",
    "authorize_rejects_unregistered_redirect",
    "full_flow_issues_working_token",
    "token_ignores_client_secret_for_public_clients",
    "unknown_client_at_token_is_rejected",
    "seeded_claude_callback_registers",
    "unapproved_callback_is_refused",
    "userinfo_callback_is_refused",
    "query_callback_is_refused_when_policy_disallows",
    "one_bad_callback_blocks_the_whole_registration",
    "withdrawing_a_policy_blocks_the_client",
    "withdrawn_policy_cannot_deliver_a_code",
    "changed_callback_cannot_receive_a_code",
    "authorize_allocation_is_bounded",
    "code_cannot_be_reused",
    "wrong_verifier_is_rejected",
    "complete_rejects_unknown_ticket",
    "complete_requires_ticket",
    "refresh_rotation_and_replay",
    "revocation_stops_access",
    "grant_revocation_stops_access",
    "rejected_account_loses_mcp_access",
    "rejected_account_cannot_refresh",
    "key_revocation_kills_the_connector",
    "dcr_can_be_disabled",
):
    setattr(OAuthFlowTests, "test_" + _name, _make_test(_name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
