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
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from auth import Auth
from credit import (
    CreditLedger,
    HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS,
    HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD,
    HOSTED_MODEL_CATALOG,
    HOSTED_MODEL_POLICY_SETTING_KEY,
    HOSTED_MODEL_POLICY_VERSION,
    InsufficientBalanceError,
    RunNotActiveError,
    _default_reservation,
    _usd_to_micro,
    _micro_to_usd,
    effective_hosted_model_policy,
    hosted_model_credit_allows,
    hosted_model_reservation_policy,
    is_hosted_model_allowed,
    is_hosted_model_allowed_for_identity,
    is_openrouter_model_id,
    normalize_hosted_model_ids,
    validate_hosted_context_budget,
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

    def test_finish_run_releases_unsubmitted_hold(self):
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
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM credit_call_reservations WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "released")

    def test_finish_run_captures_submitted_hold(self):
        self.ledger.grant("u-test-submitted", 1_000, idempotency_key="g-submitted")
        run = self.ledger.create_run(
            "u-test-submitted", idempotency_key="r-submitted"
        )
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=400,
            idempotency_key="c-submitted",
        )
        submitted = self.ledger.mark_call_submitted(call["call_id"])
        self.assertEqual(submitted["status"], "submitted")

        self.ledger.finish_run(run["run_id"], status="cancelled")

        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT status, settled_micro_usd FROM credit_call_reservations "
                "WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
        self.assertEqual(row, ("settled", 400))
        self.assertEqual(self.ledger.get_balance("u-test-submitted"), 600)

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
            run["run_id"], model="google/gemini-3.1-flash-lite",
            idempotency_key="c-def",
        )
        # Default for this model should match catalog (100000 micro-USD)
        expected = HOSTED_MODEL_CATALOG.get("google/gemini-3.1-flash-lite", 100000)
        self.assertEqual(call["reserved_micro_usd"], expected)

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

    def test_settle_overage_uses_unreserved_balance(self):
        # A reported cost higher than the hold is charged when funds remain.
        self.ledger.grant("u-test-18", _usd_to_micro(1.0), idempotency_key="g-cap")
        run = self.ledger.create_run("u-test-18", idempotency_key="r-cap")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-cap",
        )
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=999999,
        )
        self.assertEqual(result["settled_micro_usd"], 999999)
        self.assertEqual(result["overage_micro_usd"], 999899)

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

    def test_release_rejects_submitted_call(self):
        self.ledger.grant("u-sub-rel", 1_000, idempotency_key="g-sub-rel")
        run = self.ledger.create_run("u-sub-rel", idempotency_key="r-sub-rel")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100,
            idempotency_key="c-sub-rel",
        )
        self.ledger.mark_call_submitted(call["call_id"])
        with self.assertRaisesRegex(ValueError, "submitted provider call"):
            self.ledger.release_call(call["call_id"])

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
        """When actual cost is zero but cost is not absent, reservation released."""
        self.ledger.grant("u-test-23", _usd_to_micro(0.20), idempotency_key="g-missing")
        run = self.ledger.create_run("u-test-23", idempotency_key="r-missing")
        call = self.ledger.reserve_call(
            run["run_id"], model="google/gemini-3.1-flash-lite",
            idempotency_key="c-missing",
        )
        # Settle with zero cost (not cost_absent — genuinely zero)
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=0,
        )
        # Still records usage — settled_micro_usd should be 0
        self.assertIsNotNone(result)
        self.assertEqual(result["settled_micro_usd"], 0)

        # Balance after — the 100000 reservation was released
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
        self.assertTrue(
            is_hosted_model_allowed("nvidia/nemotron-3-super-120b-a12b:free")
        )
        self.assertFalse(
            is_hosted_model_allowed("arcee-ai/trinity-large-preview:free")
        )

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

    def test_openrouter_model_id_validation_is_bounded(self):
        self.assertTrue(is_openrouter_model_id("openai/gpt-5.2"))
        self.assertTrue(is_openrouter_model_id("vendor/family/model:free"))
        self.assertFalse(is_openrouter_model_id("missing-slash"))
        self.assertFalse(is_openrouter_model_id("vendor/model with spaces"))
        self.assertFalse(is_openrouter_model_id("vendor//model"))
        self.assertFalse(is_openrouter_model_id("codex-cli:gpt/model"))

    def test_hosted_model_policy_is_exact_for_users_and_open_for_admins(self):
        auth = Auth(self._db_path)
        core = SimpleNamespace(
            _auth=auth,
            ADMIN_EMAILS=["admin@example.com"],
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _OPENROUTER_TRIAL_FALLBACK_MODEL="arcee-ai/trinity-large-preview:free",
            _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS=(
                "arcee-ai/trinity-large-preview:free",
                "stepfun/step-3.5-flash:free",
            ),
        )
        policy = effective_hosted_model_policy(core)
        self.assertIn("arcee-ai/trinity-large-preview:free", policy["models"])
        # Catalog membership alone no longer grants non-admin availability.
        self.assertFalse(
            is_hosted_model_allowed_for_identity(
                core,
                "google/gemini-2.5-pro",
                email="person@example.com",
            )
        )
        self.assertTrue(
            is_hosted_model_allowed_for_identity(
                core,
                "openai/gpt-5.2",
                email="ADMIN@example.com",
            )
        )
        self.assertFalse(
            is_hosted_model_allowed_for_identity(
                core,
                "invalid model/id",
                email="admin@example.com",
            )
        )

        configured = [
            "google/gemini-3.1-flash-lite",
            "arcee-ai/trinity-large-preview:free",
            "stepfun/step-3.5-flash:free",
            "openai/gpt-5.2",
            "deepseek-v4-flash",  # required as the authenticated default
        ]
        auth.set_app_setting(
            HOSTED_MODEL_POLICY_SETTING_KEY,
            {"version": HOSTED_MODEL_POLICY_VERSION, "models": configured},
            updated_by="admin@example.com",
        )
        self.assertTrue(
            is_hosted_model_allowed_for_identity(
                core,
                "openai/gpt-5.2",
                email="person@example.com",
            )
        )
        self.assertEqual(effective_hosted_model_policy(core)["models"], configured)

    def test_hosted_model_policy_requires_operational_models_and_no_duplicates(self):
        required = (
            "google/gemini-3.1-flash-lite",
            "arcee-ai/trinity-large-preview:free",
        )
        with self.assertRaisesRegex(ValueError, "required models"):
            normalize_hosted_model_ids(
                ["google/gemini-3.1-flash-lite"],
                required_models=required,
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            normalize_hosted_model_ids(
                [
                    "google/gemini-3.1-flash-lite",
                    "google/gemini-3.1-flash-lite",
                ]
            )

    def test_auth_app_setting_round_trip_preserves_audit_metadata(self):
        auth = Auth(self._db_path)
        first = auth.set_app_setting(
            "test_setting",
            {"version": 1, "values": ["a"]},
            updated_by="admin@example.com",
        )
        self.assertEqual(auth.get_app_setting("test_setting")["values"], ["a"])
        record = auth.get_app_setting_record("test_setting")
        self.assertEqual(record["updated_by"], "admin@example.com")
        self.assertEqual(record["updated_at"], first["updated_at"])

        auth.set_app_setting(
            "test_setting",
            {"version": 1, "values": ["b"]},
            updated_by="second@example.com",
        )
        self.assertEqual(auth.get_app_setting("test_setting")["values"], ["b"])
        self.assertEqual(
            auth.get_app_setting_record("test_setting")["updated_by"],
            "second@example.com",
        )

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

    def test_hosted_context_budget_requires_current_hold_certification(self):
        validate_hosted_context_budget(HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS)
        with self.assertRaisesRegex(ValueError, "exceeds the credit-hold"):
            validate_hosted_context_budget(
                HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS + 1
            )

        model = "google/gemini-3.1-flash-lite"
        with patch.dict(HOSTED_MODEL_CATALOG, {model: 1}):
            with self.assertRaisesRegex(RuntimeError, "below their certified"):
                validate_hosted_context_budget(
                    HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS
                )

        with patch.dict(HOSTED_MODEL_CATALOG, {"vendor/new-paid-model": 1}):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                validate_hosted_context_budget(
                    HOSTED_HOLD_CERTIFIED_MAX_INTERNAL_CONTEXT_CHARS
                )

        self.assertEqual(
            set(HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD),
            {model for model, hold in HOSTED_MODEL_CATALOG.items() if hold > 0},
        )
        self.assertEqual(
            HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD,
            {
                "google/gemini-3.1-flash-lite": 140_493,
                "google/gemini-3.5-flash-lite": 172_175,
                "google/gemini-2.5-flash-lite": 55_173,
                "google/gemini-2.5-flash": 172_175,
                "google/gemini-2.5-pro": 1_404_925,
                "google/gemini-3-flash-preview": 280_985,
                "qwen/qwen3.6-plus": 710_593,
                "qwen/qwen3.5-flash-02-23": 35_863,
                # DeepSeek now certified against the time-of-use PEAK
                # worst-case rates (Flash $0.44 miss / $1.32 output; Pro
                # $1.32 miss / $3.96 output) while the fixed holds stay
                # $0.25 (Flash) and $1.00 (Pro).
                "deepseek-v4-flash": 240_509,
                "deepseek-v4-pro": 721_526,
            },
        )
        self.assertEqual(HOSTED_MODEL_CATALOG["google/gemini-2.5-pro"], 1_500_000)
        self.assertEqual(HOSTED_MODEL_CATALOG["qwen/qwen3.6-plus"], 750_000)

    def test_default_reservation_for_free_model(self):
        res = _default_reservation("google/gemma-3-27b-it:free")
        self.assertEqual(res, 0)  # free models have zero reservation

    def test_admin_custom_reservation_policy_caps_only_unknown_admin_models(self):
        core = SimpleNamespace(
            _auth=SimpleNamespace(find_user_by_id=lambda _user_id: None),
            ADMIN_EMAILS=["admin@example.com"],
        )
        admin_custom = hosted_model_reservation_policy(
            core,
            "openai/future-model",
            email="ADMIN@example.com",
        )
        non_admin_custom = hosted_model_reservation_policy(
            core,
            "openai/future-model",
            email="person@example.com",
        )
        known_admin_model = hosted_model_reservation_policy(
            core,
            "google/gemini-3.5-flash-lite",
            email="admin@example.com",
        )

        self.assertEqual(admin_custom["reservation_micro_usd"], 1_000_000)
        self.assertTrue(admin_custom["cap_to_available"])
        self.assertTrue(hosted_model_credit_allows(admin_custom, 939_400))
        self.assertFalse(non_admin_custom["cap_to_available"])
        self.assertFalse(hosted_model_credit_allows(non_admin_custom, 939_400))
        self.assertEqual(
            known_admin_model["reservation_micro_usd"],
            HOSTED_MODEL_CATALOG["google/gemini-3.5-flash-lite"],
        )
        self.assertFalse(known_admin_model["cap_to_available"])

    def test_admin_custom_reservation_atomically_uses_remaining_balance(self):
        self.ledger.grant("u-admin-cap", 939_400, idempotency_key="g-admin-cap")
        run = self.ledger.create_run("u-admin-cap", idempotency_key="r-admin-cap")
        call = self.ledger.reserve_call(
            run["run_id"],
            model="openai/future-model",
            reservation_micro_usd=1_000_000,
            idempotency_key="c-admin-cap",
            cap_reservation_to_available=True,
        )

        self.assertEqual(call["reserved_micro_usd"], 939_400)
        self.assertEqual(call["nominal_reservation_micro_usd"], 1_000_000)
        self.assertTrue(call["reservation_capped"])
        replay = self.ledger.reserve_call(
            run["run_id"],
            model="openai/future-model",
            reservation_micro_usd=1_000_000,
            idempotency_key="c-admin-cap",
            cap_reservation_to_available=True,
        )
        self.assertEqual(replay["reserved_micro_usd"], 939_400)
        with self.assertRaises(InsufficientBalanceError):
            self.ledger.reserve_call(
                run["run_id"],
                model="openai/future-model",
                reservation_micro_usd=1_000_000,
                idempotency_key="c-admin-cap-concurrent",
                cap_reservation_to_available=True,
            )

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

    def test_concurrent_settle_release_finish_has_one_terminal_charge(self):
        run = self.ledger.create_run("u-conc", idempotency_key="r-terminal-race")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100_000,
            idempotency_key="c-terminal-race",
        )
        self.ledger.mark_call_submitted(call["call_id"])
        barrier = threading.Barrier(3)
        errors = []

        def settle():
            try:
                barrier.wait()
                self.ledger.settle_call(
                    call["call_id"], actual_cost_micro_usd=50_000
                )
            except Exception as exc:
                errors.append(type(exc).__name__)

        def release():
            try:
                barrier.wait()
                self.ledger.release_call(call["call_id"])
            except Exception as exc:
                errors.append(type(exc).__name__)

        def finish():
            try:
                barrier.wait()
                self.ledger.finish_run(run["run_id"], status="cancelled")
            except Exception as exc:
                errors.append(type(exc).__name__)

        threads = [
            threading.Thread(target=settle),
            threading.Thread(target=release),
            threading.Thread(target=finish),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with closing(sqlite3.connect(self._db_path)) as conn:
            call_row = conn.execute(
                "SELECT status FROM credit_call_reservations WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
            settlement_count = conn.execute(
                "SELECT COUNT(*) FROM credit_ledger "
                "WHERE call_id = ? AND entry_type = 'settlement'",
                (call["call_id"],),
            ).fetchone()[0]
        self.assertEqual(call_row[0], "settled")
        self.assertEqual(settlement_count, 1)
        self.assertIn(self.ledger.get_balance("u-conc"), (900_000, 950_000))
        self.assertTrue(errors)  # losing terminal transitions fail safely


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
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
            "poolside/laguna-xs-2.1:free",
        ]:
            self.assertIn(free_model, HOSTED_MODEL_CATALOG,
                          f"Free model {free_model} should be in catalog")

    def test_paid_models_in_catalog(self):
        """Paid trial models should be in the catalog."""
        self.assertIn("google/gemini-3.1-flash-lite", HOSTED_MODEL_CATALOG)
        self.assertIn("google/gemini-3.5-flash-lite", HOSTED_MODEL_CATALOG)
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


# ---------------------------------------------------------------------------
# Stale-run sweep tests
# ---------------------------------------------------------------------------

class TestCreditSweep(unittest.TestCase):
    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_sweep_expires_stale_runs(self):
        """Stale active runs should be expired to exhausted, releasing held calls."""
        self.ledger.grant("u-sweep", _usd_to_micro(1.0), idempotency_key="g-sweep")
        run = self.ledger.create_run("u-sweep", idempotency_key="r-sweep")
        run_id = run["run_id"]

        # Reserve a call
        call = self.ledger.reserve_call(
            run_id, reservation_micro_usd=100, idempotency_key="c-sweep",
        )
        self.assertEqual(call["status"], "held")

        # Sweep with a very short TTL (0 => use default, but we need immediate)
        # Manually set created_at to old via direct SQL to force sweep
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                "UPDATE credit_runs SET created_at = ? WHERE run_id = ?",
                (time.time() - 999999, run_id),
            )
            conn.commit()

        # Now sweep — should expire it
        expired = self.ledger.sweep_stale_runs(ttl_seconds=1)
        self.assertGreaterEqual(expired, 1)

        # Run should be exhausted
        run_info = self.ledger.get_run(run_id)
        self.assertEqual(run_info["status"], "exhausted")

        # Call should be released
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM credit_call_reservations WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
        self.assertEqual(row[0], "released")

    def test_sweep_skips_already_finished(self):
        """Sweep should not touch runs that are already completed."""
        self.ledger.grant("u-sweep2", _usd_to_micro(1.0), idempotency_key="g-sweep2")
        run = self.ledger.create_run("u-sweep2", idempotency_key="r-sweep2")
        self.ledger.finish_run(run["run_id"], status="completed")

        # Make it look old
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                "UPDATE credit_runs SET created_at = ? WHERE run_id = ?",
                (time.time() - 999999, run["run_id"]),
            )
            conn.commit()

        expired = self.ledger.sweep_stale_runs(ttl_seconds=1)
        self.assertEqual(expired, 0)  # Already finished, skipped

    def test_sweep_captures_submitted_and_releases_unsubmitted(self):
        self.ledger.grant("u-sweep-mixed", 1_000, idempotency_key="g-sweep-mixed")
        run = self.ledger.create_run(
            "u-sweep-mixed", idempotency_key="r-sweep-mixed"
        )
        submitted = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=300,
            idempotency_key="c-sweep-submitted",
        )
        unsubmitted = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=200,
            idempotency_key="c-sweep-unsubmitted",
        )
        self.ledger.mark_call_submitted(submitted["call_id"])
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                "UPDATE credit_runs SET created_at = ? WHERE run_id = ?",
                (time.time() - 999999, run["run_id"]),
            )
            conn.commit()

        self.assertEqual(self.ledger.sweep_stale_runs(ttl_seconds=1), 1)
        with closing(sqlite3.connect(self._db_path)) as conn:
            rows = dict(conn.execute(
                "SELECT call_id, status FROM credit_call_reservations "
                "WHERE run_id = ?",
                (run["run_id"],),
            ).fetchall())
        self.assertEqual(rows[submitted["call_id"]], "settled")
        self.assertEqual(rows[unsubmitted["call_id"]], "released")
        self.assertEqual(self.ledger.get_balance("u-sweep-mixed"), 700)

    def test_sweep_idempotent(self):
        """Multiple sweeps should be safe (idempotent)."""
        self.ledger.grant("u-sweep3", _usd_to_micro(1.0), idempotency_key="g-sweep3")
        run = self.ledger.create_run("u-sweep3", idempotency_key="r-sweep3")
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                "UPDATE credit_runs SET created_at = ? WHERE run_id = ?",
                (time.time() - 999999, run["run_id"]),
            )
            conn.commit()

        expired1 = self.ledger.sweep_stale_runs(ttl_seconds=1)
        expired2 = self.ledger.sweep_stale_runs(ttl_seconds=1)
        self.assertEqual(expired2, 0)  # Second sweep finds nothing new


# ---------------------------------------------------------------------------
# Terminal run finish tests
# ---------------------------------------------------------------------------

class TestCreditTerminalFinish(unittest.TestCase):
    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_finish_run_releases_held_and_marks_terminal(self):
        """Finishing a run releases held calls and marks it completed."""
        self.ledger.grant("u-term", _usd_to_micro(1.0), idempotency_key="g-term")
        run = self.ledger.create_run("u-term", idempotency_key="r-term")
        run_id = run["run_id"]

        call = self.ledger.reserve_call(
            run_id, reservation_micro_usd=100, idempotency_key="c-term",
        )
        self.assertEqual(call["status"], "held")

        result = self.ledger.finish_run(run_id)
        self.assertEqual(result["status"], "completed")

        # Call released
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM credit_call_reservations WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
        self.assertEqual(row[0], "released")

    def test_finish_run_idempotent_multiple_calls(self):
        """Calling finish_run repeatedly is safe."""
        self.ledger.grant("u-term2", _usd_to_micro(1.0), idempotency_key="g-term2")
        run = self.ledger.create_run("u-term2", idempotency_key="r-term2")

        self.ledger.finish_run(run["run_id"], status="completed")
        result = self.ledger.finish_run(run["run_id"], status="cancelled")  # try to change
        self.assertTrue(result.get("already_finished"))
        self.assertEqual(result["status"], "completed")  # unchanged

    def test_finish_run_as_cancelled(self):
        """Dispatch failure: finish run as cancelled."""
        self.ledger.grant("u-cancel", _usd_to_micro(1.0), idempotency_key="g-cancel")
        run = self.ledger.create_run("u-cancel", idempotency_key="r-cancel")

        self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100, idempotency_key="c-cancel",
        )
        result = self.ledger.finish_run(run["run_id"], status="cancelled")
        self.assertEqual(result["status"], "cancelled")

    def test_finish_run_as_exhausted(self):
        """Budget exhausted finish."""
        self.ledger.grant("u-exh", _usd_to_micro(1.0), idempotency_key="g-exh")
        run = self.ledger.create_run("u-exh", idempotency_key="r-exh")
        result = self.ledger.finish_run(run["run_id"], status="exhausted")
        self.assertEqual(result["status"], "exhausted")


# ---------------------------------------------------------------------------
# TOCTOU-safe grant concurrency tests
# ---------------------------------------------------------------------------

class TestCreditTOCTOU(unittest.TestCase):
    """Verify that concurrent grants use the same BEGIN IMMEDIATE connection
    and do not produce stale balance returns."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_concurrent_grants_no_stale_balance(self):
        """Multiple threads granting simultaneously should sum correctly."""
        num_threads = 5
        grant_amount = _usd_to_micro(0.1)  # 100000 micro each
        errors = []
        done = threading.Event()

        def grant_thread(i):
            try:
                self.ledger.grant("u-toctou", grant_amount, idempotency_key=f"g-toc-{i}")
            except Exception as e:
                errors.append(str(e))
            finally:
                done.set()

        threads = [threading.Thread(target=grant_thread, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No errors
        self.assertEqual(len(errors), 0)

        # Balance should be exactly num_threads * grant_amount
        balance = self.ledger.get_balance("u-toctou")
        self.assertEqual(balance, num_threads * grant_amount,
                         f"Expected {num_threads * grant_amount}, got {balance}")

    def test_grant_and_reverse_no_stale(self):
        """Interleaved grant and reversal should not produce stale returns."""
        self.ledger.grant("u-toc2", _usd_to_micro(1.0), idempotency_key="g-toc-init")
        balance = self.ledger.get_balance("u-toc2")
        self.assertEqual(balance, _usd_to_micro(1.0))

        # Reverse half
        self.ledger.reverse_grant("u-toc2", _usd_to_micro(0.5), idempotency_key="r-toc-half")
        balance = self.ledger.get_balance("u-toc2")
        self.assertEqual(balance, _usd_to_micro(0.5))


# ---------------------------------------------------------------------------
# Service token isolation tests
# ---------------------------------------------------------------------------

class TestCreditServiceToken(unittest.TestCase):
    """Verify credit_service_token does not use PRIVATE_CORE_TOKEN."""

    def setUp(self):
        self._saved_env = {}
        for key in ("HOSTED_AGENT_SERVICE_TOKEN", "CREDIT_SERVICE_TOKEN",
                     "TRIAL_AGENT_KEY",
                     "PRIVATE_CORE_TOKEN", "RELAY_SHARED_TOKEN"):
            self._saved_env[key] = os.environ.get(key, "__UNSET__")

    def tearDown(self):
        for key, val in self._saved_env.items():
            if val == "__UNSET__":
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_credit_service_token_prefers_hosted(self):
        """HOSTED_AGENT_SERVICE_TOKEN takes top precedence."""
        os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = "hosted-token-aaa"
        os.environ["CREDIT_SERVICE_TOKEN"] = "credit-secret-123"
        os.environ["TRIAL_AGENT_KEY"] = "trial-key-456"
        os.environ["PRIVATE_CORE_TOKEN"] = "private-core-789"

        from credit import credit_service_token
        token = credit_service_token()
        self.assertEqual(token, "hosted-token-aaa")

    def test_credit_service_token_does_not_fall_back_to_credit(self):
        """Legacy CREDIT_SERVICE_TOKEN cannot authorize hosted callbacks."""
        os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        os.environ["CREDIT_SERVICE_TOKEN"] = "credit-secret-123"
        os.environ["TRIAL_AGENT_KEY"] = "trial-key-456"
        os.environ["PRIVATE_CORE_TOKEN"] = "private-core-789"

        from credit import credit_service_token
        token = credit_service_token()
        self.assertEqual(token, "")
        self.assertNotEqual(token, "private-core-789")

    def test_credit_service_token_does_not_fall_back_to_trial_agent_key(self):
        """The trial-agent WebSocket key cannot authorize credit callbacks."""
        os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        os.environ.pop("CREDIT_SERVICE_TOKEN", None)
        os.environ["TRIAL_AGENT_KEY"] = "trial-key-fallback"
        os.environ["PRIVATE_CORE_TOKEN"] = "private-core-should-not-use"

        from credit import credit_service_token
        token = credit_service_token()
        self.assertEqual(token, "")
        self.assertNotEqual(token, "private-core-should-not-use")

    def test_credit_service_token_never_uses_private_core(self):
        """Even when all service tokens are unset, never fall through to
        PRIVATE_CORE_TOKEN."""
        os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        os.environ.pop("CREDIT_SERVICE_TOKEN", None)
        os.environ.pop("TRIAL_AGENT_KEY", None)
        os.environ["PRIVATE_CORE_TOKEN"] = "private-core-alone"

        from credit import credit_service_token
        token = credit_service_token()
        self.assertEqual(token, "")  # empty, not "private-core-alone"


# ---------------------------------------------------------------------------
# Trial grant error handling tests
# ---------------------------------------------------------------------------

class TestCreditTrialGrantSafety(unittest.TestCase):
    """Verify trial grant does not swallow programming errors."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_trial_grant_handles_idempotency_gracefully(self):
        """First call grants; second call returns already-granted without error."""
        r1 = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-safe", current_spend_usd=0, budget_usd=1.0,
        )
        self.assertGreater(r1.get("granted_micro_usd", 0), 0)

        r2 = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-safe", current_spend_usd=0, budget_usd=1.0,
        )
        self.assertEqual(r2.get("granted_micro_usd", -1), 0)

    def test_trial_grant_returns_balance_on_constraint(self):
        """After a grant, ensure_trial_grant returns sensible state."""
        result = self.ledger.ensure_trial_grant_from_openrouter_budget(
            "u-trial-state", current_spend_usd=0.25, budget_usd=1.0,
        )
        # (1.0 - 0.25) = 0.75 USD granted
        self.assertAlmostEqual(result.get("balance_usd", 0), 0.75, places=5)


# ---------------------------------------------------------------------------
# Ledger invariant tests
# ---------------------------------------------------------------------------


class TestCreditLedgerInvariant(unittest.TestCase):
    """Verify the append-only ledger reconstructs balance correctly."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_reconstruct_balance_from_ledger(self):
        """sum(grant + reversal + settlement + fee) == cached balance."""
        self.ledger.grant("u-inv", _usd_to_micro(2.0), idempotency_key="g-inv1")
        run = self.ledger.create_run("u-inv", idempotency_key="r-inv1")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=100_000, idempotency_key="c-inv1",
        )
        self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=50_000)
        self.ledger.reverse_grant("u-inv", _usd_to_micro(0.5), idempotency_key="rev-inv1")

        acct = self.ledger.get_account("u-inv")
        reconstructed = self.ledger.reconstruct_balance(acct["account_id"])
        self.assertEqual(reconstructed, acct["balance_micro_usd"],
                         f"ledger reconstruction {reconstructed} != cached {acct['balance_micro_usd']}")

    def test_reservation_release_ignored_by_reconstruction(self):
        """Reservations and releases are holds, not monetary events."""
        self.ledger.grant("u-inv2", _usd_to_micro(1.0), idempotency_key="g-inv2")
        run = self.ledger.create_run("u-inv2", idempotency_key="r-inv2")

        # Reserve then release — neither should change the reconstructed balance
        acct_before = self.ledger.get_account("u-inv2")
        bal_before = acct_before["balance_micro_usd"]

        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=50_000, idempotency_key="c-inv2",
        )
        # Balance unchanged (reservation is a hold)
        acct_after_reserve = self.ledger.get_account("u-inv2")
        self.assertEqual(acct_after_reserve["balance_micro_usd"], bal_before)

        self.ledger.release_call(call["call_id"])
        # Balance still unchanged after release
        acct_after_release = self.ledger.get_account("u-inv2")
        self.assertEqual(acct_after_release["balance_micro_usd"], bal_before)

        # Ledger reconstruction matches
        reconstructed = self.ledger.reconstruct_balance(acct_after_release["account_id"])
        self.assertEqual(reconstructed, bal_before)

    def test_reserve_settle_reconstruct(self):
        """Full reserve→settle cycle: reconstruction equals balance."""
        self.ledger.grant("u-inv3", _usd_to_micro(1.0), idempotency_key="g-inv3")
        run = self.ledger.create_run("u-inv3", idempotency_key="r-inv3")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=300_000, idempotency_key="c-inv3",
        )
        self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=150_000)

        acct = self.ledger.get_account("u-inv3")
        reconstructed = self.ledger.reconstruct_balance(acct["account_id"])
        # 1,000,000 - 150,000 = 850,000
        expected = _usd_to_micro(0.85)
        self.assertEqual(reconstructed, expected)
        self.assertEqual(reconstructed, acct["balance_micro_usd"])


# ---------------------------------------------------------------------------
# Free-model zero reservation tests
# ---------------------------------------------------------------------------


class TestCreditFreeModels(unittest.TestCase):
    """Free-tier models reserve and settle zero."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_free_model_default_reservation_zero(self):
        """':free' models get zero hold from catalog."""
        from credit import _default_reservation
        for free_model in (
            "google/gemma-3-27b-it:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "deepseek/deepseek-chat:free",
        ):
            with self.subTest(model=free_model):
                self.assertEqual(_default_reservation(free_model), 0,
                                 f"Free model {free_model} should have zero reservation")

    def test_free_model_reserve_and_settle_zero(self):
        """Reserving a free model reserves and settles at zero."""
        self.ledger.grant("u-free", _usd_to_micro(0.01), idempotency_key="g-free")
        run = self.ledger.create_run("u-free", idempotency_key="r-free")
        call = self.ledger.reserve_call(
            run["run_id"], model="google/gemma-3-27b-it:free",
            reservation_micro_usd=0, idempotency_key="c-free",
        )
        self.assertEqual(call["reserved_micro_usd"], 0)
        result = self.ledger.settle_call(call["call_id"], actual_cost_micro_usd=0)
        self.assertEqual(result["settled_micro_usd"], 0)


# ---------------------------------------------------------------------------
# Settlement missing-cost tests
# ---------------------------------------------------------------------------


class TestCreditSettlementCost(unittest.TestCase):
    """Settle a call with missing vs. zero cost."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_cost_absent_settles_full_reservation(self):
        """When cost is absent, the full reservation is settled."""
        self.ledger.grant("u-costabs", _usd_to_micro(0.02), idempotency_key="g-ca")
        run = self.ledger.create_run("u-costabs", idempotency_key="r-ca")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=10_000, idempotency_key="c-ca",
        )
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=0, cost_absent=True,
        )
        # Full 10_000 micro settled
        self.assertEqual(result["settled_micro_usd"], 10_000)
        self.assertEqual(result["released_micro_usd"], 0)
        self.assertTrue(result.get("cost_absent"))

    def test_cost_present_zero_settles_zero(self):
        """Reported zero cost genuinely settles zero (not absent)."""
        self.ledger.grant("u-costzero", _usd_to_micro(0.02), idempotency_key="g-cz")
        run = self.ledger.create_run("u-costzero", idempotency_key="r-cz")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=10_000, idempotency_key="c-cz",
        )
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=0, cost_absent=False,
        )
        self.assertEqual(result["settled_micro_usd"], 0)
        self.assertEqual(result["released_micro_usd"], 10_000)

    def test_cost_absent_rejects_conflicting_actual_cost(self):
        self.ledger.grant("u-cost-conflict", 20_000, idempotency_key="g-conflict")
        run = self.ledger.create_run(
            "u-cost-conflict", idempotency_key="r-conflict"
        )
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=10_000,
            idempotency_key="c-conflict",
        )
        with self.assertRaisesRegex(ValueError, "must be zero"):
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=1,
                cost_absent=True,
            )

    def test_provider_cost_over_hold_is_charged_when_funded(self):
        """Provider cost exceeding the hold consumes available balance."""
        self.ledger.grant("u-disc", _usd_to_micro(0.02), idempotency_key="g-disc")
        run = self.ledger.create_run("u-disc", idempotency_key="r-disc")
        call = self.ledger.reserve_call(
            run["run_id"], reservation_micro_usd=5_000, idempotency_key="c-disc",
        )
        result = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=7_000,
            provider_cost_micro_usd=7_000,
        )
        self.assertEqual(result["settled_micro_usd"], 7_000)
        self.assertEqual(result["overage_micro_usd"], 2_000)
        self.assertEqual(result["discrepancy_micro_usd"], 0)
        # Provider cost in usage table is the actual reported value
        usage = self.ledger.get_usage_for_run(run["run_id"])
        self.assertEqual(usage[0]["provider_cost_micro_usd"], 7_000)


# ---------------------------------------------------------------------------
# Conservative reservation tests
# ---------------------------------------------------------------------------


class TestCreditConservativeReservations(unittest.TestCase):
    """Verify catalog entries are meaningfully conservative."""

    def test_paid_models_minimum_cents(self):
        """Paid models have reservation >= $0.01 (10,000 micro-USD)."""
        from credit import HOSTED_MODEL_CATALOG as cat
        for model, res in cat.items():
            if model.endswith(":free"):
                continue
            self.assertGreaterEqual(
                res, 10_000,
                f"Paid model {model} has reservation {res} < 10k micro-USD ($0.01)",
            )

    def test_free_models_zero(self):
        """Free models have exactly zero reservation."""
        from credit import HOSTED_MODEL_CATALOG as cat
        for model, res in cat.items():
            if model.endswith(":free"):
                self.assertEqual(res, 0,
                                 f"Free model {model} has non-zero reservation {res}")

    def test_catalog_includes_all_ui_models(self):
        """All models exposed by /trial are in the catalog."""
        from credit import HOSTED_MODEL_CATALOG as cat
        expected_models = {
            "google/gemini-3.1-flash-lite",
            "google/gemini-2.5-flash-lite",
            "google/gemini-2.5-flash",
            "google/gemini-2.5-pro",
            "google/gemini-3-flash-preview",
            "qwen/qwen3.6-plus",
            "qwen/qwen3.5-flash-02-23",
            "nvidia/nemotron-3-super-120b-a12b:free",
        }
        for m in expected_models:
            self.assertIn(m, cat, f"Model {m} missing from HOSTED_MODEL_CATALOG")


# ---------------------------------------------------------------------------
# CreditLedger context manager tests
# ---------------------------------------------------------------------------


class TestCreditLedgerContextManager(unittest.TestCase):
    """CreditLedger implements the context manager protocol."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_context_manager_works(self):
        """with CreditLedger(...) as ledger: ... works."""
        from credit import CreditLedger as CL
        # Clear the process-level cache for this test
        CL._instances.pop(self._db_path, None)
        try:
            with CL(db_path=self._db_path) as ledger:
                acct = ledger.ensure_account("u-ctxmgr")
                self.assertEqual(acct["user_id"], "u-ctxmgr")
        finally:
            CL._instances.pop(self._db_path, None)

    def test_cached_instance_reuses_schema(self):
        """Calling CreditLedger twice with same path returns same instance."""
        from credit import CreditLedger as CL
        CL._instances.pop(self._db_path, None)
        try:
            l1 = CL(db_path=self._db_path)
            l2 = CL(db_path=self._db_path)
            self.assertIs(l1, l2, "Same db_path should return cached instance")
        finally:
            CL._instances.pop(self._db_path, None)

    def test_reconstruct_balance_empty_account(self):
        """Empty account with no ledger entries reconstructs to zero."""
        ledger = CreditLedger(db_path=self._db_path)
        acct = ledger.ensure_account("u-empty")
        reconstructed = ledger.reconstruct_balance(acct["account_id"])
        self.assertEqual(reconstructed, 0)

    def test_existing_reservation_table_migrates_submitted_at(self):
        from credit import CreditLedger as CL

        CL._instances.pop(self._db_path, None)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE credit_call_reservations (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'held',
                    reserved_micro_usd INTEGER NOT NULL,
                    settled_micro_usd INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    settled_at REAL,
                    released_at REAL
                )
            """)
            conn.commit()

        try:
            CL(self._db_path)
            with closing(sqlite3.connect(self._db_path)) as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(credit_call_reservations)"
                    ).fetchall()
                }
            self.assertIn("submitted_at", columns)
        finally:
            CL._instances.pop(self._db_path, None)

    def test_existing_provider_usage_table_migrates_pricing_columns(self):
        """Pre-existing credit_provider_usage tables gain the nullable pricing
        audit columns (historical rows stay NULL and are never repriced)."""
        from credit import CreditLedger as CL

        CL._instances.pop(self._db_path, None)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE credit_provider_usage (
                    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    provider_cost_micro_usd INTEGER NOT NULL DEFAULT 0,
                    provider_response_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO credit_provider_usage "
                "(call_id, run_id, account_id, model, created_at) "
                "VALUES ('call-old', 'run-old', 'acct-old', 'deepseek-v4-flash', 1.0)"
            )
            conn.commit()

        try:
            CL(self._db_path)
            with closing(sqlite3.connect(self._db_path)) as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(credit_provider_usage)"
                    ).fetchall()
                }
                row = conn.execute(
                    "SELECT pricing_schedule_version, pricing_tier, "
                    "pricing_basis_ts, input_cache_hit_rate_micro_usd_per_million "
                    "FROM credit_provider_usage WHERE call_id = 'call-old'"
                ).fetchone()
            for column in (
                "pricing_schedule_version",
                "pricing_tier",
                "pricing_basis_ts",
                "input_cache_hit_rate_micro_usd_per_million",
                "input_cache_miss_rate_micro_usd_per_million",
                "output_rate_micro_usd_per_million",
            ):
                self.assertIn(column, columns)
            # Historical row is not repriced: all audit fields remain NULL.
            self.assertIsNone(row[0])
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])
            self.assertIsNone(row[3])
        finally:
            CL._instances.pop(self._db_path, None)

    def test_existing_balance_snapshot_table_migrates_component_columns(self):
        """Pre-existing provider_balance_snapshots tables gain nullable
        granted/topped-up component balance columns."""
        from credit import CreditLedger as CL

        CL._instances.pop(self._db_path, None)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE provider_balance_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total_balance REAL NOT NULL DEFAULT 0,
                    is_available INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            conn.commit()

        try:
            CL(self._db_path)
            with closing(sqlite3.connect(self._db_path)) as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(provider_balance_snapshots)"
                    ).fetchall()
                }
            self.assertIn("granted_balance", columns)
            self.assertIn("topped_up_balance", columns)
        finally:
            CL._instances.pop(self._db_path, None)


# ---------------------------------------------------------------------------
# Service token hierarchy tests
# ---------------------------------------------------------------------------


class TestCreditServiceTokenFull(unittest.TestCase):
    """Only HOSTED_AGENT_SERVICE_TOKEN is accepted."""

    def setUp(self):
        self._saved = {}
        for k in ("HOSTED_AGENT_SERVICE_TOKEN", "CREDIT_SERVICE_TOKEN",
                   "TRIAL_AGENT_KEY", "PRIVATE_CORE_TOKEN"):
            self._saved[k] = os.environ.get(k, "__UNSET__")

    def tearDown(self):
        for k, v in self._saved.items():
            if v == "__UNSET__":
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_empty_when_all_unset(self):
        """No token is set → return empty string."""
        for k in ("HOSTED_AGENT_SERVICE_TOKEN", "CREDIT_SERVICE_TOKEN",
                   "TRIAL_AGENT_KEY"):
            os.environ.pop(k, None)
        from credit import credit_service_token
        self.assertEqual(credit_service_token(), "")

    def test_fallback_from_trial_to_empty(self):
        """CREDIT_SERVICE_TOKEN fallback; TRIAL fallback; no PRIVATE_CORE."""
        os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        os.environ.pop("CREDIT_SERVICE_TOKEN", None)
        os.environ.pop("TRIAL_AGENT_KEY", None)
        os.environ["PRIVATE_CORE_TOKEN"] = "pct"
        from credit import credit_service_token
        self.assertEqual(credit_service_token(), "")

    def test_hierarchy_all_set(self):
        """When all are set, HOSTED wins."""
        os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = "h"
        os.environ["CREDIT_SERVICE_TOKEN"] = "c"
        os.environ["TRIAL_AGENT_KEY"] = "t"
        from credit import credit_service_token
        self.assertEqual(credit_service_token(), "h")


# ---------------------------------------------------------------------------
# DeepSeek direct provider tests
# ---------------------------------------------------------------------------


class TestDeepSeekProvider(unittest.TestCase):
    """DeepSeek model IDs, catalog defaults, metering, and reconciliation."""

    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.ledger = CreditLedger(db_path=self._db_path)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_deepseek_model_id_validation(self):
        from credit import is_deepseek_model_id, is_hosted_model_id
        self.assertTrue(is_deepseek_model_id("deepseek-v4-flash"))
        self.assertTrue(is_deepseek_model_id("deepseek-v4-pro"))
        self.assertFalse(is_deepseek_model_id("deepseek-"))
        self.assertFalse(is_deepseek_model_id("openai/gpt-5.2"))
        self.assertFalse(is_deepseek_model_id("deepseek v4"))
        self.assertTrue(is_hosted_model_id("deepseek-v4-flash"))
        self.assertTrue(is_hosted_model_id("openai/gpt-5.2"))

    def test_deepseek_catalog_and_defaults(self):
        from credit import HOSTED_USER_MODEL_DEFAULTS
        self.assertIn("deepseek-v4-flash", HOSTED_MODEL_CATALOG)
        self.assertIn("deepseek-v4-pro", HOSTED_MODEL_CATALOG)
        self.assertGreater(HOSTED_MODEL_CATALOG["deepseek-v4-pro"],
                           HOSTED_MODEL_CATALOG["deepseek-v4-flash"])
        self.assertIn("deepseek-v4-flash", HOSTED_USER_MODEL_DEFAULTS)
        self.assertIn("deepseek-v4-pro", HOSTED_USER_MODEL_DEFAULTS)
        self.assertNotEqual(_default_reservation("deepseek-v4-flash"), 0)

    def test_hosted_policy_accepts_deepseek_defaults(self):
        auth = Auth(self._db_path)
        core = SimpleNamespace(
            _auth=auth,
            ADMIN_EMAILS=[],
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _OPENROUTER_TRIAL_FALLBACK_MODEL="nvidia/nemotron-3-super-120b-a12b:free",
            _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS=(
                "nvidia/nemotron-3-super-120b-a12b:free",
            ),
        )
        policy = effective_hosted_model_policy(core)
        self.assertIn("deepseek-v4-flash", policy["models"])
        self.assertIn("deepseek-v4-pro", policy["models"])
        self.assertTrue(
            is_hosted_model_allowed_for_identity(
                core, "deepseek-v4-flash", email="person@example.com"
            )
        )
        # Slash-free deepseek IDs must pass the policy validator.
        normalize_hosted_model_ids(
            ["google/gemini-3.1-flash-lite", "deepseek-v4-flash"]
        )

    def test_settle_stores_cache_tokens_and_metering(self):
        self.ledger.grant("u-ds-1", _usd_to_micro(2.0), idempotency_key="g-ds-1")
        run = self.ledger.create_run("u-ds-1", idempotency_key="r-ds-1")
        call = self.ledger.reserve_call(
            run["run_id"], model="deepseek-v4-flash", idempotency_key="c-ds-1",
        )
        result = self.ledger.settle_call(
            call["call_id"],
            actual_cost_micro_usd=50_000,
            prompt_tokens=10_000,
            completion_tokens=2_000,
            total_tokens=12_000,
            prompt_cache_hit_tokens=9_000,
            prompt_cache_miss_tokens=1_000,
            provider_cost_micro_usd=50_000,
        )
        self.assertEqual(result["prompt_cache_hit_tokens"], 9_000)
        self.assertEqual(result["prompt_cache_miss_tokens"], 1_000)

        meter = self.ledger.provider_usage_since(0)
        self.assertEqual(meter["prompt_cache_hit_tokens"], 9_000)
        self.assertEqual(meter["prompt_cache_miss_tokens"], 1_000)
        self.assertEqual(meter["completion_tokens"], 2_000)
        self.assertEqual(meter["total_tokens"], 12_000)
        self.assertEqual(meter["estimated_cost_micro_usd"], 50_000)

        series = self.ledger.provider_usage_series(window_seconds=600, since_ts=0)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["prompt_cache_miss_tokens"], 1_000)

        usage_rows = self.ledger.get_usage_for_run(run["run_id"])
        self.assertEqual(len(usage_rows), 1)
        self.assertEqual(usage_rows[0]["prompt_cache_hit_tokens"], 9_000)

    def test_balance_snapshot_and_reconciliation(self):
        now = time.time()
        # Real flow: snapshot → spend settles → snapshot. The reconciliation
        # window spans the first and last snapshots, which includes the spend.
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=11.32, is_available=True, snapshot_at=now - 100.0,
        )
        self.ledger.grant("u-ds-2", _usd_to_micro(5.0), idempotency_key="g-ds-2")
        run = self.ledger.create_run("u-ds-2", idempotency_key="r-ds-2")
        call = self.ledger.reserve_call(
            run["run_id"], model="deepseek-v4-flash", idempotency_key="c-ds-2",
        )
        self.ledger.settle_call(
            call["call_id"],
            actual_cost_micro_usd=500_000,
            prompt_tokens=10_000,
            completion_tokens=0,
            total_tokens=10_000,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=10_000,
            provider_cost_micro_usd=500_000,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=10.02, is_available=True, snapshot_at=now + 10.0,
        )
        latest = self.ledger.latest_provider_balance_snapshot("deepseek")
        self.assertEqual(latest["currency"], "USD")
        self.assertEqual(latest["total_balance"], 10.02)
        self.assertTrue(latest["is_available"])
        self.assertIsNone(self.ledger.latest_provider_balance_snapshot("openrouter"))

        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertFalse(report["stale"])
        # 11.32 - 10.02 = $1.30 actual vs $0.50 estimated → ~61% drift
        self.assertAlmostEqual(report["actual_spend_usd"], 1.30, places=6)
        self.assertAlmostEqual(report["estimated_spend_usd"], 0.50, places=6)
        self.assertGreater(report["drift_percent"], 50.0)
        self.assertFalse(report["balance_increased"])

    def test_reconciliation_stale_without_snapshots(self):
        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertTrue(report["stale"])

    def test_reconciliation_skips_topup_segments(self):
        """A balance increase (top-up/grant) must not contaminate spend math."""
        now = time.time()
        # Segment 1: real spend 11.32 -> 10.02 ($1.30) at now-100..now-90.
        # Segment 2: top-up back up to 15.00 (skipped).
        # Segment 3: spend 15.00 -> 14.50 ($0.50) at now-10..now+10.
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=11.32, is_available=True, snapshot_at=now - 100.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=10.02, is_available=True, snapshot_at=now - 90.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=15.00, is_available=True, snapshot_at=now - 50.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=14.50, is_available=True, snapshot_at=now + 10.0,
        )
        # Ledger spend in the two spend segments: $0.30 + $0.10 = $0.40.
        for i, (amt, lo, hi) in enumerate([
            (300_000, now - 100.0, now - 90.0),
            (100_000, now - 10.0, now + 10.0),
        ]):
            self.ledger.grant(f"u-ds-{i}", _usd_to_micro(5.0), idempotency_key=f"g-ds-t{i}")
            run = self.ledger.create_run(f"u-ds-{i}", idempotency_key=f"r-ds-t{i}")
            call = self.ledger.reserve_call(
                run["run_id"], model="deepseek-v4-flash", idempotency_key=f"c-ds-t{i}",
            )
            self.ledger.settle_call(
                call["call_id"],
                actual_cost_micro_usd=amt,
                prompt_tokens=10_000,
                completion_tokens=0,
                total_tokens=10_000,
                provider_cost_micro_usd=amt,
            )
        # Force the settle created_at into the intended windows.
        with self.ledger._conn() as conn:
            rows = conn.execute(
                "SELECT usage_id FROM credit_provider_usage ORDER BY usage_id ASC"
            ).fetchall()
            timestamps = [lo, hi]
            for j, (usage_id,) in enumerate(rows):
                conn.execute(
                    "UPDATE credit_provider_usage SET created_at = ? WHERE usage_id = ?",
                    (timestamps[j], usage_id),
                )
        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertFalse(report["stale"])
        self.assertEqual(report["usable_segments"], 2)
        self.assertEqual(report["topup_segments"], 1)
        self.assertTrue(report["balance_increased"])
        # Actual spend only from spend segments: 1.30 + 0.50 = $1.80.
        self.assertAlmostEqual(report["actual_spend_usd"], 1.80, places=6)
        self.assertAlmostEqual(report["estimated_spend_usd"], 0.40, places=6)

    def test_reconciliation_scopes_to_deepseek_rows(self):
        """OpenRouter settlements must not inflate the DeepSeek estimate."""
        now = time.time()
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=10.00, is_available=True, snapshot_at=now - 100.0,
        )
        self.ledger.grant("u-ds-3", _usd_to_micro(5.0), idempotency_key="g-ds-3")
        run = self.ledger.create_run("u-ds-3", idempotency_key="r-ds-3")
        # A DeepSeek call ($0.10) and an unrelated OpenRouter call ($0.90).
        for model, amt, key in [
            ("deepseek-v4-flash", 100_000, "c-ds-scope-ds"),
            ("google/gemini-3.1-flash-lite", 900_000, "c-ds-scope-or"),
        ]:
            call = self.ledger.reserve_call(
                run["run_id"], model=model, idempotency_key=key,
            )
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=amt,
                prompt_tokens=1, completion_tokens=1, total_tokens=2,
                provider_cost_micro_usd=amt,
            )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=9.60, is_available=True, snapshot_at=now + 10.0,
        )
        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertFalse(report["stale"])
        self.assertAlmostEqual(report["actual_spend_usd"], 0.40, places=6)
        # Only the DeepSeek row counts toward the estimate.
        self.assertAlmostEqual(report["estimated_spend_usd"], 0.10, places=6)

    # ------------------------------------------------------------------
    # Time-versioned pricing audit + reconciliation fixes
    # ------------------------------------------------------------------

    def test_settle_stores_pricing_audit_metadata(self):
        from credit import deepseek_pricing_for_timestamp

        self.ledger.grant("u-pricing", _usd_to_micro(2.0), idempotency_key="g-pricing")
        run = self.ledger.create_run("u-pricing", idempotency_key="r-pricing")
        call = self.ledger.reserve_call(
            run["run_id"], model="deepseek-v4-flash", idempotency_key="c-pricing",
        )
        ts = 1_786_896_000 + 3600  # 2026-08-16T17:00:00Z → offpeak
        pricing = deepseek_pricing_for_timestamp("deepseek-v4-flash", ts)
        result = self.ledger.settle_call(
            call["call_id"],
            actual_cost_micro_usd=44_520,
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            total_tokens=1_100_000,
            prompt_cache_hit_tokens=900_000,
            prompt_cache_miss_tokens=100_000,
            provider_cost_micro_usd=44_520,
            pricing_schedule_version=pricing["schedule_version"],
            pricing_tier=pricing["tier"],
            pricing_basis_ts=ts,
            input_cache_hit_rate_micro_usd_per_million=pricing[
                "input_cache_hit_micro_usd_per_million"
            ],
            input_cache_miss_rate_micro_usd_per_million=pricing[
                "input_cache_miss_micro_usd_per_million"
            ],
            output_rate_micro_usd_per_million=pricing[
                "output_micro_usd_per_million"
            ],
        )
        self.assertEqual(result["pricing_tier"], "offpeak")
        self.assertEqual(result["pricing_basis_ts"], ts)
        self.assertEqual(result["output_rate_micro_usd_per_million"], 660_000)

        rows = self.ledger.get_usage_for_run(run["run_id"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["pricing_schedule_version"], "2026-08-16T16:00:00Z")
        self.assertEqual(row["pricing_tier"], "offpeak")
        self.assertEqual(row["pricing_basis_ts"], ts)
        self.assertEqual(row["input_cache_hit_rate_micro_usd_per_million"], 7_000)
        self.assertEqual(row["input_cache_miss_rate_micro_usd_per_million"], 220_000)
        self.assertEqual(row["output_rate_micro_usd_per_million"], 660_000)

    def test_settle_without_pricing_keeps_audit_defaults(self):
        """Non-DeepSeek / legacy settles leave pricing columns NULL/empty."""
        self.ledger.grant("u-noprice", _usd_to_micro(2.0), idempotency_key="g-noprice")
        run = self.ledger.create_run("u-noprice", idempotency_key="r-noprice")
        call = self.ledger.reserve_call(
            run["run_id"], model="deepseek-v4-flash", idempotency_key="c-noprice",
        )
        self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=50_000, provider_cost_micro_usd=50_000,
        )
        rows = self.ledger.get_usage_for_run(run["run_id"])
        self.assertEqual(rows[0]["pricing_schedule_version"], "")
        self.assertEqual(rows[0]["pricing_tier"], "")
        self.assertIsNone(rows[0]["pricing_basis_ts"])
        self.assertEqual(rows[0]["input_cache_hit_rate_micro_usd_per_million"], 0)

    # ------------------------------------------------------------------
    # Settle pricing-metadata validation (worker is not fully trusted)
    # ------------------------------------------------------------------

    def _reserve_deepseek_call(self, user_id, key_prefix, model="deepseek-v4-flash"):
        self.ledger.grant(user_id, _usd_to_micro(5.0), idempotency_key=f"g-{key_prefix}")
        run = self.ledger.create_run(user_id, idempotency_key=f"r-{key_prefix}")
        call = self.ledger.reserve_call(
            run["run_id"], model=model, idempotency_key=f"c-{key_prefix}",
        )
        return call

    _OFFPEAK_TS = 1_786_896_000.0  # 2026-08-16T16:00:00Z → off-peak
    _PEAK_TS = 1_786_932_000.0     # 2026-08-17T02:00:00Z → peak

    _OFFPEAK_FLASH_RATES = (7_000, 220_000, 660_000)
    _PEAK_FLASH_RATES = (14_000, 440_000, 1_320_000)

    def test_settle_rejects_unknown_pricing_schedule_version(self):
        call = self._reserve_deepseek_call("u-val-sv", "val-sv")
        with self.assertRaisesRegex(ValueError, "schedule version"):
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=1_000,
                provider_cost_micro_usd=1_000,
                pricing_schedule_version="2099-01-01T00:00:00Z",
                pricing_tier="offpeak",
                pricing_basis_ts=self._OFFPEAK_TS,
                input_cache_hit_rate_micro_usd_per_million=7_000,
                input_cache_miss_rate_micro_usd_per_million=220_000,
                output_rate_micro_usd_per_million=660_000,
            )

    def test_settle_rejects_invalid_pricing_tier(self):
        call = self._reserve_deepseek_call("u-val-tier", "val-tier")
        with self.assertRaisesRegex(ValueError, "tier"):
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=1_000,
                provider_cost_micro_usd=1_000,
                pricing_schedule_version="2026-08-16T16:00:00Z",
                pricing_tier="weekend",
                pricing_basis_ts=self._OFFPEAK_TS,
                input_cache_hit_rate_micro_usd_per_million=7_000,
                input_cache_miss_rate_micro_usd_per_million=220_000,
                output_rate_micro_usd_per_million=660_000,
            )

    def test_settle_rejects_nonfinite_pricing_basis_ts(self):
        call = self._reserve_deepseek_call("u-val-nan", "val-nan")
        for bad_ts in (float("nan"), float("inf"), -1.0):
            with self.assertRaisesRegex(ValueError, "basis timestamp"):
                self.ledger.settle_call(
                    call["call_id"], actual_cost_micro_usd=1_000,
                    provider_cost_micro_usd=1_000,
                    pricing_schedule_version="2026-08-16T16:00:00Z",
                    pricing_tier="offpeak",
                    pricing_basis_ts=bad_ts,
                    input_cache_hit_rate_micro_usd_per_million=7_000,
                    input_cache_miss_rate_micro_usd_per_million=220_000,
                    output_rate_micro_usd_per_million=660_000,
                )

    def test_settle_rejects_tier_timestamp_mismatch(self):
        call = self._reserve_deepseek_call("u-val-mismatch", "val-mismatch")
        # "peak" claimed for an off-peak timestamp → rejected.
        with self.assertRaisesRegex(ValueError, "tier"):
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=1_000,
                provider_cost_micro_usd=1_000,
                pricing_schedule_version="2026-08-16T16:00:00Z",
                pricing_tier="peak",
                pricing_basis_ts=self._OFFPEAK_TS,
                input_cache_hit_rate_micro_usd_per_million=14_000,
                input_cache_miss_rate_micro_usd_per_million=440_000,
                output_rate_micro_usd_per_million=1_320_000,
            )

    def test_settle_rejects_rate_mismatch(self):
        call = self._reserve_deepseek_call("u-val-rate", "val-rate")
        # Off-peak tier but peak rates supplied → rejected.
        with self.assertRaisesRegex(ValueError, "rates"):
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=1_000,
                provider_cost_micro_usd=1_000,
                pricing_schedule_version="2026-08-16T16:00:00Z",
                pricing_tier="offpeak",
                pricing_basis_ts=self._OFFPEAK_TS,
                input_cache_hit_rate_micro_usd_per_million=14_000,
                input_cache_miss_rate_micro_usd_per_million=440_000,
                output_rate_micro_usd_per_million=1_320_000,
            )

    def test_settle_rejects_pricing_for_unpriced_model(self):
        call = self._reserve_deepseek_call(
            "u-val-openrouter", "val-openrouter", model="google/gemini-3.1-flash-lite"
        )
        with self.assertRaisesRegex(ValueError, "unpriced model"):
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=1_000,
                provider_cost_micro_usd=1_000,
                pricing_schedule_version="2026-08-16T16:00:00Z",
                pricing_tier="offpeak",
                pricing_basis_ts=self._OFFPEAK_TS,
                input_cache_hit_rate_micro_usd_per_million=7_000,
                input_cache_miss_rate_micro_usd_per_million=220_000,
                output_rate_micro_usd_per_million=660_000,
            )

    def test_already_settled_call_skips_pricing_validation(self):
        call = self._reserve_deepseek_call("u-val-idem", "val-idem")
        first = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=1_000,
            provider_cost_micro_usd=1_000,
            pricing_schedule_version="2026-08-16T16:00:00Z",
            pricing_tier="offpeak",
            pricing_basis_ts=self._OFFPEAK_TS,
            input_cache_hit_rate_micro_usd_per_million=7_000,
            input_cache_miss_rate_micro_usd_per_million=220_000,
            output_rate_micro_usd_per_million=660_000,
        )
        self.assertFalse(first["already_settled"])
        # A duplicate settle with tampered metadata must return idempotently
        # rather than error (the call is already settled).
        replay = self.ledger.settle_call(
            call["call_id"], actual_cost_micro_usd=1_000,
            provider_cost_micro_usd=1_000,
            pricing_schedule_version="2099-01-01T00:00:00Z",
            pricing_tier="bogus",
            pricing_basis_ts=float("nan"),
            input_cache_hit_rate_micro_usd_per_million=999_999,
            input_cache_miss_rate_micro_usd_per_million=999_999,
            output_rate_micro_usd_per_million=999_999,
        )
        self.assertTrue(replay["already_settled"])

    # ------------------------------------------------------------------
    # Authoritative recompute: control plane prices from reservation
    # submitted_at and overrides worker-supplied cost.
    # ------------------------------------------------------------------

    def _reserve_and_submit_deepseek_call(self, user_id, key_prefix, submitted_at):
        self.ledger.grant(user_id, _usd_to_micro(10.0), idempotency_key=f"g-{key_prefix}")
        run = self.ledger.create_run(user_id, idempotency_key=f"r-{key_prefix}")
        call = self.ledger.reserve_call(
            run["run_id"], model="deepseek-v4-flash", idempotency_key=f"c-{key_prefix}",
        )
        with patch("credit._now_ts", return_value=submitted_at):
            self.ledger.mark_call_submitted(call["call_id"])
        return call

    def test_settle_recomputes_cost_overrides_buggy_worker_cost(self):
        """Correct tier/rates but a wrong (zero) worker cost is overridden by
        the authoritative recompute."""
        call = self._reserve_and_submit_deepseek_call(
            "u-recompute", "recompute", self._PEAK_TS
        )
        result = self.ledger.settle_call(
            call["call_id"],
            actual_cost_micro_usd=0,          # buggy worker: zero cost
            provider_cost_micro_usd=0,
            prompt_tokens=1_000_000,
            prompt_cache_hit_tokens=1_000_000,
            prompt_cache_miss_tokens=0,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            pricing_schedule_version="2026-08-16T16:00:00Z",
            pricing_tier="peak",
            pricing_basis_ts=self._PEAK_TS,
            input_cache_hit_rate_micro_usd_per_million=14_000,
            input_cache_miss_rate_micro_usd_per_million=440_000,
            output_rate_micro_usd_per_million=1_320_000,
        )
        # 1M hit × $0.014 + 1M out × $1.32 = $1.334 → 1_334_000 micro.
        self.assertEqual(result["settled_micro_usd"], 1_334_000)
        self.assertEqual(result["provider_cost_micro_usd"], 1_334_000)
        self.assertEqual(result["pricing_tier"], "peak")

    def test_settle_autoprices_old_worker_missing_metadata(self):
        """An old worker that omits pricing metadata after the cutoff is priced
        from the authoritative submitted_at and audit fields are populated."""
        call = self._reserve_and_submit_deepseek_call(
            "u-autoprice", "autoprice", self._PEAK_TS
        )
        result = self.ledger.settle_call(
            call["call_id"],
            actual_cost_micro_usd=0,
            provider_cost_micro_usd=0,
            prompt_tokens=1_000_000,
            prompt_cache_hit_tokens=1_000_000,
            prompt_cache_miss_tokens=0,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        self.assertEqual(result["settled_micro_usd"], 1_334_000)
        self.assertEqual(result["pricing_tier"], "peak")
        self.assertEqual(result["pricing_schedule_version"], "2026-08-16T16:00:00Z")
        self.assertEqual(result["pricing_basis_ts"], self._PEAK_TS)
        self.assertEqual(result["input_cache_hit_rate_micro_usd_per_million"], 14_000)
        self.assertEqual(result["input_cache_miss_rate_micro_usd_per_million"], 440_000)
        self.assertEqual(result["output_rate_micro_usd_per_million"], 1_320_000)

    def test_settle_rejects_basis_timestamp_mismatch_with_submitted_at(self):
        """A worker pricing_basis_ts that differs from the reservation
        submitted_at is rejected (even with otherwise-consistent metadata)."""
        call = self._reserve_and_submit_deepseek_call(
            "u-basis-mismatch", "basis-mismatch", self._PEAK_TS
        )
        # Correct peak tier/rates but a basis timestamp 1h off → rejected.
        with self.assertRaisesRegex(ValueError, "basis timestamp"):
            self.ledger.settle_call(
                call["call_id"],
                actual_cost_micro_usd=1_000,
                provider_cost_micro_usd=1_000,
                prompt_tokens=1_000_000,
                prompt_cache_hit_tokens=1_000_000,
                prompt_cache_miss_tokens=0,
                completion_tokens=1_000_000,
                total_tokens=2_000_000,
                pricing_schedule_version="2026-08-16T16:00:00Z",
                pricing_tier="peak",
                pricing_basis_ts=self._PEAK_TS + 3600.0,
                input_cache_hit_rate_micro_usd_per_million=14_000,
                input_cache_miss_rate_micro_usd_per_million=440_000,
                output_rate_micro_usd_per_million=1_320_000,
            )

    def test_settle_zero_token_rejection_stays_zero(self):
        """A definitive zero-token rejection (cost_absent=False, no tokens)
        recomputes to zero rather than falling back to the full hold."""
        call = self._reserve_and_submit_deepseek_call(
            "u-zero-token", "zero-token", self._PEAK_TS
        )
        result = self.ledger.settle_call(
            call["call_id"],
            actual_cost_micro_usd=0,
            provider_cost_micro_usd=0,
            prompt_tokens=0,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_absent=False,
        )
        self.assertEqual(result["settled_micro_usd"], 0)
        self.assertEqual(result["provider_cost_micro_usd"], 0)
        self.assertEqual(result["pricing_tier"], "peak")

    def test_settle_rejects_oversized_tokens_without_mutation(self):
        """An impossible worker usage payload fails before any ledger write."""
        from credit import MAX_PROVIDER_USAGE_TOKENS_PER_FIELD

        call = self._reserve_and_submit_deepseek_call(
            "u-token-cap", "token-cap", self._PEAK_TS
        )
        balance_before = self.ledger.get_balance("u-token-cap")

        with self.assertRaisesRegex(ValueError, "prompt_tokens must be between"):
            self.ledger.settle_call(
                call["call_id"],
                prompt_tokens=MAX_PROVIDER_USAGE_TOKENS_PER_FIELD + 1,
                cost_absent=False,
            )

        self.assertEqual(self.ledger.get_balance("u-token-cap"), balance_before)
        with self.ledger._conn() as conn:
            status, settled = conn.execute(
                "SELECT status, settled_micro_usd "
                "FROM credit_call_reservations WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()
            usage_count = conn.execute(
                "SELECT COUNT(*) FROM credit_provider_usage WHERE call_id = ?",
                (call["call_id"],),
            ).fetchone()[0]
        self.assertEqual(status, "held")
        self.assertEqual(settled, 0)
        self.assertEqual(usage_count, 0)

    def test_already_settled_replay_ignores_oversized_tokens(self):
        """Malformed retry payloads cannot break an idempotent settled replay."""
        from credit import MAX_PROVIDER_USAGE_TOKENS_PER_FIELD

        call = self._reserve_and_submit_deepseek_call(
            "u-token-replay", "token-replay", self._PEAK_TS
        )
        first = self.ledger.settle_call(
            call["call_id"], prompt_tokens=1, total_tokens=1,
        )
        self.assertFalse(first["already_settled"])

        replay = self.ledger.settle_call(
            call["call_id"],
            prompt_tokens=MAX_PROVIDER_USAGE_TOKENS_PER_FIELD + 1,
        )
        self.assertTrue(replay["already_settled"])

    def test_deepseek_hold_certification_uses_peak_rates(self):
        from credit import (
            HOSTED_HOLD_CERTIFIED_RATES_MICRO_USD_PER_MILLION_TOKENS,
            HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD,
            validate_hosted_context_budget,
        )
        rates = HOSTED_HOLD_CERTIFIED_RATES_MICRO_USD_PER_MILLION_TOKENS
        self.assertEqual(rates["deepseek-v4-flash"], (440_000, 1_320_000))
        self.assertEqual(rates["deepseek-v4-pro"], (1_320_000, 3_960_000))
        # Fixed holds remain $0.25 / $1.00 and stay above certified minimums.
        self.assertEqual(HOSTED_MODEL_CATALOG["deepseek-v4-flash"], 250_000)
        self.assertEqual(HOSTED_MODEL_CATALOG["deepseek-v4-pro"], 1_000_000)
        self.assertLess(
            HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD["deepseek-v4-flash"],
            250_000,
        )
        self.assertLess(
            HOSTED_HOLD_CERTIFIED_MIN_RESERVATION_MICRO_USD["deepseek-v4-pro"],
            1_000_000,
        )
        validate_hosted_context_budget(400_000)

    def test_reconciliation_unchanged_segments_are_not_topups(self):
        """An unchanged balance is neither spend nor a top-up, and its spend
        estimate is not lost (monotonic span includes it)."""
        now = time.time()
        # 10.00 -> 9.90 (spend) -> 9.90 (unchanged) -> 9.80 (spend)
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=10.00, is_available=True, snapshot_at=now - 100.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=9.90, is_available=True, snapshot_at=now - 80.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=9.90, is_available=True, snapshot_at=now - 60.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=9.80, is_available=True, snapshot_at=now - 40.0,
        )
        # Usage spanning the whole monotonic span ($0.10 + $0.10 estimate).
        for i, amt in enumerate([50_000, 50_000]):
            self.ledger.grant(f"u-unc-{i}", _usd_to_micro(5.0), idempotency_key=f"g-unc-{i}")
            run = self.ledger.create_run(f"u-unc-{i}", idempotency_key=f"r-unc-{i}")
            call = self.ledger.reserve_call(
                run["run_id"], model="deepseek-v4-flash", idempotency_key=f"c-unc-{i}",
            )
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=amt,
                provider_cost_micro_usd=amt,
            )
        with self.ledger._conn() as conn:
            rows = conn.execute(
                "SELECT usage_id FROM credit_provider_usage ORDER BY usage_id ASC"
            ).fetchall()
            for j, (usage_id,) in enumerate(rows):
                conn.execute(
                    "UPDATE credit_provider_usage SET created_at = ? WHERE usage_id = ?",
                    ([now - 90.0, now - 70.0][j], usage_id),
                )
        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertFalse(report["stale"])
        self.assertEqual(report["unchanged_segments"], 1)
        self.assertEqual(report["topup_segments"], 0)
        self.assertFalse(report["balance_increased"])
        # One monotonic span: 10.00 -> 9.80 = $0.20 actual.
        self.assertAlmostEqual(report["actual_spend_usd"], 0.20, places=6)
        # Estimate includes BOTH spend rows across the unchanged interval.
        self.assertAlmostEqual(report["estimated_spend_usd"], 0.10, places=6)

    def test_reconciliation_topup_creates_boundary_and_skips_interval(self):
        """A real increase creates a span boundary; the increasing interval is
        skipped, so a top-up cannot contaminate actual spend."""
        now = time.time()
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=10.00, is_available=True, snapshot_at=now - 100.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=9.80, is_available=True, snapshot_at=now - 80.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=12.00, is_available=True, snapshot_at=now - 60.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=11.70, is_available=True, snapshot_at=now - 40.0,
        )
        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertFalse(report["stale"])
        self.assertEqual(report["topup_segments"], 1)
        self.assertTrue(report["balance_increased"])
        # Actual spend only across the two monotonic spans: 0.20 + 0.30 = $0.50.
        self.assertAlmostEqual(report["actual_spend_usd"], 0.50, places=6)
        # The increasing interval [9.80 -> 12.00] contributed no estimate.
        self.assertAlmostEqual(report["estimated_spend_usd"], 0.0, places=6)

    def test_balance_snapshot_components_round_trip(self):
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=11.32, is_available=True,
            granted_balance=8.00, topped_up_balance=3.32,
            snapshot_at=time.time(),
        )
        latest = self.ledger.latest_provider_balance_snapshot("deepseek")
        self.assertEqual(latest["total_balance"], 11.32)
        self.assertEqual(latest["granted_balance"], 8.00)
        self.assertEqual(latest["topped_up_balance"], 3.32)
        self.assertTrue(latest["is_available"])

    def test_balance_snapshot_components_absent_are_none(self):
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=11.32, is_available=True,
            snapshot_at=time.time(),
        )
        latest = self.ledger.latest_provider_balance_snapshot("deepseek")
        self.assertIsNone(latest["granted_balance"])
        self.assertIsNone(latest["topped_up_balance"])

    def test_balance_snapshot_rejects_nonfinite_total(self):
        for bad in ("nan", "inf", "-inf"):
            with self.assertRaises(ValueError):
                self.ledger.record_provider_balance_snapshot(
                    provider="deepseek", currency="USD",
                    total_balance=bad, is_available=True,
                )
        # Nothing non-finite was stored.
        self.assertIsNone(self.ledger.latest_provider_balance_snapshot("deepseek"))

    def test_balance_snapshot_rejects_nonfinite_components(self):
        for bad in ("nan", "inf"):
            with self.assertRaises(ValueError):
                self.ledger.record_provider_balance_snapshot(
                    provider="deepseek", currency="USD",
                    total_balance=11.32, is_available=True,
                    granted_balance=bad,
                )
            with self.assertRaises(ValueError):
                self.ledger.record_provider_balance_snapshot(
                    provider="deepseek", currency="USD",
                    total_balance=11.32, is_available=True,
                    topped_up_balance=bad,
                )

    def test_balance_snapshot_rejects_nonfinite_snapshot_at(self):
        for bad in ("nan", "inf"):
            with self.assertRaises(ValueError):
                self.ledger.record_provider_balance_snapshot(
                    provider="deepseek", currency="USD",
                    total_balance=11.32, is_available=True,
                    snapshot_at=bad,
                )

    def test_reconciliation_excludes_topup_interval_usage(self):
        """Usage inside the increasing (top-up) interval is excluded, and
        disjoint monotonic spans never double-count boundary usage."""
        now = time.time()
        # 10.00 -> 9.80 (spend span 1) -> 12.00 (top-up) -> 11.70 (spend span 2)
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=10.00, is_available=True, snapshot_at=now - 100.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=9.80, is_available=True, snapshot_at=now - 80.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=12.00, is_available=True, snapshot_at=now - 60.0,
        )
        self.ledger.record_provider_balance_snapshot(
            provider="deepseek", currency="USD",
            total_balance=11.70, is_available=True, snapshot_at=now - 40.0,
        )
        # Four usage rows placed at: span1 interior, span1 boundary (end),
        # top-up interval (must be excluded), span2 interior.
        for i, cost in enumerate([50_000, 10_000, 999_000, 60_000]):
            self.ledger.grant(
                f"u-topup-boundary-{i}", _usd_to_micro(5.0),
                idempotency_key=f"g-tb-{i}",
            )
            run = self.ledger.create_run(
                f"u-topup-boundary-{i}", idempotency_key=f"r-tb-{i}"
            )
            call = self.ledger.reserve_call(
                run["run_id"], model="deepseek-v4-flash",
                idempotency_key=f"c-tb-{i}",
            )
            self.ledger.settle_call(
                call["call_id"], actual_cost_micro_usd=cost,
                provider_cost_micro_usd=cost,
            )
        with self.ledger._conn() as conn:
            rows = conn.execute(
                "SELECT usage_id FROM credit_provider_usage ORDER BY usage_id ASC"
            ).fetchall()
            timestamps = [now - 90.0, now - 80.0, now - 70.0, now - 50.0]
            for j, (usage_id,) in enumerate(rows):
                conn.execute(
                    "UPDATE credit_provider_usage SET created_at = ? WHERE usage_id = ?",
                    (timestamps[j], usage_id),
                )
        report = self.ledger.provider_cost_reconciliation("deepseek")
        self.assertFalse(report["stale"])
        self.assertEqual(report["topup_segments"], 1)
        self.assertEqual(report["span_count"], 2)
        # Actual spend across the two spans: 0.20 + 0.30 = $0.50.
        self.assertAlmostEqual(report["actual_spend_usd"], 0.50, places=6)
        # Estimate = span1 (50k + 10k boundary) + span2 (60k); the top-up
        # interval row (999k) is excluded and the boundary is counted once.
        self.assertAlmostEqual(report["estimated_spend_usd"], 0.12, places=6)
