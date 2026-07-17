"""Privacy and lifecycle contracts for the public unbrowser growth funnel."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, urlencode

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from aiohttp import web as aiohttp_web

import web as core_module
from web_app.handlers import pages, unbrowser_demo


class _Request:
    def __init__(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, str] | None = None,
        referer: str = "https://unchainedsky.com/unbrowser",
    ):
        self.path = path
        self.method = method
        self.query = dict(query or {})
        self.headers = {
            "User-Agent": "Mozilla/5.0 Chrome/140.0 Safari/537.36",
            "Referer": referer,
        }
        self.remote = "10.0.0.91"


class _Core:
    def __init__(self):
        self.calls: list[dict] = []

    def _analytics_acquisition_meta(self, request):
        return core_module._analytics_acquisition_meta(request)

    def _track_event(self, _request, event: str, **kwargs):
        call = dict(kwargs)
        call["meta"] = dict(kwargs.get("meta") or {})
        self.calls.append({"event": event, **call})
        return True


class _InstallCore:
    INSTALL_ONBOARD_HTML = core_module.INSTALL_ONBOARD_HTML
    GOOGLE_CLIENT_ID = ""

    def __init__(self):
        self.page_views = 0

    def _track_page_view(self, _request):
        self.page_views += 1
        return True

    def _analytics_acquisition_meta(self, request):
        return core_module._analytics_acquisition_meta(request)

    def inject_google_client_id(self, html, _client_id):
        return html


class _StreamResponse:
    def __init__(self, *, fail_prepare: BaseException | None = None):
        self.fail_prepare = fail_prepare
        self.prepared = False

    async def prepare(self, _request):
        if self.fail_prepare is not None:
            raise self.fail_prepare
        self.prepared = True
        return self


class _BusySemaphore:
    async def acquire(self):
        raise TimeoutError

    def release(self):
        raise AssertionError("a semaphore that was never acquired must not be released")


class UnbrowserOutboundAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_records_fixed_server_event_then_redirects(self):
        core = _Core()
        request = _Request(
            "/go/unbrowser-github",
            query={
                "utm_source": "github",
                "utm_medium": "repository",
                "utm_campaign": "unbrowser_guide",
                "private": "must-not-be-stored",
            },
        )
        with patch.object(pages, "_core", return_value=core):
            with self.assertRaises(aiohttp_web.HTTPFound) as raised:
                await pages.handle_unbrowser_outbound(request)

        self.assertEqual(raised.exception.location, "https://github.com/protostatis/unbrowser")
        self.assertEqual(len(core.calls), 1)
        call = core.calls[0]
        self.assertEqual(call["event"], "unbrowser_outbound_click")
        self.assertEqual(call["route"], "/go/unbrowser-github")
        self.assertEqual(call["route_effective"], "https://github.com/protostatis/unbrowser")
        self.assertEqual(call["cta_id"], "unbrowser_install_github")
        self.assertEqual(
            call["meta"],
            {
                "utm_source": "github",
                "utm_medium": "repository",
                "utm_campaign": "unbrowser_guide",
                "destination": "github_repository",
            },
        )

    async def test_completed_demo_connect_records_then_redirects_to_fixed_install_campaign(self):
        core = _Core()
        request = _Request(
            "/go/unbrowser-connect",
            query={
                "utm_source": "mcp_so",
                "utm_medium": "marketplace",
                "utm_campaign": "unbrowser_directory",
                "private": "must-not-be-stored",
            },
        )
        with patch.object(pages, "_core", return_value=core):
            with self.assertRaises(aiohttp_web.HTTPFound) as raised:
                await pages.handle_unbrowser_outbound(request)

        expected = (
            "/install?ref=unbrowser_demo_complete_v1&utm_source=unchainedsky"
            "&utm_medium=product&utm_campaign=unbrowser_demo_complete_v1"
        )
        self.assertEqual(raised.exception.location, expected)
        self.assertEqual(len(core.calls), 1)
        call = core.calls[0]
        self.assertEqual(call["event"], "unbrowser_outbound_click")
        self.assertEqual(call["route"], "/go/unbrowser-connect")
        self.assertEqual(call["route_effective"], expected)
        self.assertEqual(call["cta_id"], "unbrowser_connect_computer")
        self.assertEqual(
            call["meta"],
            {
                "utm_source": "mcp_so",
                "utm_medium": "marketplace",
                "utm_campaign": "unbrowser_directory",
                "destination": "unchained_install",
            },
        )

    async def test_head_redirects_without_recording_a_click(self):
        core = _Core()
        request = _Request("/go/unbrowser-smithery", method="HEAD")
        with patch.object(pages, "_core", return_value=core):
            with self.assertRaises(aiohttp_web.HTTPFound) as raised:
                await pages.handle_unbrowser_outbound(request)

        self.assertEqual(
            raised.exception.location,
            "https://smithery.ai/servers/protostatis-dev/unbrowser",
        )
        self.assertEqual(core.calls, [])


class InstallAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sign_in_return_preserves_only_bounded_acquisition_fields(self):
        core = _InstallCore()
        request = _Request(
            "/install",
            query={
                "ref": "unbrowser_demo_complete_v1",
                "utm_source": "unchainedsky",
                "utm_medium": "product",
                "utm_campaign": "unbrowser_demo_complete_v1",
                "private": "must-not-be-stored",
            },
        )
        with patch.object(pages, "_core", return_value=core):
            response = await pages.handle_install_page(request)

        acquisition = core_module._analytics_acquisition_meta(request)
        expected_path = f"/install?{urlencode(acquisition)}"
        self.assertEqual(core.page_views, 1)
        self.assertEqual(response.status, 200)
        self.assertIn(
            f'href="/local?next={quote(expected_path, safe="")}">Sign In</a>',
            response.text,
        )
        self.assertIn(
            f"const INSTALL_RETURN_PATH = '{expected_path}';",
            response.text,
        )
        self.assertNotIn("must-not-be-stored", response.text)
        self.assertNotIn("__INSTALL_RETURN_PATH", response.text)

    async def test_sign_in_return_falls_back_to_plain_install_path(self):
        core = _InstallCore()
        request = _Request("/install")
        with patch.object(pages, "_core", return_value=core):
            response = await pages.handle_install_page(request)

        self.assertIn('href="/local?next=%2Finstall">Sign In</a>', response.text)
        self.assertIn("const INSTALL_RETURN_PATH = '/install';", response.text)
        self.assertNotIn("__INSTALL_RETURN_PATH", response.text)


class UnbrowserDemoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, scenario: str = "ai-agents") -> _Request:
        return _Request(
            "/web/unbrowser/stream",
            query={
                "scenario": scenario,
                "ref": "unbrowser-readme",
                "utm_source": "github",
                "utm_medium": "repository",
                "utm_campaign": "unbrowser_guide",
                "secret": "must-not-be-stored",
            },
        )

    async def test_head_does_not_resolve_core_acquire_capacity_or_record_a_run(self):
        request = self._request()
        request.method = "HEAD"
        with patch.object(
            unbrowser_demo,
            "_core",
            side_effect=AssertionError("HEAD must stop before analytics or scan setup"),
        ):
            response = await unbrowser_demo.handle_unbrowser_stream(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.headers.get("Content-Type"),
            "text/event-stream; charset=utf-8",
        )

    async def test_completed_run_has_one_accepted_and_one_terminal_event(self):
        core = _Core()
        response = _StreamResponse()
        semaphore = asyncio.Semaphore(1)
        with (
            patch.object(unbrowser_demo, "_core", return_value=core),
            patch.object(unbrowser_demo, "_SCAN_SEMAPHORE", semaphore),
            patch.object(unbrowser_demo.web, "StreamResponse", return_value=response),
            patch.object(unbrowser_demo, "_run_scan", new=AsyncMock()),
        ):
            returned = await unbrowser_demo.handle_unbrowser_stream(self._request("unknown"))

        self.assertIs(returned, response)
        self.assertTrue(response.prepared)
        self.assertFalse(semaphore.locked())
        self.assertEqual(
            [call["event"] for call in core.calls],
            ["unbrowser_demo_run_accepted", "unbrowser_demo_run_terminal"],
        )
        accepted, terminal = core.calls
        self.assertEqual(accepted["meta"]["run_id"], terminal["meta"]["run_id"])
        self.assertEqual(accepted["meta"]["scenario_id"], "ai-agents")
        self.assertEqual(terminal["meta"]["outcome"], "completed")
        self.assertEqual(terminal["status_code"], 200)
        captured = json.dumps(core.calls, sort_keys=True)
        self.assertNotIn("must-not-be-stored", captured)
        self.assertNotIn("secret", captured)
        self.assertNotIn("https://", captured)

    async def test_prepare_failure_is_not_counted_as_an_accepted_run(self):
        core = _Core()
        response = _StreamResponse(fail_prepare=ConnectionResetError("closed"))
        semaphore = asyncio.Semaphore(1)
        with (
            patch.object(unbrowser_demo, "_core", return_value=core),
            patch.object(unbrowser_demo, "_SCAN_SEMAPHORE", semaphore),
            patch.object(unbrowser_demo.web, "StreamResponse", return_value=response),
        ):
            with self.assertRaises(ConnectionResetError):
                await unbrowser_demo.handle_unbrowser_stream(self._request())

        self.assertEqual(core.calls, [])
        self.assertFalse(semaphore.locked())

    async def test_busy_run_records_only_rejection(self):
        core = _Core()
        with (
            patch.object(unbrowser_demo, "_core", return_value=core),
            patch.object(unbrowser_demo, "_SCAN_SEMAPHORE", _BusySemaphore()),
        ):
            response = await unbrowser_demo.handle_unbrowser_stream(self._request("news"))

        self.assertEqual(response.status, 429)
        self.assertEqual([call["event"] for call in core.calls], ["unbrowser_demo_run_rejected"])
        self.assertEqual(core.calls[0]["error_code"], "demo_busy")
        self.assertEqual(core.calls[0]["meta"]["scenario_id"], "news")

    async def test_accepted_failures_emit_exactly_one_terminal_outcome(self):
        cases = (
            (ConnectionResetError("closed"), "client_disconnected", 499),
            (BrokenPipeError("closed"), "client_disconnected", 499),
            (asyncio.CancelledError(), "cancelled", 499),
            (RuntimeError("internal detail"), "error", 500),
        )
        for failure, outcome, status_code in cases:
            with self.subTest(outcome=outcome, failure=type(failure).__name__):
                core = _Core()
                response = _StreamResponse()
                semaphore = asyncio.Semaphore(1)
                with (
                    patch.object(unbrowser_demo, "_core", return_value=core),
                    patch.object(unbrowser_demo, "_SCAN_SEMAPHORE", semaphore),
                    patch.object(unbrowser_demo.web, "StreamResponse", return_value=response),
                    patch.object(
                        unbrowser_demo,
                        "_run_scan",
                        new=AsyncMock(side_effect=failure),
                    ),
                ):
                    with self.assertRaises(type(failure)):
                        await unbrowser_demo.handle_unbrowser_stream(self._request())

                self.assertFalse(semaphore.locked())
                self.assertEqual(
                    [call["event"] for call in core.calls],
                    ["unbrowser_demo_run_accepted", "unbrowser_demo_run_terminal"],
                )
                accepted, terminal = core.calls
                self.assertEqual(accepted["meta"]["run_id"], terminal["meta"]["run_id"])
                self.assertEqual(terminal["meta"]["outcome"], outcome)
                self.assertEqual(terminal["status_code"], status_code)
                if str(failure):
                    self.assertNotIn(str(failure), json.dumps(core.calls, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
