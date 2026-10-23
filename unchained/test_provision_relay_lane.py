"""The profile picker and the provision launch must target the same lane.

`/web/provision/profiles` resolves the user's *bridge* agent (which may be a named
profile such as the desktop app's `desktop` lane), while `/web/provision/start` used
`auth_info["agent_id"]` — the base account id. With two clients on one account that
means the picker lists profiles from one agent and the launch is sent to another, or
to nobody. This test pins the two to one resolution.

Run:
    python3 test_provision_relay_lane.py
"""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from web_app.handlers import provision as provision_handler


class _FakeRequest:
    def __init__(self, body):
        self._body = body
        self.query = {}
        self.headers = {}
        self.remote = "127.0.0.1"

    async def json(self):
        return self._body


class _FakeCore:
    _PROVISION_COOLDOWN_SECS = 30

    def __init__(self, bridge_agent="claude-base-desktop", raises=False):
        self._provision_cooldowns = {}
        self._pending_provision = {}
        self._bridge_agent = bridge_agent
        self._raises = raises

    def _authenticate(self, request):
        return {
            "user_id": "u-test",
            "agent_id": "claude-base",
            "key": "uc_live_test",
            "user_type": "full",
        }

    def _is_pending_user(self, auth_info):
        return False

    def _pending_limited_response(self):  # pragma: no cover - not reached
        raise AssertionError("pending gate must not run")

    async def _resolve_bridge_agent(self, auth_info):
        if self._raises:
            raise RuntimeError("relay unavailable")
        return {"bridge_agent_id": self._bridge_agent}

    def _parse_relay(self):
        return ("relay.invalid", 443)

    def _relay_auth_headers(self):
        return {}

    def _track_event(self, *args, **kwargs):
        pass


def _result():
    return SimpleNamespace(
        status=SimpleNamespace(value="failed"),
        provider="anthropic",
        message="test",
        duration_ms=1,
        api_key=None,
    )


class ProvisionRelayLaneTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, core, profile_path="/Users/test/Library/Application Support/Google/Chrome/Default"):
        request = _FakeRequest(
            {"provider": "anthropic", "use_relay": True, "profile_path": profile_path}
        )
        with patch.object(provision_handler, "_core", lambda: core), \
                patch("signup_agent.list_providers", return_value=["anthropic"]), \
                patch("signup_agent.provision_key", new=AsyncMock(return_value=_result())) as launch:
            response = await provision_handler.handle_provision_start(request)
        return response, launch

    async def test_launch_targets_the_resolved_bridge_lane(self):
        response, launch = await self._start(_FakeCore())
        self.assertEqual(response.status, 200)
        self.assertEqual(launch.await_count, 1)
        self.assertEqual(launch.await_args.kwargs["agent_id"], "claude-base-desktop")
        self.assertEqual(
            launch.await_args.kwargs["profile_path"],
            "/Users/test/Library/Application Support/Google/Chrome/Default",
        )

    async def test_falls_back_to_the_base_agent_when_resolution_fails(self):
        response, launch = await self._start(_FakeCore(raises=True))
        self.assertEqual(response.status, 200)
        self.assertEqual(launch.await_args.kwargs["agent_id"], "claude-base")


if __name__ == "__main__":
    unittest.main(verbosity=2)
