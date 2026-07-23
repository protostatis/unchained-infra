"""HTTP contracts for hosted credit callbacks and admin grants."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from credit import (
    CreditLedger,
    HOSTED_MODEL_POLICY_SETTING_KEY,
    HOSTED_MODEL_POLICY_VERSION,
)
from web_app.handlers import auth_admin, credit_internal
from web_app.routes import ROUTE_SPECS


class _Request:
    def __init__(self, body=None, *, token="", query=None):
        self._body = body
        self.headers = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )
        self.query = query or {}

    async def json(self):
        return self._body


def _payload(response) -> dict:
    return json.loads(response.body.decode())


class CreditHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "auth.db")
        self.ledger = CreditLedger(self.db_path)
        self._saved_token = os.environ.get("HOSTED_AGENT_SERVICE_TOKEN")
        os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = "hosted-callback-test"
        self.settings = {}
        self.setting_records = {}

        def set_app_setting(key, value, *, updated_by=""):
            self.settings[key] = value
            record = {
                "key": key,
                "value": value,
                "updated_at": 1234.5,
                "updated_by": updated_by,
            }
            self.setting_records[key] = record
            return record

        users = {
            "u-target": {"user_id": "u-target", "email": "person@example.com"},
            "u-admin-target": {
                "user_id": "u-admin-target",
                "email": "admin@example.com",
            },
        }
        self.auth = SimpleNamespace(
            db_path=self.db_path,
            find_user_by_id=lambda user_id: users.get(user_id),
            list_all_users=lambda: [
                {"user_id": "u-target", "email": "person@example.com"}
            ],
            get_app_setting=lambda key: self.settings.get(key),
            get_app_setting_record=lambda key: self.setting_records.get(key),
            set_app_setting=set_app_setting,
        )
        self.core = SimpleNamespace(
            _auth=self.auth,
            _credit_ledger=self.ledger,
            ADMIN_EMAILS=["admin@example.com"],
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
            _OPENROUTER_TRIAL_FALLBACK_MODEL="arcee-ai/trinity-large-preview:free",
            _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS=(
                "arcee-ai/trinity-large-preview:free",
                "stepfun/step-3.5-flash:free",
            ),
            _authenticate=lambda _request: {
                "user_id": "u-admin",
                "email": "admin@example.com",
            },
        )

    def tearDown(self):
        CreditLedger._instances.pop(self.db_path, None)
        self._temp_dir.cleanup()
        if self._saved_token is None:
            os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        else:
            os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = self._saved_token

    async def test_internal_submission_is_irreversible_then_settles(self):
        self.ledger.grant("u-target", 1_000_000, idempotency_key="seed")
        run = self.ledger.create_run("u-target", idempotency_key="turn")

        with patch.object(credit_internal, "_core", return_value=self.core):
            reserved_response = await credit_internal.handle_credit_reserve(
                _Request(
                    {
                        "run_id": run["run_id"],
                        "model": "google/gemini-3.1-flash-lite",
                        "idempotency_key": "attempt-1",
                    },
                    token="hosted-callback-test",
                )
            )
            self.assertEqual(reserved_response.status, 200)
            call_id = _payload(reserved_response)["call_id"]

            submitted_response = await credit_internal.handle_credit_mark_submitted(
                _Request({"call_id": call_id}, token="hosted-callback-test")
            )
            self.assertEqual(submitted_response.status, 200)
            self.assertEqual(_payload(submitted_response)["status"], "submitted")

            release_response = await credit_internal.handle_credit_release(
                _Request({"call_id": call_id}, token="hosted-callback-test")
            )
            self.assertEqual(release_response.status, 400)

            settle_response = await credit_internal.handle_credit_settle(
                _Request(
                    {
                        "call_id": call_id,
                        "actual_cost_micro_usd": 25_000,
                        "provider_cost_micro_usd": 25_000,
                    },
                    token="hosted-callback-test",
                )
            )
            self.assertEqual(settle_response.status, 200)
            self.assertEqual(_payload(settle_response)["settled_micro_usd"], 25_000)

    async def test_internal_callbacks_require_dedicated_token_and_object_json(self):
        with patch.object(credit_internal, "_core", return_value=self.core):
            unauthorized = await credit_internal.handle_credit_reserve(
                _Request({}, token="wrong-token")
            )
            malformed = await credit_internal.handle_credit_reserve(
                _Request([], token="hosted-callback-test")
            )
        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(malformed.status, 400)
        self.assertIn("object", _payload(malformed)["error"])

    async def test_internal_model_policy_rechecks_run_owner(self):
        self.ledger.grant("u-target", 2_000_000, idempotency_key="user-model-seed")
        user_run = self.ledger.create_run("u-target", idempotency_key="user-model-run")
        self.ledger.grant(
            "u-admin-target", 2_000_000, idempotency_key="admin-model-seed"
        )
        admin_run = self.ledger.create_run(
            "u-admin-target", idempotency_key="admin-model-run"
        )

        with patch.object(credit_internal, "_core", return_value=self.core):
            non_admin = await credit_internal.handle_credit_reserve(
                _Request(
                    {
                        "run_id": user_run["run_id"],
                        "model": "google/gemini-2.5-pro",
                        "idempotency_key": "user-attempt",
                    },
                    token="hosted-callback-test",
                )
            )
            admin = await credit_internal.handle_credit_reserve(
                _Request(
                    {
                        "run_id": admin_run["run_id"],
                        "model": "openai/gpt-5.2",
                        "idempotency_key": "admin-attempt",
                    },
                    token="hosted-callback-test",
                )
            )

        self.assertEqual(non_admin.status, 400)
        self.assertEqual(admin.status, 200)
        self.assertEqual(_payload(admin)["reserved_micro_usd"], 1_000_000)

    async def test_configured_unknown_model_is_allowed_for_non_admin(self):
        models = [
            "google/gemini-3.1-flash-lite",
            "arcee-ai/trinity-large-preview:free",
            "stepfun/step-3.5-flash:free",
            "openai/gpt-5.2",
        ]
        self.auth.set_app_setting(
            HOSTED_MODEL_POLICY_SETTING_KEY,
            {"version": HOSTED_MODEL_POLICY_VERSION, "models": models},
            updated_by="admin@example.com",
        )
        self.ledger.grant("u-target", 2_000_000, idempotency_key="configured-seed")
        run = self.ledger.create_run("u-target", idempotency_key="configured-run")
        with patch.object(credit_internal, "_core", return_value=self.core):
            response = await credit_internal.handle_credit_reserve(
                _Request(
                    {
                        "run_id": run["run_id"],
                        "model": "openai/gpt-5.2",
                        "idempotency_key": "configured-attempt",
                    },
                    token="hosted-callback-test",
                )
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(_payload(response)["reserved_micro_usd"], 1_000_000)

    async def test_admin_grant_replay_and_intentional_duplicate(self):
        first_request = _Request({
            "user_id": "u-target",
            "amount_usd": "1.25",
            "reason": "beta allocation",
            "operation_id": "operation-0001",
        })
        with patch.object(auth_admin, "_core", return_value=self.core):
            first = await auth_admin.handle_admin_credit_grant(first_request)
            replay = await auth_admin.handle_admin_credit_grant(first_request)
            conflict = await auth_admin.handle_admin_credit_grant(_Request({
                "user_id": "u-target",
                "amount_usd": "2.00",
                "reason": "changed retry",
                "operation_id": "operation-0001",
            }))
            duplicate = await auth_admin.handle_admin_credit_grant(_Request({
                "user_id": "u-target",
                "amount_usd": "1.25",
                "reason": "second intentional allocation",
                "operation_id": "operation-0002",
            }))

        self.assertEqual(first.status, 200)
        self.assertEqual(_payload(first)["granted_micro_usd"], 1_250_000)
        self.assertEqual(replay.status, 200)
        self.assertTrue(_payload(replay)["already_applied"])
        self.assertEqual(_payload(replay)["granted_micro_usd"], 0)
        self.assertEqual(conflict.status, 409)
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(self.ledger.get_balance("u-target"), 2_500_000)

    async def test_admin_grant_rejects_non_finite_and_missing_operation_id(self):
        with patch.object(auth_admin, "_core", return_value=self.core):
            non_finite = await auth_admin.handle_admin_credit_grant(_Request({
                "user_id": "u-target",
                "amount_usd": "NaN",
                "operation_id": "operation-nan",
            }))
            missing_id = await auth_admin.handle_admin_credit_grant(_Request({
                "user_id": "u-target",
                "amount_usd": "1.00",
            }))
        self.assertEqual(non_finite.status, 400)
        self.assertEqual(missing_id.status, 400)

    async def test_admin_user_list_includes_available_credit(self):
        self.ledger.grant("u-target", 750_000, idempotency_key="list-seed")
        with patch.object(auth_admin, "_core", return_value=self.core):
            response = await auth_admin.handle_admin_users(_Request())
        self.assertEqual(response.status, 200)
        credit = _payload(response)["users"][0]["credit"]
        self.assertEqual(credit["available_micro_usd"], 750_000)

    async def test_admin_can_read_and_replace_hosted_model_policy(self):
        models = [
            "google/gemini-3.1-flash-lite",
            "arcee-ai/trinity-large-preview:free",
            "stepfun/step-3.5-flash:free",
            "openai/gpt-5.2",
        ]
        with patch.object(auth_admin, "_core", return_value=self.core):
            initial = await auth_admin.handle_admin_hosted_models(_Request())
            saved = await auth_admin.handle_admin_hosted_models_update(
                _Request({"models": models})
            )
            reloaded = await auth_admin.handle_admin_hosted_models(_Request())
            invalid = await auth_admin.handle_admin_hosted_models_update(
                _Request({"models": ["openai/gpt-5.2"]})
            )
            self.core._authenticate = lambda _request: {
                "user_id": "u-target",
                "email": "person@example.com",
            }
            forbidden = await auth_admin.handle_admin_hosted_models_update(
                _Request({"models": models})
            )

        self.assertEqual(initial.status, 200)
        self.assertFalse(_payload(initial)["policy"]["configured"])
        self.assertEqual(saved.status, 200)
        self.assertEqual(_payload(saved)["policy"]["models"], models)
        self.assertEqual(_payload(reloaded)["policy"]["updated_by"], "admin@example.com")
        self.assertEqual(invalid.status, 400)
        self.assertIn("required models", _payload(invalid)["error"])
        self.assertEqual(forbidden.status, 403)

    def test_internal_routes_are_not_under_public_web_namespace(self):
        paths = {path for _method, path, _handler in ROUTE_SPECS}
        self.assertIn("/internal/credit/reserve", paths)
        self.assertIn("/internal/credit/submitted", paths)
        self.assertIn("/admin/settings/hosted-models", paths)
        self.assertNotIn("/web/credit/reserve", paths)
        caddy = Path(__file__).resolve().parents[1].joinpath("Caddyfile").read_text()
        self.assertIn("handle /internal/*", caddy)
        self.assertIn('respond "Not found" 404', caddy)


if __name__ == "__main__":
    unittest.main()
