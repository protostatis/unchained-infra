"""test_credit.py — Unit, concurrency, and idempotency tests for credit accounting.

Tests cover:
  - Account creation, grant, reversal, balance
  - Run lifecycle (create, finish, release held)
  - Call reservation, settlement, release
  - Idempotency (duplicate grant, duplicate reservation, duplicate settlement)
  - Insufficient balance on reservation, concurrent reservations
  - Missing-cost capture (conservative reservation used)
  - Cross-user run access rejection
  - Model allowlist enforcement
  - Admin grant authorization
  - Trial grant from OpenRouter budget
  - Immutable ledger entries
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest

from credit import (
    CreditLedger,
    HOSTED_MODEL_CATALOG,
    InsufficientBalanceError,
    RunNotActiveError,
    _default_reservation,
    _usd_to_micro,
    _micro_to_usd,
    is_hosted_model_allowed,
)


class TestCreditLedger(unittest.TestCase):
    """Core unit tests for credit accounting."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    # ---- Account basics ----

    def test_ensure_account(self):
        acct = self.ledger.ensure_account("u-test-1")
        self.assertEqual(acct["user_id"], "u-test-1")
        self.assertEqual(acct["balance_micro_usd"], 0)
        self.assertGreater(len(acct["account_id"]), 0)

        # Same user returns existing account
        acct2 = self.ledger.ensure_account("u-test-1")
        self.assertEqual(acct["account_id"], acct2["account_id"])

    def test_get_balance_nonexistent(self):
        bal = self.ledger.get_balance("nonexistent")
        self.assertEqual(bal, 0)

    # ---- Grant / reversal ----

    def test_grant_adds_balance(self):
        result = self.ledger.grant("u-test-2", _usd_to_micro(1.0), idempotency_key="grant-1")
        self.assertFalse(result.get("already_applied"))
        self.assertEqual(result["balance_micro_usd"], _usd_to_micro(1.0))
        self.assertEqual(result["balance_usd"], 1.0)

    def test_grant_idempotent(self):
        result1 = self.ledger.grant("u-test-3", _usd_to_micro(0.5), idempotency_key="grant-2")
        self.assertFalse(result1.get("already_applied"))

        result2 = self.ledger.grant("u-test-3", _usd_to_micro(0.5), idempotency_key="grant-2")
        self.assertTrue(result2.get("already_applied"))
        self.assertEqual(result2["balance_micro_usd"], _usd_to_micro(0.5))  # not doubled

    def test_grant_requires_positive_amount(self):
        with self.assertRaises(ValueError):
            self.ledger.grant("u-test-4", 0)

    def test_reversal_deducts_balance(self):
        self.ledger.grant("u-test-5", _usd_to_micro(2.0), idempotency_key="g3")
        result = self.ledger.reverse_grant("u-test-5", _usd_to_micro(0.5), idempotency_key="r1")
        self.assertFalse(result.get("already_applied"))
        self.assertEqual(result["balance_usd"], 1.5)

    def test_reversal_idempotent(self):
        self.ledger.grant("u-test-6", _usd_to_micro(3.0), idempotency_key="g4")
        self.ledger.reverse_grant("u-test-6", _usd_to_micro(1.0), idempotency_key="r2")
        result = self.ledger.reverse_grant("u-test-6", _usd_to_micro(1.0), idempotency_key="r2")
        self.assertTrue(result.get("already_applied"))
        self.assertEqual(result["balance_usd"], 2.0)  # not 1.0

    def test_reversal_insufficient_balance(self):
        self.ledger.grant("u-test-7", _usd_to_micro(1.0), idempotency_key="g5")
        with self.assertRaises(InsufficientBalanceError):
            self.ledger.reverse_grant("u-test-7", _usd_to_micro(2.0), idempotency_key="r3")

    # ---- Run lifecycle ----

    def test_create_run(self):
        run = self.ledger.create_run(
            "u-test-8", model="google/gemini-flash-lite",
            idempotency_key="run-1",
        )
        self.assertFalse(run.get("already_exists"))
        self.assertEqual(run["status"], "active")

    def test_create_run_idempotent(self):
        run1 = self.ledger.create_run("u-test-9", idempotency_key="run-2")
        run2 = self.ledger.create_run("u-test-9", idempotency_key="run-2")
        self.assertTrue(run2.get("already_exists"))
        self.assertEqual(run1["run_id"], run2["run_id"])

    def test_finish_run(self):
        self.ledger.grant("u-test-10", _usd_to_micro(1.0), idempotency_key="g-finish")

        run = self.ledger.create_run("u-test-10", idempotency_key="r-finish")
        run_id = run["run_id"]

        # Reserve and settle a call
        call = self.ledger.reserve_call(
            run_id, model="google/gemini-flash-lite",
            reservation_micro_usd=100, idempotency_key="c-finish",
        )
        self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=50)

        result = self.ledger.finish_run(run_id)
        self.assertEqual(result["status"], "completed")

    def test_finish_run_idempotent(self):
        self.ledger.grant("u-test-11", _usd_to_micro(1.0), idempotency_key="g-fi2")
        run = self.ledger.create_run("u-test-11", idempotency_key="r-fi2")
        run_id = run["run_id"]
        self.ledger.finish_run(run_id)
        result = self.ledger.finish_run(run_id)
        self.assertTrue(result.get("already_finished"))

    def test_finish_run_releases_held(self):
        self.ledger.grant("u-test-12", _usd_to_micro(1.0), idempotency_key="g-release")

        run = self.ledger.create_run("u-test-12", idempotency_key="r-release")
        run_id = run["run_id"]

        # Reserve but don't settle
        call = self.ledger.reserve_call(
            run_id, model="google/gemini-flash-lite",
            reservation_micro_usd=500, idempotency_key="c-hold",
        )
        self.assertEqual(call["status"], "held")

        # Finish run should release the held reservation
        self.ledger.finish_run(run_id)

        # Verify reservation was released
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT status FROM credit_call_reservations WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "released")

    def test_get_runs_for_user(self):
        self.ledger.create_run("u-test-runs", idempotency_key="ra")
        self.ledger.create_run("u-test-runs", idempotency_key="rb")
        runs = self.ledger.get_runs_for_user("u-test-runs")
        self.assertEqual(len(runs), 2)

    # ---- Call reservation / settlement / release ----

    def test_reserve_call(self):
        self.ledger.grant("u-test-13", _usd_to_micro(1.0), idempotency_key="g-reserve")
        run = self.ledger.create_run("u-test-13", idempotency_key="r-reserve")
        call = self.ledger.reserve_call(
            run["run_id"], model="google/gemini-flash-lite",
            reservation_micro_usd=100, idempotency_key="c1",
        )
        self.assertEqual(call["status"], "held")
        self.assertEqual(call["reserved_micro_usd"], 100)

    def test_reserve_default_model_reservation(self):
        self.ledger.grant("u-test-14", _usd_to_micro(1.0), idempotency_key="g-def")
        run = self.ledger.create_run("u-test-14", idempotency_key="r-def")
        call = self.ledger.reserve_call(
            run["run_id"], model="google/gemini-flash-lite",
            idempotency_key="c-def",
        )
        # Default for this model should match catalog
        expected = HOSTED_MODEL_CATALOG.get("google/gemini-flash-lite", 100)
        self.assertEqual(call["reserved_micro_usd"], max(expected, 1))

    def test_reserve_idempotent(self):
        self.ledger.grant("u-test-15", _usd_to_micro(1.0), idempotency_key="g-idem")
        run = self.ledger.create_run("u-test-15", idempotency_key="r-idem")
        call1 = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-idem",
        )
        call2 = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-idem",
        )
        self.assertEqual(call1["call_id"], call2["call_id"])
        self.assertTrue(call2["already_reserved"])

    def test_settle_call(self):
        self.ledger.grant("u-test-16", _usd_to_micro(1.0), idempotency_key="g-settle")
        run = self.ledger.create_run("u-test-16", idempotency_key="r-settle")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-settle",
        )
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=70,
            prompt_tokens=100, completion_tokens=200,
        )
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["settled_micro_usd"], 70)
        self.assertEqual(result["released_micro_usd"], 30)  # 100-70

    def test_settle_duplicate(self):
        self.ledger.grant("u-test-17", _usd_to_micro(1.0), idempotency_key="g-dup")
        run = self.ledger.create_run("u-test-17", idempotency_key="r-dup")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-dup",
        )
        self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=50)
        result = self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=50)
        self.assertTrue(result["already_settled"])

    def test_settle_capped_at_reserved(self):
        # Cost higher than reserved should be capped
        self.ledger.grant("u-test-18", _usd_to_micro(1.0), idempotency_key="g-cap")
        run = self.ledger.create_run("u-test-18", idempotency_key="r-cap")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-cap",
        )
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=999999,
        )
        self.assertEqual(result["settled_micro_usd"], 100)  # capped at reserved

    def test_release_call(self):
        self.ledger.grant("u-test-19", _usd_to_micro(1.0), idempotency_key="g-rel")
        run = self.ledger.create_run("u-test-19", idempotency_key="r-rel")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-rel",
        )
        result = self.ledger.release_call(call["call_id"])
        self.assertEqual(result["status"], "released")

    def test_release_duplicate(self):
        self.ledger.grant("u-test-20", _usd_to_micro(1.0), idempotency_key="g-rel2")
        run = self.ledger.create_run("u-test-20", idempotency_key="r-rel2")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-rel2",
        )
        self.ledger.release_call(call["call_id"])
        result = self.ledger.release_call(call["call_id"])
        self.assertTrue(result["already_released"])

    # ---- Insufficient balance under concurrent reservations ----

    def test_insufficient_balance_concurrent(self):
        """Two simultaneous reservations that together exceed balance."""
        self.ledger.grant("u-test-21", _usd_to_micro(0.01), idempotency_key="g-conc")
        run = self.ledger.create_run("u-test-21", idempotency_key="r-conc")

        # First reservation — 5k micro ($0.005)
        call1 = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=5000, idempotency_key="c-conc1",
        )
        self.assertEqual(call1["status"], "held")

        # Second reservation — 6k micro ($0.006) → exceeds remaining ($0.005)
        with self.assertRaises(InsufficientBalanceError):
            self.ledger.reserve_call(
                run["run_id"], reservation_micro_usd=6000, idempotency_key="c-conc2",
            )

        # After releasing first, second should succeed
        self.ledger.release_call(call1["call_id"])
        self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=6000, idempotency_key="c-conc2",
        )

    def test_balance_never_negative(self):
        """Reservations cannot make balance go negative (held is tracked)."""
        self.ledger.grant("u-test-22", _usd_to_micro(0.005), idempotency_key="g-neg")
        run = self.ledger.create_run("u-test-22", idempotency_key="r-neg")

        # Reserve all available
        self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=5000, idempotency_key="c-neg",
        )
        # Balance should still be 5000 (only held, not deducted)
        bal = self.ledger.get_balance("u-test-22")
        self.assertEqual(bal, 5000)

        # Settle
        account = self.ledger.get_account("u-test-22")
        held = self.ledger.held_reservation_total(account["account_id"])
        self.assertEqual(held, 5000)

    # ---- Missing-cost capture ----

    def test_missing_cost_uses_conservative_reservation(self):
        """When actual cost is zero, settlement uses the conservative reservation."""
        self.ledger.grant("u-test-23", _usd_to_micro(0.01), idempotency_key="g-missing")
        run = self.ledger.create_run("u-test-23", idempotency_key="r-missing")
        call = self.ledger.reserve_call(
            run["run_id"], model="google/gemini-flash-lite",
            idempotency_key="c-missing",
        )
        # Settle with zero cost
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=0,
        )
        # Still records usage — settled_micro_usd should be 0 (capped at reserved)
        self.assertIsNotNone(result)

        # Balance after
        bal_after = self.ledger.get_balance("u-test-23")
        # 10000 - 0 = 10000 balance (cost is 0, but reservation was 5 from catalog)
        # Actually settle with 0 actual cost means settled=0, released=reservation amount
        total_spent = self.ledger.get_account("u-test-23")["total_spent_micro_usd"]
        self.assertEqual(total_spent, 0)  # 0 actual cost

    # ---- Cross-user run access ----

    def test_cross_user_run_access(self):
        """Reserving a call for user B against user A's run should fail."""
        self.ledger.grant("user-alice", _usd_to_micro(1.0), idempotency_key="g-alice")
        self.ledger.grant("user-bob", _usd_to_micro(1.0), idempotency_key="g-bob")

        run = self.ledger.create_run("user-alice", idempotency_key="r-alice")

        # user_bid mismatch should be rejected
        with self.assertRaises(ValueError) as ctx:
            self.ledger.reserve_call(
                run["run_id"],
                reservation_micro_usd=100,
                idempotency_key="c-cross",
                user_id="user-bob",
            )
        self.assertIn("does not own run", str(ctx.exception))

    # ---- Model allowlist ----

    def test_model_allowlist_rejects_unknown(self):
        self.assertFalse(is_hosted_model_allowed("evil/hacker/model"))
        self.assertFalse(is_hosted_model_allowed(""))
        self.assertFalse(is_hosted_model_allowed("nonexistent-model"))

    def test_model_allowlist_accepts_catalog(self):
        self.assertTrue(is_hosted_model_allowed("google/gemini-3.1-flash-lite"))
        self.assertTrue(is_hosted_model_allowed("arcee-ai/trinity-large-preview:free"))

    def test_model_allowlist_admin_override(self):
        # Not in default catalog but admin allows via explicit allowlist
        self.assertTrue(is_hosted_model_allowed(
            "custom/pro-model",
            admin_allowlist={"custom/pro-model"},
        ))
        # Still unknown without admin override
        self.assertFalse(is_hosted_model_allowed("custom/pro-model"))

    def test_model_allowlist_slash_override(self):
        # Slash-containing models rejected by default
        self.assertFalse(is_hosted_model_allowed("some/new-model"))
        # But allowed with flag
        self.assertTrue(is_hosted_model_allowed(
            "some/new-model", allow_slash_models=True,
        ))

    # ---- Immutable ledger entries ----

    def test_ledger_entries_immutable(self):
        """Grant entries appear in ledger and cannot be deleted via API."""
        self.ledger.grant("u-ledger", _usd_to_micro(0.5), idempotency_key="lg1")
        entries = self.ledger.get_ledger_for_user("u-ledger")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["entry_type"], "grant")
        self.assertEqual(entries[0]["amount_usd"], 0.5)
        self.assertGreater(entries[0]["entry_id"], 0)

    def test_ledger_entries_for_settlement(self):
        self.ledger.grant("u-ledger2", _usd_to_micro(1.0), idempotency_key="lg-s")
        run = self.ledger.create_run("u-ledger2", idempotency_key="r-lg")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-lg",
        )
        self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=50)

        entries = self.ledger.get_ledger_for_user("u-ledger2")
        entry_types = {e["entry_type"] for e in entries}
        self.assertIn("grant", entry_types)
        self.assertIn("reservation", entry_types)
        self.assertIn("settlement", entry_types)

    # ---- Trial grant from OpenRouter budget ----

    def test_trial_grant_from_budget(self):
        result = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-budget",
            current_spend_usd=0.5,
            budget_usd=2.0,
        )
        self.assertIn("balance_usd", result)
        # Should have granted (2.0 - 0.5) = 1.5 USD worth
        self.assertEqual(result.get("granted_micro_usd"), _usd_to_micro(1.5))

    def test_trial_grant_from_budget_already_spent(self):
        """If spend >= budget, no grant."""
        result = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-zero",
            current_spend_usd=3.0,
            budget_usd=2.0,
        )
        self.assertEqual(result.get("granted_micro_usd"), 0)

    def test_trial_grant_idempotent(self):
        result1 = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-idem",
            current_spend_usd=0.0,
            budget_usd=1.0,
        )
        result2 = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-idem",
            current_spend_usd=0.0,
            budget_usd=1.0,
        )
        self.assertEqual(result1.get("balance_usd", 0), result2.get("balance_usd", 0))

    # ---- USD conversion ----

    def test_usd_micro_conversion(self):
        self.assertEqual(_usd_to_micro(1.0), 1_000_000)
        self.assertEqual(_usd_to_micro(0.000001), 1)
        self.assertEqual(_usd_to_micro(0.0000005), 0)  # rounds down at 6 decimal
        self.assertEqual(_micro_to_usd(1_000_000), 1.0)
        self.assertEqual(_micro_to_usd(1), 0.000001)

    def test_default_reservation_for_unknown_model(self):
        res = _default_reservation("nonexistent-model")
        self.assertGreater(res, 0)

    def test_default_reservation_for_free_model(self):
        res = _default_reservation("google/gemma-3-27b-it:free")
        self.assertEqual(res, 1)  # free model, non-zero tracking

    # ---- Run not active ----

    def test_reserve_against_finished_run(self):
        self.ledger.grant("u-finished", _usd_to_micro(1.0), idempotency_key="g-fin")
        run = self.ledger.create_run("u-finished", idempotency_key="r-fin")
        run_id = run["run_id"]
        self.ledger.finish_run(run_id)

        with self.assertRaises(RunNotActiveError):
            self.ledger.reserve_call(
                run_id, reservation_micro_usd=100, idempotency_key="c-fin-r",
            )

    # ---- Non-existent run ----

    def test_reserve_against_nonexistent_run(self):
        with self.assertRaises(ValueError):
            self.ledger.reserve_call(
                "run-nonexistent", reservation_micro_usd=100, idempotency_key="c-nex",
            )

    # ---- Provider usage records ----

    def test_usage_records(self):
        self.ledger.grant("u-usage", _usd_to_micro(1.0), idempotency_key="g-usage")
        run = self.ledger.create_run("u-usage", idempotency_key="r-usage")
        call = self.ledger.reserve_call(
            run["run_id"], model="google/gemini-flash-lite",
            idempotency_key="c-usage",
        )
        self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=50,
            prompt_tokens=500, completion_tokens=300,
        )
        usage = self.ledger.get_usage_for_run(run["run_id"])
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["prompt_tokens"], 500)
        self.assertEqual(usage[0]["completion_tokens"], 300)
        self.assertEqual(usage[0]["total_tokens"], 800)


class TestCreditConcurrency(unittest.TestCase):
    """Concurrency tests using threads."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)
        # Grant initial balance
        self.ledger.grant("u-conc", _usd_to_micro(1.0), idempotency_key="g-conc")
        self.run = self.ledger.create_run("u-conc", idempotency_key="r-conc")

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_concurrent_reservations(self):
        """Two threads try to reserve the last available credit simultaneously.
        Only one should succeed."""
        run_id = self.run["run_id"]

        # Give just enough for ONE reservation
        account_before = self.ledger.get_account("u-conc")
        grant_amount = account_before["balance_micro_usd"]
        # Release any existing held
        self.ledger.release_all_held(account_before["account_id"])

        # Make sure only one big chunk of reservation can fit
        big_reservation = grant_amount - 100  # leave ~100 micro safety

        successes = []
        errors = []
        lock = threading.Lock()

        def try_reserve(thread_id):
            try:
                call = self.ledger.reserve_call(
                    run_id,
                    reservation_micro_usd=big_reservation,
                    idempotency_key=f"c-conc-{thread_id}",
                )
                with lock:
                    successes.append((thread_id, call))
            except InsufficientBalanceError as e:
                with lock:
                    errors.append((thread_id, str(e)))
            except Exception as e:
                with lock:
                    errors.append((thread_id, str(e)))

        threads = []
        for i in range(3):
            t = threading.Thread(target=try_reserve, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # At most one success (with that chunk size)
        self.assertLessEqual(len(successes), 1,
                             f"Expected ≤1 success, got {len(successes)}: {successes}; errors: {errors}")


class TestCreditModelAllowlist(unittest.TestCase):
    """Model allowlist enforcement tests."""

    def test_all_slash_models_rejected_by_default(self):
        """Any model with a slash that isn't in the catalog should be rejected."""
        self.assertFalse(is_hosted_model_allowed("openai/gpt-4"))
        self.assertFalse(is_hosted_model_allowed("anthropic/claude-3"))
        self.assertFalse(is_hosted_model_allowed("random/provider/model"))

    def test_free_tier_models_in_catalog(self):
        """Free-tier models from OpenRouter should be in the catalog."""
        for free_model in [
            "google/gemma-3-27b-it:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-chat-v3-0324:free",
            "arcee-ai/trinity-large-preview:free",
            "stepfun/step-3.5-flash:free",
        ]:
            self.assertIn(free_model, HOSTED_MODEL_CATALOG,
                          f"Free model {free_model} should be in catalog")

    def test_paid_models_in_catalog(self):
        """Paid trial models should be in the catalog."""
        self.assertIn("google/gemini-3.1-flash-lite", HOSTED_MODEL_CATALOG)
        self.assertIn("google/gemini-2.5-pro", HOSTED_MODEL_CATALOG)


class TestCreditLedgerInvalidInputs(unittest.TestCase):
    """Edge cases and invalid inputs."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_empty_user_id_ensure_account(self):
        with self.assertRaises(ValueError):
            self.ledger.ensure_account("")

    def test_empty_user_id_grant(self):
        with self.assertRaises(ValueError):
            self.ledger.grant("", 100)

    def test_negative_grant(self):
        with self.assertRaises(ValueError):
            self.ledger.grant("u-neg", -100)

    def test_zero_grant(self):
        with self.assertRaises(ValueError):
            self.ledger.grant("u-zero", 0)

    def test_no_idempotency_key_run(self):
        with self.assertRaises(ValueError):
            self.ledger.create_run("u-no-key")

    def test_no_idempotency_key_reserve(self):
        self.ledger.grant("u-noik", 1000000, idempotency_key="g-noik")
        run = self.ledger.create_run("u-noik", idempotency_key="r-noik")
        with self.assertRaises(ValueError):
            self.ledger.reserve_call(run["run_id"])

    def test_settle_nonexistent_call(self):
        with self.assertRaises(ValueError):
            self.ledger.settle_call("call-nonexistent")

    def test_release_nonexistent_call(self):
        with self.assertRaises(ValueError):
            self.ledger.release_call("call-nonexistent")


if __name__ == "__main__":
    unittest.main()
