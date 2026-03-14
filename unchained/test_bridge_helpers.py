"""Tests for chrome_bridge helper functions: _sanitize_profile,
_parse_port_from_cmdline, _check_port_conflict."""

from __future__ import annotations

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
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
            os.environ.pop("UNCHAINED_API_URL", None)
            self.assertEqual(
                cb._default_new_tab_url("wss://example.com/tunnel"),
                "about:blank",
            )

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


class TestNewTabRequest(unittest.TestCase):
    def test_encodes_reserved_characters_and_brackets_ipv6(self):
        req = cb._new_tab_request("::1", 9222, "https://example.com/a?x=1&y=2#frag")
        self.assertEqual(
            req.full_url,
            "http://[::1]:9222/json/new?https%3A%2F%2Fexample.com%2Fa%3Fx%3D1%26y%3D2%23frag",
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


if __name__ == "__main__":
    unittest.main()
