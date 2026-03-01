"""Tests for provisioning helper functions."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import provision_helpers


class TestFetchRelayProfiles(unittest.IsolatedAsyncioTestCase):
    """Ensure relay profile fetches include internal auth when provided."""

    @patch("provision_helpers.httpx.AsyncClient")
    async def test_fetch_relay_profiles_passes_headers(self, mock_client_cls):
        response = MagicMock()
        response.is_success = True
        response.json.return_value = {
            "profiles": [{"email": "zhiminzou@gmail.com", "path": "/tmp/Default"}]
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
            [{"email": "zhiminzou@gmail.com", "path": "/tmp/Default"}],
        )
        client.get.assert_awaited_once_with(
            "http://relay.example.com:8765/api/agents/claude-abc12345/profiles",
            timeout=5.0,
            headers=headers,
        )


if __name__ == "__main__":
    unittest.main()
