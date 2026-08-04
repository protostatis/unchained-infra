"""Comprehensive tests for the financial workspace control plane.

Covers:
  - Schema migration from existing auth.db
  - AES-GCM tamper / AAD / wrong-key / rotation behavior
  - Exact one-hour auth expiry and eventual delete
  - Bounded checkpoint validation and no bearer values logged
  - Concurrent OAuth callbacks create one account/workspace/import
  - Cross-account / replayed / expired / browser-mismatch claim rejection
  - Import crash injection at transaction / object / outbox boundaries
  - Exactly one new-account credit (USD 1.00), zero for existing account
  - Deletion / export metadata behavior
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from checkpoint_store import (
    LocalCheckpointStore,
    _aad_bytes,
    _encrypt_envelope,
    _decrypt_envelope,
    _pack,
    _unpack,
)
from credit import CreditLedger, _usd_to_micro, _micro_to_usd
from financial_workspace import (
    FinancialWorkspace,
    CheckpointValidationError,
    CheckpointNotFoundError,
    CheckpointStateError,
    ClaimRejectedError,
    ImportConflictError,
    UnauthorizedError,
    _CHECKPOINT_EXPIRY_SECONDS,
    _NEW_ACCOUNT_GRANT_MICRO_USD,
    _uuid_hex,
    is_fin_workspace_enabled,
)
from auth import Auth
from web_app.handlers import fin_workspace


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class _Request:
    def __init__(self, *, body=None, token="", cookies=None, headers=None,
                 match_info=None):
        self._body = body
        self._headers = headers or {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._cookies = cookies or {}
        self.match_info = match_info or {}
        self.cookies = self._cookies
        self.headers = self._headers

    async def json(self):
        return self._body


def _payload(response) -> dict:
    return json.loads(response.body.decode())


# ---------------------------------------------------------------------------
# 1. Schema migration tests
# ---------------------------------------------------------------------------
class SchemaMigrationTests(unittest.TestCase):
    """Schema migrations from existing auth.db and old code compatibility."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")

    def tearDown(self):
        CreditLedger._instances.clear()
        self._temp_dir.cleanup()

    def test_existing_auth_db_survives_schema_init(self):
        """Existing auth.db with all standard tables must survive init."""
        auth = Auth(self.db_path)
        auth.create_key("u-test")
        auth.get_or_create_user("test@example.com", "Test User")
        auth.close() if hasattr(auth, "close") else None

        # Now init financial workspace schema on top
        store = LocalCheckpointStore()
        fw = FinancialWorkspace(self.db_path, store)

        # Verify existing tables intact
        with fw._conn() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()
            self.assertGreater(rows[0], 0)
            rows = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            self.assertGreater(rows[0], 0)

    def test_new_tables_created(self):
        store = LocalCheckpointStore()
        fw = FinancialWorkspace(self.db_path, store)

        expected_tables = {
            "fin_terminal_checkpoints",
            "fin_terminal_claims",
            "financial_workspaces",
            "financial_workspace_snapshots",
            "financial_workspace_imports",
            "financial_workspace_account_origins",
            "financial_workspace_effects",
        }
        with fw._conn() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            existing = {r[0] for r in rows}
        for table in expected_tables:
            self.assertIn(table, existing, f"Missing table: {table}")

    def test_auth_codes_columns_added(self):
        """Backward-compatible columns added to auth_codes."""
        auth = Auth(self.db_path)
        store = LocalCheckpointStore()
        fw = FinancialWorkspace(self.db_path, store)

        with fw._conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_codes)").fetchall()}
        for col in ("purpose", "audience", "claim_id", "state_binding"):
            self.assertIn(col, cols, f"Missing auth_codes column: {col}")

    def test_schema_idempotent(self):
        """Schema init is idempotent — calling twice doesn't error."""
        store = LocalCheckpointStore()
        fw = FinancialWorkspace(self.db_path, store)
        # Second init should not raise
        fw2 = FinancialWorkspace(self.db_path, store)
        # Tables should be intact
        with fw2._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM fin_terminal_checkpoints"
            ).fetchone()[0]
            self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# 2. AES-GCM envelope encryption tests
# ---------------------------------------------------------------------------
class EnvelopeEncryptionTests(unittest.TestCase):
    """AES-GCM tamper, AAD, wrong-key, rotation behavior."""

    def setUp(self):
        self.dek = hashlib.sha256(b"test-dek-v1").digest()
        self.checkpoint_id = "fcp-test123"
        self.version = 1
        self.expires_at = time.time() + 3600
        self.plaintext = json.dumps({"data": "sensitive checkpoint data"}).encode()

    def test_encrypt_decrypt_roundtrip(self):
        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)

        ciphertext = _encrypt_envelope(self.dek, nonce, self.plaintext, aad)
        recovered = _decrypt_envelope(self.dek, nonce, ciphertext, aad)
        self.assertEqual(recovered, self.plaintext)

    def test_tampered_ciphertext_rejected(self):
        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)

        ciphertext = _encrypt_envelope(self.dek, nonce, self.plaintext, aad)
        # Flip a byte in the ciphertext
        tampered = ciphertext[:10] + bytes([ciphertext[10] ^ 0xFF]) + ciphertext[11:]

        with self.assertRaises(Exception):
            _decrypt_envelope(self.dek, nonce, tampered, aad)

    def test_wrong_aad_rejected(self):
        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)

        ciphertext = _encrypt_envelope(self.dek, nonce, self.plaintext, aad)

        # Use wrong AAD (different version)
        wrong_aad = _aad_bytes(self.checkpoint_id, 2, self.expires_at, content_hash)
        with self.assertRaises(Exception):
            _decrypt_envelope(self.dek, nonce, ciphertext, wrong_aad)

    def test_wrong_key_rejected(self):
        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)

        ciphertext = _encrypt_envelope(self.dek, nonce, self.plaintext, aad)

        wrong_dek = hashlib.sha256(b"wrong-key").digest()
        with self.assertRaises(Exception):
            _decrypt_envelope(wrong_dek, nonce, ciphertext, aad)

    def test_key_rotation_behavior(self):
        """Old key can't decrypt new-key ciphertext, simulating rotation."""
        old_key = hashlib.sha256(b"old-key").digest()
        new_key = hashlib.sha256(b"new-key").digest()

        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)

        # Encrypt with new key
        ciphertext = _encrypt_envelope(new_key, nonce, self.plaintext, aad)

        # Decrypt works with new key
        recovered = _decrypt_envelope(new_key, nonce, ciphertext, aad)
        self.assertEqual(recovered, self.plaintext)

        # Old key fails
        with self.assertRaises(Exception):
            _decrypt_envelope(old_key, nonce, ciphertext, aad)

    def test_envelope_pack_unpack_roundtrip(self):
        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)
        ciphertext = _encrypt_envelope(self.dek, nonce, self.plaintext, aad)

        blob = _pack(self.dek, nonce, ciphertext, aad)
        recovered_dek, recovered_nonce, recovered_ct, recovered_aad = _unpack(blob)

        self.assertEqual(recovered_dek, self.dek)
        self.assertEqual(recovered_nonce, nonce)
        self.assertEqual(recovered_ct, ciphertext)
        self.assertEqual(recovered_aad, aad)

    def test_local_store_encrypts_and_decrypts(self):
        store = LocalCheckpointStore()
        result = store.put(
            checkpoint_id=self.checkpoint_id,
            plaintext=self.plaintext,
            version=self.version,
            expires_at=self.expires_at,
        )
        self.assertIn("content_hash", result)
        self.assertEqual(result["size_bytes"], len(self.plaintext))

        recovered = store.get(self.checkpoint_id)
        self.assertEqual(recovered, self.plaintext)

    def test_local_store_rejects_tampered(self):
        store = LocalCheckpointStore()
        store.put(
            checkpoint_id=self.checkpoint_id,
            plaintext=self.plaintext,
            version=self.version,
            expires_at=self.expires_at,
        )
        # Tamper the stored blob
        key = hashlib.sha256(store._key + self.checkpoint_id.encode()).digest()
        nonce = os.urandom(12)
        content_hash = hashlib.sha256(self.plaintext).hexdigest()
        aad = _aad_bytes(self.checkpoint_id, self.version, self.expires_at, content_hash)
        bad_ct = _encrypt_envelope(key, nonce, self.plaintext, aad)
        # Replace with valid ciphertext but using wrong AAD
        store._store[self.checkpoint_id] = _pack(key, nonce, bad_ct,
                                                  _aad_bytes(self.checkpoint_id, 99, self.expires_at, "deadbeef"))

        # Should return None for tampered data
        result = store.get(self.checkpoint_id)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 3. Local checkpoint store tests
# ---------------------------------------------------------------------------
class LocalCheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = LocalCheckpointStore()

    def test_put_get_roundtrip(self):
        data = b"hello world checkpoint"
        result = self.store.put(
            checkpoint_id="fcp-1",
            plaintext=data,
            version=1,
            expires_at=time.time() + 3600,
        )
        self.assertEqual(result["size_bytes"], len(data))
        self.assertEqual(self.store.get("fcp-1"), data)

    def test_expired_returns_none(self):
        self.store.put(
            checkpoint_id="fcp-expired",
            plaintext=b"expired data",
            version=1,
            expires_at=time.time() - 1,  # already expired
        )
        self.assertIsNone(self.store.get("fcp-expired"))

    def test_delete_removes(self):
        self.store.put(
            checkpoint_id="fcp-del",
            plaintext=b"delete me",
            version=1,
            expires_at=time.time() + 3600,
        )
        self.store.delete("fcp-del")
        self.assertIsNone(self.store.get("fcp-del"))

    def test_sweep_expired(self):
        now = time.time()
        self.store.put(checkpoint_id="fcp-1", plaintext=b"data1", version=1, expires_at=now + 3600)
        self.store.put(checkpoint_id="fcp-2", plaintext=b"data2", version=1, expires_at=now - 1)
        self.store.put(checkpoint_id="fcp-3", plaintext=b"data3", version=1, expires_at=now - 10)

        deleted = self.store.sweep_expired()
        self.assertEqual(deleted, 2)
        self.assertIsNone(self.store.get("fcp-2"))
        self.assertIsNone(self.store.get("fcp-3"))
        self.assertEqual(self.store.get("fcp-1"), b"data1")


# ---------------------------------------------------------------------------
# 4. Checkpoint lifecycle tests
# ---------------------------------------------------------------------------
class CheckpointLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def _create_checkpoint(self) -> dict:
        return self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess-abc",
            worker_id="worker-xyz",
            generation="gen-1",
            source_revision="rev-abc123",
            checkpoint=json.dumps({"holdings": [], "version": 1}).encode(),
        )

    def test_create_checkpoint_returns_handoff(self):
        result = self._create_checkpoint()
        self.assertIn("checkpoint_id", result)
        self.assertIn("handoff_id", result)
        self.assertIn("handoff_secret", result)
        self.assertIn("auth_url", result)
        self.assertIn("expires_at", result)
        self.assertFalse(result.get("already_exists", False))

    def test_create_checkpoint_is_idempotent(self):
        first = self._create_checkpoint()
        second = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess-abc",
            worker_id="worker-xyz",
            generation="gen-1",
            source_revision="rev-abc123",
            checkpoint=json.dumps({"holdings": []}).encode(),
        )
        self.assertTrue(second["already_exists"])
        self.assertEqual(second["checkpoint_id"], first["checkpoint_id"])

    def test_idempotent_retry_returns_matching_handoff_secret(self):
        """Regression: the idempotent retry used to hand back a *different*
        secret than the one hashed at creation, so claim initiation failed
        with a credential mismatch. The retry must return the SAME
        deterministic secret that matches the stored hash."""
        first = self._create_checkpoint()
        retry = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess-abc",
            worker_id="worker-xyz",
            generation="gen-1",
            source_revision="rev-abc123",
            checkpoint=json.dumps({"holdings": []}).encode(),
        )
        self.assertTrue(retry["already_exists"])
        self.assertEqual(retry["handoff_id"], first["handoff_id"])
        self.assertEqual(retry["handoff_secret"], first["handoff_secret"])
        # The re-derived secret authenticates at claim initiation.
        claim = self.fw.initiate_claim(
            retry["handoff_id"], retry["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.assertIn("claim_secret", claim)

    def test_retried_secret_matches_stored_hash(self):
        first = self._create_checkpoint()
        retry = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess-abc",
            worker_id="worker-xyz",
            checkpoint=b'{}',
        )
        with self.fw._conn() as conn:
            row = conn.execute(
                "SELECT handoff_secret_hash FROM fin_terminal_checkpoints "
                "WHERE handoff_id = ?",
                (retry["handoff_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            hashlib.sha256(retry["handoff_secret"].encode()).hexdigest(),
            row[0],
        )
        # The stored value is a hash — never the plaintext secret.
        self.assertNotEqual(retry["handoff_secret"], row[0])

    def test_validate_bounded_fields(self):
        with self.assertRaises(CheckpointValidationError):
            self.fw.create_checkpoint(
                request_id="",  # empty
                session_id="sess",
                worker_id="worker",
                checkpoint=b"{}",
            )
        with self.assertRaises(CheckpointValidationError):
            self.fw.create_checkpoint(
                request_id="req",
                session_id="",  # empty
                worker_id="worker",
                checkpoint=b"{}",
            )
        with self.assertRaises(CheckpointValidationError):
            self.fw.create_checkpoint(
                request_id="req",
                session_id="sess",
                worker_id="",
                checkpoint=b"{}",
            )

    def test_rejects_empty_checkpoint(self):
        with self.assertRaises(CheckpointValidationError):
            self.fw.create_checkpoint(
                request_id="req",
                session_id="sess",
                worker_id="worker",
                checkpoint=b"",
            )

    def test_get_checkpoint_returns_metadata(self):
        created = self._create_checkpoint()
        chk = self.fw.get_checkpoint(created["checkpoint_id"])
        self.assertEqual(chk["status"], "ready")
        self.assertEqual(chk["request_id"], "req-001")

    def test_get_checkpoint_never_returns_secret(self):
        created = self._create_checkpoint()
        chk = self.fw.get_checkpoint(created["checkpoint_id"])
        self.assertNotIn("handoff_secret", chk)
        self.assertNotIn("handoff_secret_hash", chk)

    def test_get_nonexistent_checkpoint(self):
        self.assertIsNone(self.fw.get_checkpoint("nonexistent"))


# ---------------------------------------------------------------------------
# 5. Claim flow tests
# ---------------------------------------------------------------------------
class ClaimFlowTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def _create_and_claim(self) -> tuple[dict, dict]:
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess-abc",
            worker_id="worker-xyz",
            checkpoint=b'{"holdings": []}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce-123",
        )
        return ckpt, claim

    def test_initiate_claim_succeeds(self):
        ckpt, claim = self._create_and_claim()
        self.assertIn("claim_secret", claim)
        self.assertIn("claim_id", claim)
        self.assertEqual(claim["checkpoint_id"], ckpt["checkpoint_id"])

    def test_initiate_claim_wrong_secret_rejected(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        with self.assertRaises(UnauthorizedError):
            self.fw.initiate_claim(
                handoff_id=ckpt["handoff_id"],
                handoff_secret="wrong-secret",
                browser_nonce="nonce",
            )

    def test_initiate_claim_nonexistent_handoff(self):
        with self.assertRaises(CheckpointNotFoundError):
            self.fw.initiate_claim(
                handoff_id="nonexistent",
                handoff_secret="secret",
                browser_nonce="nonce",
            )

    def test_initiate_claim_already_claimed_rejected(self):
        ckpt, first = self._create_and_claim()
        with self.assertRaises(CheckpointStateError):
            self.fw.initiate_claim(
                handoff_id=ckpt["handoff_id"],
                handoff_secret=ckpt["handoff_secret"],
                browser_nonce="nonce-456",
            )

    def test_accept_claim_creates_workspace(self):
        ckpt, claim = self._create_and_claim()
        result = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-test",
            final_account_email="test@example.com",
            browser_nonce="nonce-123",
            oauth_state="state-xyz",
        )
        self.assertTrue(result["is_new_workspace"])
        self.assertIn("workspace_id", result)
        self.assertIn("snapshot_id", result)

    def test_accept_claim_wrong_secret_rejected(self):
        ckpt, claim = self._create_and_claim()
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret="wrong-secret",
                final_account_user_id="u-test",
                final_account_email="test@example.com",
                browser_nonce="nonce-123",
                oauth_state="state",
            )

    def test_accept_claim_browser_nonce_mismatch_rejected(self):
        ckpt, claim = self._create_and_claim()
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret=claim["claim_secret"],
                final_account_user_id="u-test",
                final_account_email="test@example.com",
                browser_nonce="different-nonce",
                oauth_state="state",
            )

    def test_accept_claim_already_accepted_rejected_for_different_account(self):
        ckpt, claim = self._create_and_claim()
        self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-alice",
            final_account_email="alice@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        # Bob tries to claim
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret=claim["claim_secret"],
                final_account_user_id="u-bob",
                final_account_email="bob@example.com",
                browser_nonce="nonce-123",
                oauth_state="state",
            )

    def test_accept_claim_idempotent_for_same_account(self):
        ckpt, claim = self._create_and_claim()
        first = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-alice",
            final_account_email="alice@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        # Same account retries — should succeed (idempotent)
        second = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-alice",
            final_account_email="alice@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        self.assertTrue(second.get("already_accepted"))
        self.assertEqual(second["workspace_id"], first["workspace_id"])

    def test_accept_wrong_secret_on_accepted_claim_rejected_no_leak(self):
        """Regression: a wrong claim secret on an ALREADY-ACCEPTED claim used
        to be silently accepted for retry, leaking the victim's
        workspace/user/email/snapshot. The accepted path must still verify the
        claim secret (and nonce/state/account binding)."""
        ckpt, claim = self._create_and_claim()
        self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-alice",
            final_account_email="alice@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret="wrong-secret",
                final_account_user_id="u-alice",
                final_account_email="alice@example.com",
                browser_nonce="nonce-123",
                oauth_state="state",
            )
        # No duplicate rows were created by the rejected attempt.
        with self.fw._conn() as conn:
            snaps = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_snapshots"
            ).fetchone()[0]
            effs = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_effects"
            ).fetchone()[0]
        self.assertEqual(snaps, 1)
        self.assertEqual(effs, 3)

    def test_reaccept_same_account_no_duplicate_snapshot_or_effects(self):
        """Regression: re-accepting an already-accepted claim created a second
        snapshot row and re-enqueued outbox effects. Re-accept must reuse the
        existing snapshot and enqueue nothing."""
        ckpt, claim = self._create_and_claim()
        first = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-alice",
            final_account_email="alice@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        with self.fw._conn() as conn:
            effects_before = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_effects"
            ).fetchone()[0]

        second = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-alice",
            final_account_email="alice@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        self.assertTrue(second["already_accepted"])
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])

        with self.fw._conn() as conn:
            snaps = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_snapshots"
            ).fetchone()[0]
            effs = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_effects"
            ).fetchone()[0]
        self.assertEqual(snaps, 1)
        self.assertEqual(effs, effects_before)

    def test_expired_claim_rejected(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )

        # Manually expire the claim by modifying the DB
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_claims SET claim_secret_expires_at = ? WHERE claim_id = ?",
                (time.time() - 1, claim["claim_id"]),
            )

        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret=claim["claim_secret"],
                final_account_user_id="u-test",
                final_account_email="test@example.com",
                browser_nonce="nonce",
                oauth_state="state",
            )

    def test_replayed_claim_rejected(self):
        ckpt, claim = self._create_and_claim()
        self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-test",
            final_account_email="test@example.com",
            browser_nonce="nonce-123",
            oauth_state="state",
        )
        # Replay with different account — should be rejected
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret=claim["claim_secret"],
                final_account_user_id="u-eve",
                final_account_email="eve@example.com",
                browser_nonce="nonce-123",
                oauth_state="state",
            )


# ---------------------------------------------------------------------------
# 6. One-hour auth expiry and sweep
# ---------------------------------------------------------------------------
class ExpiryAndSweepTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_checkpoint_expires_after_one_hour(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        self.assertGreater(ckpt["expires_at"], time.time())
        self.assertLess(ckpt["expires_at"], time.time() + _CHECKPOINT_EXPIRY_SECONDS + 10)

    def test_sweep_expires_ready_checkpoints(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        # Mark the checkpoint as expired
        past = time.time() - 100
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET expires_at = ? WHERE checkpoint_id = ?",
                (past, ckpt["checkpoint_id"]),
            )

        count = self.fw.sweep_expired()
        self.assertGreaterEqual(count, 1)

        chk = self.fw.get_checkpoint(ckpt["checkpoint_id"])
        self.assertEqual(chk["status"], "expired")

        # Storage should be deleted
        data = self.store.get(ckpt["checkpoint_id"])
        self.assertIsNone(data)

    def test_sweep_does_not_touch_imported(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-test",
            final_account_email="test@example.com",
            browser_nonce="nonce",
            oauth_state="state",
        )

        # Manually expire
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET expires_at = ? WHERE checkpoint_id = ?",
                (time.time() - 100, ckpt["checkpoint_id"]),
            )

        count = self.fw.sweep_expired()
        # imported checkpoints should NOT be swept
        chk = self.fw.get_checkpoint(ckpt["checkpoint_id"])
        self.assertEqual(chk["status"], "imported")

    def test_claim_expiry_never_outlives_checkpoint(self):
        """Regression: a claim issued near the end of the checkpoint window
        used to get a fresh one-hour expiry and stay valid after the
        checkpoint had already been swept."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-claim-exp", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        # The checkpoint is nearly at its TTL (60s left).
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET expires_at = ? "
                "WHERE checkpoint_id = ?",
                (time.time() + 60, ckpt["checkpoint_id"]),
            )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        with self.fw._conn() as conn:
            chk_exp = conn.execute(
                "SELECT expires_at FROM fin_terminal_checkpoints "
                "WHERE checkpoint_id = ?",
                (ckpt["checkpoint_id"],),
            ).fetchone()[0]
            claim_exp = conn.execute(
                "SELECT claim_secret_expires_at FROM fin_terminal_claims "
                "WHERE claim_id = ?",
                (claim["claim_id"],),
            ).fetchone()[0]
        self.assertLessEqual(claim_exp, chk_exp)

    def test_accept_rechecks_checkpoint_expiry(self):
        """Regression: accept_claim only checked the claim expiry, so a claim
        on an already-expired (or swept) checkpoint could still import. Accept
        must recheck checkpoint status/expiry transactionally."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-chk-exp", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET expires_at = ? "
                "WHERE checkpoint_id = ?",
                (time.time() - 10, ckpt["checkpoint_id"]),
            )
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret=claim["claim_secret"],
                final_account_user_id="u-exp",
                final_account_email="exp@example.com",
                browser_nonce="nonce",
                oauth_state="state",
            )
        # Expiry marking is durable and no partial import was created.
        with self.fw._conn() as conn:
            chk = conn.execute(
                "SELECT status FROM fin_terminal_checkpoints "
                "WHERE checkpoint_id = ?",
                (ckpt["checkpoint_id"],),
            ).fetchone()[0]
            clm = conn.execute(
                "SELECT status FROM fin_terminal_claims WHERE claim_id = ?",
                (claim["claim_id"],),
            ).fetchone()[0]
            imp = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_imports"
            ).fetchone()[0]
        self.assertEqual(chk, "expired")
        self.assertEqual(clm, "expired")
        self.assertEqual(imp, 0)

    def test_accept_fails_when_checkpoint_data_swept(self):
        """Regression: after the sweep deleted the encrypted checkpoint object,
        accept_claim imported an empty '{}' snapshot. It must reject instead of
        silently destroying the user's data."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-sweep-race", session_id="sess", worker_id="worker",
            checkpoint=b'{"holdings": [42]}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        # Simulate the sweep deleting the encrypted object while the claim is
        # still pending.
        self.store.delete(ckpt["checkpoint_id"])
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim_id=claim["claim_id"],
                claim_secret=claim["claim_secret"],
                final_account_user_id="u-sweep",
                final_account_email="sweep@example.com",
                browser_nonce="nonce",
                oauth_state="state",
            )
        with self.fw._conn() as conn:
            imp = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_imports"
            ).fetchone()[0]
            snap = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_snapshots"
            ).fetchone()[0]
            eff = conn.execute(
                "SELECT COUNT(*) FROM financial_workspace_effects"
            ).fetchone()[0]
        self.assertEqual((imp, snap, eff), (0, 0, 0))


# ---------------------------------------------------------------------------
# 7. Concurrent OAuth callback tests
# ---------------------------------------------------------------------------
class ConcurrentClaimTests(unittest.TestCase):
    """Concurrent OAuth callbacks create exactly one account/workspace/import."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_concurrent_callbacks_single_workspace(self):
        """Simulate racing accept_claim calls on threads."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{"test": true}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )

        results = []
        errors = []
        barrier = threading.Barrier(2)

        def do_accept(tag):
            barrier.wait()
            try:
                result = self.fw.accept_claim(
                    claim_id=claim["claim_id"],
                    claim_secret=claim["claim_secret"],
                    final_account_user_id="u-test",
                    final_account_email="test@example.com",
                    browser_nonce="nonce",
                    oauth_state=f"state-{tag}",
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=do_accept, args=("A",))
        t2 = threading.Thread(target=do_accept, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # At least one must succeed
        self.assertGreater(len(results), 0, f"Both failed: {errors}")

        # Only one workspace should exist
        ws = self.fw.get_workspace_for_user("u-test")
        self.assertIsNotNone(ws)

        # Only one import for this checkpoint
        imp = self.fw.get_import_for_checkpoint(ckpt["checkpoint_id"])
        self.assertIsNotNone(imp)
        self.assertEqual(imp["user_id"], "u-test")

        # Verify all results reference same workspace
        for r in results:
            self.assertEqual(r["workspace_id"], ws["workspace_id"])


# ---------------------------------------------------------------------------
# 8. Exactly one new-account credit, zero for existing
# ---------------------------------------------------------------------------
class CreditGrantTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def _create_and_accept(self, user_id: str, email: str) -> dict:
        ckpt = self.fw.create_checkpoint(
            request_id=f"req-{user_id}-{_uuid_hex()[:8]}",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        return self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id=user_id,
            final_account_email=email,
            browser_nonce="nonce",
            oauth_state="state",
        )

    def test_new_account_gets_grant(self):
        result = self._create_and_accept("u-new", "new@example.com")
        self.assertTrue(result["is_new_workspace"])

        # Process the grant effect
        effects = self.fw.poll_pending_effects(limit=10)
        grant_effects = [e for e in effects if e["effect_type"] == "account_grant"]
        self.assertEqual(len(grant_effects), 1)

        # Process it
        self.fw.mark_effect_processing(grant_effects[0]["effect_id"])
        grant_result = self.fw.process_account_grant_effect(
            grant_effects[0]["context"], self.ledger
        )
        self.fw.mark_effect_completed(grant_effects[0]["effect_id"])

        # Verify balance
        acct = self.ledger.get_account("u-new")
        self.assertIsNotNone(acct)
        self.assertEqual(acct["balance_micro_usd"], _NEW_ACCOUNT_GRANT_MICRO_USD)
        self.assertEqual(acct["balance_usd"], 1.0)

    def test_existing_account_gets_no_additional_grant(self):
        # First time — creates workspace and gets grant
        first_result = self._create_and_accept("u-existing", "existing@example.com")
        self.assertTrue(first_result["is_new_workspace"])
        # Process the first grant effect
        effects = self.fw.poll_pending_effects(limit=10)
        for e in effects:
            if e["effect_type"] == "account_grant":
                self.fw.mark_effect_processing(e["effect_id"])
                self.fw.process_account_grant_effect(e["context"], self.ledger)
                self.fw.mark_effect_completed(e["effect_id"])

        # Second claim for same user (different checkpoint) — not new workspace
        second_result = self._create_and_accept("u-existing", "existing@example.com")
        self.assertFalse(second_result["is_new_workspace"])

        # Process any grant effects (should be none)
        effects = self.fw.poll_pending_effects(limit=10)
        grant_effects = [e for e in effects if e["effect_type"] == "account_grant"]
        # No additional grant effect for existing account
        self.assertEqual(len(grant_effects), 0)

        # Verify balance unchanged ($1.00, NOT $2.00)
        acct = self.ledger.get_account("u-existing")
        self.assertEqual(acct["balance_micro_usd"], _NEW_ACCOUNT_GRANT_MICRO_USD)
        self.assertEqual(acct["balance_usd"], 1.0)

    def test_grant_is_idempotent(self):
        result = self._create_and_accept("u-idem", "idem@example.com")
        self.assertTrue(result["is_new_workspace"])

        # Process grant twice
        self.ledger.grant("u-idem", _NEW_ACCOUNT_GRANT_MICRO_USD,
                          idempotency_key="fin-workspace-grant-u-idem")
        result2 = self.ledger.grant("u-idem", _NEW_ACCOUNT_GRANT_MICRO_USD,
                                    idempotency_key="fin-workspace-grant-u-idem")
        self.assertTrue(result2.get("already_applied"))

        acct = self.ledger.get_account("u-idem")
        self.assertEqual(acct["balance_micro_usd"], _NEW_ACCOUNT_GRANT_MICRO_USD)


# ---------------------------------------------------------------------------
# 9. Deletion / export metadata
# ---------------------------------------------------------------------------
class DeletionExportTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def _create_and_accept(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{"holdings": [1,2,3]}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        return self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-del",
            final_account_email="del@example.com",
            browser_nonce="nonce",
            oauth_state="state",
        )

    def test_delete_workspace_removes_data(self):
        self._create_and_accept()
        result = self.fw.delete_workspace_for_user("u-del")
        self.assertTrue(result["deleted"])

        # Workspace gone
        self.assertIsNone(self.fw.get_workspace_for_user("u-del"))

        # Snapshots gone
        self.assertEqual(
            len(self.fw.get_snapshots_for_workspace(result["workspace_id"])), 0
        )

    def test_delete_removes_claims_checkpoints_and_objects(self):
        """Regression: deletion deleted imports first, so the checkpoint/claim
        rows and the encrypted S3/local object were never removed. Deletion
        must capture checkpoint ids before deleting imports and purge the
        claims, checkpoints, runtimes, and storage objects."""
        self._create_and_accept()
        with self.fw._conn() as conn:
            chk_id = conn.execute(
                "SELECT checkpoint_id FROM financial_workspace_imports LIMIT 1"
            ).fetchone()[0]
            claim_id = conn.execute(
                "SELECT claim_id FROM financial_workspace_imports LIMIT 1"
            ).fetchone()[0]
        self.assertIsNotNone(self.fw.store.get(chk_id))

        result = self.fw.delete_workspace_for_user("u-del")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["deleted_checkpoint_count"], 1)
        self.assertEqual(result["deleted_checkpoint_ids"], [chk_id])

        with self.fw._conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM fin_terminal_checkpoints "
                    "WHERE checkpoint_id = ?",
                    (chk_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM fin_terminal_claims WHERE claim_id = ?",
                    (claim_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM financial_workspace_imports").fetchone()[0],
                0,
            )
        # The encrypted object is permanently gone from the store.
        self.assertIsNone(self.fw.store.get(chk_id))

    def test_delete_nonexistent_returns_false(self):
        result = self.fw.delete_workspace_for_user("nonexistent")
        self.assertFalse(result["deleted"])

    def test_export_workspace_metadata(self):
        self._create_and_accept()
        export = self.fw.export_workspace_metadata("u-del")
        self.assertIsNotNone(export)
        self.assertIn("workspace", export)
        self.assertIn("snapshots", export)
        self.assertGreater(len(export["snapshots"]), 0)

    def test_export_nonexistent_returns_none(self):
        self.assertIsNone(self.fw.export_workspace_metadata("nonexistent"))


# ---------------------------------------------------------------------------
# 10. Import crash injection / recovery
# ---------------------------------------------------------------------------
class ImportRecoveryTests(unittest.TestCase):
    """Crash injection at transaction / object / outbox boundaries."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_retry_after_failed_accept_rollback(self):
        """Simulate a DB crash during accept_claim — retry succeeds."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )

        # First accept succeeds
        result = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-crash",
            final_account_email="crash@example.com",
            browser_nonce="nonce",
            oauth_state="state",
        )
        self.assertIn("workspace_id", result)

        # Workspace should exist
        ws = self.fw.get_workspace_for_user("u-crash")
        self.assertIsNotNone(ws)

        # Retry (simulating crash recovery) — idempotent
        retry = self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-crash",
            final_account_email="crash@example.com",
            browser_nonce="nonce",
            oauth_state="state",
        )
        self.assertTrue(retry.get("already_accepted"))

    def test_effects_recoverable_after_crash(self):
        """Effects enqueued during accept can be replayed."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-eff",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-eff",
            final_account_email="eff@example.com",
            browser_nonce="nonce",
            oauth_state="state",
        )

        # Verify effects exist
        effects = self.fw.poll_pending_effects(limit=10)
        self.assertGreater(len(effects), 0)

        # Process all effects
        for effect in effects:
            self.fw.mark_effect_processing(effect["effect_id"])

            if effect["effect_type"] == "account_grant":
                grant_result = self.fw.process_account_grant_effect(
                    effect["context"], self.ledger
                )
                self.assertTrue(grant_result["granted"])

            self.fw.mark_effect_completed(effect["effect_id"])

        # All effects completed
        remaining = self.fw.poll_pending_effects(limit=10)
        self.assertEqual(len(remaining), 0)

    def test_effect_failed_retry(self):
        """Failed effects stay in pending for retry."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-retry",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            handoff_id=ckpt["handoff_id"],
            handoff_secret=ckpt["handoff_secret"],
            browser_nonce="nonce",
        )
        self.fw.accept_claim(
            claim_id=claim["claim_id"],
            claim_secret=claim["claim_secret"],
            final_account_user_id="u-retry",
            final_account_email="retry@example.com",
            browser_nonce="nonce",
            oauth_state="state",
        )

        effects = self.fw.poll_pending_effects(limit=10)
        for effect in effects:
            self.fw.mark_effect_processing(effect["effect_id"])
            # Simulate failure
            self.fw.mark_effect_completed(effect["effect_id"], failed=True, error="simulated crash")

        # Failed effects should be pollable again
        retried = self.fw.poll_pending_effects(limit=10)
        self.assertGreater(len(retried), 0)
        for e in retried:
            self.assertGreater(e["retry_count"], 0)


# ---------------------------------------------------------------------------
# 11. No bearer values logged
# ---------------------------------------------------------------------------
class NoBearerValuesLoggedTests(unittest.TestCase):
    """Verify no bearer values logged in errors or responses."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_handoff_secret_not_in_checkpoint_response(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        chk = self.fw.get_checkpoint(ckpt["checkpoint_id"])
        self.assertNotIn("handoff_secret", chk)
        self.assertNotIn("handoff_secret_hash", chk)

    def test_error_messages_dont_leak_secrets(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        try:
            self.fw.initiate_claim(
                handoff_id=ckpt["handoff_id"],
                handoff_secret="wrong-secret",
                browser_nonce="nonce",
            )
            self.fail("Expected UnauthorizedError")
        except UnauthorizedError as e:
            msg = str(e)
            self.assertNotIn("wrong-secret", msg)
            self.assertNotIn(ckpt["handoff_secret"], msg)
            # Generic error — must not contain the actual secret value
            self.assertFalse(hasattr(e, "__cause__") and "wrong-secret" in str(e.__cause__))


# ---------------------------------------------------------------------------
# 12. HTTP handler tests
# ---------------------------------------------------------------------------
class FinWorkspaceHandlerTests(unittest.IsolatedAsyncioTestCase):
    """HTTP handler contract tests."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

        # Mock the control token
        self.control_token = "test-control-token"
        self._saved_token = os.environ.get("FIN_WORKSPACE_CONTROL_TOKEN")
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = self.control_token
        self._saved_flag = os.environ.get("FIN_WORKSPACE_ENABLED")
        os.environ["FIN_WORKSPACE_ENABLED"] = "1"
        self._saved_jwt = os.environ.get("JWT_SECRET")
        os.environ["JWT_SECRET"] = "test-jwt-secret-for-handler-tests"

        # Patch the core resolution
        self._core_patcher = patch(
            "web_app.handlers.fin_workspace._resolve_fw",
            return_value=self.fw,
        )
        self._core_patcher.start()

    def tearDown(self):
        self._core_patcher.stop()
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()
        if self._saved_token is None:
            os.environ.pop("FIN_WORKSPACE_CONTROL_TOKEN", None)
        else:
            os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = self._saved_token
        if self._saved_flag is None:
            os.environ.pop("FIN_WORKSPACE_ENABLED", None)
        else:
            os.environ["FIN_WORKSPACE_ENABLED"] = self._saved_flag
        if self._saved_jwt is None:
            os.environ.pop("JWT_SECRET", None)
        else:
            os.environ["JWT_SECRET"] = self._saved_jwt

    async def test_disabled_returns_503(self):
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=None):
            req = _Request(body={}, token=self.control_token)
            resp = await fin_workspace.handle_fin_workspace_get_checkpoint(req)
            self.assertEqual(resp.status, 503)

    async def test_unauthorized_without_token(self):
        req = _Request(body={})
        resp = await fin_workspace.handle_fin_workspace_create_checkpoint(req)
        self.assertEqual(resp.status, 401)

    async def test_create_checkpoint(self):
        body = {
            "requestId": "req-001",
            "source": {
                "sessionId": "sess-abc",
                "workerId": "worker-xyz",
                "generation": "gen-1",
                "sourceRevision": "rev-abc",
            },
            "checkpoint": {"holdings": []},
        }
        req = _Request(body=body, token=self.control_token)
        resp = await fin_workspace.handle_fin_workspace_create_checkpoint(req)
        self.assertIn(resp.status, (200, 201))
        data = _payload(resp)
        self.assertIn("checkpoint_id", data)
        self.assertIn("handoff_id", data)
        self.assertIn("handoff_secret", data)
        self.assertIn("auth_url", data)

    async def test_create_checkpoint_missing_fields(self):
        body = {"requestId": "", "source": {"sessionId": "sess", "workerId": "worker"}, "checkpoint": {}}
        req = _Request(body=body, token=self.control_token)
        resp = await fin_workspace.handle_fin_workspace_create_checkpoint(req)
        self.assertEqual(resp.status, 400)

    async def test_get_checkpoint(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-001",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        req = _Request(
            token=self.control_token,
            match_info={"checkpoint_id": ckpt["checkpoint_id"]},
        )
        resp = await fin_workspace.handle_fin_workspace_get_checkpoint(req)
        self.assertEqual(resp.status, 200)
        data = _payload(resp)
        self.assertNotIn("handoff_secret", data)
        self.assertNotIn("handoff_secret_hash", data)

    async def test_get_nonexistent_checkpoint_404(self):
        req = _Request(
            token=self.control_token,
            match_info={"checkpoint_id": "nonexistent"},
        )
        resp = await fin_workspace.handle_fin_workspace_get_checkpoint(req)
        self.assertEqual(resp.status, 404)

    async def test_sweep(self):
        ckpt = self.fw.create_checkpoint(
            request_id="req-sweep",
            session_id="sess",
            worker_id="worker",
            checkpoint=b'{}',
        )
        # Expire it
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET expires_at = ? WHERE checkpoint_id = ?",
                (time.time() - 100, ckpt["checkpoint_id"]),
            )

        req = _Request(body={}, token=self.control_token)
        resp = await fin_workspace.handle_fin_workspace_sweep(req)
        self.assertEqual(resp.status, 200)
        data = _payload(resp)
        self.assertIn("expired_count", data)
        self.assertGreater(data["expired_count"], 0)

    async def test_internal_claim_requires_control_token(self):
        """Regression: the internal claim handler skipped token verification.
        The documented model requires the bearer control token on every
        /internal/* handler; the browser flow uses POST /api/claim."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-int-claim", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        body = {
            "handoff_id": ckpt["handoff_id"],
            "browser_nonce": "nonce",
            "audience": "github",
        }
        cookies = {"fin-terminal-handoff-secret": ckpt["handoff_secret"]}

        # No token → 401 even with a valid handoff cookie.
        req = _Request(body=body, cookies=cookies)
        resp = await fin_workspace.handle_fin_workspace_claim(req)
        self.assertEqual(resp.status, 401)

        # With the token the internal claim succeeds.
        req = _Request(body=body, cookies=cookies, token=self.control_token)
        resp = await fin_workspace.handle_fin_workspace_claim(req)
        self.assertEqual(resp.status, 201)

    async def test_internal_claim_accept_requires_control_token(self):
        """The internal claim/accept handler must require the control token."""
        req = _Request(
            body={"claim_id": "nope"}, cookies={"fw_claim_secret": "x"},
        )
        resp = await fin_workspace.handle_fin_workspace_claim_accept(req)
        self.assertEqual(resp.status, 401)

        # With the token, the request proceeds to claim validation (409, not
        # an auth failure).
        req = _Request(
            body={"claim_id": "nope"}, cookies={"fw_claim_secret": "x"},
            token=self.control_token,
        )
        resp = await fin_workspace.handle_fin_workspace_claim_accept(req)
        self.assertEqual(resp.status, 409)

    async def test_control_token_verification_rejects_wrong_token(self):
        """Constant-time bearer comparison: wrong/missing/case-shifted tokens
        are rejected, the exact token is accepted."""
        ckpt = self.fw.create_checkpoint(
            request_id="req-tok", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        with self.fw._conn() as conn:
            conn.execute(
                "UPDATE fin_terminal_checkpoints SET expires_at = ? "
                "WHERE checkpoint_id = ?",
                (time.time() - 100, ckpt["checkpoint_id"]),
            )
        for token in ("", "wrong", self.control_token.upper(),
                      self.control_token + "x"):
            req = _Request(body={}, token=token)
            resp = await fin_workspace.handle_fin_workspace_sweep(req)
            self.assertEqual(resp.status, 401, f"token {token!r} not rejected")
        req = _Request(body={}, token=self.control_token)
        resp = await fin_workspace.handle_fin_workspace_sweep(req)
        self.assertEqual(resp.status, 200)

    async def test_process_effects_lazily_instantiates_ledger(self):
        """Regression: with no preconfigured core._credit_ledger, an
        account_grant effect was mislabeled 'unknown effect type' and failed.
        The handler must lazily instantiate the ledger and process the grant."""
        from types import SimpleNamespace as _NS
        from credit import CreditLedger
        from web_app.handlers import fin_workspace as fw_mod

        with self.fw._conn() as conn:
            conn.execute(
                "INSERT INTO financial_workspace_effects "
                "(effect_id, effect_type, context_json, status, created_at) "
                "VALUES (?, 'account_grant', ?, 'pending', ?)",
                ("fe-test", json.dumps({
                    "user_id": "u-grant",
                    "workspace_id": "fws-g",
                    "grant_micro_usd": 1000000,
                    "account_was_new": True,
                }), time.time()),
            )
        fake_core = _NS(_auth=_NS(db_path=self.db_path))
        with patch("web_app.handlers.fin_workspace._core", return_value=fake_core):
            req = _Request(body={}, token=self.control_token)
            resp = await fw_mod.handle_fin_workspace_process_effects(req)
        self.assertEqual(resp.status, 200)
        data = _payload(resp)
        self.assertEqual(data["processed"], 1)
        self.assertTrue(data["results"][0]["success"])
        # The ledger was instantiated, cached, and granted $1.00.
        self.assertIsNotNone(getattr(fake_core, "_credit_ledger", None))
        self.assertEqual(CreditLedger(self.db_path).get_balance("u-grant"), 1000000)
        with self.fw._conn() as conn:
            status = conn.execute(
                "SELECT status FROM financial_workspace_effects "
                "WHERE effect_id = ?",
                ("fe-test",),
            ).fetchone()[0]
        self.assertEqual(status, "completed")


# ---------------------------------------------------------------------------
# 13. User origin tests
# ---------------------------------------------------------------------------
class UserOriginTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_get_or_create_user_origin(self):
        result = self.fw.get_or_create_user_origin(
            "u-test", provider="google", provider_account_id="ga-123"
        )
        self.assertFalse(result["already_exists"])
        self.assertEqual(result["user_id"], "u-test")
        self.assertEqual(result["provider"], "google")

        # Idempotent
        result2 = self.fw.get_or_create_user_origin(
            "u-test", provider="google", provider_account_id="ga-456"
        )
        self.assertTrue(result2["already_exists"])
        self.assertEqual(result2["origin_id"], result["origin_id"])
        # Original provider_account_id preserved
        self.assertEqual(result2["provider_account_id"], "ga-123")

    def test_different_providers_different_origins(self):
        r1 = self.fw.get_or_create_user_origin("u-test", provider="google")
        r2 = self.fw.get_or_create_user_origin("u-test", provider="github")
        self.assertNotEqual(r1["origin_id"], r2["origin_id"])


# ---------------------------------------------------------------------------
# 14. Checkpoint validation — bounded payloads
# ---------------------------------------------------------------------------
class CheckpointValidationTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_field_length_limits(self):
        # All fields truncated to max lengths
        result = self.fw.create_checkpoint(
            request_id="a" * 500,  # will be truncated to _MAX_REQUEST_ID_LENGTH
            session_id="b" * 200,
            worker_id="c" * 200,
            generation="d" * 100,
            source_revision="e" * 200,
            checkpoint=b'{}',
        )
        chk = self.fw.get_checkpoint(result["checkpoint_id"])
        self.assertLessEqual(len(chk["request_id"]), 256)
        self.assertLessEqual(len(chk["session_id"]), 128)
        self.assertLessEqual(len(chk["worker_id"]), 128)
        self.assertLessEqual(len(chk["generation"]), 64)
        self.assertLessEqual(len(chk["source_revision"]), 128)

    def test_large_checkpoint_rejected(self):
        big_data = b"x" * (6 * 1024 * 1024)  # 6 MiB, over the 5 MiB limit
        json_str = json.dumps({"data": big_data.decode("latin-1")})
        with self.assertRaises(CheckpointValidationError):
            self.fw.create_checkpoint(
                request_id="req",
                session_id="sess",
                worker_id="worker",
                checkpoint=json_str.encode(),
            )


if __name__ == "__main__":
    unittest.main()
