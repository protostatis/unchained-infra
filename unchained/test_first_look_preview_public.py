from __future__ import annotations

import unittest

from aiohttp.test_utils import AioHTTPTestCase
from aiohttp import web

from rate_limit import SlidingWindowRateLimiter
from web_app.handlers import chat_flow


class TestFirstLookPreviewPublicEndpoints(AioHTTPTestCase):

    async def get_application(self):
        self._original_limiter = chat_flow._FIRST_LOOK_PUBLIC_RATE_LIMITER
        self._original_preflight_limit = chat_flow._FIRST_LOOK_PREFLIGHT_LIMIT
        self._original_signal_limit = chat_flow._FIRST_LOOK_SIGNAL_LIMIT
        chat_flow._FIRST_LOOK_PUBLIC_RATE_LIMITER = SlidingWindowRateLimiter()
        chat_flow._FIRST_LOOK_PREFLIGHT_LIMIT = 2
        chat_flow._FIRST_LOOK_SIGNAL_LIMIT = 2
        app = web.Application()
        app.router.add_get("/preflight", chat_flow.handle_first_look_preflight)
        app.router.add_post("/signal", chat_flow.handle_first_look_signal)
        return app

    async def asyncTearDown(self):
        chat_flow._FIRST_LOOK_PUBLIC_RATE_LIMITER = self._original_limiter
        chat_flow._FIRST_LOOK_PREFLIGHT_LIMIT = self._original_preflight_limit
        chat_flow._FIRST_LOOK_SIGNAL_LIMIT = self._original_signal_limit
        await super().asyncTearDown()

    async def test_preflight_is_rate_limited_per_source_ip(self):
        headers = {"X-Forwarded-For": "203.0.113.10"}
        resp = await self.client.request("GET", "/preflight?url=https://www.zillow.com/", headers=headers)
        self.assertEqual(resp.status, 200)
        resp = await self.client.request("GET", "/preflight?url=https://www.zillow.com/", headers=headers)
        self.assertEqual(resp.status, 200)

        limited = await self.client.request("GET", "/preflight?url=https://www.zillow.com/", headers=headers)
        self.assertEqual(limited.status, 429)
        self.assertIn("Retry-After", limited.headers)
        payload = await limited.json()
        self.assertEqual(payload["error"], "Rate limit exceeded")
        self.assertGreaterEqual(payload["retry_after"], 1)

    async def test_signal_is_rate_limited_per_source_ip(self):
        headers = {"X-Forwarded-For": "203.0.113.11"}
        body = {"url": "https://www.zillow.com/", "text": "verify you are human"}
        resp = await self.client.post("/signal", json=body, headers=headers)
        self.assertEqual(resp.status, 200)
        resp = await self.client.post("/signal", json=body, headers=headers)
        self.assertEqual(resp.status, 200)

        limited = await self.client.post("/signal", json=body, headers=headers)
        self.assertEqual(limited.status, 429)
        self.assertIn("Retry-After", limited.headers)
        payload = await limited.json()
        self.assertEqual(payload["error"], "Rate limit exceeded")
        self.assertGreaterEqual(payload["retry_after"], 1)


if __name__ == "__main__":
    unittest.main()
