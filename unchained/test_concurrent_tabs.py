"""E2E tests for concurrent tab isolation.

Requires a running Chrome instance on DEBUG_PORT. Skipped automatically if Chrome
isn't running. Run manually with:
    CDP_PROFILE=panicradar_ai uv run python -m pytest test_concurrent_tabs.py -v
"""
import asyncio
import json
import os
import sys
import unittest
import urllib.request

# Ensure unchained/ is on the path
sys.path.insert(0, os.path.dirname(__file__))


def _chrome_available():
    """Check if Chrome is running on the debug port."""
    try:
        import cdp as tt
        if os.environ.get("CDP_PROFILE"):
            tt._select_profile(os.environ["CDP_PROFILE"])
        req = urllib.request.Request(f"http://127.0.0.1:{tt.DEBUG_PORT}/json/version")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


needs_chrome = unittest.skipUnless(_chrome_available(), "Chrome not running on debug port")


@needs_chrome
class TestConcurrentTabs(unittest.TestCase):
    """Two CDP connections on different tabs, parallel navigate, verify no stomp."""

    def test_concurrent_agents(self):
        """Create two tabs, navigate each independently, verify isolation."""
        import cdp as tt

        async def _run():
            tab_a = tt._create_new_tab()
            tab_b = tt._create_new_tab()
            try:
                # Connect to both tabs
                cdp_a = tt.CDP(tab_a["webSocketDebuggerUrl"])
                cdp_b = tt.CDP(tab_b["webSocketDebuggerUrl"])
                await cdp_a.connect()
                await cdp_b.connect()

                # Navigate both in parallel to different URLs
                await asyncio.gather(
                    cdp_a.navigate("data:text/html,<h1>TabA</h1>", wait=1),
                    cdp_b.navigate("data:text/html,<h1>TabB</h1>", wait=1),
                )

                # Verify each tab has its own content
                text_a = await cdp_a.get_text()
                text_b = await cdp_b.get_text()

                self.assertIn("TabA", text_a)
                self.assertIn("TabB", text_b)
                self.assertNotIn("TabB", text_a)
                self.assertNotIn("TabA", text_b)

                await cdp_a.close()
                await cdp_b.close()
            finally:
                tt._close_tab(tab_a["id"])
                tt._close_tab(tab_b["id"])

        asyncio.run(_run())

    def test_tab_survives_reconnect(self):
        """Disconnect from a tab, reconnect via saved tab ID, still works."""
        import cdp as tt

        async def _run():
            tab = tt._create_new_tab()
            tab_id = tab["id"]
            try:
                # First connection — navigate
                cdp1 = tt.CDP(tab["webSocketDebuggerUrl"])
                await cdp1.connect()
                await cdp1.navigate("data:text/html,<h1>Persistent</h1>", wait=1)
                await cdp1.close()

                # Reconnect via tab ID
                ws_url = tt._get_ws_url_for_tab(tab_id)
                cdp2 = tt.CDP(ws_url)
                await cdp2.connect()
                text = await cdp2.get_text()
                self.assertIn("Persistent", text)
                await cdp2.close()
            finally:
                tt._close_tab(tab_id)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
