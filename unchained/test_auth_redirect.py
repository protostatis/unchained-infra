"""Tests for external auth redirect and provider session behavior."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
from aiohttp import web as aiohttp_web

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-that-is-at-least-32-bytes")

import web as core_module
from web_app.handlers import auth_admin


_validate_redirect_uri = auth_admin._validate_redirect_uri


class _FakeAuth:
    def __init__(self, user: dict | None = None):
        self.user = user

    def find_user_by_id(self, user_id: str) -> dict | None:
        if self.user and self.user.get("user_id") == user_id:
            return self.user
        return None


class _FakeCore:
    GOOGLE_CLIENT_ID = "google-client"
    JWT_EXPIRY_HOURS = 24 * 30
    JWT_ABSOLUTE_EXPIRY_HOURS = 24 * 90

    def __init__(self, auth_info: dict | None = None, user: dict | None = None):
        self.auth_info = auth_info
        self._auth = _FakeAuth(user)
        self.rotated: dict | None = None
        self.cleared = False

    def _authenticate(self, _request):
        return self.auth_info

    def create_session_token(
        self,
        user_id: str,
        email: str,
        *,
        auth_time: int | None = None,
    ) -> str:
        self.rotated = {"user_id": user_id, "email": email, "auth_time": auth_time}
        return "rotated-session"

    def _set_session_cookie(
        self,
        response,
        token: str,
        _request,
        *,
        max_age: int | None = None,
    ):
        response.set_cookie(
            "uc_session",
            token,
            max_age=max_age,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )

    def _clear_session_cookie(self, response, _request):
        self.cleared = True
        response.del_cookie("uc_session", path="/")

    def inject_google_client_id(self, html: str, _client_id: str) -> str:
        return html.replace("__GOOGLE_CLIENT_ID__", "google-client")


class TestAuthRedirectUri(unittest.TestCase):
    def test_production_redirect_allowlist_includes_external_apps(self):
        for uri in [
            "https://analytics.unchainedsky.com/auth/callback",
            "https://searchagentsky.com/auth/callback",
            "https://search.unchainedsky.com/auth/callback",
            "https://searchagentsky.com/auth/callback?source=share",
        ]:
            with self.subTest(uri=uri):
                self.assertTrue(_validate_redirect_uri(uri))

    def test_redirect_allowlist_rejects_wrong_origin_path_or_fragment(self):
        for uri in [
            "https://analytics.unchainedsky.com/auth/other",
            "https://analytics.evil.example/auth/callback",
            "http://analytics.unchainedsky.com/auth/callback",
            "https://analytics.unchainedsky.com.evil.example/auth/callback",
            "https://searchagentsky.com/auth/callback#fragment",
        ]:
            with self.subTest(uri=uri):
                self.assertFalse(_validate_redirect_uri(uri))


class TestExternalAuthLogin(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(**query):
        return SimpleNamespace(query=query)

    async def test_prompt_none_requires_state(self):
        core = _FakeCore()
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            response = await auth_admin.handle_auth_login(request)

        self.assertEqual(response.status, 400)
        self.assertIn("state is required", response.text)

    async def test_state_size_is_bounded(self):
        core = _FakeCore()
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            state="x" * (auth_admin._AUTH_LOGIN_STATE_MAX_BYTES + 1),
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            response = await auth_admin.handle_auth_login(request)

        self.assertEqual(response.status, 400)
        self.assertIn("state is too large", response.text)

    async def test_prompt_none_without_session_returns_login_required(self):
        core = _FakeCore()
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            scope="signin",
            state="a" * 32,
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            response = await auth_admin.handle_auth_login(request)

        self.assertEqual(response.status, 302)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query["error"], ["login_required"])
        self.assertEqual(query["state"], ["a" * 32])
        self.assertTrue(core.cleared)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Sign in to continue", response.text)

    async def test_bearer_auth_cannot_broker_passive_sso(self):
        user = {
            "user_id": "u-bearer",
            "email": "bearer@example.com",
            "status": "approved",
        }
        core = _FakeCore(
            auth_info={
                "auth_method": "bearer",
                "user_id": "u-bearer",
                "email": "bearer@example.com",
            },
            user=user,
        )
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            state="bearer-state",
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            with patch.object(auth_admin, "_issue_auth_code") as issue_code:
                response = await auth_admin.handle_auth_login(request)

        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query["error"], ["login_required"])
        issue_code.assert_not_called()
        self.assertIsNone(core.rotated)

    async def test_bearer_auth_cannot_broker_interactive_sso(self):
        user = {
            "user_id": "u-bearer",
            "email": "bearer@example.com",
            "status": "approved",
        }
        core = _FakeCore(
            auth_info={
                "auth_method": "bearer",
                "user_id": "u-bearer",
                "email": "bearer@example.com",
            },
            user=user,
        )
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            state="interactive-state",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            with patch.object(auth_admin, "_issue_auth_code") as issue_code:
                response = await auth_admin.handle_auth_login(request)

        self.assertEqual(response.status, 200)
        self.assertIn("Sign in to continue", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        issue_code.assert_not_called()
        self.assertIsNone(core.rotated)

    async def test_existing_session_rotates_cookie_and_issues_code(self):
        auth_time = int(time.time()) - 3600
        user = {
            "user_id": "u-auth",
            "email": "person@example.com",
            "status": "approved",
        }
        core = _FakeCore(
            auth_info={
                "auth_method": "session",
                "user_id": "u-auth",
                "email": "person@example.com",
                "session_auth_time": auth_time,
            },
            user=user,
        )
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            scope="signin",
            state="b" * 32,
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            with patch.object(auth_admin, "_issue_auth_code", return_value="c" * 64):
                response = await auth_admin.handle_auth_login(request)

        self.assertEqual(response.status, 302)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query["code"], ["c" * 64])
        self.assertEqual(query["state"], ["b" * 32])
        self.assertEqual(core.rotated, {
            "user_id": "u-auth",
            "email": "person@example.com",
            "auth_time": auth_time,
        })
        self.assertEqual(response.cookies["uc_session"].value, "rotated-session")
        self.assertGreater(int(response.cookies["uc_session"]["max-age"]), 0)
        self.assertLessEqual(
            int(response.cookies["uc_session"]["max-age"]),
            core.JWT_EXPIRY_HOURS * 3600,
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_existing_callback_query_is_preserved(self):
        auth_time = int(time.time()) - 60
        user = {
            "user_id": "u-query",
            "email": "query@example.com",
            "status": "approved",
        }
        core = _FakeCore(
            auth_info={
                "auth_method": "session",
                "user_id": "u-query",
                "session_auth_time": auth_time,
            },
            user=user,
        )
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback?source=share",
            state="query-state",
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            with patch.object(auth_admin, "_issue_auth_code", return_value="q" * 64):
                response = await auth_admin.handle_auth_login(request)

        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query["source"], ["share"])
        self.assertEqual(query["code"], ["q" * 64])
        self.assertEqual(query["state"], ["query-state"])

    async def test_unapproved_session_cannot_issue_code(self):
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                user = {
                    "user_id": f"u-{status}",
                    "email": f"{status}@example.com",
                    "status": status,
                }
                core = _FakeCore(
                    auth_info={
                        "auth_method": "session",
                        "user_id": user["user_id"],
                        "session_auth_time": int(time.time()),
                    },
                    user=user,
                )
                request = self._request(
                    redirect_uri="https://searchagentsky.com/auth/callback",
                    state=f"{status}-state",
                    prompt="none",
                )

                with patch.object(auth_admin, "_core", return_value=core):
                    with patch.object(auth_admin, "_issue_auth_code") as issue_code:
                        response = await auth_admin.handle_auth_login(request)

                query = parse_qs(urlparse(response.headers["Location"]).query)
                self.assertEqual(query["error"], ["login_required"])
                issue_code.assert_not_called()

    async def test_rotated_cookie_does_not_cross_absolute_session_deadline(self):
        now = int(time.time())
        absolute_seconds = _FakeCore.JWT_ABSOLUTE_EXPIRY_HOURS * 3600
        auth_time = now - absolute_seconds + 600
        user = {
            "user_id": "u-capped",
            "email": "capped@example.com",
            "status": "approved",
        }
        core = _FakeCore(
            auth_info={
                "auth_method": "session",
                "user_id": "u-capped",
                "session_auth_time": auth_time,
            },
            user=user,
        )
        request = self._request(
            redirect_uri="https://searchagentsky.com/auth/callback",
            state="d" * 32,
            prompt="none",
        )

        with patch.object(auth_admin, "_core", return_value=core):
            with patch.object(auth_admin, "_issue_auth_code", return_value="e" * 64):
                response = await auth_admin.handle_auth_login(request)

        max_age = int(response.cookies["uc_session"]["max-age"])
        self.assertGreater(max_age, 0)
        self.assertLessEqual(max_age, 600)


class TestSessionTokenLifecycle(unittest.TestCase):
    @staticmethod
    def _decode(token: str) -> dict:
        return jwt.decode(
            token,
            core_module.JWT_SECRET,
            algorithms=[core_module.JWT_ALGORITHM],
            options={"verify_exp": False, "verify_iat": False},
        )

    def test_fresh_session_uses_refresh_window_and_absolute_origin(self):
        now = int(time.time())
        with patch.object(core_module.time, "time", return_value=now):
            token = core_module.create_session_token("u-fresh", "fresh@example.com")

        payload = self._decode(token)
        self.assertEqual(payload["iat"], now)
        self.assertEqual(payload["auth_time"], now)
        self.assertEqual(payload["exp"], now + core_module.JWT_EXPIRY_HOURS * 3600)

    def test_session_expiry_is_capped_by_original_login(self):
        now = int(time.time())
        auth_time = now - (core_module.JWT_ABSOLUTE_EXPIRY_HOURS - 24) * 3600
        with patch.object(core_module.time, "time", return_value=now):
            token = core_module.create_session_token(
                "u-capped",
                "capped@example.com",
                auth_time=auth_time,
            )

        payload = self._decode(token)
        self.assertEqual(
            payload["exp"],
            auth_time + core_module.JWT_ABSOLUTE_EXPIRY_HOURS * 3600,
        )

    def test_legacy_session_uses_iat_as_auth_time(self):
        now = int(time.time())
        token = jwt.encode(
            {
                "user_id": "u-legacy",
                "email": "legacy@example.com",
                "iat": now - 60,
                "exp": now + 3600,
            },
            core_module.JWT_SECRET,
            algorithm=core_module.JWT_ALGORITHM,
        )

        session = core_module.verify_session_token(token)
        self.assertIsNotNone(session)
        self.assertEqual(session["auth_time"], now - 60)

    def test_invalid_or_future_auth_time_is_reset(self):
        now = int(time.time())
        for auth_time in ("not-a-timestamp", now + 3600, 0, -1):
            with self.subTest(auth_time=auth_time):
                with patch.object(core_module.time, "time", return_value=now):
                    token = core_module.create_session_token(
                        "u-reset",
                        "reset@example.com",
                        auth_time=auth_time,
                    )
                payload = self._decode(token)
                self.assertEqual(payload["auth_time"], now)

    def test_malformed_signed_session_is_rejected(self):
        now = int(time.time())
        token = jwt.encode(
            {
                "user_id": "u-bad",
                "email": "bad@example.com",
                "iat": now,
                "auth_time": "not-a-timestamp",
                "exp": now + 3600,
            },
            core_module.JWT_SECRET,
            algorithm=core_module.JWT_ALGORITHM,
        )
        self.assertIsNone(core_module.verify_session_token(token))


class _AuthStore:
    def __init__(self):
        self.user = {
            "user_id": "u-method",
            "email": "method@example.com",
            "api_key": "uc_live_method",
            "status": "approved",
            "user_type": "claude",
        }

    def find_user_by_id(self, user_id: str):
        return self.user if user_id == self.user["user_id"] else None

    def validate_key(self, key: str):
        if key == self.user["api_key"]:
            return {"user_id": self.user["user_id"], "key": key}
        return None


class TestAuthenticationMethod(unittest.TestCase):
    def test_cookie_auth_is_marked_as_session(self):
        store = _AuthStore()
        request = SimpleNamespace(headers={})
        session = {
            "user_id": store.user["user_id"],
            "email": store.user["email"],
            "iat": 100,
            "auth_time": 50,
        }
        with patch.object(core_module, "_auth", store):
            with patch.object(core_module, "_session_cookie_candidates", return_value=["token"]):
                with patch.object(core_module, "verify_session_token", return_value=session):
                    result = core_module._authenticate(request)

        self.assertEqual(result["auth_method"], "session")
        self.assertEqual(result["session_auth_time"], 50)

    def test_api_key_auth_is_marked_as_bearer(self):
        store = _AuthStore()
        request = SimpleNamespace(
            headers={"Authorization": f"Bearer {store.user['api_key']}"},
        )
        with patch.object(core_module, "_auth", store):
            with patch.object(core_module, "_session_cookie_candidates", return_value=[]):
                result = core_module._authenticate(request)

        self.assertEqual(result["auth_method"], "bearer")
        self.assertNotIn("session_auth_time", result)


class TestSessionCookieScope(unittest.TestCase):
    @staticmethod
    def _production_request():
        return SimpleNamespace(
            headers={
                "X-Forwarded-Host": "unchainedsky.com",
                "X-Forwarded-Proto": "https",
            },
            host="unchainedsky.com",
            scheme="https",
        )

    def test_custom_cookie_max_age_is_applied(self):
        response = aiohttp_web.Response()
        core_module._set_session_cookie(
            response,
            "session-token",
            self._production_request(),
            max_age=123,
        )
        cookie = response.cookies["uc_session"]
        self.assertEqual(cookie["max-age"], "123")
        self.assertEqual(cookie["domain"], ".unchainedsky.com")

    def test_clear_cookie_preserves_host_and_parent_domain_deletions(self):
        response = aiohttp_web.Response()
        core_module._clear_session_cookie(response, self._production_request())

        host_headers = response.headers.getall("Set-Cookie", [])
        self.assertEqual(len(host_headers), 1)
        self.assertNotIn("Domain=", host_headers[0])
        domain_cookie = response.cookies["uc_session"].output(header="").lstrip()
        self.assertIn("Domain=.unchainedsky.com", domain_cookie)
        self.assertIn("Max-Age=0", host_headers[0])
        self.assertIn("Max-Age=0", domain_cookie)


if __name__ == "__main__":
    unittest.main()
