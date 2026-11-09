import unittest
from unittest.mock import AsyncMock, patch

import cloud_tools


class _FakeClient:
    def __init__(self):
        self.click = AsyncMock(return_value='Clicked BUTTON "Search" at (100,200)\n--- changed ---')
        self.run_ddm = AsyncMock(return_value="ddm-output")
        self.run_rhythm_catch = AsyncMock(return_value='{"caught": []}')


class TestCloudToolsBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_click_delegates_to_private_core_client(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.click("agent", "auto", 100, 200)

        self.assertIn("Clicked BUTTON", out)
        fake.click.assert_awaited_once_with("agent", "auto", 100, 200, "127.0.0.1", 8765,
                                                  element_id="", label="")

    async def test_run_ddm_delegates_to_private_core_client(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_ddm("agent", "auto", ["--llm-2pass", "--cols", "60"])

        self.assertEqual(out, "ddm-output")
        fake.run_ddm.assert_awaited_once_with(
            "agent", "auto", ["--llm-2pass", "--cols", "60"], "127.0.0.1", 8765
        )

    async def test_rhythm_catch_forwards_scan_options(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_rhythm_catch(
                "agent", "auto", "https://example.com", "find cards", ["4090"],
                group_selector="li.s-item", limit=50,
            )

        self.assertEqual(out, '{"caught": []}')
        fake.run_rhythm_catch.assert_awaited_once_with(
            "agent", "auto", "https://example.com", "find cards", ["4090"],
            click_text="", group_selector="li.s-item", limit=50,
            relay_host="127.0.0.1", relay_port=8765,
        )

    async def test_rhythm_catch_defaults_keep_text_node_mode(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            await cloud_tools.run_rhythm_catch("agent", "auto", "u", "t", ["4090"])

        fake.run_rhythm_catch.assert_awaited_once_with(
            "agent", "auto", "u", "t", ["4090"],
            click_text="", group_selector="", limit=20,
            relay_host="127.0.0.1", relay_port=8765,
        )


if __name__ == "__main__":
    unittest.main()
