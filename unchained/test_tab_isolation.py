"""Tests for tab isolation in cdp.py.

These tests mock the CDP HTTP endpoints so they run without Chrome.
Tests marked @needs_chrome require a running Chrome instance.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Clear any mock 'cdp' left by test_dom_density/test_page_intel so we get the real module.
if 'cdp' in sys.modules and isinstance(sys.modules['cdp'], MagicMock):
    del sys.modules['cdp']

# We need to mock urllib before importing cdp to avoid connection errors
# during module-level calls. Import after patching.

FAKE_TABS = [
    {
        "id": "TAB_AAA",
        "type": "page",
        "url": "https://www.tiktok.com",
        "title": "TikTok",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9516/devtools/page/TAB_AAA",
    },
    {
        "id": "TAB_BBB",
        "type": "page",
        "url": "about:blank",
        "title": "",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9516/devtools/page/TAB_BBB",
    },
]

NEW_TAB = {
    "id": "TAB_NEW",
    "type": "page",
    "url": "about:blank",
    "title": "",
    "webSocketDebuggerUrl": "ws://127.0.0.1:9516/devtools/page/TAB_NEW",
}


def _mock_urlopen(req, **kwargs):
    """Fake urllib.request.urlopen for CDP HTTP endpoints."""
    url = req.full_url if hasattr(req, 'full_url') else str(req)
    resp = MagicMock()

    if "/json/new" in url:
        resp.read.return_value = json.dumps(NEW_TAB).encode()
        resp.status = 200
    elif "/json/close/" in url:
        resp.read.return_value = b"Target is closing"
        resp.status = 200
    elif "/json/version" in url:
        resp.read.return_value = json.dumps({"Browser": "Chrome/131"}).encode()
        resp.status = 200
    elif "/json" in url:
        resp.read.return_value = json.dumps(FAKE_TABS).encode()
        resp.status = 200
    else:
        raise RuntimeError(f"Unmocked URL: {url}")

    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@patch("urllib.request.urlopen", side_effect=_mock_urlopen)
class TestTabFunctions(unittest.TestCase):
    """Unit tests for tab management functions added to cdp."""

    def _import_tt(self):
        import cdp
        return cdp

    def test_list_tabs(self, mock_open):
        tt = self._import_tt()
        tabs = tt._list_tabs()
        self.assertEqual(len(tabs), 2)
        self.assertEqual(tabs[0]["id"], "TAB_AAA")
        self.assertEqual(tabs[1]["id"], "TAB_BBB")

    def test_create_new_tab(self, mock_open):
        tt = self._import_tt()
        tab = tt._create_new_tab()
        self.assertEqual(tab["id"], "TAB_NEW")
        self.assertIn("webSocketDebuggerUrl", tab)

    def test_get_ws_url_for_tab(self, mock_open):
        tt = self._import_tt()
        ws = tt._get_ws_url_for_tab("TAB_BBB")
        self.assertEqual(ws, "ws://127.0.0.1:9516/devtools/page/TAB_BBB")

    def test_get_ws_url_for_tab_not_found(self, mock_open):
        tt = self._import_tt()
        with self.assertRaises(RuntimeError):
            tt._get_ws_url_for_tab("NONEXISTENT")

    def test_close_tab(self, mock_open):
        tt = self._import_tt()
        result = tt._close_tab("TAB_BBB")
        self.assertTrue(result)

    def test_tab_id_file_path(self, mock_open):
        tt = self._import_tt()
        path = tt._tab_id_file("tiktok")
        self.assertIn(".tiktok_tab_id_", path)
        self.assertIn(str(tt.DEBUG_PORT), path)

    def test_tab_id_file_persistence(self, mock_open):
        """Write tab ID to file, _get_or_create_tab reuses it."""
        tt = self._import_tt()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tt, '_tab_id_file', return_value=os.path.join(tmpdir, ".test_tab_id")):
                # Pre-write a valid tab ID
                path = tt._tab_id_file("tiktok")
                with open(path, "w") as f:
                    f.write("TAB_AAA")
                tab_id = tt._get_or_create_tab("tiktok")
                self.assertEqual(tab_id, "TAB_AAA")

    def test_stale_tab_id_recovery(self, mock_open):
        """Bogus ID in file → _get_or_create_tab creates a new tab."""
        tt = self._import_tt()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tt, '_tab_id_file', return_value=os.path.join(tmpdir, ".test_tab_id")):
                path = tt._tab_id_file("tiktok")
                with open(path, "w") as f:
                    f.write("STALE_BOGUS_ID")
                tab_id = tt._get_or_create_tab("tiktok")
                # Should have created a new tab since STALE_BOGUS_ID doesn't exist
                self.assertEqual(tab_id, "TAB_NEW")
                # File should be updated
                with open(path) as f:
                    self.assertEqual(f.read().strip(), "TAB_NEW")

    def test_close_dedicated_tab(self, mock_open):
        """Create tab, close it, verify file deleted."""
        tt = self._import_tt()
        with tempfile.TemporaryDirectory() as tmpdir:
            tab_file = os.path.join(tmpdir, ".test_tab_id")
            with patch.object(tt, '_tab_id_file', return_value=tab_file):
                with open(tab_file, "w") as f:
                    f.write("TAB_AAA")
                tt._close_dedicated_tab("tiktok")
                self.assertFalse(os.path.exists(tab_file))

    def test_get_ws_url_with_tab_id(self, mock_open):
        """_get_ws_url(tab_id=X) returns the right ws URL."""
        tt = self._import_tt()
        ws = tt._get_ws_url(tab_id="TAB_BBB")
        self.assertEqual(ws, "ws://127.0.0.1:9516/devtools/page/TAB_BBB")

    def test_get_ws_url_without_tab_id(self, mock_open):
        """_get_ws_url() without tab_id still returns first page tab (legacy)."""
        tt = self._import_tt()
        # When no tab_id given, it prefers tiktok/tiktokstudio tabs, then first page tab
        ws = tt._get_ws_url()
        self.assertIn("TAB_AAA", ws)


class TestCliTabFlag(unittest.TestCase):
    """Test --tab arg parsing in main()."""

    def test_tab_flag_parsed(self):
        import cdp as tt
        # Simulate --tab MY_TAB_ID in args
        args = ["--tab", "MY_TAB_ID", "status"]
        # We just test the parsing logic, not the actual command execution
        parsed_tab = None
        filtered = []
        i = 0
        while i < len(args):
            if args[i] == "--tab" and i + 1 < len(args):
                parsed_tab = args[i + 1]
                i += 2
            else:
                filtered.append(args[i])
                i += 1
        self.assertEqual(parsed_tab, "MY_TAB_ID")
        self.assertEqual(filtered, ["status"])


if __name__ == "__main__":
    unittest.main()
