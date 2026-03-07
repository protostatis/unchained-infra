"""Focused tests for Google OAuth redirect handlers and template gating."""

from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from web_app.handlers import auth_admin
import template_utils


class _FakeCore:
    ADMIN_EMAILS = []

    def __init__(self):
        self.events: list[dict] = []

    def _cookie_secure(self, _request) -> bool:
        return False

    def _public_base_url(self, _request) -> str:
        return "https://api.unchainedsky.com"

    def _track_event(self, _request, event_name: str, **fields) -> None:
        self.events.append({"event": event_name, **fields})


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp = httpx.Response(self.status_code, request=req)
        raise httpx.HTTPStatusError("error", request=req, response=resp)


class _TokenFailAsyncClient:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    async def post(self, _url: str, data: dict | None = None, headers: dict | None = None):
        del data, headers
        return _FakeResponse(500, {"error": "provider_down"})


class TestGoogleOAuthRedirectHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_start_sets_state_cookies_when_configured(self):
        core = _FakeCore()
        req = SimpleNamespace(query={"source": "trial", "next": "/trial"}, cookies={})
        env = {"GOOGLE_CLIENT_ID": "google-id", "GOOGLE_CLIENT_SECRET": "google-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                resp = await auth_admin.handle_google_start(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("accounts.google.com", resp.headers.get("Location", ""))
        self.assertIn(auth_admin._GOOGLE_OAUTH_STATE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GOOGLE_OAUTH_SOURCE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GOOGLE_OAUTH_NEXT_COOKIE, resp.cookies)

    async def test_callback_invalid_state_maps_error_and_clears_oauth_cookies(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"source": "trial", "state": "wrong", "code": "ok"},
            cookies={
                auth_admin._GOOGLE_OAUTH_STATE_COOKIE: "expected",
                auth_admin._GOOGLE_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._GOOGLE_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"GOOGLE_CLIENT_ID": "google-id", "GOOGLE_CLIENT_SECRET": "google-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                resp = await auth_admin.handle_google_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=google_state_invalid", resp.headers.get("Location", ""))
        self.assertIn(auth_admin._GOOGLE_OAUTH_STATE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GOOGLE_OAUTH_SOURCE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GOOGLE_OAUTH_NEXT_COOKIE, resp.cookies)

    async def test_callback_token_exchange_failure_maps_exchange_error(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"state": "ok", "code": "ok"},
            cookies={
                auth_admin._GOOGLE_OAUTH_STATE_COOKIE: "ok",
                auth_admin._GOOGLE_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._GOOGLE_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"GOOGLE_CLIENT_ID": "google-id", "GOOGLE_CLIENT_SECRET": "google-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                with patch("web_app.handlers.auth_admin.httpx.AsyncClient", _TokenFailAsyncClient):
                    resp = await auth_admin.handle_google_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=google_exchange_failed", resp.headers.get("Location", ""))


class TestTemplateGoogleRedirectGate(unittest.TestCase):
    def test_google_redirect_button_requires_client_secret(self):
        template = (
            "<!DOCTYPE html><html><body><div id='login'>"
            "<div class='g_id_signin'></div><div id='loginerr'></div></div></body></html>"
        )

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLIENT_SECRET": "",
                "FACEBOOK_APP_ID": "",
                "FACEBOOK_APP_SECRET": "",
                "FACEBOOK_LOGIN_UI_ENABLED": "",
                "GITHUB_CLIENT_ID": "",
                "GITHUB_CLIENT_SECRET": "",
            },
            clear=False,
        ):
            html = template_utils.inject_google_client_id(template, "google-id")
        self.assertNotIn("/auth/google/start", html)

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLIENT_SECRET": "google-secret",
                "FACEBOOK_APP_ID": "",
                "FACEBOOK_APP_SECRET": "",
                "FACEBOOK_LOGIN_UI_ENABLED": "",
                "GITHUB_CLIENT_ID": "",
                "GITHUB_CLIENT_SECRET": "",
            },
            clear=False,
        ):
            html = template_utils.inject_google_client_id(template, "google-id")
        self.assertIn("/auth/google/start", html)
        self.assertIn("Continue with Google", html)
        self.assertIn("#login .g_id_signin{display:none !important;}", html)


if __name__ == "__main__":
    unittest.main()
