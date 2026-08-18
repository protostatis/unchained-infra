"""Tests for MCP tool result shapes."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
