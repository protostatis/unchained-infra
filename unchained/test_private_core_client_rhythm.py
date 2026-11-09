"""Wire-format tests for rhythm_catch across the private-core boundary.

A private core deployed before the grouping feature has a run_rhythm_catch
that does not accept group_selector or limit. The server dispatches with
fn_map[op](**payload), so an unexpected keyword is a TypeError that fails the
whole call — during a staggered deploy, every rhythm_catch. These tests pin
the rule that keeps that from happening: the options only travel when a caller
actually used them.
"""

import unittest
from unittest.mock import AsyncMock, patch

from private_core_client import DEFAULT_CATCH_LIMIT, PrivateCoreClient


def _client() -> PrivateCoreClient:
    return PrivateCoreClient(base_url="http://private-core.test", mode="http")


class TestRhythmCatchWireFormat(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_omit_the_new_fields(self):
        client = _client()
        with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
            await client.run_rhythm_catch("agent", "auto", "https://e.com", "task", ["4090"])

        kwargs = execute.await_args.kwargs
        self.assertNotIn("group_selector", kwargs)
        self.assertNotIn("limit", kwargs)
        # the pre-existing fields still travel
        self.assertEqual(kwargs["catch_terms"], ["4090"])
        self.assertEqual(kwargs["click_text"], "")

    async def test_grouping_and_limit_travel_when_set(self):
        client = _client()
        with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
            await client.run_rhythm_catch(
                "agent", "auto", "https://e.com", "task", ["4090"],
                group_selector="li.s-item", limit=60,
            )

        kwargs = execute.await_args.kwargs
        self.assertEqual(kwargs["group_selector"], "li.s-item")
        self.assertEqual(kwargs["limit"], 60)

    async def test_default_limit_is_not_sent_even_when_passed_explicitly(self):
        client = _client()
        with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
            await client.run_rhythm_catch(
                "agent", "auto", "u", "t", ["x"], limit=DEFAULT_CATCH_LIMIT,
            )

        self.assertNotIn("limit", execute.await_args.kwargs)

    async def test_string_limit_is_normalized_or_dropped(self):
        client = _client()
        with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
            await client.run_rhythm_catch("agent", "auto", "u", "t", ["x"], limit="50")
        self.assertEqual(execute.await_args.kwargs["limit"], 50)

        client = _client()
        with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
            await client.run_rhythm_catch("agent", "auto", "u", "t", ["x"], limit="not-a-number")
        self.assertNotIn("limit", execute.await_args.kwargs)

    async def test_limit_is_clamped_to_the_documented_range(self):
        # The tool documents 1-200; a caller must not be able to forward 0 or
        # 500 past that contract and depend on the engine to clamp it.
        for requested, expected in ((0, 1), (-5, 1), (500, 200), (50.7, 50)):
            client = _client()
            with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
                await client.run_rhythm_catch("agent", "auto", "u", "t", ["x"], limit=requested)
            self.assertEqual(execute.await_args.kwargs["limit"], expected, requested)

    async def test_empty_group_selector_is_not_sent(self):
        client = _client()
        with patch.object(client, "execute", AsyncMock(return_value="{}")) as execute:
            await client.run_rhythm_catch("agent", "auto", "u", "t", ["x"], group_selector="")
        self.assertNotIn("group_selector", execute.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
