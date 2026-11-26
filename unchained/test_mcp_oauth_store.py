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

    def _mkuser(self, user_id: str):
        from auth import Auth

        auth = Auth(self.db_path)
        with auth._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, email, api_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, f"{user_id}@example.test", f"key-{user_id}", time.time()),
            )

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
        rotated = self.store.rotate_refresh_token(
            issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
        )
        self.assertIsNotNone(rotated)
        self.assertEqual(rotated["family_id"], issued["family_id"])
        # The old refresh token is spent: replaying it revokes the family.
        replay = self.store.rotate_refresh_token(
            issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
        )
        self.assertEqual(replay, {"replay": True})

    def test_refresh_cannot_widen_scopes(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        rotated = self.store.rotate_refresh_token(
            issued["refresh_token"],
            client_id="client-1",
            scopes=["mcp:browser", "mcp:provision"],
        )
        self.assertIsNotNone(rotated)
        self.assertEqual(rotated["scopes"], ["mcp:browser"])
        loaded = self.store.load_access_token(rotated["access_token"])
        self.assertEqual(loaded["scopes"], ["mcp:browser"])

    def test_refresh_with_only_ungranted_scopes_is_refused(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertIsNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"],
                client_id="client-1",
                scopes=["mcp:provision"],
            )
        )
        # The token was not consumed by the refused attempt.
        self.assertIsNotNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"],
                client_id="client-1",
                scopes=["mcp:browser"],
            )
        )

    def test_refresh_replay_revokes_family(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertIsNotNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
            )
        )
        # Replay of the rotated token is treated as compromise.
        replay = self.store.rotate_refresh_token(
            issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
        )
        self.assertEqual(replay, {"replay": True})
        # The access token from the same family no longer works.
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))

    def test_refresh_bound_to_client(self):
        _, _, grant_id, ticket = self._approved()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )
        self.assertIsNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="intruder", scopes=["mcp:browser"]
            )
        )
        # A wrong client must not have revoked anything: the rightful client
        # can still rotate it.
        self.assertIsNotNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
            )
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


class FamilyTests(StoreTestBase):
    def _issue(self):
        _, _, grant_id, _ = self._approved()
        return grant_id, self.store.issue_tokens(
            grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
        )

    def test_issue_refuses_revoked_grant(self):
        grant_id, _ = self._issue()
        self.store.revoke_grant(grant_id)
        with self.assertRaises(ValueError):
            self.store.issue_tokens(
                grant_id=grant_id, client_id="client-1", scopes=["mcp:browser"], resource=None
            )

    def test_issue_refuses_revoked_family(self):
        _grant_id, issued = self._issue()
        self.store.revoke_family(issued["family_id"])
        with self.assertRaises(ValueError):
            self.store.issue_tokens(
                grant_id=_grant_id,
                client_id="client-1",
                scopes=["mcp:browser"],
                resource=None,
                family_id=issued["family_id"],
            )

    def test_family_has_absolute_expiry(self):
        grant_id, issued = self._issue()
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_families SET expires_at = 1 WHERE family_id = ?",
                (issued["family_id"],),
            )
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
            )
        )

    def test_access_revocation_does_not_stop_refresh(self):
        _grant_id, issued = self._issue()
        self.assertTrue(self.store.revoke_token(issued["access_token"]))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNotNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
            )
        )

    def test_refresh_revocation_kills_the_family(self):
        _grant_id, issued = self._issue()
        self.assertTrue(self.store.revoke_token(issued["refresh_token"]))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
            )
        )

    def test_rotation_is_atomic_under_concurrency(self):
        _grant_id, issued = self._issue()
        results = []
        lock = threading.Lock()

        def rotate():
            result = self.store.rotate_refresh_token(
                issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=rotate) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [r for r in results if r and not r.get("replay")]
        replays = [r for r in results if r and r.get("replay")]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(replays), 5)
        # A replay revokes the family, so even the winner's fresh tokens die.
        self.assertIsNone(self.store.load_access_token(winners[0]["access_token"]))

    def test_prune_removes_expired_families_and_tokens(self):
        _grant_id, issued = self._issue()
        one_day_ago = time.time() - 24 * 60 * 60
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_families SET expires_at = ? WHERE family_id = ?",
                (one_day_ago, issued["family_id"]),
            )
        pruned = self.store.prune()
        # Expired, but inside the 7-day retention window: still present, and
        # already unusable.
        self.assertEqual(pruned["families"], 0)
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))

        with self.store._conn() as conn:
            conn.execute(
                "UPDATE mcp_oauth_families SET expires_at = ? WHERE family_id = ?",
                (time.time() - 40 * 24 * 60 * 60, issued["family_id"]),
            )
        pruned = self.store.prune()
        self.assertEqual(pruned["families"], 1)
        self.assertGreaterEqual(pruned["orphan_tokens"], 2)

    def test_prune_keeps_consumed_ancestors_of_a_live_family(self):
        _grant_id, issued = self._issue()
        self.store.rotate_refresh_token(
            issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
        )
        self.store.prune()
        with self.store._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM mcp_oauth_tokens WHERE family_id = ?",
                (issued["family_id"],),
            ).fetchone()
        # Consumed ancestor + fresh access/refresh stay, so replay is detectable.
        self.assertGreaterEqual(int(row["n"]), 3)
        replay = self.store.rotate_refresh_token(
            issued["refresh_token"], client_id="client-1", scopes=["mcp:browser"]
        )
        self.assertEqual(replay, {"replay": True})


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

    def test_static_providers_are_seeded(self):
        hosts = {p["host"] for p in self.store.list_policies()}
        for host in ("claude.ai", "vertexaisearch.cloud.google.com",
                     "chatgpt.com", "agent.meta.ai"):
            self.assertIn(host, hosts)

    def test_v1_database_receives_only_the_new_static_policies(self):
        with self.store._conn() as conn:
            conn.execute(
                "DELETE FROM mcp_oauth_policies WHERE host IN ('chatgpt.com', 'agent.meta.ai')"
            )
            # An operator removed Claude's policy on this v1 database.
            conn.execute("DELETE FROM mcp_oauth_policies WHERE host = 'claude.ai'")
            conn.execute(
                "UPDATE mcp_oauth_meta SET value = '1' WHERE key = 'policy_seed_version'"
            )
        fresh = OAuthStore(self.db_path)
        hosts = {p["host"] for p in fresh.list_policies()}
        self.assertIn("chatgpt.com", hosts)
        self.assertIn("agent.meta.ai", hosts)
        self.assertNotIn("claude.ai", hosts)  # removal is sticky across a seed bump
        self.assertIn("vertexaisearch.cloud.google.com", hosts)

    def test_removed_policy_is_not_resurrected(self):
        policy_id = self.store.list_policies()[0]["id"]
        self.assertTrue(self.store.remove_policy(policy_id))
        remaining = len(self.store.list_policies())

        # Re-instantiating the store must not re-seed defaults.
        fresh = OAuthStore(self.db_path)
        self.assertEqual(len(fresh.list_policies()), remaining)

    def test_policy_owner_round_trip(self):
        policy_id = self.store.add_policy(
            scheme="https", host="x.test", port=443, path="/cb", owner_user_id="u-1"
        )
        row = next(p for p in self.store.list_policies() if p["id"] == policy_id)
        self.assertEqual(row["owner_user_id"], "u-1")

    def test_policy_group_is_idempotent_and_keeps_the_first_owner(self):
        entries = [
            dict(scheme="https", host="g.test", port=443, path=f"/{p}/x", match="exact", allow_query=0)
            for p in ("r", "a")
        ]
        first = self.store.add_policy_group(entries, owner_user_id="u-1")
        second = self.store.add_policy_group(entries, owner_user_id="u-2")
        self.assertEqual(set(first), set(second))
        owners = {
            p["owner_user_id"] for p in self.store.list_policies() if p["id"] in first
        }
        self.assertEqual(owners, {"u-1"})

    def test_owner_column_is_added_to_an_older_database(self):
        with self.store._conn() as conn:
            conn.execute("ALTER TABLE mcp_oauth_policies DROP COLUMN owner_user_id")
        fresh = OAuthStore(self.db_path)
        with fresh._conn() as conn:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(mcp_oauth_policies)")}
        self.assertIn("owner_user_id", columns)

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


class BindingTests(StoreTestBase):
    def setUp(self):
        super().setUp()
        for user_id in ("u-1", "u-2", "u-3"):
            self._mkuser(user_id)

    def _bound_client(self, owner="u-1", binding="b1"):
        self.store.upsert_binding(binding, owner)
        self.store.save_client(
            client_id="c1",
            client_name="C",
            redirect_uris=["https://x.test/cb"],
            source="dcr",
        )
        self.store.add_policy(
            scheme="https", host="x.test", port=443, path="/cb", binding_id=binding
        )
        return "c1"

    def _grant(self, client_id="c1", user="u-1"):
        request_id = self.store.create_request(
            client_id=client_id,
            redirect_uri="https://x.test/cb",
            state=None,
            scopes=["mcp:browser"],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource=None,
        )
        grant_id, ticket = self.store.approve_request(
            request_id=request_id,
            user_id=user,
            agent_id="claude-aaaaaaaa",
            key_hash="aaaaaaaa",
            key_fingerprint="f" * 64,
            scopes=["mcp:browser"],
            resource=None,
        )
        return grant_id, ticket

    def _clear_binding_migration_marker(self):
        with self.store._conn() as conn:
            conn.execute(
                "DELETE FROM mcp_oauth_meta WHERE key = 'bindings_migrated'"
            )

    def test_enroll_binding_refuses_a_different_owner(self):
        self._mkuser("u-1")
        self._mkuser("u-2")
        entry = dict(scheme="https", host="g.test", port=443, path="/r/x", match="exact")
        self.store.enroll_binding("b-enroll", "u-1", [entry])
        self.assertEqual(self.store.binding_events("b-enroll")[0]["event"], "enrolled")
        with self.assertRaises(ValueError):
            self.store.enroll_binding("b-enroll", "u-2", [entry])

    def test_migration_promotes_owned_policy_to_binding(self):
        binding = "user_bound_custom-mcp-abc"
        self.store.add_policy(
            scheme="https",
            host="g.test",
            port=443,
            path=f"/r/{binding}",
            owner_user_id="u-1",
        )
        self._clear_binding_migration_marker()
        fresh = OAuthStore(self.db_path)
        row = fresh.get_binding(binding)
        self.assertIsNotNone(row)
        self.assertEqual(row["owner_user_id"], "u-1")
        self.assertEqual(row["status"], "active")
        policy = [p for p in fresh.list_policies() if binding in p["path"]][0]
        self.assertEqual(policy["binding_id"], binding)

    def test_migration_blocks_conflicting_owners(self):
        binding = "user_bound_custom-mcp-def"
        self.store.add_policy(
            scheme="https", host="g.test", port=443, path=f"/r/{binding}", owner_user_id="u-1"
        )
        self.store.add_policy(
            scheme="https", host="g.test", port=443, path=f"/a/{binding}", owner_user_id="u-2"
        )
        self._clear_binding_migration_marker()
        fresh = OAuthStore(self.db_path)
        # No arbitrary owner is chosen; the rows stay unbound and fail closed.
        self.assertIsNone(fresh.get_binding(binding))
        from mcp_oauth_policy import client_callback_status

        client = {"source": "dcr", "redirect_uris": [f"https://g.test/r/{binding}"]}
        self.assertEqual(
            client_callback_status(client, fresh.list_policies(), fresh.bindings_map()),
            "blocked",
        )

    def test_transfer_revokes_grants_tokens_codes_and_pending_requests(self):
        client_id = self._bound_client()
        grant_id, ticket = self._grant()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id=client_id, scopes=["mcp:browser"], resource=None
        )
        # A pending request with a live approval and an unused code.
        _g2, ticket2 = self._grant()
        code = self.store.complete_request(ticket2)["code"]

        result = self.store.transfer_binding("b1", from_owner="u-1", to_owner="u-2")

        self.assertEqual(result["version"], 2)
        self.assertEqual(self.store.get_binding("b1")["owner_user_id"], "u-2")
        self.assertIsNone(self.store.get_grant(grant_id))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNone(
            self.store.rotate_refresh_token(
                issued["refresh_token"], client_id=client_id, scopes=["mcp:browser"]
            )
        )
        self.assertIsNone(self.store.get_authorization_code(code))
        event = self.store.binding_events("b1")[0]
        self.assertEqual(event["event"], "transferred")
        self.assertEqual(event["old_owner"], "u-1")
        self.assertEqual(event["new_owner"], "u-2")
        self.assertTrue(
            all(
                p["owner_user_id"] == "u-2"
                for p in self.store.list_policies()
                if p.get("binding_id") == "b1"
            )
        )

    def test_attach_matches_only_the_exact_binding_suffix(self):
        self.store.add_policy(scheme="https", host="g.test", port=443, path="/r/abc")
        self.store.add_policy(scheme="https", host="g.test", port=443, path="/r/xabc")
        self.store.upsert_binding("abc", "u-1")
        self.store.attach_policies_to_binding("abc")
        rows = {
            p["path"]: p.get("binding_id")
            for p in self.store.list_policies()
            if "abc" in p["path"]
        }
        self.assertEqual(rows["/r/abc"], "abc")
        self.assertIsNone(rows["/r/xabc"])

    def test_enrollment_permits_the_owner(self):
        entry = dict(scheme="https", host="g.test", port=443, path="/r/x", match="exact")
        self.store.enroll_binding("b-enroll", "u-1", [entry])
        self.assertEqual(self.store.get_binding("b-enroll")["accounts"], ["u-1"])

    def test_permit_adds_an_account(self):
        self._bound_client()
        self.store.permit_binding_account("b1", "u-2", actor="ops")
        self.assertEqual(self.store.get_binding("b1")["accounts"], ["u-1", "u-2"])

    def test_revoke_permit_revokes_that_account_only(self):
        client_id = self._bound_client()
        grant_u2, _ticket = self._grant(user="u-2")
        issued = self.store.issue_tokens(
            grant_id=grant_u2, client_id=client_id, scopes=["mcp:browser"], resource=None
        )
        grant_u1, _ticket1 = self._grant(user="u-1")
        self.store.permit_binding_account("b1", "u-2")

        revoked = self.store.revoke_binding_account("b1", "u-2")

        self.assertEqual(revoked, 1)
        self.assertNotIn("u-2", self.store.get_binding("b1")["accounts"])
        self.assertIsNone(self.store.get_grant(grant_u2))
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        # The administrator's grant is untouched.
        self.assertIsNotNone(self.store.get_grant(grant_u1))

    def test_administrator_cannot_be_unpermitted(self):
        self._bound_client()
        with self.assertRaises(ValueError):
            self.store.revoke_binding_account("b1", "u-1")

    def test_transfer_resets_permitted_accounts_to_the_new_owner(self):
        self._bound_client()
        self.store.permit_binding_account("b1", "u-2")
        self.store.transfer_binding("b1", from_owner="u-1", to_owner="u-3")
        self.assertEqual(self.store.get_binding("b1")["accounts"], ["u-3"])

    def test_transfer_rolls_back_when_revocation_fails(self):
        self._bound_client()
        original = self.store._revoke_clients_tx

        def boom(*args, **kwargs):
            raise RuntimeError("revocation failed")

        self.store._revoke_clients_tx = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                self.store.transfer_binding("b1", from_owner="u-1", to_owner="u-2")
        finally:
            self.store._revoke_clients_tx = original  # type: ignore[assignment]
        binding = self.store.get_binding("b1")
        self.assertEqual(binding["owner_user_id"], "u-1")
        self.assertEqual(binding["authorization_version"], 1)

    def test_revocation_batches_large_client_sets(self):
        ids = [f"c{i}" for i in range(1100)]
        with self.store._conn() as conn:
            conn.executemany(
                """
                INSERT INTO mcp_oauth_grants
                    (id, user_id, client_id, agent_id, key_hash,
                     key_fingerprint, scopes, created_at)
                VALUES (?, 'u-1', ?, 'claude-aaaaaaaa', 'aaaaaaaa', 'f', '[]', ?)
                """,
                [(f"g{i}", cid, time.time()) for i, cid in enumerate(ids)],
            )
        result = self.store.revoke_client_authorizations(ids)
        self.assertEqual(result["grants_revoked"], 1100)

    def test_transfer_requires_the_expected_owner(self):
        self._bound_client()
        with self.assertRaises(ValueError):
            self.store.transfer_binding("b1", from_owner="u-9", to_owner="u-2")

    def test_transfer_of_unknown_binding_fails(self):
        with self.assertRaises(ValueError):
            self.store.transfer_binding("nope", from_owner="u-1", to_owner="u-2")

    def test_blocking_a_dead_owner_revokes_and_tombstones(self):
        client_id = self._bound_client()
        grant_id, _ticket = self._grant()
        issued = self.store.issue_tokens(
            grant_id=grant_id, client_id=client_id, scopes=["mcp:browser"], resource=None
        )
        self.assertEqual(self.store.block_bindings_for_user("u-1"), 1)
        self.assertEqual(self.store.get_binding("b1")["status"], "blocked")
        self.assertEqual(self.store.binding_events("b1")[0]["event"], "blocked")
        self.assertIsNone(self.store.load_access_token(issued["access_token"]))
        self.assertIsNone(self.store.get_grant(grant_id))

    def test_transfer_reactivates_a_blocked_binding(self):
        self._bound_client()
        self.store.block_bindings_for_user("u-1")
        self.store.transfer_binding("b1", from_owner="u-1", to_owner="u-2")
        self.assertEqual(self.store.get_binding("b1")["status"], "active")


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
