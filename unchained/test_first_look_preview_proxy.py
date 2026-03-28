from __future__ import annotations

import unittest

from aiohttp import web

from web_app.handlers import chat_flow


class _DummyCore:
    def __init__(self):
        self.HEADLESS_AGENT_ID = "headless-123"
        self._session_tabs = {
            "s-guest-aaaa1111-demo": "tab-xyz",
        }


class TestFirstLookPreviewProxyHelpers(unittest.TestCase):

    def test_normalize_preview_dimensions_bounds_values(self):
        params = {"width": "9999", "height": "40"}
        width = chat_flow._normalize_first_look_preview_dimension(params, "width", 960)
        height = chat_flow._normalize_first_look_preview_dimension(params, "height", 640, min_value=240)

        self.assertEqual(width, 1920)
        self.assertEqual(height, 240)

    def test_resolve_preview_target_accepts_guest_owned_session(self):
        core = _DummyCore()
        guest_auth = {"agent_id": "guest-aaaa1111"}

        agent_id, tab_id = chat_flow._resolve_first_look_preview_target(
            core,
            guest_auth,
            "s-guest-aaaa1111-demo",
        )

        self.assertEqual(agent_id, "headless-123")
        self.assertEqual(tab_id, "tab-xyz")

    def test_resolve_preview_target_rejects_foreign_session(self):
        core = _DummyCore()
        guest_auth = {"agent_id": "guest-aaaa1111"}

        with self.assertRaises(web.HTTPForbidden):
            chat_flow._resolve_first_look_preview_target(
                core,
                guest_auth,
                "s-guest-bbbb2222-demo",
            )

    def test_resolve_preview_target_requires_live_tab(self):
        core = _DummyCore()
        core._session_tabs = {}
        guest_auth = {"agent_id": "guest-aaaa1111"}

        with self.assertRaises(web.HTTPNotFound):
            chat_flow._resolve_first_look_preview_target(
                core,
                guest_auth,
                "s-guest-aaaa1111-demo",
            )


if __name__ == "__main__":
    unittest.main()
