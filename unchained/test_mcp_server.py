"""Tests for MCP tool result shapes."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import mcp.types

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import mcp_server
from mcp_server import _append_iframe_tip


class TestMcpScreenshotTool(unittest.IsolatedAsyncioTestCase):
    async def test_cdp_screenshot_returns_image_content(self):
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6xkAAAAASUVORK5CYII="
        )

        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.screenshot", new=AsyncMock(return_value=png_b64)),
        ):
            tool = await mcp_server.mcp._tool_manager.get_tool("cdp_screenshot")
            result = await mcp_server.mcp._tool_manager.call_tool(
                "cdp_screenshot",
                {"tab_id": "auto", "agent_id": "Profile_5"},
            )

        self.assertIsNone(tool.output_schema)
        self.assertIsNone(result.structured_content)
        self.assertEqual(len(result.content), 1)
        self.assertIsInstance(result.content[0], mcp.types.ImageContent)
        self.assertEqual(result.content[0].type, "image")
        self.assertEqual(result.content[0].mimeType, "image/png")
        self.assertEqual(result.content[0].data, png_b64)


class TestAppendIframeTip(unittest.TestCase):
    def test_tip_appended_when_iframe_present(self):
        out = "=== DOM Density Map ===\nIframe: stripe.com (400×200)\n"
        result = _append_iframe_tip(out)
        self.assertIn("[Iframes detected]", result)
        self.assertIn("cdp_list_frames", result)
        self.assertIn("ddm_frame", result)
        self.assertIn("js_eval_frame", result)

    def test_no_tip_when_no_iframes(self):
        out = "=== DOM Density Map ===\nCanvas: full-page\n"
        self.assertEqual(_append_iframe_tip(out), out)

    def test_tip_appended_once_for_multiple_iframes(self):
        out = "Iframe: stripe.com (400×200)\nIframe: doubleclick.net (1×1)\n"
        result = _append_iframe_tip(out)
        self.assertEqual(result.count("[Iframes detected]"), 1)

    def test_tip_appended_to_end(self):
        out = "content\nIframe: x.com (100×100)"
        result = _append_iframe_tip(out)
        self.assertTrue(result.endswith(
            "ddm_frame(frame_id) to see inside or js_eval_frame(frame_id, expr) to interact."
        ))


class TestDdmToolIframeTip(unittest.IsolatedAsyncioTestCase):
    async def test_ddm_tool_includes_tip_when_iframe_in_output(self):
        ddm_with_iframe = "=== DOM Density Map ===\nIframe: checkout.stripe.com (480×300)\n"
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.run_ddm", new=AsyncMock(return_value=ddm_with_iframe)),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "ddm", {"tab_id": "auto"},
            )
        self.assertIn("[Iframes detected]", result.content[0].text)

    async def test_ddm_tool_no_tip_without_iframes(self):
        ddm_no_iframe = "=== DOM Density Map ===\nElements: 42 visible, 10 interactive\n"
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.run_ddm", new=AsyncMock(return_value=ddm_no_iframe)),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "ddm", {"tab_id": "auto"},
            )
        self.assertNotIn("[Iframes detected]", result.content[0].text)


class TestDdmFrameFlagsSerialisation(unittest.IsolatedAsyncioTestCase):
    async def test_flags_list_passes_through_execute_as_list(self):
        """flags: list[str] in run_ddm_in_frame uses the same execute() path as run_ddm,
        which already serialises list kwargs correctly via JSON (HTTP) or direct call (inprocess).
        Verify the payload structure reaches execute() intact."""
        captured = {}

        async def fake_execute(op, **kwargs):
            captured.update(kwargs)
            return "ok"

        from private_core_client import PrivateCoreClient
        client = PrivateCoreClient.__new__(PrivateCoreClient)
        client.execute = fake_execute

        await client.run_ddm_in_frame(
            agent_id="a", tab_id="t", frame_id="0",
            flags=["--llm-2pass", "--cols", "60"],
            relay_host="127.0.0.1", relay_port=8765,
        )

        self.assertIsInstance(captured["flags"], list)
        self.assertEqual(captured["flags"], ["--llm-2pass", "--cols", "60"])
        self.assertEqual(captured["frame_id"], "0")


class TestDdmFrameTool(unittest.IsolatedAsyncioTestCase):
    async def test_ddm_frame_returns_fallback_when_not_implemented(self):
        """When private core doesn't have run_ddm_in_frame yet, agent gets actionable guidance."""
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.run_ddm_in_frame",
                  new=AsyncMock(side_effect=NotImplementedError("Op not available in engine"))),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "ddm_frame", {"frame_id": "0"},
            )
        text = result.content[0].text
        self.assertIn("not yet available", text)
        self.assertIn("js_eval_frame", text)
        self.assertIn("document.querySelectorAll", text)

    async def test_ddm_frame_returns_fallback_on_private_core_error(self):
        from private_core_client import PrivateCoreError
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.run_ddm_in_frame",
                  new=AsyncMock(side_effect=PrivateCoreError("run_ddm_in_frame not found"))),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "ddm_frame", {"frame_id": "0"},
            )
        self.assertIn("not yet available", result.content[0].text)

    async def test_ddm_frame_passes_frame_id_and_default_flags(self):
        """ddm_frame should call run_ddm_in_frame with the frame_id and default DDM flags."""
        fake_ddm_output = "=== DOM Density Map (frame 0) ===\n[card-number] Input\n[expiry] Input"

        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.run_ddm_in_frame",
                  new=AsyncMock(return_value=fake_ddm_output)) as mock_ddm,
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "ddm_frame",
                {"frame_id": "0", "tab_id": "auto", "agent_id": ""},
            )

        mock_ddm.assert_called_once_with(
            "claude-abc", "auto", "0", ["--llm-2pass", "--cols", "60"]
        )
        self.assertEqual(len(result.content), 1)
        self.assertIn("card-number", result.content[0].text)

    async def test_ddm_frame_raw_cdp_frame_id(self):
        """ddm_frame accepts a raw CDP frameId string, not just an index."""
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.run_ddm_in_frame",
                  new=AsyncMock(return_value="frame output")) as mock_ddm,
        ):
            await mcp_server.mcp._tool_manager.call_tool(
                "ddm_frame",
                {"frame_id": "ABCD1234", "tab_id": "tab-7"},
            )

        mock_ddm.assert_called_once_with(
            "claude-abc", "tab-7", "ABCD1234", ["--llm-2pass", "--cols", "60"]
        )


class TestCdpNavigateBringToFront(unittest.IsolatedAsyncioTestCase):
    async def test_navigate_defaults_to_background(self):
        """cdp_navigate must not bring Chrome to the front by default —
        the browser should stay out of the way of other work on the machine."""
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.navigate", new=AsyncMock(return_value="Navigated")) as mock_nav,
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "cdp_navigate", {"url": "https://example.com"},
            )

        mock_nav.assert_awaited_once_with(
            "claude-abc", "auto", "https://example.com",
            bring_to_front=False,
        )
        self.assertEqual(result.content[0].text, "Navigated")

    async def test_navigate_can_opt_into_bring_to_front(self):
        """Callers can opt into the Chrome 147+ AIM foreground workaround."""
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.navigate", new=AsyncMock(return_value="Navigated")) as mock_nav,
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "cdp_navigate",
                {"url": "https://example.com", "bring_to_front": True},
            )

        mock_nav.assert_awaited_once_with(
            "claude-abc", "auto", "https://example.com",
            bring_to_front=True,
        )
        self.assertEqual(result.content[0].text, "Navigated")


class TestCdpNavigateSignatureContract(unittest.TestCase):
    """The MCP tool mocks cloud_tools.navigate in tests, so validate the real
    call chain accepts the bring_to_front kwarg end-to-end."""

    def test_cloud_tools_navigate_accepts_bring_to_front(self):
        import inspect

        import cloud_tools

        sig = inspect.signature(cloud_tools.navigate)
        param = sig.parameters.get("bring_to_front")
        self.assertIsNotNone(
            param,
            "cloud_tools.navigate must accept bring_to_front kwarg",
        )
        self.assertIs(param.default, True)

    def test_private_core_client_navigate_accepts_bring_to_front(self):
        import inspect

        from private_core_client import PrivateCoreClient

        sig = inspect.signature(PrivateCoreClient.navigate)
        param = sig.parameters.get("bring_to_front")
        self.assertIsNotNone(
            param,
            "PrivateCoreClient.navigate must accept bring_to_front kwarg",
        )
        self.assertIs(param.default, True)


class TestListChromeProfilesTool(unittest.IsolatedAsyncioTestCase):
    """list_chrome_profiles must surface provisionable paths, not agent names."""

    def _principal(self):
        return mcp_server.Principal(
            kind="api_key",
            user_id="user-1",
            caller_tag="tag",
            api_key="uc_live_test",
        )

    async def _call(self, fetch_mock, agent_id=""):
        with (
            patch("mcp_server._require_principal", return_value=self._principal()),
            patch("mcp_server._resolve_agent", return_value="claude-abc-facebook"),
            patch("provision_helpers.fetch_relay_profiles", new=fetch_mock),
        ):
            return await mcp_server.mcp._tool_manager.call_tool(
                "list_chrome_profiles", {"agent_id": agent_id},
            )

    async def test_lists_profile_paths_for_provisioning(self):
        profiles = [
            {
                "path": "/Users/you/Library/Application Support/Google/Chrome/Profile 5",
                "dir_name": "Profile 5",
                "name": "Work",
                "email": "work@example.com",
            },
            {
                "path": "/Users/you/Library/Application Support/Google/Chrome/Default",
                "dir_name": "Default",
                "name": "Personal",
                "email": "me@example.com",
            },
        ]
        fetch = AsyncMock(return_value=profiles)
        result = await self._call(fetch, agent_id="facebook")
        text = result.content[0].text

        self.assertIn("Work (work@example.com)", text)
        self.assertIn("Personal (me@example.com)", text)
        self.assertIn(
            "path: /Users/you/Library/Application Support/Google/Chrome/Profile 5",
            text,
        )
        self.assertIn("cdp_provision_launch", text)
        self.assertNotIn("claude-abc-facebook", text)

        kwargs = fetch.await_args.kwargs
        self.assertEqual(kwargs["agent_id"], "claude-abc-facebook")
        self.assertEqual(
            kwargs["headers"], {"Authorization": "Bearer uc_live_test"}
        )

    async def test_empty_inventory_is_explicit(self):
        result = await self._call(AsyncMock(return_value=[]))
        self.assertEqual(
            result.content[0].text,
            "No Chrome profiles with a Google sign-in were found on this machine.",
        )

    async def test_disconnected_agent_points_at_list_connected_agents(self):
        import httpx

        request = httpx.Request(
            "GET", "http://relay:8765/api/agents/claude-abc-facebook/profiles"
        )
        error = httpx.HTTPStatusError(
            "404", request=request, response=httpx.Response(404, request=request)
        )
        result = await self._call(AsyncMock(side_effect=error))
        text = result.content[0].text
        self.assertIn("No browser is connected", text)
        self.assertIn("list_connected_agents", text)

    async def test_unexpected_error_text_is_not_forwarded(self):
        result = await self._call(
            AsyncMock(side_effect=RuntimeError("bridge at 10.9.9.9 refused"))
        )
        text = result.content[0].text
        self.assertEqual(
            text,
            "Error listing Chrome profiles: the browser service is unavailable.",
        )
        self.assertNotIn("10.9.9.9", text)

    async def test_invalid_inventory_is_reported_without_internals(self):
        result = await self._call(
            AsyncMock(
                side_effect=ValueError("bad payload from relay.internal:8765")
            )
        )
        text = result.content[0].text
        self.assertIn("invalid profile inventory", text)
        self.assertNotIn("relay.internal", text)

    async def test_oauth_uses_the_grant_key(self):
        oauth = mcp_server.Principal(
            kind="oauth",
            user_id="user-1",
            caller_tag="oauth-deadbeef",
            agent_id="claude-abc",
            grant_id="g1",
        )
        fetch = AsyncMock(return_value=[])
        with (
            patch("mcp_server._require_principal", return_value=oauth),
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("mcp_server._key_for_grant", return_value="uc_live_grant"),
            patch("provision_helpers.fetch_relay_profiles", new=fetch),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "list_chrome_profiles", {},
            )
        self.assertIn("No Chrome profiles", result.content[0].text)
        self.assertEqual(
            fetch.await_args.kwargs["headers"],
            {"Authorization": "Bearer uc_live_grant"},
        )


class TestProvisionScopeTools(unittest.IsolatedAsyncioTestCase):
    """OAuth connectors may provision, but only see and drive their own slots."""

    def _oauth(self):
        return mcp_server.Principal(
            kind="oauth",
            user_id="user-1",
            caller_tag="oauth-deadbeef",
            agent_id="claude-abc",
            grant_id="g1",
        )

    async def test_list_provisioned_tabs_filters_other_callers(self):
        status = {
            "slots": {
                "dc31": {
                    "profile": "Profile 1",
                    "caller_tag": "oauth-deadbeef",
                    "tabs": [{"tab_id": "prov-dc31-AA", "title": "Mine"}],
                },
                "ab12": {
                    "profile": "Profile 2",
                    "caller_tag": "abc123",
                    "tabs": [{"tab_id": "prov-ab12-BB", "title": "Other"}],
                },
            }
        }
        with (
            patch("mcp_server._require_principal", return_value=self._oauth()),
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch(
                "cloud_tools.provision_status",
                new=AsyncMock(return_value=status),
            ),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "list_provisioned_tabs", {},
            )
        text = result.content[0].text
        self.assertIn("Slot dc31", text)
        self.assertNotIn("Slot ab12", text)
        self.assertNotIn("Other", text)

    async def test_oauth_launch_passes_the_grant_tag(self):
        launch = AsyncMock(
            return_value={"slot": "dc31", "tab_id": "prov-dc31-AA", "port": 9600}
        )
        with (
            patch("mcp_server._require_principal", return_value=self._oauth()),
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.provision_launch", new=launch),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "cdp_provision_launch", {"profile_path": "/x"},
            )
        self.assertIn("Slot: dc31", result.content[0].text)
        self.assertEqual(launch.await_args.kwargs["caller_tag"], "oauth-deadbeef")

    async def test_verdict_allows_only_the_grants_slot(self):
        status = {"slots": {"dc31": {"caller_tag": "oauth-deadbeef"}}}
        with patch(
            "cloud_tools.provision_status", new=AsyncMock(return_value=status)
        ):
            own = await mcp_server._oauth_prov_tab_verdict(
                self._oauth(), "prov-dc31-AA"
            )
            other = await mcp_server._oauth_prov_tab_verdict(
                self._oauth(), "prov-ab12-AA"
            )
        self.assertIs(own, True)
        self.assertIs(other, False)

    async def test_aliases_hide_other_callers_prov_targets(self):
        raw = json.dumps(
            {
                "mine": "prov-dc31-AA",
                "other": "prov-ab12-BB",
                "plain": "AABBCCDD",
            }
        )
        status = {
            "slots": {
                "dc31": {"caller_tag": "oauth-deadbeef"},
                "ab12": {"caller_tag": "abc123"},
            }
        }
        with (
            patch("mcp_server._require_principal", return_value=self._oauth()),
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch(
                "cloud_tools.list_tab_aliases",
                new=AsyncMock(return_value=raw),
            ),
            patch(
                "cloud_tools.provision_status",
                new=AsyncMock(return_value=status),
            ),
        ):
            result = await mcp_server.mcp._tool_manager.call_tool(
                "cdp_list_tab_aliases", {},
            )
        visible = json.loads(result.content[0].text)
        self.assertIn("mine", visible)
        self.assertIn("plain", visible)
        self.assertNotIn("other", visible)

    async def test_alias_resolves_to_prov_target_for_scoping(self):
        with patch(
            "cloud_tools.list_tab_aliases",
            new=AsyncMock(return_value='{"reddit": "prov-ab12-BB"}'),
        ):
            target = await mcp_server._oauth_alias_prov_target(
                self._oauth(), "reddit"
            )
        self.assertEqual(target, "prov-ab12-BB")

    async def test_verdict_is_unknown_when_status_fails(self):
        with patch(
            "cloud_tools.provision_status",
            new=AsyncMock(side_effect=RuntimeError("relay down")),
        ):
            verdict = await mcp_server._oauth_prov_tab_verdict(
                self._oauth(), "prov-dc31-AA"
            )
        self.assertIsNone(verdict)


if __name__ == "__main__":
    unittest.main()
