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
    parse_feature_flag,
    validate_fin_workspace_config,
    workspace_runtime_slug,
)
from web_app.handlers import fin_workspace
from web_app.handlers import fin_workspace_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _Request:
    def __init__(self, *, body=None, token="", cookies=None, headers=None,
                 match_info=None, query=None, path="/", method="GET"):
        self._body = body
        self._headers = headers or {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._cookies = cookies or {}
        self.match_info = match_info or {}
        self.query = query or {}
        self.path = path
        self.method = method
        self.cookies = self._cookies
        self.headers = self._headers

    async def json(self):
        return self._body


def _payload(response) -> dict:
    return json.loads(response.body.decode())


async def _async_read():
    return b""


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
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
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
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
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

    def test_enabled_without_runtime_provider_fails_closed(self):
        """Hard enablement gate: activating the workspace feature requires a
        configured host-side runtime provider; without one the private leg
        (/fin-terminal/) would falsely route to the marketing index."""
        self._enable(FIN_WORKSPACE_RUNTIME_PROVIDER_URL=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_RUNTIME_PROVIDER_URL" in e for e in errors))

    def test_enabled_without_runtime_provider_token_fails_closed(self):
        self._enable(FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN=None)
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN" in e for e in errors))

    def test_short_runtime_provider_token_rejected(self):
        self._enable(FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN="short")
        errors = validate_fin_workspace_config()
        self.assertTrue(any("FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN" in e for e in errors))


class FeatureFlagBooleanContractTests(unittest.TestCase):
    """The feature-flag contract accepts 1|true|yes|on (trimmed, case-
    insensitive); everything else is off. Caddy/Compose use canonical ``true``
    while every runtime consumer normalizes all four spellings."""

    def test_truthy_spellings_accepted(self):
        for value in ("1", "true", "TRUE", "yes", "on", " True ", " YES "):
            self.assertTrue(parse_feature_flag(value), f"expected {value!r} truthy")

    def test_falsy_and_invalid_rejected(self):
        for value in ("0", "false", "no", "off", "", "garbage", "2", None):
            self.assertFalse(parse_feature_flag(value), f"expected {value!r} falsy")

    def test_env_flag_uses_contract(self):
        saved = os.environ.get("FIN_WORKSPACE_ENABLED")
        try:
            for value in ("1", "true", "yes", "on"):
                os.environ["FIN_WORKSPACE_ENABLED"] = value
                self.assertTrue(is_fin_workspace_enabled(), f"env={value!r}")
            os.environ["FIN_WORKSPACE_ENABLED"] = "0"
            self.assertFalse(is_fin_workspace_enabled())
            os.environ["FIN_WORKSPACE_ENABLED"] = "on"
        finally:
            if saved is None:
                os.environ.pop("FIN_WORKSPACE_ENABLED", None)
            else:
                os.environ["FIN_WORKSPACE_ENABLED"] = saved


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
        self.assertIn("/fin-terminal-workspace/workspace/auth/claim", chk["auth_url"])

    def test_auth_url_defaults_to_workspace_base(self):
        os.environ.pop("FIN_TERMINAL_BASE_URL", None)
        chk = self.fw.create_checkpoint(
            request_id="req-url2", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        self.assertIn("unbrowser.unchainedsky.com/fin-terminal-workspace", chk["auth_url"])

    async def test_create_checkpoint_response_is_canonical_snake_case(self):
        """The S2S create response is snake_case and expires_at is epoch seconds."""
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            req = _Request(body={
                "requestId": "req-wire",
                "source": {"sessionId": "sess", "workerId": "worker", "generation": "gen-1"},
                "checkpoint": {"k": 1},
            }, token="tok")
            resp = await fin_workspace.handle_fin_workspace_create_checkpoint(req)
            self.assertEqual(resp.status, 201)
            data = _payload(resp)
            # Canonical snake_case wire keys only.
            self.assertIn("checkpoint_id", data)
            self.assertIn("expires_at", data)
            self.assertIn("handoff_id", data)
            self.assertIn("handoff_secret", data)
            self.assertIn("auth_url", data)
            self.assertNotIn("checkpointId", data)
            self.assertNotIn("expiresAt", data)
            self.assertNotIn("handoffSecret", data)
            self.assertNotIn("authUrl", data)
            # expires_at is Unix epoch SECONDS (matches the control plane's
            # time.time() base), not milliseconds.
            self.assertIsInstance(data["expires_at"], float)
            self.assertGreater(data["expires_at"], 1_600_000_000)
            self.assertLess(data["expires_at"], 9_000_000_000)

    def test_public_paths_match_caddy_prefix_stripping_and_routes(self):
        """auth_url / oauth start / callback / done all live under the Caddy
        /fin-terminal-workspace prefix and strip to the exact dedicated
        /workspace/* handler routes (never /auth/... or /api/...)."""
        from web_app.routes import ROUTE_SPECS
        public = {(m, p) for m, p, _ in ROUTE_SPECS if not p.startswith("/internal")}
        for expected in (
            ("GET", "/workspace/auth/claim"),
            ("POST", "/workspace/claim"),
            ("GET", "/workspace/claims/{claim_id}"),
            ("GET", "/workspace/workspace"),
            ("GET", "/workspace/snapshots"),
            ("GET", "/workspace/runtime/status"),
            ("POST", "/workspace/oauth/google"),
            ("GET", "/workspace/done"),
            ("GET", "/workspace/oauth/{provider}/start"),
            ("GET", "/workspace/oauth/{provider}/callback"),
            ("GET", "/terminal"),
            ("GET", "/terminal/{tail:.*}"),
        ):
            self.assertIn(expected, public, f"handler route {expected} not registered")

        os.environ["FIN_TERMINAL_BASE_URL"] = "https://unbrowser.unchainedsky.com/fin-terminal-workspace"
        chk = self.fw.create_checkpoint(
            request_id="req-path", session_id="sess", worker_id="w",
            checkpoint=b'{}',
        )
        self.assertIn("/fin-terminal-workspace/workspace/auth/claim", chk["auth_url"])
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="n", audience="github",
        )
        start = fin_workspace._claim_oauth_start_url(claim["claim_id"], "github")
        self.assertIn("/fin-terminal-workspace/workspace/oauth/github/start", start)
        done = fin_workspace_auth._claim_done_url(claim["claim_id"], "accepted")
        self.assertIn("/fin-terminal-workspace/workspace/done", done)
        callback = f"{fin_workspace_auth._claim_callback_base_url()}/workspace/oauth/github/callback"
        self.assertEqual(
            callback,
            "https://unbrowser.unchainedsky.com/fin-terminal-workspace/workspace/oauth/github/callback",
        )

    async def test_claim_requires_handoff_cookie_and_rotates_it(self):
        os.environ["FIN_WORKSPACE_COOKIE_DOMAIN"] = ".unchainedsky.com"
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        chk = self.fw.create_checkpoint(
            request_id="req-cookie2", session_id="sess", worker_id="worker",
            checkpoint=b'{}',
        )
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            # No handoff cookie → 401.
            req = _Request(body={
                "handoff_id": chk["handoff_id"],
                "browser_nonce": "nonce-123",
                "audience": "github",
            })
            resp = await fin_workspace.handle_fin_workspace_browser_claim(req)
            self.assertEqual(resp.status, 401)

            # Sending the secret in the body is rejected outright.
            req = _Request(
                body={
                    "handoff_id": chk["handoff_id"],
                    "handoff_secret": chk["handoff_secret"],
                    "browser_nonce": "nonce-123",
                    "audience": "github",
                },
                cookies={"fin-terminal-handoff-secret": chk["handoff_secret"]},
            )
            resp = await fin_workspace.handle_fin_workspace_browser_claim(req)
            self.assertEqual(resp.status, 400)

            # Correct cookie → claim created; handoff cookie rotated away and
            # the claim cookie set (HttpOnly parent-domain).
            req = _Request(
                body={
                    "handoff_id": chk["handoff_id"],
                    "browser_nonce": "nonce-123",
                    "audience": "github",
                },
                cookies={"fin-terminal-handoff-secret": chk["handoff_secret"]},
            )
            resp = await fin_workspace.handle_fin_workspace_browser_claim(req)
            self.assertEqual(resp.status, 201)
            set_cookie = str(resp.cookies)
            # The gateway handoff cookie is cleared (host-only, no Domain).
            self.assertIn("fin-terminal-handoff-secret", set_cookie)
            # The claim secret cookie is set with the parent-domain scope.
            self.assertIn("fw_claim_secret=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("Secure", set_cookie)
            self.assertIn("SameSite=Lax", set_cookie)
            self.assertIn("Path=/", set_cookie)
            self.assertIn("Domain=.unchainedsky.com", set_cookie)
            self.assertNotIn("__Host-", set_cookie)

    async def test_auth_claim_page_never_echoes_secrets_or_postmessage(self):
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            req = _Request()
            resp = await fin_workspace.handle_fin_workspace_auth_claim_page(req)
            self.assertEqual(resp.status, 200)
            # No bearer value in the page, no postMessage secret path: the
            # handoff secret is read server-side from the HttpOnly cookie.
            self.assertNotIn("handoff_secret=", resp.text)
            self.assertNotIn("?handoff_secret", resp.text)
            self.assertNotIn("handoffSecretFromParent", resp.text)
            self.assertNotIn("postMessage", resp.text)
            self.assertNotIn("fin-workspace-handoff-secret", resp.text)
            # The claim initiation is a cookie-scoped POST without the secret.
            self.assertIn('method: "POST"', resp.text)
            self.assertNotIn("handoff_secret:", resp.text)
            self.assertIn("Content-Security-Policy", resp.text)
            self.assertIn('name="referrer" content="no-referrer"', resp.text)


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
            req = _Request(
                body={
                    "handoff_id": chk["handoff_id"],
                    "browser_nonce": "nonce",
                    "audience": "evil-provider",
                },
                cookies={"fin-terminal-handoff-secret": chk["handoff_secret"]},
            )
            resp = await fin_workspace.handle_fin_workspace_browser_claim(req)
            self.assertEqual(resp.status, 400)

    async def test_auth_claim_page_never_echoes_secrets(self):
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "tok"
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw):
            req = _Request()
            resp = await fin_workspace.handle_fin_workspace_auth_claim_page(req)
            self.assertEqual(resp.status, 200)
            # No bearer value in the page and no postMessage secret path: the
            # handoff secret is read server-side from the HttpOnly cookie.
            self.assertNotIn("handoff_secret=", resp.text)
            self.assertNotIn("?handoff_secret", resp.text)
            self.assertNotIn("handoffSecretFromParent", resp.text)
            self.assertNotIn("postMessage", resp.text)
            # The claim initiation is a POST without the secret in the body.
            self.assertIn('method: "POST"', resp.text)
            self.assertNotIn("handoff_secret:", resp.text)
            self.assertIn("Content-Security-Policy", resp.text)

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
# Private workspace leg — authenticated /fin-terminal/ (fail closed, never the
# marketing index or the public singleton)
# ---------------------------------------------------------------------------
class TerminalLegTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.store = LocalCheckpointStore()
        self.fw = FinancialWorkspace(self.db_path, self.store)
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_CONTROL_TOKEN",
            "FIN_WORKSPACE_COOKIE_DOMAIN",
        )}
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "t" * 40

    def tearDown(self):
        _env_cleanup(self._saved)
        self._temp_dir.cleanup()

    def _create_workspace(self, user_id, email):
        chk = self.fw.create_checkpoint(
            request_id=f"req-leg-{user_id}", session_id="sess", worker_id="worker",
            checkpoint=b'{"holdings":[{"ticker":"AAPL","qty":10}]}',
        )
        claim = self.fw.initiate_claim(
            chk["handoff_id"], chk["handoff_secret"],
            browser_nonce="nonce", audience="github",
        )
        self.fw.bind_oauth_state(claim["claim_id"], "state-leg", audience="github")
        return self.fw.accept_claim(
            claim["claim_id"], claim["claim_secret"],
            final_account_user_id=user_id, final_account_email=email,
            browser_nonce="nonce", oauth_state="state-leg",
        )

    def _core(self, *, user_id="u-leg"):
        return SimpleNamespace(
            _authenticate=lambda request: {"user_id": user_id} if user_id else None,
        )

    async def test_leg_requires_authentication(self):
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._core",
                   return_value=self._core(user_id=None)):
            resp = await fin_workspace.handle_fin_workspace_terminal_proxy(_Request())
        self.assertEqual(resp.status, 401)
        self.assertNotIn("/fin-terminal/", resp.text)

    async def test_leg_fails_closed_without_workspace(self):
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._core",
                   return_value=self._core(user_id="u-none")):
            resp = await fin_workspace.handle_fin_workspace_terminal_proxy(_Request())
        self.assertEqual(resp.status, 404)
        self.assertNotIn("/fin-terminal/", resp.text)

    async def test_leg_fails_closed_when_provider_not_validated(self):
        """No validated runtime provider ⇒ 503 with an explicit reason and NO
        CTA — the leg never falsely routes to the marketing index."""
        self._create_workspace("u-leg", "leg@example.com")
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._core", return_value=self._core()), \
             patch("web_app.handlers.fin_workspace.runtime_provider_validate",
                   return_value=None):
            resp = await fin_workspace.handle_fin_workspace_terminal_proxy(_Request())
        self.assertEqual(resp.status, 503)
        self.assertNotIn("/fin-terminal/", resp.text)
        self.assertIn("runtime provider is not validated", resp.text)

    async def test_leg_wakes_runtime_then_proxies_with_server_derived_principal(self):
        """A validated provider wakes the account runtime and the leg proxies
        the runtime surface; the principal is derived server-side from the
        authenticated session (never a caller-supplied slug)."""
        self._create_workspace("u-leg", "leg@example.com")
        slug = workspace_runtime_slug("u-leg")
        captured: dict = {}
        async def _fake_proxy_http(request, upstream, s):
            captured.update({"upstream": upstream, "slug": s})
            from aiohttp import web as _web
            return _web.Response(status=200, text="ok")
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._core", return_value=self._core()), \
             patch("web_app.handlers.fin_workspace.runtime_provider_validate",
                   return_value={"status": "ok", "provider": "host-side-v1"}), \
             patch("web_app.handlers.fin_workspace.runtime_provider_wake",
                   return_value={"slug": slug, "state": "running"}), \
             patch("web_app.handlers.fin_workspace.runtime_provider_status",
                   return_value={"slug": slug, "state": "running"}), \
             patch("web_app.handlers.fin_workspace._proxy_http",
                   side_effect=_fake_proxy_http):
            req = _Request(match_info={"tail": ""})
            resp = await fin_workspace.handle_fin_workspace_terminal_proxy(req)
        self.assertEqual(resp.status, 200)
        # Server-derived slug — the handler never asks the caller for one.
        self.assertEqual(captured["slug"], slug)
        self.assertIn(f"fin-workspace-{slug}:8787", captured["upstream"])
        self.assertNotIn("Workspace unavailable", resp.text)

    async def test_leg_provisions_imported_checkpoint_to_provider(self):
        """wake must receive the imported workspace snapshot (checkpoint-file
        payload) — never an empty placeholder."""
        self._create_workspace("u-leg", "leg@example.com")
        slug = workspace_runtime_slug("u-leg")
        captured: dict = {}
        def _fake_wake(s, checkpoint, **kwargs):
            captured.update({"slug": s, "checkpoint": checkpoint})
            return {"slug": s, "state": "running"}
        async def _fake_proxy_http(request, upstream, s):
            from aiohttp import web as _web
            return _web.Response(status=200, text="ok")
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._core", return_value=self._core()), \
             patch("web_app.handlers.fin_workspace.runtime_provider_validate",
                   return_value={"status": "ok"}), \
             patch("web_app.handlers.fin_workspace.runtime_provider_status",
                   return_value={"slug": slug, "state": "running"}), \
             patch("web_app.handlers.fin_workspace.runtime_provider_wake",
                   side_effect=_fake_wake), \
             patch("web_app.handlers.fin_workspace._proxy_http",
                   side_effect=_fake_proxy_http):
            resp = await fin_workspace.handle_fin_workspace_terminal_proxy(
                _Request(match_info={"tail": ""})
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(captured["slug"], slug)
        self.assertEqual(
            captured["checkpoint"]["holdings"],
            [{"ticker": "AAPL", "qty": 10}],
        )

    async def test_leg_requires_running_runtime(self):
        self._create_workspace("u-leg", "leg@example.com")
        slug = workspace_runtime_slug("u-leg")
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._core", return_value=self._core()), \
             patch("web_app.handlers.fin_workspace.runtime_provider_validate",
                   return_value={"status": "ok"}), \
             patch("web_app.handlers.fin_workspace.runtime_provider_wake",
                   return_value={"slug": slug, "state": "running"}), \
             patch("web_app.handlers.fin_workspace.runtime_provider_status",
                   return_value=None):
            resp = await fin_workspace.handle_fin_workspace_terminal_proxy(
                _Request(match_info={"tail": ""})
            )
        self.assertEqual(resp.status, 503)

    async def test_proxy_injects_principal_and_strips_caller_identity(self):
        """The HTTP proxy injects ONLY the server-derived principal + proxy
        token and always strips caller-supplied versions."""
        self._create_workspace("u-leg", "leg@example.com")
        slug = workspace_runtime_slug("u-leg")
        self._saved_proxy_token = os.environ.get("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN")
        os.environ["FIN_WORKSPACE_RUNTIME_PROXY_TOKEN"] = "r" * 40
        try:
            captured = {}
            class _FakeResp:
                status = 200
                async def read(self):
                    return b"ok"
                headers = {"Content-Type": "text/plain"}
            class _Ctx:
                async def __aenter__(self):
                    return _FakeResp()
                async def __aexit__(self, *a):
                    return False

            def _fake_request(method, upstream, *, headers, data, timeout):
                captured.update({"upstream": upstream, "headers": headers, "data": data})
                return _Ctx()
            with patch("aiohttp.ClientSession") as m_session:
                session = m_session.return_value
                session.__aenter__.return_value = session
                session.__aexit__.return_value = False
                session.request.side_effect = _fake_request
                req = _Request(headers={
                    "X-Fin-Terminal-User": "forged",
                    "X-Fin-Terminal-Proxy-Token": "forged-token",
                    "X-Workspace-Runtime-Token": "internal",
                })
                req.read = _async_read  # type: ignore[attr-defined]
                resp = await fin_workspace._proxy_http(req, f"http://fin-workspace-{slug}:8787/", slug)
            self.assertEqual(resp.status, 200)
            self.assertNotIn("forged", captured["headers"].get("X-Fin-Terminal-User", ""))
            self.assertNotIn("forged-token", captured["headers"].get("X-Fin-Terminal-Proxy-Token", ""))
            self.assertEqual(captured["headers"]["X-Fin-Terminal-User"], f"account:{slug}")
            self.assertEqual(captured["headers"]["X-Fin-Terminal-Proxy-Token"], "r" * 40)
            self.assertNotIn("X-Workspace-Runtime-Token", captured["headers"])
        finally:
            _env_cleanup({"FIN_WORKSPACE_RUNTIME_PROXY_TOKEN": self._saved_proxy_token})

    async def test_ws_proxy_headers_inject_principal_and_token(self):
        """The WebSocket upstream headers carry the server-derived principal +
        proxy token (identical header set used by both the HTTP and WS
        proxies), and fail closed when the token is unconfigured."""
        self._create_workspace("u-leg", "leg@example.com")
        slug = workspace_runtime_slug("u-leg")
        self._saved_proxy_token = os.environ.get("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN")
        try:
            os.environ.pop("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN", None)
            self.assertIsNone(fin_workspace._runtime_upstream_headers(slug))
            os.environ["FIN_WORKSPACE_RUNTIME_PROXY_TOKEN"] = "r" * 40
            headers = fin_workspace._runtime_upstream_headers(slug)
            self.assertIsNotNone(headers)
            self.assertEqual(headers["X-Fin-Terminal-User"], f"account:{slug}")
            self.assertEqual(headers["X-Fin-Terminal-Proxy-Token"], "r" * 40)
        finally:
            _env_cleanup({"FIN_WORKSPACE_RUNTIME_PROXY_TOKEN": self._saved_proxy_token})

    async def test_done_page_cta_renders_only_when_provider_validated(self):
        """Done-page 'Open workspace' link must work when the feature is
        enabled (validated provider) and carry no CTA otherwise."""
        with patch("financial_workspace.runtime_provider_validate", return_value=None):
            resp = await fin_workspace_auth.handle_claim_done(
                _Request(query={"status": "accepted"})
            )
        self.assertNotIn("/fin-terminal/", resp.text)
        with patch("financial_workspace.runtime_provider_validate",
                   return_value={"status": "ok"}):
            resp = await fin_workspace_auth.handle_claim_done(
                _Request(query={"status": "accepted"})
            )
        self.assertEqual(resp.status, 200)
        self.assertIn('<a href="/fin-terminal/">Open workspace</a>', resp.text)

    async def test_flush_endpoint_persists_snapshot(self):
        self._create_workspace("u-leg", "leg@example.com")
        slug = workspace_runtime_slug("u-leg")
        body = {"slug": slug, "checkpoint": {"holdings": [{"ticker": "TSLA"}]}}
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._verify_control_token",
                   return_value=True):
            req = _Request(body=body)
            resp = await fin_workspace.handle_fin_workspace_runtime_flush(req)
        self.assertEqual(resp.status, 200)
        data = _payload(resp)
        self.assertTrue(data["ok"])
        snapshots = self.fw.get_snapshots_for_workspace(
            self.fw.get_workspace_for_user("u-leg")["workspace_id"]
        )
        self.assertEqual(snapshots[0]["snapshot"], {"holdings": [{"ticker": "TSLA"}]})

    async def test_flush_unknown_slug_404(self):
        with patch("web_app.handlers.fin_workspace._resolve_fw", return_value=self.fw), \
             patch("web_app.handlers.fin_workspace._verify_control_token",
                   return_value=True):
            req = _Request(body={"slug": "f" * 24, "checkpoint": {}})
            resp = await fin_workspace.handle_fin_workspace_runtime_flush(req)
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
            "FIN_WORKSPACE_RUNTIME_PROVIDER_URL",
            "FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN",
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

    def test_activation_refused_without_validated_runtime_provider(self):
        """Hard enablement gate: FIN_WORKSPACE_ENABLED=true cannot activate
        when the runtime provider is missing or unreachable — activation fails
        instead of falsely routing /fin-terminal/ to the marketing index."""
        os.environ["FIN_WORKSPACE_ENABLED"] = "true"
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "t" * 40
        os.environ["FIN_WORKSPACE_S3_BUCKET"] = "chk-bucket"
        os.environ["FIN_WORKSPACE_S3_REGION"] = "us-west-2"
        os.environ["FIN_WORKSPACE_KMS_KEY_ID"] = "kms-key"
        os.environ["FIN_WORKSPACE_COOKIE_DOMAIN"] = ".unchainedsky.com"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
        import web as web_module
        with patch.object(web_module, "_fin_workspace", None), \
             patch("financial_workspace.runtime_provider_validate", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                web_module._init_fin_workspace_control_plane()
            self.assertIn("runtime provider is not validated", str(ctx.exception))

    def test_activation_succeeds_with_validated_runtime_provider(self):
        os.environ["FIN_WORKSPACE_ENABLED"] = "true"
        os.environ["FIN_WORKSPACE_CONTROL_TOKEN"] = "t" * 40
        os.environ["FIN_WORKSPACE_S3_BUCKET"] = "chk-bucket"
        os.environ["FIN_WORKSPACE_S3_REGION"] = "us-west-2"
        os.environ["FIN_WORKSPACE_KMS_KEY_ID"] = "kms-key"
        os.environ["FIN_WORKSPACE_COOKIE_DOMAIN"] = ".unchainedsky.com"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_URL"] = "http://host.docker.internal:8793"
        os.environ["FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN"] = "p" * 40
        import web as web_module
        with patch.object(web_module, "_fin_workspace", None), \
             patch("financial_workspace.runtime_provider_validate",
                   return_value={"status": "ok", "provider": "host-side-v1"}), \
             patch("checkpoint_store.create_checkpoint_store") as m_store:
            web_module._init_fin_workspace_control_plane()
            self.assertIsNotNone(web_module._fin_workspace)
            m_store.assert_called_once_with(require_s3=True)


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

    async def test_feature_off_auth_code_flow_works_without_control_plane(self):
        """Regression (feature-OFF OAuth): the auth_codes binding columns are
        part of the UNCONDITIONAL Auth schema. A deployment where the financial
        workspace is disabled never initializes FinancialWorkspace, so the
        columns must already exist from ``Auth._init_db`` — otherwise issuing
        or exchanging an auth code fails with 'no such column'."""
        from auth import Auth

        temp_dir = tempfile.TemporaryDirectory()
        try:
            db_path = os.path.join(temp_dir.name, "auth-off.db")
            auth = Auth(db_path)
            with auth._conn() as conn:
                conn.execute(
                    "INSERT INTO users (user_id, email, api_key, status, "
                    "created_at, last_login_at) VALUES ('u-off', "
                    "'off@example.com', 'key', 'approved', ?, ?)",
                    (time.time(), time.time()),
                )
            # No FinancialWorkspace is ever constructed for this DB.
            core = SimpleNamespace(
                _auth=auth,
                _authenticate=lambda request: None,
                GOOGLE_CLIENT_ID="",
                JWT_SECRET=os.environ.get("JWT_SECRET", "test-jwt-secret-for-auth-code"),
                _public_base_url=lambda request: "https://unchainedsky.com",
                _PUBLIC_BASE_URL="https://unchainedsky.com",
            )
            from web_app.handlers import auth_admin

            with patch("web_app.handlers.auth_admin._core", return_value=core):
                code = auth_admin._issue_auth_code(
                    {"user_id": "u-off"}, "https://app.example/cb", "workspace"
                )
                self.assertTrue(code)
                req = _Request(body={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "https://app.example/cb",
                })
                resp = await auth_admin.handle_auth_token(req)
                self.assertEqual(resp.status, 200)
                self.assertIn("access_token", json.loads(resp.body))
        finally:
            temp_dir.cleanup()


# ---------------------------------------------------------------------------
# WS proxy Origin forwarding (blocker: real runtime accepts through the
# control plane; missing/foreign Origin is rejected before any dial)
# ---------------------------------------------------------------------------
class WsProxyOriginForwardingTests(unittest.IsolatedAsyncioTestCase):
    """Proxy-level test of the control-plane WS proxy.

    The control plane forwards the VALIDATED browser ``Origin`` when dialing
    the account runtime (the runtime enforces its own ``ALLOWED_ORIGINS`` gate
    on WebSocket upgrades), while continuing to strip caller identity/proxy
    headers and injecting only server-owned principal/token. A missing or
    foreign Origin is rejected by the control plane itself (403) before any
    dial. The runtime server below implements the app runtime's exact origin
    policy (only the configured origin is accepted on upgrade)."""

    VALID_ORIGIN = "https://unbrowser.unchainedsky.com"
    FOREIGN_ORIGIN = "https://evil.example.com"
    PROXY_TOKEN = "r" * 40
    SLUG = "a" * 24

    async def asyncSetUp(self):
        self._saved_proxy = os.environ.get("FIN_WORKSPACE_RUNTIME_PROXY_TOKEN")
        self._saved_allowed = os.environ.get("FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS")
        os.environ["FIN_WORKSPACE_RUNTIME_PROXY_TOKEN"] = self.PROXY_TOKEN
        os.environ["FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS"] = self.VALID_ORIGIN
        import aiohttp  # noqa: F401  (exercised below)

    async def asyncTearDown(self):
        _env_cleanup({
            "FIN_WORKSPACE_RUNTIME_PROXY_TOKEN": self._saved_proxy,
            "FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS": self._saved_allowed,
        })

    async def _start_runtime(self):
        """Real WS echo server that only accepts upgrades whose Origin is the
        allowed origin (mirrors the app runtime's isAllowedWebSocketRequest:
        exactly one non-null, canonical http(s) origin in ALLOWED_ORIGINS)."""
        import aiohttp
        from aiohttp import web as aio_web

        received = {"origin": None, "user": None, "token": None}

        async def handle(request):
            ws = aio_web.WebSocketResponse()
            await ws.prepare(request)
            origin = request.headers.get("Origin", "")
            if origin != self.VALID_ORIGIN:
                await ws.close(code=1008)
                return ws
            received["origin"] = origin
            received["user"] = request.headers.get("X-Fin-Terminal-User", "")
            received["token"] = request.headers.get("X-Fin-Terminal-Proxy-Token", "")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str("echo:" + msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    await ws.close()
                    break
            return ws

        app = aio_web.Application()
        app.router.add_get("/ws", handle)
        runner = aio_web.AppRunner(app)
        await runner.setup()
        site = aio_web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}/ws", received

    async def _start_proxy(self, upstream):
        """Control-plane WS proxy endpoint: browser → _proxy_websocket → runtime."""
        import aiohttp  # noqa: F401
        from aiohttp import web as aio_web
        from financial_workspace import FinancialWorkspaceRuntimeScheduler

        async def proxy_handler(request):
            scheduler = FinancialWorkspaceRuntimeScheduler.__new__(  # type: ignore[misc]
                FinancialWorkspaceRuntimeScheduler
            )
            return await fin_workspace._proxy_websocket(
                request, upstream, self.SLUG, scheduler, "u-proxy",
            )

        app = aio_web.Application()
        app.router.add_get("/ws", proxy_handler)
        runner = aio_web.AppRunner(app)
        await runner.setup()
        site = aio_web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}/ws"

    async def test_real_runtime_accepts_valid_origin_through_control_plane(self):
        import aiohttp

        runtime, runtime_url, received = await self._start_runtime()
        proxy, proxy_url = await self._start_proxy(runtime_url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    proxy_url, headers={"Origin": self.VALID_ORIGIN},
                ) as ws:
                    await ws.send_str("hello")
                    msg = await ws.receive()
            self.assertEqual(msg.data, "echo:hello")
            # The runtime saw the forwarded Origin + server-derived identity.
            self.assertEqual(received["origin"], self.VALID_ORIGIN)
            self.assertEqual(received["user"], f"account:{self.SLUG}")
            self.assertEqual(received["token"], self.PROXY_TOKEN)
        finally:
            await proxy.cleanup()
            await runtime.cleanup()

    async def test_missing_origin_rejected_by_control_plane(self):
        import aiohttp

        runtime, runtime_url, _received = await self._start_runtime()
        proxy, proxy_url = await self._start_proxy(runtime_url)
        try:
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
                    await session.ws_connect(proxy_url)
            self.assertEqual(ctx.exception.status, 403)
        finally:
            await proxy.cleanup()
            await runtime.cleanup()

    async def test_foreign_origin_rejected_by_control_plane(self):
        import aiohttp

        runtime, runtime_url, _received = await self._start_runtime()
        proxy, proxy_url = await self._start_proxy(runtime_url)
        try:
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
                    await session.ws_connect(
                        proxy_url, headers={"Origin": self.FOREIGN_ORIGIN},
                    )
            self.assertEqual(ctx.exception.status, 403)
        finally:
            await proxy.cleanup()
            await runtime.cleanup()


class ValidatedBrowserOriginTests(unittest.TestCase):
    """Unit behavior of the control plane's Origin validation gate."""

    def _req(self, origin):
        from types import SimpleNamespace as _NS
        return _NS(headers={"Origin": origin} if origin else {})

    def setUp(self):
        self._saved = os.environ.get("FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS")
        os.environ["FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS"] = (
            "https://unbrowser.unchainedsky.com"
        )

    def tearDown(self):
        _env_cleanup({"FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS": self._saved})

    def test_allowed_origin_passes(self):
        self.assertEqual(
            fin_workspace._validated_browser_origin(self._req("https://unbrowser.unchainedsky.com")),
            "https://unbrowser.unchainedsky.com",
        )

    def test_missing_origin_rejected(self):
        self.assertEqual(fin_workspace._validated_browser_origin(self._req(None)), "")

    def test_null_origin_rejected(self):
        self.assertEqual(fin_workspace._validated_browser_origin(self._req("null")), "")

    def test_foreign_origin_rejected(self):
        self.assertEqual(
            fin_workspace._validated_browser_origin(self._req("https://evil.example.com")), ""
        )

    def test_noncanonical_origin_rejected(self):
        for raw in (
            "https://unbrowser.unchainedsky.com/",
            "https://unbrowser.unchainedsky.com/path",
            "unbrowser.unchainedsky.com",
            "https://",
            "javascript:alert(1)",
        ):
            self.assertEqual(
                fin_workspace._validated_browser_origin(self._req(raw)), ""
            )

    def test_fails_closed_when_allowlist_empty(self):
        os.environ["FIN_WORKSPACE_RUNTIME_ALLOWED_ORIGINS"] = ""
        self.assertEqual(
            fin_workspace._validated_browser_origin(self._req("https://unbrowser.unchainedsky.com")),
            "",
        )


class _BreakSweepLoop(Exception):
    """Raised from the fake asyncio.sleep to stop the endless sweep loop."""


class RuntimeIdleSweepLoopTests(unittest.IsolatedAsyncioTestCase):
    """web.py's idle-sweep loop executes the REAL loop/tick seam: it resolves
    the actual in-process core scheduler (regression: an undefined ``_core``
    used to raise NameError so scheduler.tick() never ran) and drives
    ``tick()``, which schedules the durable-flush idle sleep."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "FIN_WORKSPACE_RUNTIME_SWEEP_INTERVAL_SECONDS",
            "FIN_WORKSPACE_RUNTIME_IDLE_SLEEP_SECONDS",
            "JWT_SECRET",
        )}
        os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET", "test-jwt-secret-for-sweep")

    def tearDown(self):
        _env_cleanup(self._saved)

    async def test_sweep_loop_resolves_core_and_ticks_idle_sleep(self):
        import web as web_module
        from financial_workspace import FinancialWorkspace

        tmp = tempfile.TemporaryDirectory()
        try:
            fw = FinancialWorkspace(
                os.path.join(tmp.name, "auth.db"), LocalCheckpointStore()
            )
            user_id = "u-sweep"
            # The scheduler sees exactly one awake runtime with no live
            # activity and an elapsed idle window.
            fw._iter_workspace_user_ids = lambda: [(user_id,)]
            fw.runtime_status = lambda uid: {
                "runtime_state": "awake", "last_wake_at": 0.0,
            }
            sleep_calls: list[str] = []
            fw.runtime_sleep_durable = (
                lambda uid, reason="idle", **kw: sleep_calls.append(uid)
                or {"runtime_state": "asleep", "flush": {"ok": True}}
            )

            os.environ["FIN_WORKSPACE_RUNTIME_SWEEP_INTERVAL_SECONDS"] = "15"
            os.environ["FIN_WORKSPACE_RUNTIME_IDLE_SLEEP_SECONDS"] = "60"

            sleep_count = 0

            def _fake_sleep(*_args, **_kwargs):
                nonlocal sleep_count
                sleep_count += 1
                if sleep_count > 1:
                    raise _BreakSweepLoop()

            with patch.object(web_module, "_fin_workspace", fw), \
                 patch("asyncio.sleep", side_effect=_fake_sleep):
                with self.assertRaises(_BreakSweepLoop):
                    await web_module._fin_workspace_runtime_sweep_loop()

            # The loop completed one real iteration: the real resolver
            # constructed the scheduler off the in-process core and tick()
            # invoked the durable-flush idle sleep for the idle account.
            self.assertEqual(sleep_calls, [user_id])
            self.assertEqual(sleep_count, 2)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
