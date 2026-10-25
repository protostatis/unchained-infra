"""The desktop app's download lane, and the card that offers it.

Two properties are easy to lose later, so they are pinned here:

  * the beta is a **deployment** decision twice over — the endpoint 404s when the flag is
    off, and the page still refuses to show the card off macOS, because the build is arm64
    and a download for another OS would be dead;
  * the artifact has its **own filename**, never the agent installer's, so the two clients
    cannot be confused at the download step.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import web
from web_app.handlers import install_flow, pages


class DesktopArtifactResolutionTests(unittest.TestCase):
    def test_flag_off_means_no_path_even_when_the_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "unchained-desktop-mac-arm64.dmg").write_bytes(b"dmg")
            with patch.object(web, "_INSTALLER_ASSETS_DIR", Path(tmp)), \
                 patch.object(web, "_DESKTOP_BETA_ENABLED", False):
                self.assertIsNone(web._desktop_installer_path())

    def test_missing_artifact_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(web, "_INSTALLER_ASSETS_DIR", Path(tmp)), \
             patch.object(web, "_DESKTOP_BETA_ENABLED", True):
            self.assertIsNone(web._desktop_installer_path())

    def test_freshest_of_the_configured_names_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            older = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            newer = Path(tmp) / "unchained-desktop-mac-arm64-2.dmg"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            newer.touch()
            with patch.object(web, "_INSTALLER_ASSETS_DIR", Path(tmp)), \
                 patch.object(web, "_DESKTOP_BETA_ENABLED", True), \
                 patch.object(web, "_DESKTOP_MAC_INSTALLER_FILES",
                              [older.name, newer.name]):
                self.assertEqual(web._desktop_installer_path(), newer.resolve())

    def test_it_never_reaches_outside_the_assets_directory(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(web, "_INSTALLER_ASSETS_DIR", Path(tmp)), \
             patch.object(web, "_DESKTOP_BETA_ENABLED", True), \
             patch.object(web, "_DESKTOP_MAC_INSTALLER_FILES", ["../escape.dmg"]):
            self.assertIsNone(web._desktop_installer_path())


class DownloadDesktopHandlerTests(unittest.IsolatedAsyncioTestCase):
    def _core(self, *, beta=True, authenticated=True, artifact=None, platform="mac"):
        return SimpleNamespace(
            _DESKTOP_BETA_ENABLED=beta,
            _normalize_installer_platform=lambda value: platform,
            _request_install_token=lambda request: "",
            _authenticate=lambda request: (
                {"user_id": "u-test", "user_type": "claude"} if authenticated else None
            ),
            _desktop_installer_path=lambda: artifact,
            _track_event=Mock(),
            _auth=SimpleNamespace(
                validate_install_token=lambda token, consume=False: {"user_id": "u-test"}
            ),
        )

    async def _call(self, core):
        request = SimpleNamespace(query={}, headers={}, match_info={})
        with patch.object(install_flow, "_core", return_value=core):
            return await install_flow.handle_download_desktop(request)

    async def test_flag_off_is_404(self):
        response = await self._call(self._core(beta=False))
        self.assertEqual(response.status, 404)

    async def test_windows_is_refused(self):
        response = await self._call(self._core(platform="windows"))
        self.assertEqual(response.status, 400)

    async def test_an_authenticated_download_is_not_capped(self):
        # A shared address (office NAT, CI) must not lock out attributable downloads.
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            dmg.write_bytes(b"dmg-bytes")
            core = self._core(artifact=dmg)
            for _ in range(install_flow._DESKTOP_DOWNLOAD_MAX_PER_IP + 2):
                response = await self._call(core)
                self.assertEqual(response.status, 200)

    async def test_anonymous_download_is_allowed(self):
        # The download is the top of the funnel; the account gate belongs in the app's
        # Connect step, not in front of a 135MB file.
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            dmg.write_bytes(b"dmg-bytes")
            core = self._core(authenticated=False, artifact=dmg)
            response = await self._call(core)
        self.assertEqual(response.status, 200)
        meta = core._track_event.call_args.kwargs["meta"]
        self.assertEqual(meta["channel"], "public")

    async def test_a_session_makes_the_download_attributable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            dmg.write_bytes(b"dmg-bytes")
            core = self._core(artifact=dmg)
            await self._call(core)
        self.assertEqual(core._track_event.call_args.kwargs["meta"]["channel"], "session")

    async def test_missing_artifact_is_503(self):
        response = await self._call(self._core(artifact=None))
        self.assertEqual(response.status, 503)

    async def test_present_artifact_is_served_and_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            dmg.write_bytes(b"dmg-bytes")
            core = self._core(artifact=dmg)
            response = await self._call(core)
        self.assertEqual(response.status, 200)
        self.assertIn("unchained-desktop-mac-arm64.dmg", response.headers["Content-Disposition"])
        core._track_event.assert_called_once()
        meta = core._track_event.call_args.kwargs["meta"]
        self.assertEqual(meta["artifact"], "desktop")
        self.assertEqual(core._track_event.call_args.kwargs["cta_id"], "download_desktop")


class DesktopDownloadLimitTests(unittest.TestCase):
    def setUp(self):
        install_flow._desktop_download_hits.clear()

    def tearDown(self):
        install_flow._desktop_download_hits.clear()

    def test_forwarded_for_is_only_trusted_from_a_proxy(self):
        class Req:
            def __init__(self, remote, forwarded=""):
                self.remote = remote
                self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}

        # The deployment's proxy may speak for the client...
        self.assertEqual(install_flow._client_source_ip(Req("172.18.0.4", "203.0.113.7")), "203.0.113.7")
        self.assertEqual(install_flow._client_source_ip(Req("127.0.0.1", "203.0.113.7")), "203.0.113.7")
        # ...a public peer may not: its header would let it rotate the cap away.
        # A genuinely public peer (8.8.8.8): its header is ignored. Note Python counts
        # the documentation ranges as private, so they cannot stand in for "public" here.
        self.assertEqual(install_flow._client_source_ip(Req("8.8.8.8", "1.1.1.1")), "8.8.8.8")
        self.assertEqual(install_flow._client_source_ip(Req("8.8.8.8")), "8.8.8.8")

    def test_the_key_table_cannot_grow_without_bound(self):
        now = 2_000_000.0
        for index in range(install_flow._DESKTOP_DOWNLOAD_MAX_KEYS + 50):
            install_flow._desktop_download_allowed(f"198.51.100.{index % 256}:{index}", now)[0]
        self.assertLessEqual(
            len(install_flow._desktop_download_hits),
            install_flow._DESKTOP_DOWNLOAD_MAX_KEYS,
        )

    def test_one_address_may_not_pull_it_in_a_loop(self):
        now = 1_000_000.0
        allowed = [
            install_flow._desktop_download_allowed("203.0.113.7", now + i)[0]
            for i in range(install_flow._DESKTOP_DOWNLOAD_MAX_PER_IP)
        ]
        self.assertTrue(all(allowed))
        blocked, retry_after = install_flow._desktop_download_allowed(
            "203.0.113.7", now + install_flow._DESKTOP_DOWNLOAD_MAX_PER_IP
        )
        self.assertFalse(blocked)
        # The wait is what is left of the window, not the whole hour again.
        self.assertLessEqual(retry_after, install_flow._DESKTOP_DOWNLOAD_WINDOW_S)
        self.assertGreater(retry_after, install_flow._DESKTOP_DOWNLOAD_WINDOW_S - 10)
        # Another address is unaffected, and the window lets the first one back in.
        self.assertTrue(install_flow._desktop_download_allowed("203.0.113.8", now)[0])
        self.assertTrue(
            install_flow._desktop_download_allowed(
                "203.0.113.7", now + install_flow._DESKTOP_DOWNLOAD_WINDOW_S + 1
            )[0]
        )


class DesktopPageTests(unittest.IsolatedAsyncioTestCase):
    def _core(self, *, beta=True, artifact=None, version=""):
        from web_app.templates import DESKTOP_PAGE_HTML

        return SimpleNamespace(
            _DESKTOP_BETA_ENABLED=beta,
            _DESKTOP_VERSION=version,
            DESKTOP_PAGE_HTML=DESKTOP_PAGE_HTML,
            _desktop_installer_path=lambda: artifact,
            _track_page_view=Mock(),
        )

    async def _call(self, core):
        request = SimpleNamespace(query={}, headers={}, remote="127.0.0.1")
        with patch.object(pages, "_core", return_value=core):
            return await pages.handle_desktop_page(request)

    async def test_the_page_is_public_and_offers_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            dmg.write_bytes(b"x" * (3 * 1024 * 1024))
            response = await self._call(self._core(artifact=dmg, version="0.5.2"))
        self.assertEqual(response.status, 200)
        self.assertIn("Download for macOS", response.text)
        self.assertIn('href="/web/download-desktop"', response.text)
        self.assertIn("3 MB", response.text)
        self.assertIn("v0.5.2", response.text)

    async def test_flag_off_is_404_and_is_not_a_page_view(self):
        core = self._core(beta=False)
        response = await self._call(core)
        self.assertEqual(response.status, 404)
        core._track_page_view.assert_not_called()

    async def test_a_served_page_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "unchained-desktop-mac-arm64.dmg"
            dmg.write_bytes(b"x")
            core = self._core(artifact=dmg)
            await self._call(core)
        core._track_page_view.assert_called_once()

    async def test_missing_build_is_404(self):
        response = await self._call(self._core(artifact=None))
        self.assertEqual(response.status, 404)

    def test_the_page_is_in_the_sitemap(self):
        body = asyncio.run(web.handle_sitemap_xml(None)).text
        self.assertIn("https://unchainedsky.com/desktop", body)


class InstallPageMarkupTests(unittest.TestCase):
    def test_the_card_is_present_but_hidden_and_gated(self):
        from web_app.templates import INSTALL_ONBOARD_HTML as html

        self.assertIn('<section class="card card-primary" id="desktop-card" hidden>', html)
        self.assertIn('href="/web/download-desktop"', html)
        self.assertIn("const DESKTOP_BETA = __DESKTOP_BETA__;", html)
        # Shown only when the deployment ships the beta *and* the visitor is on macOS.
        self.assertIn("const desktopAvailable = DESKTOP_BETA && _installOs === 'mac';", html)
        self.assertIn("card.hidden = !desktopAvailable;", html)

    def test_the_card_names_the_client_it_is_not(self):
        from web_app.templates import INSTALL_ONBOARD_HTML as html

        self.assertIn("No agent, no terminal", html)
        self.assertIn("different clients", html)


if __name__ == "__main__":
    unittest.main()
