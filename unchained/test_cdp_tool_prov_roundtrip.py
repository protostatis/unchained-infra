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
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        self.state_path = os.path.join(slots, "2ddd.json")
        self.active_state = {
            "slot": "2ddd",
            "pid": 12345,
            "port": 59464,
            "temp_dir": "/tmp/prov_tmp_2ddd",
            "ready": True,
            "agent_id": "claude-test",
        }
        with open(self.state_path, "w") as f:
            json.dump(self.active_state, f)
        self.env = {
            "UNCHAINED_DATA_DIR": self.tmpdir,
            "CDP_TAB_ID": "prov-2ddd-AABBCCDDEEFF112233",
            "CDP_AGENT_ID": "claude-test",
            "UNCHAINED_CHAT_SESSION_ID": None,
        }

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _active_slot(self):
        return "active", dict(self.active_state)

    def test_active_prov_slot_extracts_slot(self):
        ct = _reload_with_env(self.env)
        with patch.object(ct, "_provision_slot_status", return_value=self._active_slot()):
            self.assertEqual(ct._active_prov_slot(), "2ddd")

    def test_active_prov_slot_returns_empty_for_default_session(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "auto"
        ct = _reload_with_env(env)
        self.assertEqual(ct._active_prov_slot(), "")

    def test_active_prov_slot_returns_empty_for_stale_slot(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "prov-dead-AABBCCDDEEFF112233"
        ct = _reload_with_env(env)
        self.assertEqual(ct._active_prov_slot(), "")
        self.assertEqual(ct._resolve_cdp_port(), 9222)

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
        with patch.object(ct, "_provision_slot_status", return_value=self._active_slot()):
            self.assertEqual(ct._resolve_cdp_port(), 59464)

    def test_resolve_cdp_port_default_when_no_prov(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "auto"
        ct = _reload_with_env(env)
        self.assertEqual(ct._resolve_cdp_port(), 9222)

    def _run_main(self, ct, argv, slot_status=None):
        buf = io.StringIO()
        with (
            patch.object(ct, "_chrome_tabs", return_value=FAKE_TABS),
            patch.object(ct, "_provision_slot_status", return_value=slot_status or self._active_slot()),
            patch.object(sys, "argv", argv),
            redirect_stdout(buf),
        ):
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

        with (
            patch.object(ct, "cmd", side_effect=fake_cmd),
            patch.object(ct, "_provision_slot_status", return_value=self._active_slot()),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--tab", "AABBCCDDEEFF", "--text"]),
        ):
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

        with (
            patch.object(ct, "cmd", side_effect=fake_cmd),
            patch.object(ct, "_provision_slot_status", return_value=self._active_slot()),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--tab", "prov-2ddd-AABBCCDDEEFF", "--text"]),
        ):
            try:
                ct.main()
            except SystemExit:
                pass

        self.assertEqual(captured["kwargs"]["tab_id"],
                         "prov-2ddd-AABBCCDDEEFF")

    def test_chat_session_defaults_to_server_authoritative_tab(self):
        env = dict(self.env)
        env["UNCHAINED_CHAT_SESSION_ID"] = "s-claude-test-session"
        ct = _reload_with_env(env)
        captured = {}

        def fake_cmd(action, **kwargs):
            captured["action"] = action
            captured["kwargs"] = kwargs
            return {"data": ""}

        with (
            patch.object(ct, "cmd", side_effect=fake_cmd),
            patch.object(ct, "_provision_slot_status", return_value=self._active_slot()),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--text"]),
        ):
            ct.main()

        self.assertEqual(captured["action"], "ddm")
        self.assertEqual(captured["kwargs"]["tab_id"], "auto")

    def test_chat_session_explicit_tab_remains_in_provision_slot(self):
        env = dict(self.env)
        env["UNCHAINED_CHAT_SESSION_ID"] = "s-claude-test-session"
        ct = _reload_with_env(env)
        captured = {}

        def fake_cmd(_action, **kwargs):
            captured["kwargs"] = kwargs
            return {"data": ""}

        with (
            patch.object(ct, "cmd", side_effect=fake_cmd),
            patch.object(ct, "_provision_slot_status", return_value=self._active_slot()),
            patch.object(
                sys,
                "argv",
                ["cdp_tool.py", "ddm", "--tab", "AABBCCDDEEFF", "--text"],
            ),
        ):
            ct.main()

        self.assertEqual(
            captured["kwargs"]["tab_id"],
            "prov-2ddd-AABBCCDDEEFF",
        )

    def test_stale_env_prov_tab_fails_closed(self):
        ct = _reload_with_env(self.env)
        stderr = io.StringIO()

        with (
            patch.object(ct, "cmd") as mock_cmd,
            patch.object(ct, "_pid_is_running", return_value=False),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--text"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                ct.main()

        mock_cmd.assert_not_called()
        self.assertIn("no longer running", stderr.getvalue())

    def test_dead_pid_state_is_stale_without_deleting_metadata(self):
        ct = _reload_with_env(self.env)
        with patch.object(ct, "_pid_is_running", return_value=False):
            status, state = ct._provision_slot_status("2ddd")

        self.assertEqual(status, "stale")
        self.assertEqual(state["pid"], 12345)
        self.assertTrue(os.path.exists(self.state_path))

    def test_reused_pid_with_wrong_chrome_command_is_stale(self):
        ct = _reload_with_env(self.env)
        with (
            patch.object(ct, "_pid_is_running", return_value=True),
            patch.object(ct, "_process_cmdline", return_value="Google Chrome --remote-debugging-port=59464"),
        ):
            status, _state = ct._provision_slot_status("2ddd")

        self.assertEqual(status, "stale")
        self.assertTrue(os.path.exists(self.state_path))

    def test_matching_process_and_chrome_endpoint_is_active(self):
        ct = _reload_with_env(self.env)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "Browser": "Chrome/150",
            "webSocketDebuggerUrl": "ws://127.0.0.1:59464/devtools/browser/test",
        }).encode()
        response.__exit__.return_value = False
        cmdline = "Google Chrome --user-data-dir=/tmp/prov_tmp_2ddd --remote-debugging-port=59464"
        with (
            patch.object(ct, "_pid_is_running", return_value=True),
            patch.object(ct, "_process_cmdline", return_value=cmdline),
            patch.object(ct.urllib.request, "urlopen", return_value=response),
        ):
            status, state = ct._provision_slot_status("2ddd")

        self.assertEqual(status, "active")
        self.assertEqual(state["port"], 59464)

    def test_recent_not_ready_slot_is_not_routed_to_default_chrome(self):
        state = dict(self.active_state, ready=False)
        with open(self.state_path, "w") as f:
            json.dump(state, f)
        ct = _reload_with_env(self.env)
        cmdline = "Google Chrome --user-data-dir=/tmp/prov_tmp_2ddd --remote-debugging-port=59464"
        with (
            patch.object(ct, "_pid_is_running", return_value=True),
            patch.object(ct, "_process_cmdline", return_value=cmdline),
        ):
            status, _state = ct._provision_slot_status("2ddd")

        self.assertEqual(status, "starting")
        self.assertTrue(os.path.exists(self.state_path))

    def test_unresponsive_matching_chrome_is_not_routed_to_default_chrome(self):
        ct = _reload_with_env(self.env)
        cmdline = "Google Chrome --user-data-dir=/tmp/prov_tmp_2ddd --remote-debugging-port=59464"
        with (
            patch.object(ct, "_pid_is_running", return_value=True),
            patch.object(ct, "_process_cmdline", return_value=cmdline),
            patch.object(ct.urllib.request, "urlopen", side_effect=OSError("connection refused")),
        ):
            status, _state = ct._provision_slot_status("2ddd")

        self.assertEqual(status, "unavailable")
        self.assertTrue(os.path.exists(self.state_path))

    def test_foreign_agent_slot_is_not_routed(self):
        state = dict(self.active_state, agent_id="claude-other")
        with open(self.state_path, "w") as f:
            json.dump(state, f)
        ct = _reload_with_env(self.env)

        status, _state = ct._provision_slot_status("2ddd")

        self.assertEqual(status, "unavailable")

    def test_windows_liveness_check_does_not_signal_the_process(self):
        ct = _reload_with_env(self.env)
        with (
            patch.object(ct.platform, "system", return_value="Windows"),
            patch.object(ct.subprocess, "check_output", return_value="running\n"),
            patch.object(ct.os, "kill") as mock_kill,
        ):
            self.assertTrue(ct._pid_is_running(12345))

        mock_kill.assert_not_called()

    def test_invalid_slot_never_resolves_to_a_state_file(self):
        ct = _reload_with_env(self.env)

        status, state = ct._provision_slot_status("../2ddd")

        self.assertEqual(status, "stale")
        self.assertEqual(state, {})
        self.assertEqual(ct._parse_prov_slot("prov-../2ddd-any"), "")

    def test_explicit_stale_provision_tab_fails_closed(self):
        ct = _reload_with_env(self.env)
        stderr = io.StringIO()
        with (
            patch.object(ct, "cmd") as mock_cmd,
            patch.object(ct, "_provision_slot_status", return_value=("stale", dict(self.active_state))),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--tab", "prov-2ddd-AABBCCDDEEFF", "--text"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                ct.main()

        mock_cmd.assert_not_called()
        self.assertIn("no longer running", stderr.getvalue())

    def test_cross_slot_tab_flag_fails_closed(self):
        ct = _reload_with_env(self.env)
        stderr = io.StringIO()
        with (
            patch.object(ct, "cmd") as mock_cmd,
            patch.object(ct, "_provision_slot_status", return_value=self._active_slot()),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--tab", "prov-abcd-AABBCCDDEEFF", "--text"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                ct.main()

        mock_cmd.assert_not_called()
        self.assertIn("bound to slot '2ddd'", stderr.getvalue())

    def test_unbound_provision_tab_fails_closed(self):
        env = dict(self.env)
        env["CDP_TAB_ID"] = "auto"
        ct = _reload_with_env(env)
        stderr = io.StringIO()
        with (
            patch.object(ct, "cmd") as mock_cmd,
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--tab", "prov-2ddd-AABBCCDDEEFF", "--text"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                ct.main()

        mock_cmd.assert_not_called()
        self.assertIn("not bound", stderr.getvalue())

    def test_starting_slot_never_routes_to_default_chrome(self):
        ct = _reload_with_env(self.env)
        stderr = io.StringIO()
        with (
            patch.object(ct, "cmd") as mock_cmd,
            patch.object(ct, "_provision_slot_status", return_value=("starting", dict(self.active_state))),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--text"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                ct.main()

        mock_cmd.assert_not_called()
        self.assertIn("still starting", stderr.getvalue())

    def test_unavailable_slot_never_routes_to_default_chrome(self):
        ct = _reload_with_env(self.env)
        stderr = io.StringIO()
        with (
            patch.object(ct, "cmd") as mock_cmd,
            patch.object(ct, "_provision_slot_status", return_value=("unavailable", dict(self.active_state))),
            patch.object(sys, "argv", ["cdp_tool.py", "ddm", "--text"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                ct.main()

        mock_cmd.assert_not_called()
        self.assertIn("unavailable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
