"""Focused tests for GitHub auth edge-cases and template gating."""

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

    def _cookie_domain(self, _request) -> str | None:
        return None

    def _track_event(self, _request, event_name: str, **fields) -> None:
        self.events.append({"event": event_name, **fields})


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        req = httpx.Request("GET", "https://api.github.com/test")
        resp = httpx.Response(self.status_code, request=req)
        raise httpx.HTTPStatusError("error", request=req, response=resp)


class _ProfileFailAsyncClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    async def post(self, _url: str, data: dict | None = None, headers: dict | None = None):
        del data, headers
        _ProfileFailAsyncClient.calls += 1
        return _FakeResponse(200, {"access_token": "tok"})

    async def get(self, _url: str, headers: dict | None = None):
        del headers
        return _FakeResponse(500, {"error": "provider_down"})


class _MissingEmailAsyncClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    async def post(self, _url: str, data: dict | None = None, headers: dict | None = None):
        del data, headers
        return _FakeResponse(200, {"access_token": "tok"})

    async def get(self, _url: str, headers: dict | None = None):
        del headers
        _MissingEmailAsyncClient.calls += 1
        if _MissingEmailAsyncClient.calls == 1:
            return _FakeResponse(
                200,
                {
                    "login": "octocat",
                    "name": "Octo Cat",
                    "email": None,
                    "avatar_url": "https://avatars.githubusercontent.com/u/1",
                },
            )
        return _FakeResponse(200, [])


class TestGithubAuthHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_start_sets_state_cookies_when_configured(self):
        core = _FakeCore()
        req = SimpleNamespace(query={"source": "trial", "next": "/trial"}, cookies={})
        env = {"GITHUB_CLIENT_ID": "gh-id", "GITHUB_CLIENT_SECRET": "gh-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                resp = await auth_admin.handle_github_start(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("github.com/login/oauth/authorize", resp.headers.get("Location", ""))
        self.assertIn(auth_admin._GITHUB_OAUTH_STATE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GITHUB_OAUTH_SOURCE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GITHUB_OAUTH_NEXT_COOKIE, resp.cookies)

    async def test_callback_invalid_state_maps_error_and_clears_oauth_cookies(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"source": "trial", "state": "wrong", "code": "ok"},
            cookies={
                auth_admin._GITHUB_OAUTH_STATE_COOKIE: "expected",
                auth_admin._GITHUB_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._GITHUB_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"GITHUB_CLIENT_ID": "gh-id", "GITHUB_CLIENT_SECRET": "gh-secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                resp = await auth_admin.handle_github_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=github_state_invalid", resp.headers.get("Location", ""))
        self.assertIn(auth_admin._GITHUB_OAUTH_STATE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GITHUB_OAUTH_SOURCE_COOKIE, resp.cookies)
        self.assertIn(auth_admin._GITHUB_OAUTH_NEXT_COOKIE, resp.cookies)

    async def test_callback_profile_http_failure_maps_profile_error(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"state": "ok", "code": "ok"},
            cookies={
                auth_admin._GITHUB_OAUTH_STATE_COOKIE: "ok",
                auth_admin._GITHUB_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._GITHUB_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"GITHUB_CLIENT_ID": "gh-id", "GITHUB_CLIENT_SECRET": "gh-secret"}
        _ProfileFailAsyncClient.calls = 0
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                with patch("web_app.handlers.auth_admin.httpx.AsyncClient", _ProfileFailAsyncClient):
                    resp = await auth_admin.handle_github_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=github_profile_failed", resp.headers.get("Location", ""))

    async def test_callback_missing_email_maps_email_required(self):
        core = _FakeCore()
        req = SimpleNamespace(
            query={"state": "ok", "code": "ok"},
            cookies={
                auth_admin._GITHUB_OAUTH_STATE_COOKIE: "ok",
                auth_admin._GITHUB_OAUTH_SOURCE_COOKIE: "trial",
                auth_admin._GITHUB_OAUTH_NEXT_COOKIE: "/trial",
            },
        )
        env = {"GITHUB_CLIENT_ID": "gh-id", "GITHUB_CLIENT_SECRET": "gh-secret"}
        _MissingEmailAsyncClient.calls = 0
        with patch.dict(os.environ, env, clear=False):
            with patch("web_app.handlers.auth_admin._core", return_value=core):
                with patch("web_app.handlers.auth_admin.httpx.AsyncClient", _MissingEmailAsyncClient):
                    resp = await auth_admin.handle_github_callback(req)

        self.assertEqual(resp.status, 302)
        self.assertIn("auth_error=github_email_required", resp.headers.get("Location", ""))


class TestTemplateGithubGate(unittest.TestCase):
    def test_github_button_requires_client_id_and_secret(self):
        template = (
            "<!DOCTYPE html><html><body><div id='login'>"
            "<div class='g_id_signin'></div><div id='loginerr'></div></div></body></html>"
        )

        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "gh-id"}, clear=False):
            with patch.dict(os.environ, {"GITHUB_CLIENT_SECRET": ""}, clear=False):
                html = template_utils.inject_google_client_id(template, "google-id")
        self.assertNotIn("/auth/github/start", html)

        with patch.dict(
            os.environ,
            {"GITHUB_CLIENT_ID": "gh-id", "GITHUB_CLIENT_SECRET": "gh-secret"},
            clear=False,
        ):
            html = template_utils.inject_google_client_id(template, "google-id")
        self.assertIn("/auth/github/start", html)
        self.assertIn("margin:10px auto 0", html)
        self.assertIn("width:min(100%,320px)", html)

    def test_dev_auth_flag_is_server_rendered(self):
        template = "<script>const devAuthEnabled = __DEV_AUTH_ENABLED__;</script>"

        prod_html = template_utils.inject_google_client_id(template, "google-id")
        self.assertIn("const devAuthEnabled = false;", prod_html)
        self.assertNotIn("__DEV_AUTH_ENABLED__", prod_html)

        dev_html = template_utils.inject_google_client_id(template, "")
        self.assertIn("const devAuthEnabled = true;", dev_html)
        self.assertNotIn("__DEV_AUTH_ENABLED__", dev_html)


if __name__ == "__main__":
    unittest.main()
