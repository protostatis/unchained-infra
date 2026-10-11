"""Tests for provisioning helper functions."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import provision_helpers
from web_app.handlers import provision as provision_handler


class TestFetchRelayProfiles(unittest.IsolatedAsyncioTestCase):
    """Ensure relay profile fetches include internal auth when provided."""

    @patch("provision_helpers.httpx.AsyncClient")
    async def test_fetch_relay_profiles_passes_headers(self, mock_client_cls):
        response = MagicMock()
        response.is_success = True
        response.json.return_value = {
            "profiles": [{"email": "user@example.com", "path": "/tmp/Default"}]
        }

        client = AsyncMock()
        client.get.return_value = response
        mock_client_cls.return_value.__aenter__.return_value = client

        headers = {"Authorization": "Bearer relay-token"}
        profiles = await provision_helpers.fetch_relay_profiles(
            agent_id="claude-abc12345",
            relay_host="relay.example.com",
            relay_port=8765,
            headers=headers,
        )

        self.assertEqual(
            profiles,
            [{"email": "user@example.com", "path": "/tmp/Default"}],
        )
        client.get.assert_awaited_once_with(
            "http://relay.example.com:8765/api/agents/claude-abc12345/profiles",
            timeout=5.0,
            headers=headers,
        )

    @patch("provision_helpers.httpx.AsyncClient")
    async def test_transport_failure_is_not_reported_as_empty_inventory(self, mock_client_cls):
        client = AsyncMock()
        client.get.side_effect = httpx.ConnectError("bridge temporarily disconnected")
        mock_client_cls.return_value.__aenter__.return_value = client

        with self.assertRaises(httpx.ConnectError):
            await provision_helpers.fetch_relay_profiles(
                agent_id="claude-abc12345",
                relay_host="relay.example.com",
                relay_port=8765,
            )


class TestProvisionProfilesHandler(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_failure_is_not_returned_as_successful_empty_inventory(self):
        auth_info = {"user_id": "user-1", "agent_id": "claude-abc12345"}
        profile_helpers = SimpleNamespace(
            fetch_relay_profiles=AsyncMock(
                side_effect=httpx.ConnectError("bridge temporarily disconnected")
            )
        )
        core = SimpleNamespace(
            _authenticate=lambda _request: auth_info,
            _is_pending_user=lambda _auth: False,
            _resolve_bridge_agent=AsyncMock(
                return_value={"bridge_agent_id": "claude-abc12345"}
            ),
            _parse_relay=lambda: ("relay.example.com", 8765),
            _relay_auth_headers=lambda: {"Authorization": "Bearer test"},
            provision_helpers=profile_helpers,
        )

        with (
            patch.dict(os.environ, {"JWT_SECRET": "test-jwt-secret"}),
            patch.object(provision_handler, "_core", return_value=core),
            patch("signup_agent.list_chrome_profiles", return_value=[]),
        ):
            response = await provision_handler.handle_provision_profiles(
                SimpleNamespace()
            )

        self.assertEqual(response.status, 503)
        self.assertIn("temporarily unavailable", json.loads(response.body)["error"])

    async def test_malformed_inventory_is_reported_as_bad_gateway(self):
        auth_info = {"user_id": "user-1", "agent_id": "claude-abc12345"}
        profile_helpers = SimpleNamespace(
            fetch_relay_profiles=AsyncMock(
                side_effect=ValueError("invalid Chrome profile inventory")
            )
        )
        core = SimpleNamespace(
            _authenticate=lambda _request: auth_info,
            _is_pending_user=lambda _auth: False,
            _resolve_bridge_agent=AsyncMock(
                return_value={"bridge_agent_id": "claude-abc12345"}
            ),
            _parse_relay=lambda: ("relay.example.com", 8765),
            _relay_auth_headers=lambda: {"Authorization": "Bearer test"},
            provision_helpers=profile_helpers,
        )

        with (
            patch.dict(os.environ, {"JWT_SECRET": "test-jwt-secret"}),
            patch.object(provision_handler, "_core", return_value=core),
            patch("signup_agent.list_chrome_profiles", return_value=[]),
        ):
            response = await provision_handler.handle_provision_profiles(
                SimpleNamespace()
            )

        self.assertEqual(response.status, 502)
        self.assertIn("invalid Chrome profile inventory", json.loads(response.body)["error"])


if __name__ == "__main__":
    unittest.main()
