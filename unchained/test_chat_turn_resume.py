"""Public contracts for reconnecting to an in-memory chat turn."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web as aiohttp_web
from aiohttp.test_utils import TestClient, TestServer

from chat_event_transport import MALFORMED_TEXT_EVENT_MESSAGE

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from web_app.handlers import chat_stream
from web_app.routes import ROUTE_SPECS
from web_state import ChatTurnRegistry, ChatTurnState


class TestChatTurnResumeContracts(unittest.IsolatedAsyncioTestCase):
    """Keep resumption behavior tied to routes and handlers, not state names."""

    _RESUME_ROUTES = ("/web/chat/active", "/web/chat/events")
    _OWNER = {"user_id": "user-1", "key_hash": "key-1"}
    _FOREIGN_OWNER = {"user_id": "user-2", "key_hash": "key-2"}

    async def asyncSetUp(self):
        self.registry = ChatTurnRegistry()

        def authenticate(request):
            identity = request.headers.get("X-Test-Identity", "")
            if identity == "owner":
                return self._OWNER
            if identity == "foreign":
                return self._FOREIGN_OWNER
            return None

        self.core = SimpleNamespace(
            _authenticate=authenticate,
            _chat_turns=self.registry,
        )
        self.core_patch = patch.object(chat_stream, "_core", return_value=self.core)
        self.core_patch.start()
        app = aiohttp_web.Application()
        app.router.add_get("/web/chat/active", chat_stream.handle_chat_active)
        app.router.add_get("/web/chat/events", chat_stream.handle_chat_events)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.core_patch.stop()

    async def _start_turn(self, *, req_id: str = "r-1") -> ChatTurnState:
        turn = ChatTurnState(
            owner_user_id=self._OWNER["user_id"],
            owner_key_hash=self._OWNER["key_hash"],
            session_id="s-agent-key-1",
            req_id=req_id,
            chat_agent_id="agent-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            tab_id="tab-1",
        )
        started, created, conflict = await self.registry.start(turn)
        self.assertIs(started, turn)
        self.assertTrue(created)
        self.assertFalse(conflict)
        return turn

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
        for path in self._RESUME_ROUTES:
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 401)
                self.assertEqual(await response.json(), {"error": "Not authenticated"})

    async def test_active_endpoint_returns_owned_active_turn_shape(self):
        turn = await self._start_turn()
        turn.publish({"type": "tool_start", "name": "click", "input": {"selector": "#go"}})

        response = await self.client.get(
            "/web/chat/active?session_id=s-agent-key-1",
            headers={"X-Test-Identity": "owner"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = await response.json()
        self.assertEqual(
            set(payload),
            {
                "active",
                "session_id",
                "req_id",
                "status",
                "phase",
                "current_action",
                "first_seq",
                "last_seq",
                "created_at",
                "updated_at",
                "routing",
                "scheduler_grant_id",
                "events",
            },
        )
        self.assertTrue(payload["active"])
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["session_id"], turn.session_id)
        self.assertEqual(payload["req_id"], turn.req_id)
        self.assertEqual(payload["current_action"]["name"], "click")
        self.assertEqual(payload["events"][0]["seq"], 1)

    async def test_active_endpoint_denies_foreign_owner(self):
        await self._start_turn()

        response = await self.client.get(
            "/web/chat/active?session_id=s-agent-key-1",
            headers={"X-Test-Identity": "foreign"},
        )

        self.assertEqual(response.status, 404)
        self.assertEqual(await response.json(), {"active": False})

    async def test_active_endpoint_does_not_return_terminal_turn(self):
        turn = await self._start_turn()
        turn.publish({"type": "done"})

        response = await self.client.get(
            "/web/chat/active?session_id=s-agent-key-1",
            headers={"X-Test-Identity": "owner"},
        )

        self.assertEqual(response.status, 404)
        self.assertEqual(await response.json(), {"active": False})

    async def test_events_endpoint_replays_ordered_events_after_cursor(self):
        turn = await self._start_turn()
        turn.publish({"type": "tool_start", "name": "click"})
        turn.publish({"type": "text", "data": "Opened it."})
        turn.publish({"type": "done"})

        response = await self.client.get(
            "/web/chat/events?session_id=s-agent-key-1&req_id=r-1&after=1",
            headers={"X-Test-Identity": "owner"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/event-stream")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        records = [record for record in (await response.text()).split("\n\n") if record]
        ids = []
        events = []
        for record in records:
            lines = record.splitlines()
            ids.append(int(next(line[4:] for line in lines if line.startswith("id: "))))
            events.append(json.loads(next(line[6:] for line in lines if line.startswith("data: "))))
        self.assertEqual(ids, [2, 3])
        self.assertEqual([event["seq"] for event in events], [2, 3])
        self.assertEqual([event["type"] for event in events], ["text", "done"])

    def test_agent_event_requires_exact_turn_agent_and_dispatch_socket(self):
        dispatch_ws = object()
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            routing_agent_id="agent-1",
            dispatch_ws=dispatch_ws,
        )
        replacement_ws = object()
        wrong_agent_ws = object()
        core = SimpleNamespace(
            _chat_agents={"agent-1": replacement_ws, "agent-2": wrong_agent_ws}
        )

        self.assertTrue(
            chat_stream._agent_event_matches_turn(core, turn, "agent-1", dispatch_ws, "r-1")
        )
        self.assertFalse(
            chat_stream._agent_event_matches_turn(core, turn, "agent-1", dispatch_ws, "r-old")
        )
        self.assertFalse(
            chat_stream._agent_event_matches_turn(core, turn, "agent-2", wrong_agent_ws, "r-1")
        )
        self.assertFalse(
            chat_stream._agent_event_matches_turn(core, turn, "agent-1", replacement_ws, "r-1")
        )

    def test_hosted_new_tab_does_not_replace_a_newer_session_target(self):
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            tab_id="prov-slot-old-tab",
        )
        newer_tab = "prov-slot-user-selected-tab"
        core = SimpleNamespace(
            TRIAL_AGENT_ID="agent-1",
            _session_tabs={turn.session_id: newer_tab},
            _session_allowed_tabs={turn.session_id: {newer_tab}},
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_last_active={},
        )

        updated = chat_stream._sync_hosted_agent_new_tab(
            core,
            turn,
            {
                "type": "tool_result",
                "name": "ddm",
                "new_tab_id": "A" * 32,
            },
        )

        self.assertEqual(updated, "")
        self.assertEqual(core._session_tabs[turn.session_id], newer_tab)
        self.assertEqual(core._session_allowed_tabs[turn.session_id], {newer_tab})
        self.assertEqual(turn.tab_id, "prov-slot-old-tab")

    def test_hosted_new_tab_binds_an_unbound_turn_to_its_raw_target(self):
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
        )
        raw_tab_id = "A" * 32
        core = SimpleNamespace(
            TRIAL_AGENT_ID="agent-1",
            _session_tabs={turn.session_id: ""},
            _session_allowed_tabs={},
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_last_active={},
        )

        updated = chat_stream._sync_hosted_agent_new_tab(
            core,
            turn,
            {
                "type": "tool_result",
                "name": "ddm",
                "new_tab_id": raw_tab_id,
            },
        )

        self.assertEqual(updated, raw_tab_id)
        self.assertEqual(core._session_tabs[turn.session_id], raw_tab_id)
        self.assertEqual(core._session_allowed_tabs[turn.session_id], {raw_tab_id})
        self.assertEqual(turn.tab_id, raw_tab_id)
        self.assertIn(turn.session_id, core._session_last_active)

    def test_hosted_default_profile_keeps_new_tab_compare_and_swap_baseline(self):
        previous_tab_id = "C" * 32
        self.assertEqual(
            chat_stream._resolve_profile_intent(
                {"profile_path": ""}, previous_tab_id, ""
            ),
            ("unchanged", ""),
        )
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            # handle_chat_msg retains this value for an unchanged profile.
            tab_id=previous_tab_id,
        )
        raw_tab_id = "A" * 32
        core = SimpleNamespace(
            TRIAL_AGENT_ID="agent-1",
            _session_tabs={turn.session_id: previous_tab_id},
            _session_allowed_tabs={turn.session_id: {previous_tab_id}},
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_last_active={},
        )

        updated = chat_stream._sync_hosted_agent_new_tab(
            core,
            turn,
            {
                "type": "tool_result",
                "name": "ddm",
                "new_tab_id": raw_tab_id,
            },
        )

        self.assertEqual(updated, raw_tab_id)
        self.assertEqual(core._session_tabs[turn.session_id], raw_tab_id)
        self.assertEqual(turn.tab_id, raw_tab_id)

    async def test_dispatch_socket_events_survive_agent_supersession(self):
        class ControlledWebSocket:
            _STOP = object()

            def __init__(self):
                self.closed = False
                self.inbound = asyncio.Queue()
                self.processed = None

            async def prepare(self, request):
                return None

            async def receive_json(self):
                return {"key": "trial-key"}

            async def send_json(self, data):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.processed is not None:
                    self.processed.set()
                    self.processed = None
                item = await self.inbound.get()
                if item is self._STOP:
                    raise StopAsyncIteration
                message, self.processed = item
                return message

            async def push_json(self, payload):
                processed = asyncio.Event()
                await self.inbound.put(
                    (
                        SimpleNamespace(
                            type=chat_stream.web.WSMsgType.TEXT,
                            data=json.dumps(payload),
                        ),
                        processed,
                    )
                )
                await asyncio.wait_for(processed.wait(), timeout=1)

            async def finish(self):
                await self.inbound.put(self._STOP)

            async def close(self, **kwargs):
                self.closed = True

        registry = ChatTurnRegistry()
        original_ws = ControlledWebSocket()
        replacement_ws = ControlledWebSocket()
        close_calls = []

        async def close_session_tab(session_id, **kwargs):
            close_calls.append((session_id, kwargs))

        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="agent-1",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={},
            _response_req_ids={},
            _chat_turns=registry,
            _session_agent_map={},
            _session_tabs={},
            _session_allowed_tabs={},
            _session_last_active={},
            _session_profile_paths={},
            _scheduler_turn_grants={},
            _overlay_sessions={},
            _close_session_tab=close_session_tab,
        )

        async def wait_until_current(expected):
            for _ in range(100):
                if core._chat_agents.get("agent-1") is expected:
                    return
                await asyncio.sleep(0)
            self.fail("chat WebSocket was not registered")

        with patch.object(chat_stream, "_core", return_value=core), patch.object(
            chat_stream.web,
            "WebSocketResponse",
            side_effect=[original_ws, replacement_ws],
        ):
            original_task = asyncio.create_task(chat_stream.handle_chat_ws(SimpleNamespace()))
            await wait_until_current(original_ws)
            turn = ChatTurnState(
                owner_user_id="user-1",
                owner_key_hash="key-1",
                session_id="s-agent-key-1",
                req_id="r-1",
                chat_agent_id="agent-1",
                routing_agent_id="agent-1",
                cdp_agent_id="bridge-1",
                tab_id="prov-slot-original-tab",
                dispatch_ws=original_ws,
            )
            await registry.start(turn)
            core._session_agent_map[turn.session_id] = turn.cdp_agent_id
            core._session_tabs[turn.session_id] = turn.tab_id
            core._session_allowed_tabs[turn.session_id] = {turn.tab_id}

            replacement_task = asyncio.create_task(chat_stream.handle_chat_ws(SimpleNamespace()))
            await wait_until_current(replacement_ws)
            try:
                # Events are now dispatched through the replacement WS because
                # dispatch_ws was migrated on reconnect.  The original transport
                # is superseded and its events would be dropped — this is the
                # intended fix for reconnect response loss.
                new_tab_id = "A" * 32
                canonical_new_tab = f"prov-slot-{new_tab_id}"
                for event in (
                    {"type": "tool_start", "name": "ddm"},
                    {
                        "type": "tool_result",
                        "name": "ddm",
                        "data": "layout",
                        "new_tab_id": new_tab_id,
                    },
                    {"type": "text", "data": "Final answer"},
                    {"type": "done"},
                ):
                    await replacement_ws.push_json(
                        {**event, "session_id": turn.session_id, "req_id": turn.req_id}
                    )

                self.assertEqual(
                    [event["type"] for event in turn.journal],
                    ["tool_start", "tool_result", "text", "done"],
                )
                self.assertEqual([event["seq"] for event in turn.journal], [1, 2, 3, 4])
                self.assertTrue(turn.stream_finished)
                self.assertIs(core._chat_agents["agent-1"], replacement_ws)
                self.assertEqual(core._session_tabs[turn.session_id], canonical_new_tab)
                self.assertEqual(
                    core._session_allowed_tabs[turn.session_id],
                    {"prov-slot-original-tab", canonical_new_tab},
                )
                self.assertEqual(turn.tab_id, canonical_new_tab)
                self.assertIn(turn.session_id, core._session_last_active)

                # Events from the original (stale) transport are rejected after
                # dispatch_ws migration.  This prevents old-socket events from
                # being silently dropped and never re-sent.
                for event in (
                    {
                        "type": "tool_result",
                        "name": "ddm",
                        "data": "stale event",
                        "new_tab_id": "B" * 32,
                    },
                ):
                    await original_ws.push_json(
                        {**event, "session_id": turn.session_id, "req_id": turn.req_id}
                    )
                # stale event should NOT appear in journal
                self.assertEqual(
                    [event["type"] for event in turn.journal],
                    ["tool_start", "tool_result", "text", "done"],
                )

                interrupted_turn = ChatTurnState(
                    owner_user_id="user-1",
                    owner_key_hash="key-1",
                    session_id="s-agent-key-2",
                    req_id="r-2",
                    chat_agent_id="agent-1",
                    routing_agent_id="agent-1",
                    dispatch_ws=original_ws,
                )
                await registry.start(interrupted_turn)
                await original_ws.finish()
                await original_task

                self.assertEqual(interrupted_turn.status, "error")
                self.assertEqual(
                    [event["type"] for event in interrupted_turn.journal],
                    ["error", "done"],
                )
                self.assertIs(core._chat_agents["agent-1"], replacement_ws)
            finally:
                if not original_task.done():
                    await original_ws.finish()
                await replacement_ws.finish()
                await asyncio.gather(original_task, replacement_task)

        self.assertEqual([call[0] for call in close_calls], ["s-agent-key-2"])

    async def test_current_agent_disconnect_immediately_fails_turn_and_closes_target(self):
        registry = ChatTurnRegistry()
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            chat_agent_id="agent-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            tab_id="prov-aa-tab-1",
        )
        await registry.start(turn)
        close_calls = []

        async def close_session_tab(session_id, **kwargs):
            close_calls.append((session_id, kwargs))

        class DisconnectingWebSocket:
            closed = False

            async def prepare(self, request):
                return None

            async def receive_json(self):
                return {"key": "trial-key"}

            async def send_json(self, data):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def close(self, **kwargs):
                self.closed = True

        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="agent-1",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={},
            _response_req_ids={},
            _chat_turns=registry,
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_tabs={turn.session_id: turn.tab_id},
            _session_profile_paths={turn.session_id: "/chrome/Profile 1"},
            _scheduler_turn_grants={},
            _overlay_sessions={},
            _close_session_tab=close_session_tab,
        )
        ws = DisconnectingWebSocket()
        turn.dispatch_ws = ws

        # Set grace period to 0 so deferred cleanup runs after a single
        # event-loop yield instead of waiting 30s.
        with patch.object(chat_stream, "_core", return_value=core), \
             patch.object(chat_stream.web, "WebSocketResponse", return_value=ws), \
             patch.object(chat_stream, "_DISCONNECT_GRACE_SECONDS", 0):
            response = await chat_stream.handle_chat_ws(SimpleNamespace())
            # The finally block spawned a deferred cleanup task.  Await it
            # directly while the grace-period patch is still active.
            deferred = getattr(core, "_agent_deferred_cleanup", None)
            task = deferred.get("agent-1") if isinstance(deferred, dict) else None
            if task:
                await task

        self.assertIs(response, ws)
        # Turn is failed immediately (error published to journal).
        self.assertEqual(turn.status, "error")
        self.assertTrue(turn.stream_finished)
        self.assertEqual([event["type"] for event in turn.journal], ["error", "done"])
        # Tab close is deferred.  With grace=0 the deferred cleanup runs
        # after a single event-loop yield.
        self.assertEqual(
            close_calls,
            [
                (
                    turn.session_id,
                    {
                        "expected_tab_id": turn.tab_id,
                        "preserve_profile_path": "/chrome/Profile 1",
                        "preserve_agent_id": "bridge-1",
                    },
                )
            ],
        )

    async def test_deferred_tab_close_cancelled_on_reconnect(self):
        """Tabs are not closed when agent reconnects during grace period."""
        registry = ChatTurnRegistry()
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            chat_agent_id="agent-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            tab_id="tab-1",
        )
        await registry.start(turn)
        close_calls = []

        async def close_session_tab(session_id, **kwargs):
            close_calls.append((session_id, kwargs))

        class ControllableWebSocket:
            _STOP = object()
            closed = False

            def __init__(self):
                self.inbound = asyncio.Queue()

            async def prepare(self, request):
                return None

            async def receive_json(self):
                return {"key": "trial-key"}

            async def send_json(self, data):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.inbound.get()
                if item is self._STOP:
                    raise StopAsyncIteration
                self._msg_processed = True
                return item

            async def finish(self):
                await self.inbound.put(self._STOP)

            async def close(self, **kwargs):
                self.closed = True

        original_ws = ControllableWebSocket()
        replacement_ws = ControllableWebSocket()
        turn.dispatch_ws = original_ws

        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="agent-1",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={},
            _response_req_ids={},
            _chat_turns=registry,
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_tabs={turn.session_id: turn.tab_id},
            _session_profile_paths={turn.session_id: "/chrome/Profile 1"},
            _scheduler_turn_grants={},
            _overlay_sessions={},
            _close_session_tab=close_session_tab,
        )

        async def wait_until_current(expected):
            for _ in range(100):
                if core._chat_agents.get("agent-1") is expected:
                    return
                await asyncio.sleep(0)
            self.fail("chat WebSocket was not registered")

        with patch.object(chat_stream, "_core", return_value=core), \
             patch.object(chat_stream, "_DISCONNECT_GRACE_SECONDS", 300.0), \
             patch.object(chat_stream.web,
                          "WebSocketResponse",
                          side_effect=[original_ws, replacement_ws]):
            # First WS connects and stays alive (blocked on inbound queue).
            original_task = asyncio.create_task(
                chat_stream.handle_chat_ws(SimpleNamespace())
            )
            await wait_until_current(original_ws)

            # Now disconnect the first WS — this triggers deferred cleanup.
            await original_ws.finish()
            await original_task

            # Verify deferred cleanup was spawned.
            deferred = getattr(core, "_agent_deferred_cleanup", None)
            self.assertIsInstance(deferred, dict)
            self.assertIn("agent-1", deferred)

            # Agent reconnects before grace period — cancels deferred cleanup.
            replacement_task = asyncio.create_task(
                chat_stream.handle_chat_ws(SimpleNamespace())
            )
            await wait_until_current(replacement_ws)

            # Tabs must NOT have been closed (grace period not reached and
            # deferred cleanup detected the agent reconnected).
            self.assertEqual(close_calls, [])

            await replacement_ws.finish()
            await replacement_task

    async def test_agent_reconnect_delivers_final_answer(self):
        """Agent reconnects and can deliver the final answer through new WS."""
        registry = ChatTurnRegistry()
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            chat_agent_id="agent-1",
            routing_agent_id="agent-1",
        )
        await registry.start(turn)

        class ControlledWebSocket:
            _STOP = object()

            def __init__(self):
                self.closed = False
                self.inbound = asyncio.Queue()
                self.processed = None

            async def prepare(self, request):
                return None

            async def receive_json(self):
                return {"key": "trial-key"}

            async def send_json(self, data):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.processed is not None:
                    self.processed.set()
                    self.processed = None
                item = await self.inbound.get()
                if item is self._STOP:
                    raise StopAsyncIteration
                message, self.processed = item
                return message

            async def push_json(self, payload):
                processed = asyncio.Event()
                await self.inbound.put(
                    (
                        SimpleNamespace(
                            type=chat_stream.web.WSMsgType.TEXT,
                            data=json.dumps(payload),
                        ),
                        processed,
                    )
                )
                await asyncio.wait_for(processed.wait(), timeout=1)

            async def finish(self):
                await self.inbound.put(self._STOP)

            async def close(self, **kwargs):
                self.closed = True

        original_ws = ControlledWebSocket()
        replacement_ws = ControlledWebSocket()

        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="agent-1",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={},
            _response_req_ids={},
            _chat_turns=registry,
            _session_agent_map={},
            _session_tabs={},
            _session_profile_paths={},
            _scheduler_turn_grants={},
            _overlay_sessions={},
        )

        async def wait_until_current(expected):
            for _ in range(100):
                if core._chat_agents.get("agent-1") is expected:
                    return
                await asyncio.sleep(0)
            self.fail("chat WebSocket was not registered")

        with patch.object(chat_stream, "_core", return_value=core), \
             patch.object(chat_stream.web,
                          "WebSocketResponse",
                          side_effect=[original_ws, replacement_ws]):
            original_task = asyncio.create_task(
                chat_stream.handle_chat_ws(SimpleNamespace())
            )
            await wait_until_current(original_ws)
            turn.dispatch_ws = original_ws

            # Agent reconnects with new WS.
            replacement_task = asyncio.create_task(
                chat_stream.handle_chat_ws(SimpleNamespace())
            )
            await wait_until_current(replacement_ws)

            try:
                # dispatch_ws should have been migrated to the new WS.
                self.assertIs(turn.dispatch_ws, replacement_ws)

                # Agent delivers events through the new WS.
                await replacement_ws.push_json(
                    {"type": "text", "data": "Answer after reconnect",
                     "session_id": turn.session_id, "req_id": turn.req_id}
                )
                await replacement_ws.push_json(
                    {"type": "done",
                     "session_id": turn.session_id, "req_id": turn.req_id}
                )

                self.assertEqual(
                    [e["type"] for e in turn.journal],
                    ["text", "done"],
                )
                self.assertTrue(turn.stream_finished)

                # Stale events through the old WS are rejected.
                await original_ws.push_json(
                    {"type": "text", "data": "stale",
                     "session_id": turn.session_id, "req_id": turn.req_id}
                )
                self.assertEqual(
                    [e["type"] for e in turn.journal],
                    ["text", "done"],  # stale event not added
                )
            finally:
                await original_ws.finish()
                await replacement_ws.finish()
                await asyncio.gather(original_task, replacement_task)

    async def test_disconnect_first_then_reconnect_delivers_answer(self):
        """Turn stays active through grace when old WS disconnects before reconnect.

        This tests the realistic ordering: original WS closes → turn enters
        grace period (NOT failed) → replacement connects → dispatch_ws migrated
        → agent delivers final answer through replacement.
        """
        registry = ChatTurnRegistry()
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            chat_agent_id="agent-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            tab_id="tab-1",
        )
        await registry.start(turn)
        close_calls = []

        async def close_session_tab(session_id, **kwargs):
            close_calls.append((session_id, kwargs))

        class ControllableWebSocket:
            _STOP = object()

            def __init__(self):
                self.closed = False
                self.inbound = asyncio.Queue()
                self.processed = None

            async def prepare(self, request):
                return None

            async def receive_json(self):
                return {"key": "trial-key"}

            async def send_json(self, data):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.processed is not None:
                    self.processed.set()
                    self.processed = None
                item = await self.inbound.get()
                if item is self._STOP:
                    raise StopAsyncIteration
                message, self.processed = item
                return message

            async def push_json(self, payload):
                processed = asyncio.Event()
                await self.inbound.put(
                    (
                        SimpleNamespace(
                            type=chat_stream.web.WSMsgType.TEXT,
                            data=json.dumps(payload),
                        ),
                        processed,
                    )
                )
                await asyncio.wait_for(processed.wait(), timeout=1)

            async def finish(self):
                await self.inbound.put(self._STOP)

            async def close(self, **kwargs):
                self.closed = True

        original_ws = ControllableWebSocket()
        replacement_ws = ControllableWebSocket()

        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="agent-1",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={},
            _response_req_ids={},
            _chat_turns=registry,
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_tabs={turn.session_id: turn.tab_id},
            _session_profile_paths={},
            _scheduler_turn_grants={},
            _overlay_sessions={},
            _close_session_tab=close_session_tab,
        )

        async def wait_until_current(expected):
            for _ in range(100):
                if core._chat_agents.get("agent-1") is expected:
                    return
                await asyncio.sleep(0)
            self.fail("chat WebSocket was not registered")

        with patch.object(chat_stream, "_core", return_value=core), \
             patch.object(chat_stream.web,
                          "WebSocketResponse",
                          side_effect=[original_ws, replacement_ws]), \
             patch.object(chat_stream, "_DISCONNECT_GRACE_SECONDS", 300.0):
            # 1. Original WS connects, turn is bound to it.
            original_task = asyncio.create_task(
                chat_stream.handle_chat_ws(SimpleNamespace())
            )
            await wait_until_current(original_ws)
            turn.dispatch_ws = original_ws

            # 2. Original WS DISCONNECTS. Turn should stay active (deferred).
            await original_ws.finish()
            await original_task

            # Turn is still active — NOT failed yet.
            self.assertEqual(turn.status, "active")
            self.assertFalse(turn.stream_finished)

            # 3. Replacement WS connects within grace. Turn migrates.
            replacement_task = asyncio.create_task(
                chat_stream.handle_chat_ws(SimpleNamespace())
            )
            await wait_until_current(replacement_ws)

            try:
                # dispatch_ws should be migrated.
                self.assertIs(turn.dispatch_ws, replacement_ws)
                # Turn still active.
                self.assertEqual(turn.status, "active")

                # 4. Agent delivers final answer through replacement.
                await replacement_ws.push_json(
                    {"type": "text", "data": "Delivered after reconnect",
                     "session_id": turn.session_id, "req_id": turn.req_id}
                )
                await replacement_ws.push_json(
                    {"type": "done",
                     "session_id": turn.session_id, "req_id": turn.req_id}
                )

                self.assertEqual(
                    [e["type"] for e in turn.journal],
                    ["text", "done"],
                )
                self.assertTrue(turn.stream_finished)
                # Tabs were not closed (reconnected within grace).
                self.assertEqual(close_calls, [])
            finally:
                await replacement_ws.finish()
                await replacement_task

    async def test_grace_expiration_publishes_failure(self):
        """After grace period without reconnect, turn failure is published."""
        registry = ChatTurnRegistry()
        turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-agent-key-1",
            req_id="r-1",
            chat_agent_id="agent-1",
            routing_agent_id="agent-1",
            cdp_agent_id="bridge-1",
            tab_id="tab-1",
        )
        await registry.start(turn)
        close_calls = []

        async def close_session_tab(session_id, **kwargs):
            close_calls.append((session_id, kwargs))

        class DisconnectingWebSocket:
            closed = False

            async def prepare(self, request):
                return None

            async def receive_json(self):
                return {"key": "trial-key"}

            async def send_json(self, data):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def close(self, **kwargs):
                self.closed = True

        ws = DisconnectingWebSocket()
        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="agent-1",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={},
            _response_req_ids={},
            _chat_turns=registry,
            _session_agent_map={turn.session_id: "bridge-1"},
            _session_tabs={turn.session_id: turn.tab_id},
            _session_profile_paths={},
            _scheduler_turn_grants={},
            _overlay_sessions={},
            _close_session_tab=close_session_tab,
        )
        turn.dispatch_ws = ws

        # Grace period set to 0 so deferred cleanup fires immediately.
        with patch.object(chat_stream, "_core", return_value=core), \
             patch.object(chat_stream.web, "WebSocketResponse", return_value=ws), \
             patch.object(chat_stream, "_DISCONNECT_GRACE_SECONDS", 0):
            response = await chat_stream.handle_chat_ws(SimpleNamespace())
            # Await the deferred cleanup task.
            deferred = getattr(core, "_agent_deferred_cleanup", None)
            task = deferred.get("agent-1") if isinstance(deferred, dict) else None
            if task:
                await task

        self.assertIs(response, ws)
        # Turn should be failed after grace expiration.
        self.assertEqual(turn.status, "error")
        self.assertTrue(turn.stream_finished)
        self.assertEqual([e["type"] for e in turn.journal], ["error", "done"])
        self.assertEqual(
            close_calls,
            [("s-agent-key-1", {"expected_tab_id": "tab-1",
                                "preserve_profile_path": "",
                                "preserve_agent_id": ""})],
        )


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

    def test_publish_normalizes_malformed_text_before_replay(self):
        turn = self._turn()

        event = turn.publish({"type": "text", "data": None})

        self.assertEqual(event["data"], MALFORMED_TEXT_EVENT_MESSAGE)
        self.assertTrue(event["malformed_text_event"])
        self.assertEqual(event["malformed_text_data_type"], "NoneType")

    def test_publish_replaces_oversized_replay_text_after_body_omission(self):
        turn = self._turn()

        event = turn.publish({"type": "text", "data": "x" * (13 * 1024)})

        self.assertEqual(event["data"], MALFORMED_TEXT_EVENT_MESSAGE)
        self.assertTrue(event["malformed_text_event"])
        # The replay cap omits the original string before the second text-event
        # normalization, so this diagnostic describes that omitted value.
        self.assertEqual(event["malformed_text_data_type"], "NoneType")
        self.assertTrue(event["replay_body_omitted"])

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
