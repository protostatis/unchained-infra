"""Integration tests for the financial workspace control plane.

Covers the integrated behavior added on top of the core suite
(``test_fin_workspace.py``):

  - Fail-closed configuration (control token / S3 / region / KMS / cookie
    domain) with no JWT_SECRET or local-storage fallback
  - S3 store factory that refuses to run without explicit configuration
  - Handoff URLs never carry the bearer in the URL
  - Parent-domain HttpOnly claim cookie (Path=/, configurable domain)
  - Claim purpose/audience/state binding through the browser flow
  - New-account-only $1 credit (existing accounts receive none)
  - Account-scoped runtime wake/sleep/status scaffolding
  - Browser handoff/auth routes and the exact callback provider allowlist
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from checkpoint_store import (
    LocalCheckpointStore,
    CheckpointStoreConfigError,
    create_checkpoint_store,
    validate_s3_store_config,
)
from credit import CreditLedger
from financial_workspace import (
    FinancialWorkspace,
    ClaimRejectedError,
    _resolve_control_token,
    is_fin_workspace_enabled,
    validate_fin_workspace_config,
)
from web_app.handlers import fin_workspace
from web_app.handlers import fin_workspace_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _Request:
    def __init__(self, *, body=None, token="", cookies=None, headers=None,
                 match_info=None, query=None, path="/"):
        self._body = body
        self._headers = headers or {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._cookies = cookies or {}
        self.match_info = match_info or {}
        self.query = query or {}
        self.path = path
        self.cookies = self._cookies
        self.headers = self._headers

    async def json(self):
        return self._body


def _payload(response) -> dict:
    return json.loads(response.body.decode())


def _env_cleanup(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Fail-closed configuration
# ---------------------------------------------------------------------------
class FailClosedConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_ENABLED",
            "FIN_WORKSPACE_CONTROL_TOKEN",
            "FIN_WORKSPACE_S3_BUCKET",
            "FIN_WORKSPACE_S3_REGION",
            "FIN_WORKSPACE_KMS_KEY_ID",
            "FIN_WORKSPACE_COOKIE_DOMAIN",
            "JWT_SECRET",
        )}

    def tearDown(self):
        _env_cleanup(self._saved)

    def _enable(self, **overrides):
        os.environ["FIN_WORKSPACE_ENABLED"] = "true"
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "t" * 40
        os.environ["FIN_WORKSPACE_S3_BUCKET"] = "chk-bucket"
        os.environ["FIN_WORKSPACE_S3_REGION"] = "us-west-2"
        os.environ["FIN_WORKSPACE_KMS_KEY_ID"] = "arn:aws:kms:us-west-2:123:key/abc"
        os.environ["FIN_WORKSPACE_COOKIE_DOMAIN"] = ".unchainedsky.com"
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_disabled_config_passes(self):
        for key in ("FIN_WORKSPACE_ENABLED", "FIN_WORKSPACE_CONTROL_TOKEN",
                    "FIN_WORKSPACE_S3_BUCKET", "FIN_WORKSPACE_S3_REGION",
                    "FIN_WORKSPACE_KMS_KEY_ID", "FIN_WORKSPACE_COOKIE_DOMAIN"):
            os.environ.pop(key, None)
        self.assertFalse(is_fin_workspace_enabled())
        self.assertEqual(validate_fin_workspace_config(), [])

    def test_enabled_without_token_fails_closed(self):
        self._enable(FIN_WORKSPACE_CONTROL_TOKEN=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_CONTROL_TOKEN" in e for e in errors))
        self.assertTrue(any("never a fallback" in e for e in errors))

    def test_short_token_rejected(self):
        self._enable(FIN_WORKSPACE_CONTROL_TOKEN="short")
        errors = validate_fin_workspace_config()
        self.assertTrue(any(">= 32" in e for e in errors))

    def test_enabled_without_s3_config_fails_closed(self):
        self._enable(FIN_WORKSPACE_S3_BUCKET=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_S3_BUCKET" in e for e in errors))

    def test_enabled_without_region_fails_closed(self):
        self._enable(FIN_WORKSPACE_S3_REGION=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_S3_REGION" in e for e in errors))

    def test_enabled_without_kms_fails_closed(self):
        self._enable(FIN_WORKSPACE_KMS_KEY_ID=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_KMS_KEY_ID" in e for e in errors))

    def test_enabled_without_cookie_domain_fails_closed(self):
        self._enable(FIN_WORKSPACE_COOKIE_DOMAIN=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_COOKIE_DOMAIN" in e for e in errors))

    def test_fully_configured_passes(self):
        self._enable()
        self.assertEqual(validate_fin_workspace_config(), [])

    def test_control_token_never_falls_back_to_jwt(self):
        os.environ.pop("FIN_WORKSPACE_CONTROL_TOKEN", None)
        os.environ["JWT_SECRET"] = "jwt-secret-should-not-be-used"
        self.assertEqual(_resolve_control_token(), "")


class S3StoreFactoryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_ENABLED",
            "FIN_WORKSPACE_S3_BUCKET",
            "FIN_WORKSPACE_S3_REGION",
            "FIN_WORKSPACE_KMS_KEY_ID",
        )}

    def tearDown(self):
        _env_cleanup(self._saved)
        for key in ("FIN_WORKSPACE_ENABLED", "FIN_WORKSPACE_S3_BUCKET",
                    "FIN_WORKSPACE_S3_REGION", "FIN_WORKSPACE_KMS_KEY_ID"):
            os.environ.pop(key, None)

    def test_require_s3_missing_config_raises(self):
        for key in ("FIN_WORKSPACE_S3_BUCKET", "FIN_WORKSPACE_S3_REGION",
                    "FIN_WORKSPACE_KMS_KEY_ID"):
            os.environ.pop(key, None)
        with self.assertRaises(CheckpointStoreConfigError):
            create_checkpoint_store(require_s3=True)

    def test_require_s3_with_config_returns_s3(self):
        os.environ["FIN_WORKSPACE_S3_BUCKET"] = "chk-bucket"
        os.environ["FIN_WORKSPACE_S3_REGION"] = "us-west-2"
        os.environ["FIN_WORKSPACE_KMS_KEY_ID"] = "arn:aws:kms:us-west-2:123:key/abc"
        store = create_checkpoint_store(require_s3=True)
        self.assertEqual(type(store).__name__, "S3CheckpointStore")
        self.assertEqual(store._region, "us-west-2")

    def test_validate_s3_store_config_reports_missing(self):
        for key in ("FIN_WORKSPACE_S3_BUCKET", "FIN_WORKSPACE_S3_REGION",
                    "FIN_WORKSPACE_KMS_KEY_ID"):
            os.environ.pop(key, None)
        missing = validate_s3_store_config()
        self.assertEqual(len(missing), 3)

    def test_enabled_flag_selects_s3_never_local(self):
        os.environ["FIN_WORKSPACE_ENABLED"] = "true"
        os.environ.pop("FIN_WORKSPACE_S3_BUCKET", None)
        os.environ["FIN_WORKSPACE_S3_REGION"] = "us-west-2"
        os.environ["FIN_WORKSPACE_KMS_KEY_ID"] = "kms-key"
        # Feature on but storage incomplete ⇒ fail closed, never Local.
        with self.assertRaises(CheckpointStoreConfigError):
            create_checkpoint_store()


# ---------------------------------------------------------------------------
# Handoff URL / cookie contracts
# ---------------------------------------------------------------------------
class HandoffUrlAndCookieTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_CONTROL_TOKEN",
            "FIN_TERMINAL_BASE_URL",
            "FIN_WORKSPACE_COOKIE_DOMAIN",
        )}

    def tearDown(self):
        _env_cleanup(self._saved)
        self._temp_dir.cleanup()

    def test_auth_url_never_contains_handoff_secret(self):
        os.environ["FIN_TERMINAL_BASE_URL"] = "https://unbrowser.unchainedsky.com/fin-terminal-workspace"
        chk = self.fw.create_checkpoint(
            request_id="req-url", session_id="sess", worker_id="worker",
            checkpoint=b'{"k":1}',
        )
        self.assertIn("handoff_id", chk["auth_url"])
        self.assertNotIn(chk["handoff_secret"], chk["auth_url"])
        self.assertNotIn("handoff_secret=", chk["auth_url"])
        self.assertIn("/fin-terminal-workspace/auth/claim", chk["auth_url"])

    def test_auth_url_defaults_to_workspace_base(self):
        os.environ.pop("FIN_TERMINAL_BASE_URL", None)
        chk = self.fw.create_checkpoint(
            request_id="req-url2", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        self.assertIn("unbrowser.unchainedsky.com/fin-terminal-workspace", chk["auth_url"])

    async def test_claim_cookie_is_parent_domain_httponly(self):
        os.environ["FIN_WORKSPACE_COOKIE_DOMAIN"] = ".unchainedsky.com"
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        chk = self.fw.create_checkpoint(
            request_id="req-cookie", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            req = _Request(body={
                "handoff_id": chk["handoff_id"],
                "handoff_secret": chk["handoff_secret"],
                "browser_nonce": "nonce-123",
                "audience": "github",
            })
            resp = await fin_workspace.handle_fin_workspace_browser_claim(req)
            self.assertEqual(resp.status, 201)
            set_cookie = str(resp.cookies)
            self.assertIn("fw_claim_secret=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("Secure", set_cookie)
            self.assertIn("SameSite=Lax", set_cookie)
            self.assertIn("Path=/", set_cookie)
            self.assertIn("Domain=.unchainedsky.com", set_cookie)
            # No __Host- prefix (it requires Path=/ + no Domain).
            self.assertNotIn("__Host-", set_cookie)


# ---------------------------------------------------------------------------
# Claim purpose/audience/state binding
# ---------------------------------------------------------------------------
class ClaimBindingTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)
        self._saved_token = os.environ.get("FIN_WORKSPACE_CONTROL_TOKEN")
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "t" * 40

    def tearDown(self):
        _env_cleanup({"FIN_WORKSPACE_CONTROL_TOKEN": self._saved_token})
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def test_claim_records_audience_and_purpose(self):
        chk = self.fw.create_checkpoint(
            request_id="req-aud", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        row = self.fw.get_claim(claim["claim_id"])
        self.assertEqual(row["purpose"], "fin-workspace-claim")
        self.assertEqual(row["audience"], "github")

    def test_accept_requires_matching_oauth_state(self):
        chk = self.fw.create_checkpoint(
            request_id="req-state", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.assertTrue(self.fw.bind_oauth_state(claim["claim_id"], "real-state", audience="github"))
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim["claim_id"], claim["claim_secret"],
                final_account_user_id="u-1", final_account_email="a@b.com",
                browser_nonce="nonce", oauth_state="wrong-state",
            )

    def test_accept_with_unbound_state_stored_not_enforced(self):
        # A claim never bound to a provider state (legacy/internal path) keeps
        # working with the claim secret as the sole bearer; the strict binding
        # check applies only when a state was bound at start.
        chk = self.fw.create_checkpoint(
            request_id="req-state2", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.assertEqual(self.fw.get_claim(claim["claim_id"])["oauth_state_hash"], "")
        result = self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id="u-1", final_account_email="a@b.com",
            browser_nonce="nonce", oauth_state="state-late",
        )
        self.assertTrue(result["is_new_workspace"])

    def test_accept_with_bound_state_but_missing_state_rejected(self):
        chk = self.fw.create_checkpoint(
            request_id="req-state3", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.assertTrue(self.fw.bind_oauth_state(claim["claim_id"], "bound-state", audience="github"))
        with self.assertRaises(ClaimRejectedError):
            self.fw.accept_claim(
                claim["claim_id"], claim["claim_secret"],
                final_account_user_id="u-1", final_account_email="a@b.com",
                browser_nonce="nonce", oauth_state="",
            )

    def test_provider_start_allowlist(self):
        async def run():
            with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
                 patch("web_app.handlers.fin_workspace_auth._resolve_fw", return_value=self.fw), \
                 patch("web_app.core.get_core") as _core_mock:
                core = SimpleNamespace(GOOGLE_CLIENT_ID="gid")
                _core_mock.return_value = core
                chk = self.fw.create_checkpoint(
                    request_id="req-prov", session_id="sess", worker_id="worker",
                    checkpoint=b'{}',
                )
                claim = self.fw.initiate_claim(
                    chk["handoff_id"], chk["handoff_secret"],
                    browser_nonce="nonce", audience="github",
                )
                req = _Request(
                    cookies={"fw_claim_secret": claim["claim_secret"]},
                    match_info={"provider": "notreal"},
                    query={"claim_id": claim["claim_id"]},
                )
                resp = await fin_workspace_auth.handle_claim_oauth_start(req)
                return resp.status, claim
        import asyncio
        status, _claim = asyncio.run(run())
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# New-account-only credit
# ---------------------------------------------------------------------------
class NewAccountOnlyCreditTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        CreditLedger._instances.clear()
        self.ledger = CreditLedger(self.db_path)
        self._saved_token = os.environ.get("FIN_WORKSPACE_CONTROL_TOKEN")
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "t" * 40

    def tearDown(self):
        _env_cleanup({"FIN_WORKSPACE_CONTROL_TOKEN": self._saved_token})
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()

    def _claim_accept(self, user_id, email, *, request_id):
        chk = self.fw.create_checkpoint(
            request_id=request_id, session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.fw.bind_oauth_state(claim["claim_id"], "state-1", audience="github")
        result = self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id=user_id, final_account_email=email,
            browser_nonce="nonce", oauth_state="state-1",
        )
        return result

    def _process_effects(self):
        for effect in self.fw.poll_pending_effects(limit=10):
            eid = effect["effect_id"]
            if not self.fw.mark_effect_processing(eid):
                continue
            try:
                if effect["effect_type"] == "account_grant":
                    self.fw.process_account_grant_effect(effect["context"], self.ledger)
                self.fw.mark_effect_completed(eid)
            except Exception as exc:
                self.fw.mark_effect_completed(eid, failed=True, error=str(exc))

    def test_new_account_gets_exactly_one_grant(self):
        result = self._claim_accept("u-new", "new@example.com", request_id="req-grant-1")
        self.assertTrue(result["is_new_workspace"])
        self._process_effects()
        self.assertEqual(self.ledger.get_balance("u-new"), 1_000_000)
        # Idempotent: no second grant.
        self._process_effects()
        self.assertEqual(self.ledger.get_balance("u-new"), 1_000_000)

    def test_existing_account_receives_no_grant(self):
        # The account already exists before any claim.
        self.ledger.ensure_account("u-existing")
        result = self._claim_accept("u-existing", "existing@example.com", request_id="req-grant-2")
        self.assertTrue(result["is_new_workspace"])
        self._process_effects()
        # Existing account received none.
        self.assertEqual(self.ledger.get_balance("u-existing"), 0)
        effects = self.fw.poll_pending_effects(limit=10)
        self.assertNotIn("account_grant", [e["effect_type"] for e in effects])


# ---------------------------------------------------------------------------
# Account-scoped runtime control
# ---------------------------------------------------------------------------
class RuntimeControlTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)

    def tearDown(self):
        self._temp_dir.cleanup()

    def _create_workspace(self, user_id, email):
        chk = self.fw.create_checkpoint(
            request_id=f"req-rt-{user_id}", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.fw.bind_oauth_state(claim["claim_id"], "state-rt", audience="github")
        return self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id=user_id, final_account_email=email,
            browser_nonce="nonce", oauth_state="state-rt",
        )

    def test_wake_sleep_status_cycle(self):
        self._create_workspace("u-rt", "rt@example.com")
        asleep = self.fw.runtime_status("u-rt")
        self.assertEqual(asleep["runtime_state"], "asleep")

        awake = self.fw.runtime_wake("u-rt", reason="canary-test")
        self.assertEqual(awake["runtime_state"], "awake")
        self.assertEqual(awake["wake_reason"], "canary-test")

        back = self.fw.runtime_sleep("u-rt", reason="idle")
        self.assertEqual(back["runtime_state"], "asleep")
        self.assertEqual(back["sleep_reason"], "idle")

    def test_runtime_status_no_workspace_returns_none(self):
        self.assertIsNone(self.fw.runtime_status("no-such-user"))
        self.assertIsNone(self.fw.runtime_wake("no-such-user"))


# ---------------------------------------------------------------------------
# Browser routes / callback allowlist / no bearer in URL
# ---------------------------------------------------------------------------
class BrowserRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_CONTROL_TOKEN",
            "FIN_WORKSPACE_COOKIE_DOMAIN",
        )}

    def tearDown(self):
        _env_cleanup(self._saved)
        self._temp_dir.cleanup()

    async def test_browser_claim_requires_allowlisted_audience(self):
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        chk = self.fw.create_checkpoint(
            request_id="req-br", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            req = _Request(body={
                "handoff_id": chk["handoff_id"],
                "handoff_secret": chk["handoff_secret"],
                "browser_nonce": "nonce",
                "audience": "evil-provider",
            })
            resp = await fin_workspace.handle_fin_workspace_browser_claim(req)
            self.assertEqual(resp.status, 400)

    async def test_auth_claim_page_never_echoes_secrets(self):
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            req = _Request()
            resp = await fin_workspace.handle_fin_workspace_auth_claim_page(req)
            self.assertEqual(resp.status, 200)
            # No bearer value in the page: the secret travels via postMessage
            # into the POST body, never in the URL or rendered state.
            self.assertNotIn("handoff_secret=", resp.text)
            self.assertNotIn("?handoff_secret", resp.text)
            # The claim initiation is a POST with the secret in the body.
            self.assertIn('method: "POST"', resp.text)
            self.assertIn("handoff_secret: handoffSecretFromParent()", resp.text)

    async def test_browser_claim_cookie_owns_claim_read(self):
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        chk = self.fw.create_checkpoint(
            request_id="req-own", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            # Wrong cookie cannot read the claim.
            req = _Request(cookies={"fw_claim_secret": "wrong"}, match_info={"claim_id": claim["claim_id"]})
            resp = await fin_workspace.handle_fin_workspace_browser_get_claim(req)
            self.assertEqual(resp.status, 403)
            # Correct cookie reads it.
            req = _Request(cookies={"fw_claim_secret": claim["claim_secret"]}, match_info={"claim_id": claim["claim_id"]})
            resp = await fin_workspace.handle_fin_workspace_browser_get_claim(req)
            self.assertEqual(resp.status, 200)
            self.assertEqual(_payload(resp)["claim_id"], claim["claim_id"])

    async def test_callback_unlisted_provider_404(self):
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace_auth._resolve_fw", return_value=self.fw):
            req = _Request(match_info={"provider": "dropbox"})
            resp = await fin_workspace_auth.handle_claim_oauth_callback(req)
            self.assertEqual(resp.status, 404)


# ---------------------------------------------------------------------------
# Web startup fail-closed
# ---------------------------------------------------------------------------
class WebStartupFailClosedTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_ENABLED",
            "FIN_WORKSPACE_CONTROL_TOKEN",
            "FIN_WORKSPACE_S3_BUCKET",
            "FIN_WORKSPACE_S3_REGION",
            "FIN_WORKSPACE_KMS_KEY_ID",
            "FIN_WORKSPACE_COOKIE_DOMAIN",
            "JWT_SECRET",
        )}
        os.environ["JWT_SECRET"] = "test-jwt-for-web-import"

    def tearDown(self):
        _env_cleanup(self._saved)

    def test_create_app_refuses_misconfigured_feature(self):
        os.environ["FIN_WORKSPACE_ENABLED"] = "true"
        os.environ.pop("FIN_WORKSPACE_CONTROL_TOKEN", None)
        os.environ.pop("FIN_WORKSPACE_S3_BUCKET", None)
        os.environ.pop("FIN_WORKSPACE_S3_REGION", None)
        os.environ.pop("FIN_WORKSPACE_KMS_KEY_ID", None)
        os.environ.pop("FIN_WORKSPACE_COOKIE_DOMAIN", None)
        import web as web_module
        with patch.object(web_module, "_fin_workspace", None):
            with self.assertRaises(RuntimeError) as ctx:
                web_module._init_fin_workspace_control_plane()
            self.assertIn("refusing to start", str(ctx.exception))
            self.assertIn("FIN_WORKSPACE_CONTROL_TOKEN", str(ctx.exception))
            self.assertIn("FIN_WORKSPACE_S3_BUCKET", str(ctx.exception))


# ---------------------------------------------------------------------------
# Auth-code claim bindings (purpose/audience/claim_id/state_binding)
# ---------------------------------------------------------------------------
class AuthCodeClaimBindingTests(unittest.IsolatedAsyncioTestCase):
    """The auth_codes purpose/audience/claim_id/state_binding columns added by
    the control plane are enforced at token exchange time."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        from auth import Auth
        self.auth = Auth(self.db_path)
        # The control plane's additive migration adds purpose/audience/claim_id/
        # state_binding to auth_codes (same pattern as production startup).
        self.fw = FinancialWorkspace(self.db_path, LocalCheckpointStore())
        # Seed a user row.
        with self.auth._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id, email, api_key, status, created_at, "
                "last_login_at) VALUES ('u-code', 'code@example.com', 'key', "
                "'approved', ?, ?)",
                (time.time(), time.time()),
            )
        self._saved_jwt = os.environ.get("JWT_SECRET")
        os.environ["JWT_SECRET"] = "test-jwt-secret-for-auth-code"

    def tearDown(self):
        _env_cleanup({"JWT_SECRET": self._saved_jwt})
        self._temp_dir.cleanup()

    def _fake_core(self):
        from web_app.handlers import auth_admin
        return SimpleNamespace(
            _auth=self.auth,
            _authenticate=lambda request: None,
            GOOGLE_CLIENT_ID="",
            JWT_SECRET=os.environ.get("JWT_SECRET", ""),
            _public_base_url=lambda request: "https://unchainedsky.com",
            _PUBLIC_BASE_URL="https://unchainedsky.com",
        )

    def _issue(self, **bindings):
        from web_app.handlers import auth_admin
        with patch("web_app.handlers.auth_admin._core", return_value=self._fake_core()):
            return auth_admin._issue_auth_code(
                {"user_id": "u-code"}, "https://app.example/cb", "workspace",
                **bindings,
            )

    async def _exchange(self, code, **bindings):
        from web_app.handlers import auth_admin
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://app.example/cb",
            **bindings,
        }
        req = _Request(body=body)
        with patch("web_app.handlers.auth_admin._core", return_value=self._fake_core()):
            return await auth_admin.handle_auth_token(req)

    async def test_bound_code_exchanges_with_matching_bindings(self):
        code = self._issue(
            purpose="fin-workspace-claim",
            audience="github",
            claim_id="fcl-123",
            state_binding="oauth-state-abc",
        )
        resp = await self._exchange(
            code,
            purpose="fin-workspace-claim",
            audience="github",
            claim_id="fcl-123",
            state_binding="oauth-state-abc",
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("access_token", json.loads(resp.body))

    async def test_bound_code_rejects_mismatched_state(self):
        code = self._issue(
            purpose="fin-workspace-claim",
            audience="github",
            claim_id="fcl-123",
            state_binding="oauth-state-abc",
        )
        resp = await self._exchange(
            code,
            purpose="fin-workspace-claim",
            audience="github",
            claim_id="fcl-123",
            state_binding="wrong-state",
        )
        self.assertEqual(resp.status, 400)
        self.assertIn("state_binding mismatch", resp.body.decode())

    async def test_bound_code_rejects_wrong_audience(self):
        code = self._issue(audience="github", state_binding="s-1")
        resp = await self._exchange(code, audience="facebook", state_binding="s-1")
        self.assertEqual(resp.status, 400)
        self.assertIn("audience mismatch", resp.body.decode())


if __name__ == "__main__":
    unittest.main()
