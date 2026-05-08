"""Smoke tests for the local dev web server flows.

Boots the real aiohttp app on a random localhost port and exercises the
same auth, status/update, installer, and claim/bootstrap actions used
during local development.
"""

from __future__ import annotations

import asyncio
import atexit
import importlib
import os
import re
import secrets
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx
from aiohttp import web as aiohttp_web

from agent_package import MIN_VERSION, VERSION


_TMP_ROOT = tempfile.mkdtemp(prefix="uc-dev-server-smoke-")
atexit.register(lambda: shutil.rmtree(_TMP_ROOT, ignore_errors=True))

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["UNCHAINED_DB_PATH"] = os.path.join(_TMP_ROOT, "auth.db")
os.environ["UNCHAINED_ANALYTICS_DB_PATH"] = os.path.join(_TMP_ROOT, "analytics.db")
os.environ["UNCHAINED_SCHEDULER_DIR"] = os.path.join(_TMP_ROOT, "scheduler")

sys.path.insert(0, os.path.dirname(__file__))

import web as _web

web = importlib.reload(_web)


class TestDevServerSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_check_relay_agent = web._check_relay_agent
        self._orig_stale_tab_cleanup_loop = web._stale_tab_cleanup_loop
        self._orig_cleanup_idle_gemini_agents = web._cleanup_idle_gemini_agents

        async def _idle_loop():
            await asyncio.sleep(3600)

        web._check_relay_agent = AsyncMock(return_value=False)
        web._stale_tab_cleanup_loop = _idle_loop
        web._cleanup_idle_gemini_agents = _idle_loop
        web._install_claims.clear()
        web._install_claim_start_hits.clear()
        web._chat_agents.clear()
        web._chat_agent_users.clear()
        web._chat_agent_caps.clear()
        web._agent_req_queues.clear()

        app = web.create_app()
        self._runner = aiohttp_web.AppRunner(app)
        await self._runner.setup()
        self._site = aiohttp_web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()

        sockets = getattr(self._site, "_server").sockets
        port = sockets[0].getsockname()[1]
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            follow_redirects=False,
            timeout=20.0,
        )

    async def asyncTearDown(self):
        await self._client.aclose()
        await self._runner.cleanup()
        web._check_relay_agent = self._orig_check_relay_agent
        web._stale_tab_cleanup_loop = self._orig_stale_tab_cleanup_loop
        web._cleanup_idle_gemini_agents = self._orig_cleanup_idle_gemini_agents
        web._install_claims.clear()
        web._install_claim_start_hits.clear()
        web._chat_agents.clear()
        web._chat_agent_users.clear()
        web._chat_agent_caps.clear()
        web._agent_req_queues.clear()

    async def _dev_login(self) -> dict:
        response = await self._client.post("/auth/dev", json={"email": "dev@localhost"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("agent_id"))
        return data

    def _extract_install_token(self, payload: dict) -> str:
        match = re.search(r'X-Install-Token: ([^"]+)', payload.get("curl_command", ""))
        self.assertIsNotNone(match, "curl_command should embed X-Install-Token header")
        return match.group(1)

    async def test_auth_and_local_pages_render_expected_dev_markers(self):
        response = await self._client.post("/web/install-token")
        self.assertEqual(response.status_code, 401)

        response = await self._client.post("/web/chat/update-client")
        self.assertEqual(response.status_code, 401)

        login = await self._dev_login()

        response = await self._client.get("/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "dev@localhost")

        expected_pages = {
            "/": "Get Started",
            "/tab": "Ready for navigation",
            "/local": "client-update-btn",
            "/local?provider=codex-cli": "client-update-btn",
            "/setup": "setup-client-update-btn",
            "/install": "install-client-update-btn",
        }
        for path, marker in expected_pages.items():
            with self.subTest(path=path):
                page = await self._client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn(marker, page.text)

        response = await self._client.get("/chat-codex?model=codex-cli:gpt-5.5")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/local?provider=codex-cli")

        self.assertTrue(login["agent_id"].startswith("claude-"))

    async def test_status_update_and_installer_routes_follow_dev_contract(self):
        await self._dev_login()

        response = await self._client.get("/web/chat/status?chat_only=1&model=claude-sonnet-4-6")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in {
            "client_connected",
            "client_version",
            "server_version",
            "min_client_version",
            "client_update_supported",
            "client_outdated",
            "client_update_required",
            "bridge_connected",
        }:
            self.assertIn(key, data)
        self.assertFalse(data["client_connected"])
        self.assertFalse(data["client_update_supported"])

        response = await self._client.post("/web/chat/update-client")
        self.assertEqual(response.status_code, 503)
        self.assertIn("offline", response.text.lower())

        response = await self._client.get("/web/agent/version")
        self.assertEqual(response.status_code, 200)
        version_data = response.json()
        self.assertEqual(version_data["version"], VERSION)
        self.assertEqual(version_data["min_version"], MIN_VERSION)

        response = await self._client.post("/web/install-token")
        self.assertEqual(response.status_code, 200)
        install_data = response.json()
        for key in {
            "curl_command",
            "powershell_command",
            "zip_url",
            "native_available",
            "mac_installer_url",
            "windows_installer_url",
        }:
            self.assertIn(key, install_data)

        install_token = self._extract_install_token(install_data)
        headers = {"X-Install-Token": install_token}

        response = await self._client.get("/install/script", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("/web/download-agent", response.text)
        self.assertIn('INSTALL_TOKEN="inst_', response.text)
        self.assertIn("python3 -m venv", response.text)
        self.assertNotIn("/web/install/bootstrap", response.text)

        response = await self._client.get("/install/windows/script", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Install-Token", response.text)
        self.assertIn("Invoke-WebRequest", response.text)

        response = await self._client.get("/web/download-agent", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/zip", response.headers.get("content-type", ""))
        self.assertGreater(len(response.content), 0)

        for platform in ("mac", "windows"):
            with self.subTest(platform=platform):
                response = await self._client.get(
                    f"/web/download-installer?os={platform}",
                    headers=headers,
                )
                if response.status_code == 200:
                    self.assertGreater(len(response.content), 0)
                    self.assertIn("application/", response.headers.get("content-type", ""))
                else:
                    self.assertEqual(response.status_code, 503)
                    payload = response.json()
                    self.assertIn("error", payload)
                    self.assertEqual(payload.get("os"), platform)

    async def test_trial_token_and_windows_script(self):
        await self._dev_login()

        response = await self._client.post("/trial/token")
        self.assertEqual(response.status_code, 200)
        trial_data = response.json()
        self.assertIn("curl_command", trial_data)
        self.assertIn("powershell_command", trial_data)
        self.assertIn("trial/script", trial_data["curl_command"])
        self.assertIn("trial/windows/script", trial_data["powershell_command"])

        trial_token = self._extract_install_token(trial_data)
        headers = {"X-Install-Token": trial_token}

        response = await self._client.get("/trial/windows/script", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Find-PythonCommand", response.text)
        self.assertIn("Invoke-WebRequest", response.text)
        self.assertIn("Stop-Process", response.text)

    async def test_install_claim_approval_and_bootstrap_flow(self):
        await self._dev_login()

        claim_id = secrets.token_hex(16)
        claim_secret = secrets.token_urlsafe(24)

        response = await self._client.post(
            "/web/install/claim/start",
            json={"claim_id": claim_id, "claim_secret": claim_secret},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

        response = await self._client.post(
            "/web/install/claim/poll",
            json={"claim_id": claim_id, "claim_secret": claim_secret},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

        response = await self._client.get(f"/install/claim/{claim_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(claim_id, response.text)

        response = await self._client.post(
            "/web/install/claim/approve",
            json={"claim_id": claim_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")

        response = await self._client.post(
            "/web/install/claim/poll",
            json={"claim_id": claim_id, "claim_secret": claim_secret},
        )
        self.assertEqual(response.status_code, 200)
        poll_data = response.json()
        self.assertEqual(poll_data["status"], "approved")
        self.assertTrue(poll_data["install_token"])

        install_token = poll_data["install_token"]
        response = await self._client.post("/web/install/bootstrap", json={"token": install_token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["api_key"])

        response = await self._client.post("/web/install/bootstrap", json={"token": install_token})
        self.assertEqual(response.status_code, 401)
        self.assertIn("expired", response.text.lower())


if __name__ == "__main__":
    unittest.main()
