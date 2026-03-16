"""Tests for chrome_bridge helper functions: _sanitize_profile,
_parse_port_from_cmdline, _check_port_conflict."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

# Patch DATA_DIR before importing chrome_bridge so PID/port files go to a temp dir.
_tmpdir = tempfile.mkdtemp(prefix="bridge_test_")

with mock.patch.dict(os.environ, {"UNCHAINED_DATA_DIR": _tmpdir}):
    import chrome_bridge as cb


class TestSanitizeProfile(unittest.TestCase):
    def test_spaces_to_underscores(self):
        self.assertEqual(cb._sanitize_profile("Profile 5"), "Profile_5")

    def test_dots_to_underscores(self):
        self.assertEqual(cb._sanitize_profile("my.profile"), "my_profile")

    def test_strips_invalid_chars(self):
        self.assertEqual(cb._sanitize_profile("hello!@#world"), "helloworld")

    def test_preserves_case(self):
        self.assertEqual(cb._sanitize_profile("Work"), "Work")

    def test_truncates_to_32(self):
        long_name = "a" * 50
        self.assertEqual(len(cb._sanitize_profile(long_name)), 32)

    def test_empty_falls_back_to_default(self):
        self.assertEqual(cb._sanitize_profile(""), "default")

    def test_all_invalid_falls_back_to_default(self):
        self.assertEqual(cb._sanitize_profile("!!!"), "default")

    def test_default_passthrough(self):
        self.assertEqual(cb._sanitize_profile("default"), "default")

    def test_hyphens_preserved(self):
        self.assertEqual(cb._sanitize_profile("my-profile"), "my-profile")


class TestParsePortFromCmdline(unittest.TestCase):
    def test_extracts_port(self):
        cmdline = "python chrome_bridge.py start --port 9223 --relay ws://host"
        self.assertEqual(cb._parse_port_from_cmdline(cmdline), 9223)

    def test_no_port_flag_returns_default(self):
        cmdline = "python chrome_bridge.py start --relay ws://host"
        self.assertEqual(cb._parse_port_from_cmdline(cmdline), cb.DEFAULT_CDP_PORT)

    def test_port_at_end_without_value_returns_default(self):
        cmdline = "python chrome_bridge.py start --port"
        self.assertEqual(cb._parse_port_from_cmdline(cmdline), cb.DEFAULT_CDP_PORT)

    def test_non_numeric_port_returns_default(self):
        cmdline = "python chrome_bridge.py start --port abc"
        self.assertEqual(cb._parse_port_from_cmdline(cmdline), cb.DEFAULT_CDP_PORT)

    def test_empty_cmdline_returns_default(self):
        self.assertEqual(cb._parse_port_from_cmdline(""), cb.DEFAULT_CDP_PORT)


class TestDefaultNewTabUrl(unittest.TestCase):
    def test_prefers_configured_public_base(self):
        with mock.patch.dict(
            os.environ,
            {"UNCHAINED_PUBLIC_BASE_URL": "https://api.unchainedsky.com"},
            clear=False,
        ):
            self.assertEqual(
                cb._default_new_tab_url("ws://127.0.0.1:8765/tunnel"),
                "https://api.unchainedsky.com/tab",
            )

    def test_uses_local_web_port_for_local_relay(self):
        with mock.patch.dict(os.environ, {"WEB_PORT": "9090"}, clear=False):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("ws://127.0.0.1:8765/tunnel"),
                "http://127.0.0.1:9090/tab",
            )

    def test_maps_public_wss_relay_to_https_tab_page(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("wss://api.unchainedsky.com/tunnel"),
                "https://api.unchainedsky.com/tab",
            )

    def test_brackets_ipv6_local_hosts(self):
        with mock.patch.dict(os.environ, {"WEB_PORT": "9090"}, clear=False):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("ws://[::1]:8765/tunnel"),
                "http://[::1]:9090/tab",
            )

    def test_rejects_untrusted_relay_hosts(self):
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(cb.logging, "warning") as mock_warning,
        ):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("wss://example.com/tunnel"),
                "about:blank",
            )
        mock_warning.assert_called_once()

    def test_invalid_public_base_url_falls_back(self):
        with mock.patch.dict(
            os.environ,
            {"UNCHAINED_PUBLIC_BASE_URL": "javascript:alert(1)", "WEB_PORT": "8088"},
            clear=False,
        ):
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("ws://127.0.0.1:8765/tunnel"),
                "http://127.0.0.1:8088/tab",
            )

    def test_ignores_api_url_for_browser_navigation(self):
        with mock.patch.dict(
            os.environ,
            {"UNCHAINED_API_URL": "https://evil.example.com/private?token=secret", "WEB_PORT": "8088"},
            clear=False,
        ):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("ws://127.0.0.1:8765/tunnel"),
                "http://127.0.0.1:8088/tab",
            )

    def test_strips_path_query_and_fragment_from_configured_public_base(self):
        with (
            mock.patch.dict(
                os.environ,
                {"UNCHAINED_PUBLIC_BASE_URL": "https://api.unchainedsky.com/custom/path?x=1#frag"},
                clear=False,
            ),
            mock.patch.object(cb.logging, "warning") as mock_warning,
        ):
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("ws://127.0.0.1:8765/tunnel"),
                "https://api.unchainedsky.com/tab",
            )
        mock_warning.assert_called_once()


class TestWebPort(unittest.TestCase):
    def test_invalid_web_port_warns_and_falls_back(self):
        with (
            mock.patch.dict(os.environ, {"WEB_PORT": "not-a-port"}, clear=False),
            mock.patch.object(cb.logging, "warning") as mock_warning,
        ):
            self.assertEqual(cb._web_port(), cb.DEFAULT_WEB_PORT)
        mock_warning.assert_called_once()


class TestNewTabRequest(unittest.TestCase):
    def test_encodes_reserved_characters_and_brackets_ipv6(self):
        req = cb._new_tab_request("::1", 9222, "https://example.com/a?x=1&y=2#frag")
        self.assertEqual(
            req.full_url,
            "http://[::1]:9222/json/new?https%3A%2F%2Fexample.com%2Fa%3Fx%3D1%26y%3D2%23frag",
        )

    def test_preserves_already_bracketed_ipv6_hosts(self):
        req = cb._new_tab_request("[::1]", 9222, "https://example.com/")
        self.assertEqual(
            req.full_url,
            "http://[::1]:9222/json/new?https%3A%2F%2Fexample.com%2F",
        )


def _write_file(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


class TestCheckPortConflict(unittest.TestCase):
    """Tests for _check_port_conflict using an isolated DATA_DIR."""

    def setUp(self):
        # Clean all PID/port files in the temp dir before each test
        for fname in os.listdir(_tmpdir):
            if fname.startswith(".agent_pid") or fname.startswith(".agent_port"):
                os.remove(os.path.join(_tmpdir, fname))

    def test_no_pid_files_no_conflict(self):
        self.assertIsNone(cb._check_port_conflict(9222, "test"))

    def test_same_profile_not_a_conflict(self):
        # A PID file for the same profile should never conflict with itself
        _write_file(os.path.join(_tmpdir, ".agent_pid_myprofile"), str(os.getpid()))
        _write_file(os.path.join(_tmpdir, ".agent_port_myprofile"), "9222")
        # Patch _process_cmdline to say it's a bridge
        with mock.patch.object(cb, "_process_cmdline", return_value="python chrome_bridge.py start"):
            self.assertIsNone(cb._check_port_conflict(9222, "myprofile"))

    def test_different_port_no_conflict(self):
        _write_file(os.path.join(_tmpdir, ".agent_pid_other"), str(os.getpid()))
        _write_file(os.path.join(_tmpdir, ".agent_port_other"), "9223")
        with mock.patch.object(cb, "_process_cmdline", return_value="python chrome_bridge.py start"):
            self.assertIsNone(cb._check_port_conflict(9222, "new"))

    def test_same_port_conflict(self):
        _write_file(os.path.join(_tmpdir, ".agent_pid_other"), str(os.getpid()))
        _write_file(os.path.join(_tmpdir, ".agent_port_other"), "9222")
        with mock.patch.object(cb, "_process_cmdline", return_value="python chrome_bridge.py start"):
            self.assertEqual(cb._check_port_conflict(9222, "new"), "other")

    def test_legacy_no_port_file_assumes_default(self):
        # Legacy bridge: PID file exists, no port sidecar → assume DEFAULT_CDP_PORT
        _write_file(os.path.join(_tmpdir, ".agent_pid"), str(os.getpid()))
        with mock.patch.object(cb, "_process_cmdline", return_value="python chrome_bridge.py start"):
            self.assertEqual(cb._check_port_conflict(cb.DEFAULT_CDP_PORT, "facebook"), "default")

    def test_legacy_custom_port_parsed_from_cmdline(self):
        # Legacy bridge started with --port 9223 but no port file
        _write_file(os.path.join(_tmpdir, ".agent_pid_old"), str(os.getpid()))
        cmdline = "python chrome_bridge.py start --port 9223 --relay ws://host"
        with mock.patch.object(cb, "_process_cmdline", return_value=cmdline):
            self.assertEqual(cb._check_port_conflict(9223, "new"), "old")

    def test_legacy_custom_port_no_conflict_on_different_port(self):
        # Legacy bridge on --port 9223, new bridge on 9222 → no conflict
        _write_file(os.path.join(_tmpdir, ".agent_pid_old"), str(os.getpid()))
        cmdline = "python chrome_bridge.py start --port 9223 --relay ws://host"
        with mock.patch.object(cb, "_process_cmdline", return_value=cmdline):
            self.assertIsNone(cb._check_port_conflict(9222, "new"))

    def test_dead_pid_no_conflict(self):
        # PID that doesn't exist → skip
        _write_file(os.path.join(_tmpdir, ".agent_pid_dead"), "999999")
        _write_file(os.path.join(_tmpdir, ".agent_port_dead"), "9222")
        self.assertIsNone(cb._check_port_conflict(9222, "new"))

    def test_recycled_pid_not_bridge_no_conflict(self):
        # PID is alive but cmdline shows it's not chrome_bridge
        _write_file(os.path.join(_tmpdir, ".agent_pid_stale"), "1")  # PID 1 = launchd
        _write_file(os.path.join(_tmpdir, ".agent_port_stale"), "9222")
        self.assertIsNone(cb._check_port_conflict(9222, "new"))

    def test_default_profile_pid_file(self):
        # ".agent_pid" (no suffix) → profile "default"
        _write_file(os.path.join(_tmpdir, ".agent_pid"), str(os.getpid()))
        _write_file(os.path.join(_tmpdir, ".agent_port"), "9222")
        with mock.patch.object(cb, "_process_cmdline", return_value="python chrome_bridge.py start"):
            self.assertEqual(cb._check_port_conflict(9222, "other"), "default")


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _TrackedProc:
    def __init__(self, *, terminate_error: Exception | None = None, wait_error_on_first: Exception | None = None):
        self.terminate_error = terminate_error
        self.wait_error_on_first = wait_error_on_first
        self.terminate_calls = 0
        self.wait_calls = 0
        self.kill_calls = 0
        self.pid = 12345

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def wait(self, timeout=None):
        del timeout
        self.wait_calls += 1
        if self.wait_error_on_first is not None and self.wait_calls == 1:
            raise self.wait_error_on_first

    def kill(self):
        self.kill_calls += 1


class _PollingProc:
    def __init__(self):
        self.pid = 54321
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exited = False

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1
        self._exited = True

    def poll(self):
        return 0 if self._exited else None


class TestEnsureChrome(unittest.TestCase):
    def test_launches_with_branded_tab_for_local_relay(self):
        launched = {}

        def _fake_popen(cmd, stdout=None, stderr=None):
            launched["cmd"] = cmd
            return mock.Mock(pid=12345)

        urlopen_calls = 0

        def _fake_urlopen(req, timeout=0):
            nonlocal urlopen_calls
            urlopen_calls += 1
            target = req.full_url if hasattr(req, "full_url") else req
            if urlopen_calls == 1:
                raise urllib.error.URLError("not running")
            if target.endswith("/json/version"):
                return _FakeResponse(200, b"{}")
            if target.endswith("/json"):
                body = b'[{"id":"TAB_1","type":"page","url":"http://127.0.0.1:9090/tab"}]'
                return _FakeResponse(200, body)
            raise AssertionError(f"Unexpected urlopen target: {target}")

        with (
            mock.patch.object(cb, "_find_chrome_binary", return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            mock.patch.object(cb.subprocess, "Popen", side_effect=_fake_popen),
            mock.patch.object(cb.time, "sleep"),
            mock.patch.object(cb.urllib.request, "urlopen", side_effect=_fake_urlopen),
            mock.patch.dict(os.environ, {"WEB_PORT": "9090"}, clear=False),
        ):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            ok = cb._ensure_chrome(
                "127.0.0.1",
                9222,
                "default",
                False,
                "",
                "ws://127.0.0.1:8765/tunnel",
            )

        self.assertTrue(ok)
        self.assertEqual(launched["cmd"][-1], "http://127.0.0.1:9090/tab")

    def test_fails_fast_when_startup_tab_creation_fails_after_chrome_is_ready(self):
        proc = _TrackedProc()
        launched = {"proc": proc}

        def _fake_popen(cmd, stdout=None, stderr=None):
            launched["cmd"] = cmd
            return proc

        version_checks = 0

        def _fake_urlopen(req, timeout=0):
            nonlocal version_checks
            target = req.full_url if hasattr(req, "full_url") else req
            if target.endswith("/json/version"):
                version_checks += 1
                if version_checks == 1:
                    raise urllib.error.URLError("not running")
                return _FakeResponse(200, b"{}")
            raise AssertionError(f"Unexpected urlopen target: {target}")

        with (
            mock.patch.object(cb, "_find_chrome_binary", return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            mock.patch.object(cb.subprocess, "Popen", side_effect=_fake_popen),
            mock.patch.object(cb.time, "sleep") as mock_sleep,
            mock.patch.object(cb.urllib.request, "urlopen", side_effect=_fake_urlopen),
            mock.patch.object(cb, "_first_page_tab", side_effect=urllib.error.URLError("create failed")) as mock_first_page_tab,
            mock.patch.dict(os.environ, {"WEB_PORT": "9090"}, clear=False),
            mock.patch("builtins.print") as mock_print,
        ):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            ok = cb._ensure_chrome(
                "127.0.0.1",
                9222,
                "default",
                False,
                "",
                "ws://127.0.0.1:8765/tunnel",
            )

        self.assertFalse(ok)
        self.assertEqual(launched["cmd"][-1], "http://127.0.0.1:9090/tab")
        self.assertEqual(version_checks, 2)
        self.assertEqual(mock_sleep.call_count, 1)
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.wait_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        mock_first_page_tab.assert_called_once_with("127.0.0.1", 9222, "http://127.0.0.1:9090/tab")
        self.assertTrue(any("could not open startup tab" in str(call) for call in mock_print.call_args_list))

    def test_cmd_start_uses_default_relay_url_when_missing_from_config(self):
        config = {
            "api_key": "",
            "cdp_host": "127.0.0.1",
            "cdp_port": 9222,
            "profile": "default",
            "chrome_headless": False,
            "chrome_args": "",
            "daemon": False,
        }

        with (
            mock.patch.object(cb, "_is_agent_running", return_value=False),
            mock.patch.object(cb, "_check_port_conflict", return_value=None),
            mock.patch.object(cb, "_ensure_chrome", return_value=False) as mock_ensure,
            mock.patch("builtins.print"),
        ):
            cb.cmd_start(config)

        mock_ensure.assert_called_once_with(
            "127.0.0.1",
            9222,
            "default",
            False,
            "",
            cb.DEFAULT_RELAY_URL,
        )


class TestProvisionCleanup(unittest.TestCase):
    def test_cleanup_single_prov_waits_after_kill(self):
        proc = _TrackedProc(terminate_error=RuntimeError("terminate failed"))
        agent = cb.Agent(relay_url="ws://127.0.0.1:8765/tunnel")

        with mock.patch("builtins.print") as mock_print:
            agent._cleanup_single_prov({"process": proc, "temp_dir": ""})

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)
        self.assertEqual(proc.wait_calls, 1)
        self.assertTrue(any("Provision Chrome killed" in str(call) for call in mock_print.call_args_list))


class TestProvisionStateRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.makedirs(cb.PROVISION_STATE_DIR, exist_ok=True)
        for name in os.listdir(cb.PROVISION_STATE_DIR):
            if name.endswith(".json"):
                os.remove(os.path.join(cb.PROVISION_STATE_DIR, name))
        self._temp_dirs: list[str] = []

    def tearDown(self):
        for path in self._temp_dirs:
            cb.shutil.rmtree(path, ignore_errors=True)
        for name in os.listdir(cb.PROVISION_STATE_DIR):
            if name.endswith(".json"):
                os.remove(os.path.join(cb.PROVISION_STATE_DIR, name))

    async def test_cleanup_recovers_persisted_slot_when_tracking_lost(self):
        slot = "ab12"
        temp_dir = tempfile.mkdtemp(dir=_tmpdir, prefix="prov_recover_")
        self._temp_dirs.append(temp_dir)
        cb._write_prov_state(slot, {
            "pid": 4242,
            "port": 10001,
            "temp_dir": temp_dir,
            "profile_dir_name": "Profile 5",
        })

        agent = cb.Agent(relay_url="ws://127.0.0.1:8765/tunnel")
        agent.running = True
        agent.ws = mock.AsyncMock()

        with (
            mock.patch.object(cb, "_classify_prov_pid", return_value="alive"),
            mock.patch.object(cb, "_terminate_pid", return_value=True) as mock_terminate_pid,
            mock.patch("builtins.print"),
        ):
            await agent._handle_provision_cleanup(req_id="r-recover", path=f"/provision-cleanup?slot={slot}")

        mock_terminate_pid.assert_called_once_with(4242, "Provision Chrome", prefix="[agent:prov]")
        self.assertEqual(agent._prov_chromes, {})
        self.assertFalse(os.path.exists(cb._prov_state_path(slot)))
        self.assertFalse(os.path.isdir(temp_dir))
        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["body"]["status"], "cleaned_up")
        self.assertEqual(sent["body"]["cleaned"], 1)

    async def test_status_recovers_persisted_slot_when_memory_tracking_is_empty(self):
        slot = "cd34"
        temp_dir = tempfile.mkdtemp(dir=_tmpdir, prefix="prov_status_")
        self._temp_dirs.append(temp_dir)
        cb._write_prov_state(slot, {
            "pid": 4343,
            "port": 10002,
            "temp_dir": temp_dir,
            "profile_dir_name": "Profile 7",
        })

        agent = cb.Agent(relay_url="ws://127.0.0.1:8765/tunnel")
        agent.running = True
        agent.ws = mock.AsyncMock()

        tabs = [
            {
                "id": "AAA111BBB222CCC333DDD444EEE555FF",
                "type": "page",
                "title": "Recovered tab",
                "url": "https://x.com/i/flow/login",
            },
        ]

        def _fake_urlopen(req, timeout=0):
            del timeout
            target = req.full_url if hasattr(req, "full_url") else req
            if target == "http://127.0.0.1:10002/json":
                return _FakeResponse(200, json.dumps(tabs).encode())
            raise AssertionError(f"Unexpected urlopen target: {target}")

        with (
            mock.patch.object(cb, "_classify_prov_pid", return_value="alive"),
            mock.patch.object(cb.urllib.request, "urlopen", side_effect=_fake_urlopen),
        ):
            await agent._handle_provision_status(req_id="r-status")

        sent = json.loads(agent.ws.send.call_args[0][0])
        slots = sent["body"]["slots"]
        self.assertIn(slot, slots)
        self.assertEqual(slots[slot]["profile"], "Profile 7")
        self.assertEqual(slots[slot]["tabs"][0]["tab_id"], "prov-cd34-AAA111BBB222CCC333DDD444EEE555FF")


class TestTerminateProcess(unittest.TestCase):
    def test_kill_path_uses_poll_based_exit_confirmation(self):
        proc = _PollingProc()
        tick = {"now": 0.0}

        def _fake_time():
            tick["now"] += 0.2
            return tick["now"]

        with (
            mock.patch.object(cb.time, "time", side_effect=_fake_time),
            mock.patch.object(cb.time, "sleep"),
            mock.patch("builtins.print") as mock_print,
        ):
            ok = cb._terminate_process(proc, "Chrome")

        self.assertTrue(ok)
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)
        self.assertTrue(any("Chrome killed" in str(call) for call in mock_print.call_args_list))


class TestFirstPageTab(unittest.TestCase):
    def test_raises_when_tab_creation_fails(self):
        def _fake_urlopen(req, timeout=0):
            target = req.full_url if hasattr(req, "full_url") else req
            if target.endswith("/json"):
                return _FakeResponse(200, b"[]")
            if "/json/new?" in target:
                raise urllib.error.URLError("create failed")
            raise AssertionError(f"Unexpected urlopen target: {target}")

        with mock.patch.object(cb.urllib.request, "urlopen", side_effect=_fake_urlopen):
            with self.assertRaises(urllib.error.URLError):
                cb._first_page_tab("127.0.0.1", 9222, "http://127.0.0.1:8080/tab")

    def test_recovers_created_tab_when_json_new_response_is_malformed(self):
        calls = []

        def _fake_urlopen(req, timeout=0):
            del timeout
            target = req.full_url if hasattr(req, "full_url") else req
            calls.append(target)
            if target.endswith("/json") and calls.count(target) == 1:
                return _FakeResponse(200, b"[]")
            if "/json/new?" in target:
                return _FakeResponse(200, b"{not-json")
            if target.endswith("/json") and calls.count(target) == 2:
                return _FakeResponse(200, b'[{"id":"TAB_2","type":"page","url":"http://127.0.0.1:8080/tab"}]')
            raise AssertionError(f"Unexpected urlopen target: {target}")

        with (
            mock.patch.object(cb.urllib.request, "urlopen", side_effect=_fake_urlopen),
            mock.patch.object(cb.logging, "warning") as mock_warning,
        ):
            tab = cb._first_page_tab("127.0.0.1", 9222, "http://127.0.0.1:8080/tab")

        self.assertEqual(tab["id"], "TAB_2")
        mock_warning.assert_called_once()

    def test_raises_runtime_error_when_created_tab_cannot_be_discovered(self):
        calls = []

        def _fake_urlopen(req, timeout=0):
            del timeout
            target = req.full_url if hasattr(req, "full_url") else req
            calls.append(target)
            if target.endswith("/json") and calls.count(target) == 1:
                return _FakeResponse(200, b"[]")
            if "/json/new?" in target:
                return _FakeResponse(200, b"{not-json")
            if target.endswith("/json") and calls.count(target) == 2:
                return _FakeResponse(200, b"[]")
            raise AssertionError(f"Unexpected urlopen target: {target}")

        with (
            mock.patch.object(cb.urllib.request, "urlopen", side_effect=_fake_urlopen),
            mock.patch.object(cb.logging, "warning") as mock_warning,
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be discovered"):
                cb._first_page_tab("127.0.0.1", 9222, "http://127.0.0.1:8080/tab")

        mock_warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
