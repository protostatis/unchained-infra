"""Unit tests for the MCP OAuth store (mcp_oauth_store.py)."""

import hashlib
import os
import tempfile
import threading
import time
import unittest

from mcp_oauth_store import (
    ACCESS_TOKEN_PREFIX,
    AUTHORIZATION_CODE_PREFIX,
    REFRESH_TOKEN_PREFIX,
    BudgetExceeded,
    OAuthStore,
    agent_id_for_key,
    fingerprint_key,
)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self.store = OAuthStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, client_id="client-1", uris=None, name="Test Client"):
        self.store.save_client(
            client_id=client_id,
            client_name=name,
            redirect_uris=uris or ["https://client.test/cb"],
            source="manual",
        )
        return self.store.get_client(client_id)

    def _approved(self, *, scopes=("mcp:browser",), agent="claude-aaaaaaaa"):
        client = self._client()
        request_id = self.store.create_request(
            client_id=client["client_id"],
            redirect_uri="https://client.test/cb",
            state="state-1",
            scopes=scopes,
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource="https://api.test/mcp",
        )
        grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id="user-1",
            agent_id=agent,
            key_hash="aaaaaaaa",
            key_fingerprint="f" * 64,
            scopes=scopes,
            resource="https://api.test/mcp",
        )
        return client, request_id, grant_id, ticket


class ClientStoreTests(StoreTestBase):
    def test_client_round_trip(self):
        client = self._client(uris=["https://a.test/cb", "https://b.test/cb"])
        self.assertEqual(client["client_name"], "Test Client")
        self.assertEqual(len(client["redirect_uris"]), 2)
        self.assertEqual(client["token_endpoint_auth_method"], "none")

    def test_unknown_client_is_none(self):
        self.assertIsNone(self.store.get_client("nope"))

    def test_count_clients_since(self):
        self._client("c1")
        self._client("c2")
        self.assertEqual(self.store.count_clients(), 2)
        self.assertEqual(self.store.count_clients(since=time.time() - 5), 2)
        self.assertEqual(self.store.count_clients(since=time.time() + 60), 0)


class AuthorizationRequestTests(StoreTestBase):
    def test_pending_request_round_trip(self):
        self._client()
        request_id = self.store.create_request(
            client_id="client-1",
            redirect_uri="https://client.test/cb",
            state="abc",
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource="https://api.test/mcp",
        )
        pending = self.store.pending_request(request_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["state"], "abc")
        self.assertEqual(pending["status"], "pending")

    def test_expired_request_is_not_pending(self):
        self._client()
        request_id = self.store.create_request(
            client_id="client-1",
            redirect_uri="https://client.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_requests SET expires_at = ? WHERE id = ?",
                (time.time() - 1, request_id),
            )
        self.assertIsNone(self.store.pending_request(request_id))

    def test_approval_is_single_use(self):
        _, _, _, ticket = self._approved()
        first = self.store.complete_request(ticket)
        self.assertTrue(first["code"].startswith(AUTHORIZATION_CODE_PREFIX))
        self.assertEqual(first["state"], "state-1")
        with self.assertRaises(ValueError):
            self.store.complete_request(ticket)

    def test_double_approval_only_succeeds_once(self):
        self._client()
        request_id = self.store.create_request(
            client_id="client-1",
            redirect_uri="https://client.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        args = dict(
            request_id=request_id,
            user_id="user-1",
            agent_id="claude-aaaaaaaa",
            key_hash="aaaaaaaa",
            key_fingerprint="f" * 64,
            scopes=["mcp:browser"],
            resource=None,
        )
        self.store.approve_request(**args)
        with self.assertRaises(ValueError):
            self.store.approve_request(**args)

    def test_denied_request_cannot_be_approved(self):
        self._client()
        request_id = self.store.create_request(
            client_id="client-1",
            redirect_uri="https://client.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        self.assertTrue(self.store.deny_request(request_id, "user-1"))
        with self.assertRaises(ValueError):
            self.store.approve_request(
                request_id=request_id,
                user_id="user-1",
                agent_id="claude-aaaaaaaa",
                key_hash="aaaaaaaa",
                key_fingerprint="f" * 64,
                scopes=["mcp:browser"],
                resource=None,
            )


class AuthorizationCodeTests(StoreTestBase):
    def test_code_is_consumed_once(self):
        client, _, grant_id, ticket = self._approved()
        code = self.store.complete_request(ticket)["code"]
        self.assertIsNotNone(self.store.get_authorization_code(code))
        first = self.store.consume_authorization_code(code, client_id=client["client_id"])
        self.assertEqual(first["grant_id"], grant_id)
        self.assertIsNone(self.store.consume_authorization_code(code, client_id=client["client_id"]))

    def test_code_bound_to_client(self):
        _, _, _, ticket = self._approved()
        code = self.store.complete_request(ticket)["code"]
        self.assertIsNone(self.store.consume_authorization_code(code, client_id="other-client"))
        # A failed attempt must not burn the code for the rightful client.
        self.assertIsNotNone(self.store.get_authorization_code(code))


class TokenTests(StoreTestBase):
    def test_tokens_have_prefixes_and_are_hashed_at_rest(self):
        _, _, grant_id, ticket = self._approved()
        code = self.store.complete_request(ticket)["code"]
        consumed = self.store.consume_authorization_code(code, client_id="client-1")
        issued = self.store.issue_tokens(
            grant_id=grant_id,
            client_id="client-1",
            scopes=consumed["scopes"],
            resource=consumed["resource"],
        )
        self.assertTrue(issued["access_token"].startswith(ACCESS_TOKEN_PREFIX))
        self.assertTrue(issued["refresh_token"].startswith(REFRESH_TOKEN_PREFIX))

        with self.store._conn() as conn:
            rows = conn.execute(
                "SELECT token_hash FROM mcp_oauth_tokens"
            ).fetchall()
        stored = {r["token_hash"] for r in rows}
        self.assertNotIn(issued["access_token"], stored)
        self.assertNotIn(issued["refresh_token"], stored)
        self.assertIn(
            hashlib.sha256(issued["access_token"].encode()).hexdigest(), stored
        )

    def test_access_token_loads_with_pinned_agent(self):
        _, _, grant_id, ticket = self._approved(agent="claude-bbbbbbbb")
        issued = self.store.issue_tokens(
            grant_id=grant_id,
            client_id="client-1",
            scopes=["mcp:browser"],
            resource="https://api.test/mcp",
        )
        row = self.store.load_access_token(issued["access_token"])
        self.assertIsNotNone(row)
        self.assertEqual(row["agent_id"], "claude-bbbbbbbb")
        self.assertEqual(row["user_id"], "user-1")

    def test_unknown_token_loads_nothing(self):
        self.assertIsNone(self.store.load_access_token("uco_at_nope"))

    def test_refresh_rotation(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        rotated = self.store.consume_refresh_token(
            issued["refresh_token"], client_id="client-1"
        )
        self.assertIsNotNone(rotated)
        self.assertEqual(rotated["family_id"], issued["family_id"])
        # The old refresh token is spent.
        self.assertIsNone(
            self.store.consume_refresh_token(issued["refresh_token"], client_id="client-1")
        )

    def test_refresh_replay_revokes_family(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertIsNotNone(
            self.store.consume_refresh_token(issued["refresh_token"], client_id="client-1")
        )
        # Replay of the rotated token is treated as compromise.
        self.assertIsNone(
            self.store.consume_refresh_token(issued["refresh_token"], client_id="client-1")
        )
        # The access token from the same family no longer works.
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))

    def test_refresh_bound_to_client(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertIsNone(
            self.store.consume_refresh_token(issued["refresh_token"], client_id="intruder")
        )
        # The rightful client can still rotate it.
        self.assertIsNotNone(
            self.store.consume_refresh_token(issued["refresh_token"], client_id="client-1")
        )

    def test_revoke_access_token(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertTrue(self.store.revoke_token(issued["access_token"]))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))

    def test_revoke_refresh_kills_family(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertTrue(self.store.revoke_token(issued["refresh_token"]))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))


class GrantTests(StoreTestBase):
    def test_revoke_grant_blocks_tokens(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertTrue(self.store.revoke_grant(grant_id))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNone(self.store.get_grant(grant_id))

    def test_revoke_grant_requires_owner(self):
        _, _, grant_id, _ = self._approved()
        self.assertFalse(self.store.revoke_grant(grant_id, user_id="someone-else"))
        self.assertIsNotNone(self.store.get_grant(grant_id))

    def test_revoke_grants_for_key(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        count = self.store.revoke_grants_for_key("f" * 64)
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertEqual(self.store.revoke_grants_for_key("f" * 64), 0)

    def test_list_grants_for_user_includes_client_name(self):
        self._approved()
        grants = self.store.list_grants_for_user("user-1")
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["client_name"], "Test Client")
        self.assertEqual(self.store.list_grants_for_user("other"), [])


class ConcurrencyTests(StoreTestBase):
    def test_parallel_code_exchange_only_one_wins(self):
        _, _, grant_id, ticket = self._approved()
        code = self.store.complete_request(ticket)["code"]
        results = []
        lock = threading.Lock()

        def exchange():
            row = self.store.consume_authorization_code(code, client_id="client-1")
            with lock:
                results.append(row)

        threads = [threading.Thread(target=exchange) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len([r for r in results if r]), 1)

    def test_parallel_approval_only_one_wins(self):
        self._client()
        request_id = self.store.create_request(
            client_id="client-1",
            redirect_uri="https://client.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        results = []
        lock = threading.Lock()

        def approve():
            try:
                self.store.approve_request(
                    request_id=request_id,
                    user_id="user-1",
                    agent_id="claude-aaaaaaaa",
                    key_hash="aaaaaaaa",
                    key_fingerprint="f" * 64,
                    scopes=["mcp:browser"],
                    resource=None,
                )
                ok = True
            except ValueError:
                ok = False
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=approve) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count(True), 1)


class HelperTests(unittest.TestCase):
    def test_agent_id_for_default_key(self):
        key = "uc_live_abc123"
        expected = f"claude-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
        self.assertEqual(agent_id_for_key(key), expected)

    def test_agent_id_for_profile(self):
        key = "uc_live_abc123"
        expected = f"claude-{hashlib.sha256(key.encode()).hexdigest()[:8]}-facebook"
        self.assertEqual(agent_id_for_key(key, "facebook"), expected)

    def test_agent_id_for_headless_ignores_profile(self):
        key = "uc_headless_abc123"
        expected = f"headless-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
        self.assertEqual(agent_id_for_key(key, "facebook"), expected)

    def test_fingerprint_is_full_sha256(self):
        self.assertEqual(len(fingerprint_key("x")), 64)


class PolicySeedTests(StoreTestBase):
    def test_defaults_are_installed_once(self):
        policies = self.store.list_policies()
        hosts = {p["host"] for p in policies}
        self.assertIn("claude.ai", hosts)
        self.assertIn("vertexaisearch.cloud.google.com", hosts)

    def test_removed_policy_is_not_resurrected(self):
        policy_id = self.store.list_policies()[0]["id"]
        self.assertTrue(self.store.remove_policy(policy_id))
        remaining = len(self.store.list_policies())

        # Re-instantiating the store must not re-seed defaults.
        fresh = OAuthStore(self.db_path)
        self.assertEqual(len(fresh.list_policies()), remaining)

    def test_add_policy_is_idempotent(self):
        first = self.store.add_policy(
            scheme="https", host="example.test", port=443, path="/cb"
        )
        second = self.store.add_policy(
            scheme="https", host="example.test", port=443, path="/cb"
        )
        self.assertEqual(first, second)


class DynamicClientBudgetTests(StoreTestBase):
    def test_budget_refuses_and_does_not_insert(self):
        self.store.register_dynamic_client(
            client_id="c1",
            client_name="one",
            redirect_uris=["https://example.test/cb"],
            max_total=1,
        )
        with self.assertRaises(BudgetExceeded):
            self.store.register_dynamic_client(
                client_id="c2",
                client_name="two",
                redirect_uris=["https://example.test/cb"],
                max_total=1,
            )
        self.assertIsNone(self.store.get_client("c2"))


class RequestBudgetTests(StoreTestBase):
    def _create(self, **kwargs):
        return self.store.create_request(
            client_id="client-1",
            redirect_uri="https://client.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
            **kwargs,
        )

    def test_per_client_cap(self):
        self._client()
        self._create(max_per_client=1)
        with self.assertRaises(BudgetExceeded):
            self._create(max_per_client=1)

    def test_global_cap(self):
        self._client()
        self._create(max_global=1)
        with self.assertRaises(BudgetExceeded):
            self._create(max_global=1)

    def test_completing_a_request_frees_the_slot(self):
        self._client()
        request_id = self._create(max_per_client=1)
        _grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id="user-1",
            agent_id="claude-aaaaaaaa",
            key_hash="aaaaaaaa",
            key_fingerprint="f" * 64,
            scopes=["mcp:browser"],
            resource=None,
        )
        self.store.complete_request(ticket)
        # The row is no longer pending, so a new request is admitted.
        self._create(max_per_client=1)

    def test_expired_rows_are_swept_on_the_allocation_path(self):
        self._client()
        request_id = self._create()
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_requests SET expires_at = 1 WHERE id = ?",
                (request_id,),
            )
        self._create()
        with self.store._conn() as conn:
            remaining = conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_requests").fetchone()
        self.assertEqual(int(remaining["n"]), 1)

    def test_sweep_keeps_requests_with_a_live_approval_ticket(self):
        self._client()
        request_id = self._create()
        _grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id="user-1",
            agent_id="claude-aaaaaaaa",
            key_hash="aaaaaaaa",
            key_fingerprint="f" * 64,
            scopes=["mcp:browser"],
            resource=None,
        )
        # Request row is past its own expiry but the ticket (2 min) is still live.
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_requests SET expires_at = ? WHERE id = ?",
                (time.time() - 5, request_id),
            )
        self._create()  # triggers the sweep
        self.assertIsNotNone(self.store.get_request(request_id))
        payload = self.store.complete_request(ticket)
        self.assertTrue(payload["code"].startswith(AUTHORIZATION_CODE_PREFIX))

    def test_expired_pending_rows_do_not_count(self):
        self._client()
        request_id = self._create(max_per_client=1)
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_requests SET expires_at = 1 WHERE id = ?",
                (request_id,),
            )
        self._create(max_per_client=1)


class DeleteClientTests(StoreTestBase):
    def test_delete_revokes_grants_tokens_and_pending_state(self):
        client, request_id, grant_id, ticket = self._approved()
        code = self.store.complete_request(ticket)["code"]
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id=client["client_id"], scopes=["mcp:browser"], resource=None
        )
        # Leave a second, still-pending request behind too.
        pending_id = self.store.create_request(
            client_id=client["client_id"],
            redirect_uri="https://client.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )

        result = self.store.delete_client_atomic(client["client_id"])

        self.assertTrue(result["client_deleted"])
        self.assertEqual(result["grants_revoked"], 1)
        self.assertIsNone(self.store.get_client(client["client_id"]))
        self.assertIsNone(self.store.get_grant(grant_id))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNone(self.store.get_authorization_code(code))
        self.assertIsNone(self.store.get_request(pending_id))

    def test_delete_of_unknown_client_is_reported(self):
        result = self.store.delete_client_atomic("nope")
        self.assertFalse(result["client_deleted"])
        self.assertEqual(result["grants_revoked"], 0)


class PruneTests(StoreTestBase):
    def test_prune_drops_expired_state(self):
        _, _, _, ticket = self._approved()
        self.store.complete_request(ticket)
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_codes SET expires_at = ?", (time.time() - 1,)
            )
            conn.execute(
                "UPDATE mcp_oauth_approvals SET expires_at = ?", (time.time() - 1,)
            )
            conn.execute(
                "UPDATE mcp_oauth_requests SET expires_at = ?", (time.time() - 1,)
            )
        pruned = self.store.prune()
        self.assertGreaterEqual(pruned["codes"], 1)
        self.assertGreaterEqual(pruned["approvals"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
