"""Tests for prov-<slot>-<id> round-trip in the packaged cdp_tool.

When the chat session binds a provisioned Chrome (CDP_TAB_ID="prov-<slot>-<id>"),
`cdp_tool.py tabs` lists tabs from the provisioned Chrome's port. Those
listings must include the "prov-<slot>-" prefix so the agent can pass them
back via `--tab` and have the bridge route the command to the same Chrome.

Regression: the listing previously printed bare 12-char ids; passing them
back hit the bridge's default-Chrome path and produced "Tab not found".
"""
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock


FAKE_TABS = [
    {"id": "AABBCCDDEEFF112233", "type": "page",
     "url": "https://example.com", "title": "Example"},
    {"id": "00112233445566778899", "type": "page",
     "url": "https://other.test", "title": "Other"},
]


def _reload_with_env(env: dict):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    if "cdp_tool_packaged" in sys.modules:
        del sys.modules["cdp_tool_packaged"]
    return importlib.import_module("cdp_tool_packaged")


class ProvRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        slots = os.path.join(self.tmpdir, "provision_slots")
        os.makedirs(slots, exist_ok=True)
        with open(os.path.join(slots, "2ddd.json"), "w") as f:
            json.dump({"slot": "2ddd", "port": 59464}, f)
        self.env = {
            "UNCHAINED_DATA_DIR": self.tmpdir,
            "CDP_TAB_ID": "prov-2ddd-AABBCCDDEEFF112233",
        }

    def test_active_prov_slot_extracts_slot(self):
        ct = _reload_with_env(self.env)
        self.assertEqual(ct._active_prov_slot(), "2ddd")

    def test_active_prov_slot_returns_empty_for_default_session(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "auto"
        ct = _reload_with_env(env)
        self.assertEqual(ct._active_prov_slot(), "")

    def test_format_tab_id_prefixes_in_prov_mode(self):
        ct = _reload_with_env(self.env)
        out = ct._format_tab_id_for_display("AABBCCDDEEFF112233", "2ddd")
        self.assertEqual(out, "prov-2ddd-AABBCCDDEEFF")

    def test_format_tab_id_bare_in_default_mode(self):
        ct = _reload_with_env(self.env)
        out = ct._format_tab_id_for_display("AABBCCDDEEFF112233", "")
        self.assertEqual(out, "AABBCCDDEEFF")

    def test_resolve_cdp_port_uses_provision_state(self):
        ct = _reload_with_env(self.env)
        self.assertEqual(ct._resolve_cdp_port(), 59464)

    def test_resolve_cdp_port_default_when_no_prov(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "auto"
        ct = _reload_with_env(env)
        self.assertEqual(ct._resolve_cdp_port(), 9222)

    def _run_main(self, ct, argv):
        buf = io.StringIO()
        with patch.object(ct, "_chrome_tabs", return_value=FAKE_TABS):
            with patch.object(sys, "argv", argv):
                with redirect_stdout(buf):
                    try:
                        ct.main()
                    except SystemExit:
                        pass
        return buf.getvalue()

    def test_tabs_listing_prefixes_ids_in_prov_mode(self):
        ct = _reload_with_env(self.env)
        out = self._run_main(ct, ["cdp_tool.py", "tabs"])
        self.assertIn("prov-2ddd-AABBCCDDEEFF", out)
        self.assertIn("prov-2ddd-001122334455", out)
        # Bare 12-char form must not appear standalone (would be ambiguous
        # for the agent which Chrome the id belongs to).
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("AABBCCDDEEFF") or stripped.startswith("001122334455"):
                self.fail(f"bare id leaked into prov-mode listing: {line!r}")

    def test_tabs_listing_bare_ids_in_default_mode(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "auto"
        ct = _reload_with_env(env)
        out = self._run_main(ct, ["cdp_tool.py", "tabs"])
        self.assertIn("AABBCCDDEEFF", out)
        self.assertNotIn("prov-", out)

    def test_tab_flag_bare_id_rewritten_to_prov_form(self):
        ct = _reload_with_env(self.env)
        captured = {}

        def fake_cmd(action, **kwargs):
            captured["action"] = action
            captured["kwargs"] = kwargs
            return {"data": ""}

        with patch.object(ct, "cmd", side_effect=fake_cmd):
            with patch.object(sys, "argv",
                              ["cdp_tool.py", "ddm", "--tab", "AABBCCDDEEFF",
                               "--text"]):
                try:
                    ct.main()
                except SystemExit:
                    pass

        self.assertEqual(captured["action"], "ddm")
        self.assertEqual(captured["kwargs"]["tab_id"],
                         "prov-2ddd-AABBCCDDEEFF")

    def test_tab_flag_already_prov_form_left_unchanged(self):
        ct = _reload_with_env(self.env)
        captured = {}

        def fake_cmd(action, **kwargs):
            captured["kwargs"] = kwargs
            return {"data": ""}

        with patch.object(ct, "cmd", side_effect=fake_cmd):
            with patch.object(sys, "argv",
                              ["cdp_tool.py", "ddm", "--tab",
                               "prov-2ddd-AABBCCDDEEFF", "--text"]):
                try:
                    ct.main()
                except SystemExit:
                    pass

        self.assertEqual(captured["kwargs"]["tab_id"],
                         "prov-2ddd-AABBCCDDEEFF")


if __name__ == "__main__":
    unittest.main()
