"""Hosted turn admission and absolute-deadline tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from web_app.handlers import chat_stream
from web_state import ChatTurnRegistry, ChatTurnState


def _turn(session_id: str, req_id: str, user_id: str) -> ChatTurnState:
    return ChatTurnState(
        owner_user_id=user_id,
        owner_key_hash=f"key-{user_id}",
        session_id=session_id,
        req_id=req_id,
        chat_agent_id="trial-agent",
        routing_agent_id="trial-agent",
    )


class HostedAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_user_and_global_limits_are_race_safe(self):
        registry = ChatTurnRegistry()
        core = SimpleNamespace(TRIAL_AGENT_ID="trial-agent")
        auth_a = {"user_id": "user-a", "key_hash": "key-user-a"}
        auth_b = {"user_id": "user-b", "key_hash": "key-user-b"}
        auth_c = {"user_id": "user-c", "key_hash": "key-user-c"}

        with (
            patch.object(chat_stream, "_HOSTED_MAX_ACTIVE_TURNS", 2),
            patch.object(chat_stream, "_HOSTED_MAX_ACTIVE_TURNS_PER_USER", 1),
        ):
            first, created, conflict, limited = await chat_stream._start_registered_turn(
                core, registry, _turn("s-a-1", "r-a-1", "user-a"), auth_a,
                hosted=True,
            )
            self.assertTrue(created)
            self.assertFalse(conflict)
            self.assertEqual(limited, "")

            _, created, _, limited = await chat_stream._start_registered_turn(
                core, registry, _turn("s-a-2", "r-a-2", "user-a"), auth_a,
                hosted=True,
            )
            self.assertFalse(created)
            self.assertEqual(limited, "user")

            second, created, _, limited = await chat_stream._start_registered_turn(
                core, registry, _turn("s-b-1", "r-b-1", "user-b"), auth_b,
                hosted=True,
            )
            self.assertTrue(created)
            self.assertEqual(limited, "")

            _, created, _, limited = await chat_stream._start_registered_turn(
                core, registry, _turn("s-c-1", "r-c-1", "user-c"), auth_c,
                hosted=True,
            )
            self.assertFalse(created)
            self.assertEqual(limited, "global")

            first.publish({"type": "done"})
            replacement, created, _, limited = await chat_stream._start_registered_turn(
                core, registry, _turn("s-c-1", "r-c-1", "user-c"), auth_c,
                hosted=True,
            )
            self.assertTrue(created)
            self.assertEqual(limited, "")
            self.assertIsNot(replacement, second)

    async def test_simultaneous_starts_cannot_oversubscribe(self):
        registry = ChatTurnRegistry()
        core = SimpleNamespace(TRIAL_AGENT_ID="trial-agent")

        async def start(index: int):
            user_id = f"user-{index}"
            return await chat_stream._start_registered_turn(
                core,
                registry,
                _turn(f"s-{index}", f"r-{index}", user_id),
                {"user_id": user_id, "key_hash": f"key-{user_id}"},
                hosted=True,
            )

        with (
            patch.object(chat_stream, "_HOSTED_MAX_ACTIVE_TURNS", 3),
            patch.object(chat_stream, "_HOSTED_MAX_ACTIVE_TURNS_PER_USER", 2),
        ):
            results = await asyncio.gather(*(start(i) for i in range(10)))

        accepted = [result for result in results if result[1]]
        limited = [result for result in results if result[3] == "global"]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(limited), 7)
        self.assertEqual(len(registry.active_for_agent("trial-agent")), 3)

    async def test_absolute_deadline_cancels_agent_and_finishes_turn(self):
        turn = _turn("s-timeout", "r-timeout", "user-timeout")
        websocket = SimpleNamespace(closed=False, send_json=AsyncMock())
        turn.dispatch_ws = websocket
        core = SimpleNamespace()
        real_sleep = asyncio.sleep

        async def immediate_sleep(_seconds):
            return None

        with (
            patch.object(chat_stream.asyncio, "sleep", side_effect=immediate_sleep),
            patch.object(chat_stream, "_publish_turn_failure") as failure,
        ):
            chat_stream._hosted_turn_deadline_task(core, turn, 30)
            await real_sleep(0)
            await real_sleep(0)

        websocket.send_json.assert_awaited_once_with({
            "type": "cancel",
            "session_id": "s-timeout",
            "req_id": "r-timeout",
        })
        failure.assert_called_once()
        self.assertIn("time limit", failure.call_args.args[2])

    def test_error_outcome_finishes_billing_as_cancelled_before_done_marker(self):
        turn = _turn("s-error", "r-error", "user-error")
        core = SimpleNamespace()
        with (
            patch.object(chat_stream, "_broadcast_overlay"),
            patch.object(chat_stream, "_finish_credit_run") as finish,
        ):
            chat_stream._publish_turn_event(
                core, turn, {"type": "error", "data": "provider failed"}
            )

        self.assertGreaterEqual(finish.call_count, 1)
        self.assertEqual(finish.call_args_list[0].kwargs["status"], "cancelled")
        self.assertTrue(turn.stream_finished)


if __name__ == "__main__":
    unittest.main()
