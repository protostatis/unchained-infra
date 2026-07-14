"""Public contracts for reconnecting to an in-memory chat turn."""

from __future__ import annotations

import importlib
import inspect
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web as aiohttp_web
from multidict import CIMultiDict

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from web_app.routes import ROUTE_SPECS
from web_state import ChatTurnRegistry, ChatTurnState


class TestChatTurnResumeContracts(unittest.IsolatedAsyncioTestCase):
    """Keep resumption behavior tied to routes and handlers, not state names."""

    _RESUME_ROUTES = ("/web/chat/active", "/web/chat/events")

    def _handler_for(self, path: str):
        spec = next(
            (
                spec
                for spec in ROUTE_SPECS
                if spec[0] == "GET" and spec[1] == path
            ),
            None,
        )
        self.assertIsNotNone(spec, f"missing signed-in chat resumption route: {path}")
        _, _, target = spec
        self.assertIn(":", target, f"route {path} should name its public handler")
        module_name, handler_name = target.split(":", 1)
        module = importlib.import_module(module_name)
        return module, getattr(module, handler_name)

    def test_resume_routes_are_signed_in_get_endpoints(self):
        routes = {(method, path) for method, path, _ in ROUTE_SPECS}
        for path in self._RESUME_ROUTES:
            with self.subTest(path=path):
                self.assertIn(("GET", path), routes)
                self.assertNotIn(("POST", path), routes)

    async def test_resume_handlers_reject_unsigned_requests(self):
        request = SimpleNamespace(query={}, headers=CIMultiDict(), cookies={})

        for path in self._RESUME_ROUTES:
            with self.subTest(path=path):
                _, handler = self._handler_for(path)
                try:
                    response = await handler(request)
                except aiohttp_web.HTTPUnauthorized as exc:
                    response = exc
                self.assertEqual(response.status, 401)

    def test_resume_handlers_keep_ordered_owned_turn_metadata_visible(self):
        """The wire contract must expose sequence/replay and active action data.

        This deliberately examines public handler sources instead of the mutable
        in-memory state shape, which remains an implementation detail.
        """
        _, active_handler = self._handler_for("/web/chat/active")
        _, events_handler = self._handler_for("/web/chat/events")
        active_source = inspect.getsource(active_handler).lower()
        events_source = inspect.getsource(events_handler).lower()

        self.assertIn("session", active_source)
        self.assertIn("action", active_source)
        self.assertIn("seq", events_source)
        self.assertIn("replay", events_source)
        self.assertIn("session", events_source)
        self.assertIn("event-stream", events_source)


class TestChatTurnState(unittest.TestCase):
    def _turn(self, *, req_id: str = "r-1") -> ChatTurnState:
        return ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id=req_id,
        )

    def test_publish_assigns_ordered_sequences_and_replays_after_cursor(self):
        turn = self._turn()

        first = turn.publish({"type": "tool_start", "name": "click"})
        second = turn.publish({"type": "text", "data": "Opened the result."})
        third = turn.publish({"type": "done"})

        self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])
        self.assertEqual(
            [event["seq"] for event in turn.events_after(1)],
            [2, 3],
        )
        self.assertEqual(turn.first_seq, 1)
        self.assertEqual(turn.last_seq, 3)
        self.assertTrue(turn.stream_finished)
        self.assertEqual(turn.status, "done")

    def test_tool_event_updates_current_action_in_snapshot(self):
        turn = self._turn()

        turn.publish(
            {
                "type": "tool_start",
                "name": "click",
                "input": {"selector": "#submit"},
            }
        )

        snapshot = turn.snapshot()
        self.assertEqual(snapshot["phase"], "browsing")
        self.assertEqual(
            snapshot["current_action"],
            {
                "type": "tool_start",
                "step_id": "step-1",
                "name": "click",
                "input": {"selector": "#submit"},
            },
        )
        self.assertEqual(snapshot["events"][0]["step_id"], "step-1")
        self.assertEqual(snapshot["events"][0]["seq"], 1)

    def test_screenshot_replay_omits_image_bodies(self):
        turn = self._turn()
        event = {
            "type": "tool_result",
            "name": "screenshot",
            "is_screenshot": True,
            "data": "private-image-bytes",
            "screenshot_base64": "private-base64",
            "image_data": "private-image-data",
        }

        replay = turn.publish(event)

        self.assertEqual(replay["data"], "")
        self.assertTrue(replay["replay_body_omitted"])
        self.assertNotIn("is_screenshot", replay)
        self.assertNotIn("screenshot_base64", replay)
        self.assertNotIn("image_data", replay)
        self.assertEqual(event["data"], "private-image-bytes")

    def test_owner_check_requires_both_authenticated_identities(self):
        turn = self._turn()

        self.assertTrue(turn.owned_by("user-1", "key-1"))
        self.assertFalse(turn.owned_by("user-2", "key-1"))
        self.assertFalse(turn.owned_by("user-1", "key-2"))
        self.assertFalse(turn.owned_by("user-1", ""))


class TestChatTurnRegistry(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _candidate(
        *,
        req_id: str,
        owner_user_id: str = "user-1",
        owner_key_hash: str = "key-1",
    ) -> ChatTurnState:
        return ChatTurnState(
            owner_user_id=owner_user_id,
            owner_key_hash=owner_key_hash,
            session_id="s-agent-key-1",
            req_id=req_id,
        )

    async def test_start_is_idempotent_for_same_request_and_conflicts_per_session(self):
        registry = ChatTurnRegistry()
        original = self._candidate(req_id="r-1")

        started, created, conflict = await registry.start(original)
        self.assertIs(started, original)
        self.assertTrue(created)
        self.assertFalse(conflict)

        retried, created, conflict = await registry.start(self._candidate(req_id="r-1"))
        self.assertIs(retried, original)
        self.assertFalse(created)
        self.assertFalse(conflict)

        foreign, created, conflict = await registry.start(
            self._candidate(req_id="r-1", owner_user_id="user-2")
        )
        self.assertIs(foreign, original)
        self.assertFalse(created)
        self.assertTrue(conflict)

        competing, created, conflict = await registry.start(self._candidate(req_id="r-2"))
        self.assertIs(competing, original)
        self.assertFalse(created)
        self.assertTrue(conflict)

    async def test_terminal_turn_is_retained_until_retention_window_expires(self):
        with patch("web_state.time.time", return_value=100.0):
            registry = ChatTurnRegistry(retention_seconds=10.0)
            turn = self._candidate(req_id="r-1")
            await registry.start(turn)
            turn.publish({"type": "done"})

        with patch("web_state.time.time", return_value=110.0):
            self.assertIs(registry.get(turn.session_id, turn.req_id), turn)

        with patch("web_state.time.time", return_value=110.001):
            self.assertIsNone(registry.get(turn.session_id, turn.req_id))
            self.assertIsNone(registry.get(turn.session_id))


if __name__ == "__main__":
    unittest.main()
