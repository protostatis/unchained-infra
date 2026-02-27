import unittest
from unittest.mock import AsyncMock, patch

import cloud_tools


class _FakeClient:
    def __init__(self):
        self.click = AsyncMock(return_value='Clicked BUTTON "Search" at (100,200)\n--- changed ---')
        self.run_ddm = AsyncMock(return_value="ddm-output")


class TestCloudToolsBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_click_delegates_to_private_core_client(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.click("agent", "auto", 100, 200)

        self.assertIn("Clicked BUTTON", out)
        fake.click.assert_awaited_once_with("agent", "auto", 100, 200, "127.0.0.1", 8765)

    async def test_run_ddm_delegates_to_private_core_client(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_ddm("agent", "auto", ["--llm-2pass", "--cols", "60"])

        self.assertEqual(out, "ddm-output")
        fake.run_ddm.assert_awaited_once_with(
            "agent", "auto", ["--llm-2pass", "--cols", "60"], "127.0.0.1", 8765
        )


if __name__ == "__main__":
    unittest.main()
