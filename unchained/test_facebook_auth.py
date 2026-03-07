"""Focused tests for Facebook auth edge-cases and template gating."""

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
        req = httpx.Request("GET", "https://graph.facebook.com/test")
        resp = httpx.Response(self.status_code, request=req)
        raise httpx.HTTPStatusError("error", request=req, response=resp)


class _FakeAsyncClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    async def get(self, _url: str, params: dict | None = None):
        del params
        _FakeAsyncClient.calls += 1
        if _FakeAsyncClient.calls == 1:
            return _FakeResponse(200, {"access_token": "tok"})
        return _FakeResponse(500, {"error": "provider_down"})


class TestFacebookAuthHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_start_sets_state_cookies_when_configured(self):
        core = _FakeCore()
        req = SimpleNamespace(query={"source": "trial", "next": "/trial"}, cookies={})
        env = {"FACEBOOK_APP_ID": "app-id", "FACEBOOK_APP_SECRET": "app-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                resp = await auth_admin.handle_facebook_start(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("facebook.com", resp.headers.get("Location", ""))
        self.assertIn(auth_admin._FACEBOOK_OAUTH_STATE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._FACEBOOK_OAUTH_SOURCE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._FACEBOOK_OAUTH_NEXT_COOKIE, resp.cookies)

    async def test_callback_invalid_state_maps_error_and_clears_oauth_cookies(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"source": "trial", "state": "wrong", "code": "ok"},
            cookies={
                auth_admin._FACEBOOK_OAUTH_STATE_COOKIE: "expected",
                auth_admin._FACEBOOK_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._FACEBOOK_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"FACEBOOK_APP_ID": "app-id", "FACEBOOK_APP_SECRET": "app-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                resp = await auth_admin.handle_facebook_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=facebook_state_invalid", resp.headers.get("Location", ""))
        self.assertIn(auth_admin._FACEBOOK_OAUTH_STATE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._FACEBOOK_OAUTH_SOURCE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._FACEBOOK_OAUTH_NEXT_COOKIE, resp.cookies)

    async def test_callback_profile_http_failure_maps_profile_error(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"state": "ok", "code": "ok"},
            cookies={
                auth_admin._FACEBOOK_OAUTH_STATE_COOKIE: "ok",
                auth_admin._FACEBOOK_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._FACEBOOK_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"FACEBOOK_APP_ID": "app-id", "FACEBOOK_APP_SECRET": "app-secret"}
        _FakeAsyncClient.calls = 0
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                with patch("web_app.handlers.auth_admin.httpx.AsyncClient", _FakeAsyncClient):
                    resp = await auth_admin.handle_facebook_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=facebook_profile_failed", resp.headers.get("Location", ""))


class TestTemplateFacebookGate(unittest.TestCase):
    def test_facebook_button_hidden_by_default(self):
        template = (
            "<!DOCTYPE html><html><body><div id='login'>"
            "<div class='g_id_signin'></div><div id='loginerr'></div></div></body></html>"
        )

        with patch.dict(
            os.environ,
            {"FACEBOOK_APP_ID": "app-id", "FACEBOOK_APP_SECRET": "app-secret"},
            clear=False,
        ):
            html = template_utils.inject_google_client_id(template, "google-id")
        self.assertNotIn("data-uc-facebook-login", html)

    def test_facebook_button_requires_app_id_secret_and_ui_flag(self):
        template = (
            "<!DOCTYPE html><html><body><div id='login'>"
            "<div class='g_id_signin'></div><div id='loginerr'></div></div></body></html>"
        )

        with patch.dict(
            os.environ,
            {
                "FACEBOOK_APP_ID": "app-id",
                "FACEBOOK_APP_SECRET": "app-secret",
                "FACEBOOK_LOGIN_UI_ENABLED": "1",
            },
            clear=False,
        ):
            html = template_utils.inject_google_client_id(template, "google-id")
        self.assertIn("data-uc-facebook-login", html)

    def test_facebook_app_id_is_blank_when_ui_hidden_even_with_github(self):
        template = (
            "<!DOCTYPE html><html><body><div id='login'>"
            "<div class='g_id_signin'></div><div id='loginerr'></div></div></body></html>"
        )

        with patch.dict(
            os.environ,
            {
                "FACEBOOK_APP_ID": "app-id",
                "FACEBOOK_APP_SECRET": "app-secret",
                "GITHUB_CLIENT_ID": "gh-id",
                "GITHUB_CLIENT_SECRET": "gh-secret",
            },
            clear=False,
        ):
            html = template_utils.inject_google_client_id(template, "google-id")
        self.assertIn('var FB_APP_ID = "";', html)


if __name__ == "__main__":
    unittest.main()
