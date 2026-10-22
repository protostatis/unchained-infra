"""Golden regression guard for the workbench shell.

The workbench presentation must not disturb the shipped `task` / `legacy`
shells. Marker assertions cannot establish that: this module records the
*markup* of the rendered hosted pages and fails the moment it drifts.

Markup is compared with `<style>`, `<script>` and workbench asset references
normalised out, because those legitimately move (the workbench layer is
delivered as static assets rather than an inline block). Everything else —
every tag, attribute, id and text node — must be byte-identical.

Baselines were captured from origin/main before the workbench layer was moved
out of `templates.py`. See WORKBENCH_SLICE01_PLAN.md (P0.0).
"""

from __future__ import annotations

import hashlib
import os
import re
import types
import unittest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web
import web_app.templates as templates
from template_utils import inject_google_client_id
from web_app.handlers import pages as pages_module

# Captured before the workbench layer moved to static assets (P0.0). Recomputed when the
# install modal's method cards gained ids, so their labels can follow the detected OS
# ("macOS installer" / "Windows installer", "Terminal command" / "PowerShell command").
# The three id attributes are the whole diff: scripts and styles are normalised out.
BASE_MARKUP_SHA256 = {
    "trial": "cf374f4788d6efee25fb0f8d0c5ebf8b7aa2032803d65e356a58930c5e087e94",
    "workspace": "0eab31ad7da2e3748fe01bb56666f488b8d15092770e8caf6063f5c4e203fa25",
}

_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_ASSET_RE = re.compile(r"<link[^>]*workbench[^>]*>\s*", re.IGNORECASE)

WORKBENCH_STYLESHEET_REF = '<link rel="stylesheet" href="/web/static/workbench.css">'


def render_hosted_page(*, workspace: bool, workbench_enabled: bool) -> str:
    """Render the hosted page the way each route does."""
    core = types.SimpleNamespace(
        TRIAL_CHAT_HTML=templates.TRIAL_CHAT_HTML,
        WORKBENCH_SHELL_ENABLED=workbench_enabled,
        GOOGLE_CLIENT_ID="",
        inject_google_client_id=inject_google_client_id,
    )
    return pages_module._render_hosted_chat_page(core, workspace=workspace)


def markup_signature(html: str) -> str:
    """Hash the markup with the deliberately-movable payloads removed."""
    normalised = _STYLE_RE.sub("<style/>", html)
    normalised = _SCRIPT_RE.sub("<script/>", normalised)
    normalised = _ASSET_RE.sub("", normalised)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


class TestWorkbenchMarkupBaseline(unittest.TestCase):
    def test_hosted_markup_matches_recorded_baseline(self):
        for name, workspace in (("trial", False), ("workspace", True)):
            with self.subTest(page=name):
                html = render_hosted_page(workspace=workspace, workbench_enabled=False)
                self.assertEqual(
                    markup_signature(html),
                    BASE_MARKUP_SHA256[name],
                    f"{name} markup drifted from the recorded baseline",
                )

    def test_workbench_flag_changes_no_markup(self):
        """The presentation must ride on the body class plus the asset link.

        Normalised markup (styles, scripts and the workbench asset reference
        removed) must be identical whichever way the flag is set, so the only
        thing the flag can change is *which* class the client computes.
        """
        for workspace in (False, True):
            with self.subTest(workspace=workspace):
                off = markup_signature(
                    render_hosted_page(workspace=workspace, workbench_enabled=False)
                )
                on = markup_signature(
                    render_hosted_page(workspace=workspace, workbench_enabled=True)
                )
                self.assertEqual(
                    off,
                    on,
                    "enabling the workbench changed the page markup; the "
                    "presentation must ride on the body class only",
                )

    def test_flag_boolean_and_asset_reference_are_the_only_render_differences(self):
        """The hosted trial page must not react to the flag at all, and the
        workspace page must differ only in the boolean and the asset link.

        `_render_hosted_chat_page` gates on `workspace and ...`, so the trial
        page is byte-identical whether or not the flag is set.
        """
        trial_off = render_hosted_page(workspace=False, workbench_enabled=False)
        trial_on = render_hosted_page(workspace=False, workbench_enabled=True)
        self.assertEqual(
            trial_off,
            trial_on,
            "the workbench flag leaked into the non-workspace page",
        )

        workspace_off = render_hosted_page(workspace=True, workbench_enabled=False)
        workspace_on = render_hosted_page(workspace=True, workbench_enabled=True)

        # Off: the asset is never referenced, so no workbench request is made
        # and the served bytes match the pre-workbench baseline.
        self.assertNotIn(WORKBENCH_STYLESHEET_REF, workspace_off)
        self.assertIn(WORKBENCH_STYLESHEET_REF, workspace_on)

        without_asset = workspace_on.replace(
            WORKBENCH_STYLESHEET_REF + "\n", "", 1
        )
        self.assertEqual(
            workspace_off.replace("const workbenchShellEnabled = false;", "SHELL_BOOL"),
            without_asset.replace("const workbenchShellEnabled = true;", "SHELL_BOOL"),
            "enabling the workbench on /workspace changed more than the boolean "
            "and the stylesheet reference",
        )

    def test_placeholder_never_survives_into_a_served_page(self):
        for workspace in (False, True):
            for enabled in (False, True):
                with self.subTest(workspace=workspace, enabled=enabled):
                    html = render_hosted_page(
                        workspace=workspace, workbench_enabled=enabled
                    )
                    self.assertNotIn("__WORKBENCH_SHELL_ENABLED__", html)


class TestWorkbenchRouteContract(unittest.TestCase):
    def test_workspace_page_still_serves_the_hosted_template(self):
        self.assertIn("__HOSTED_WORKSPACE_MODE__", web.TRIAL_CHAT_HTML)
        self.assertIn(
            "const workbenchShellEnabled = __WORKBENCH_SHELL_ENABLED__;",
            web.TRIAL_CHAT_HTML,
        )


if __name__ == "__main__":
    unittest.main()
