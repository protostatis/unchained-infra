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


if __name__ == "__main__":
    unittest.main()
