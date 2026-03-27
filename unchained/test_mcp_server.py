"""Tests for MCP tool result shapes."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

import mcp.types

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import mcp_server


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


class TestDdmFrameTool(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
