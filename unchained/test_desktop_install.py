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
from web_app.handlers import install_flow


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

    async def test_signed_out_is_401(self):
        response = await self._call(self._core(authenticated=False))
        self.assertEqual(response.status, 401)

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


class InstallPageMarkupTests(unittest.TestCase):
    def test_the_card_is_present_but_hidden_and_gated(self):
        from web_app.templates import INSTALL_ONBOARD_HTML as html

        self.assertIn('<div class="card" id="desktop-card" hidden>', html)
        self.assertIn('href="/web/download-desktop"', html)
        self.assertIn("const DESKTOP_BETA = __DESKTOP_BETA__;", html)
        # Shown only when the deployment ships the beta *and* the visitor is on macOS.
        self.assertIn(
            "desktopCard.hidden = !(DESKTOP_BETA && _installOs === 'mac')", html
        )

    def test_the_card_names_the_client_it_is_not(self):
        from web_app.templates import INSTALL_ONBOARD_HTML as html

        self.assertIn("No agent, no terminal", html)
        self.assertIn("they are different clients", html)


if __name__ == "__main__":
    unittest.main()
