"""Hosted-provider billing boundary and retry-safety tests."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from chat_agent_openrouter import (
    HOSTED_MAX_INPUT_CHARS,
    MAX_SESSION_MESSAGES,
    TrialAgent,
    _openrouter_user_error,
    _prepare_hosted_context,
)


class HostedBillingBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_token = os.environ.get("HOSTED_AGENT_SERVICE_TOKEN")
        os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = "hosted-worker-test-token"
        self.agent = TrialAgent(
            api_key="trial-websocket-key",
            agent_id="trial-agent",
            server="ws://web:8080",
            model="google/gemini-3.1-flash-lite",
        )
        self.agent._session_billing_runs["s-test"] = "run-test"

    def tearDown(self):
        if self._saved_token is None:
            os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        else:
            os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = self._saved_token

    async def test_reserve_submit_provider_settle_order(self):
        order: list[str] = []

        async def reserve(*_args, **_kwargs):
            order.append("reserve")
            return {"call_id": "call-test"}

        async def submitted(*_args, **_kwargs):
            order.append("submitted")
            return {"call_id": "call-test", "status": "submitted"}

        async def provider(*_args, **_kwargs):
            order.append("provider")
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001},
            }

        async def settle(*_args, **_kwargs):
            order.append("settle")
            return {"call_id": "call-test", "status": "settled"}

        credit_client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(self.agent, "_credit_reserve", side_effect=reserve),
            patch.object(self.agent, "_credit_mark_submitted", side_effect=submitted),
            patch.object(self.agent, "_credit_settle", side_effect=settle),
            patch.object(self.agent, "_credit_release", new=AsyncMock()),
            patch.object(self.agent, "_do_openrouter_call", side_effect=provider),
            patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
        ):
            result = await self.agent._call_openrouter(
                SimpleNamespace(),
                [{"role": "user", "content": "hello"}],
                session_id="s-test",
            )

        self.assertTrue(result["choices"])
        self.assertEqual(order, ["reserve", "submitted", "provider", "settle"])

    async def test_provider_failure_never_releases_submitted_call(self):
        release = AsyncMock()
        settle = AsyncMock()
        credit_client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(
                self.agent, "_credit_reserve",
                new=AsyncMock(return_value={"call_id": "call-submitted"}),
            ),
            patch.object(
                self.agent, "_credit_mark_submitted",
                new=AsyncMock(return_value={"status": "submitted"}),
            ),
            patch.object(self.agent, "_credit_release", new=release),
            patch.object(self.agent, "_credit_settle", new=settle),
            patch.object(
                self.agent, "_do_openrouter_call",
                new=AsyncMock(side_effect=httpx.ReadTimeout("provider timeout")),
            ),
        ):
            with self.assertRaises(httpx.ReadTimeout):
                await self.agent._call_openrouter(
                    SimpleNamespace(),
                    [{"role": "user", "content": "hello"}],
                    session_id="s-test",
                )

        release.assert_not_awaited()
        settle.assert_not_awaited()

    async def test_definitive_provider_rejection_zero_settles_submitted_hold(self):
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "Reasoning is mandatory for this endpoint"}},
        )
        provider_error = httpx.HTTPStatusError(
            "400",
            request=request,
            response=response,
        )
        settle = AsyncMock(return_value={"call_id": "call-rejected", "status": "settled"})
        release = AsyncMock()
        credit_client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(
                self.agent,
                "_credit_reserve",
                new=AsyncMock(return_value={"call_id": "call-rejected"}),
            ),
            patch.object(
                self.agent,
                "_credit_mark_submitted",
                new=AsyncMock(return_value={"status": "submitted"}),
            ),
            patch.object(self.agent, "_credit_settle", new=settle),
            patch.object(self.agent, "_credit_release", new=release),
            patch.object(
                self.agent,
                "_do_openrouter_call",
                new=AsyncMock(side_effect=provider_error),
            ),
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                await self.agent._call_openrouter(
                    SimpleNamespace(),
                    [{"role": "user", "content": "hello"}],
                    session_id="s-test",
                )

        settle.assert_awaited_once()
        settle_kwargs = settle.await_args.kwargs
        self.assertEqual(settle_kwargs["actual_cost_micro_usd"], 0)
        self.assertFalse(settle_kwargs["cost_absent"])
        release.assert_not_awaited()

    async def test_known_mandatory_reasoning_model_is_not_explicitly_disabled(self):
        captured_body = {}

        async def provider(_client, body, *_args, **_kwargs):
            captured_body.update(body)
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001},
            }

        with (
            patch.object(self.agent, "_do_openrouter_call", side_effect=provider),
            patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
        ):
            await self.agent._call_openrouter(
                SimpleNamespace(),
                [{"role": "user", "content": "hello"}],
                model="google/gemini-3.5-flash-lite",
                session_id="s-unbilled",
                reasoning=False,
            )

        self.assertNotIn("reasoning", captured_body)

    async def test_submission_failure_does_not_call_provider(self):
        provider = AsyncMock()
        release = AsyncMock(return_value={"status": "released"})
        credit_client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(
                self.agent, "_credit_reserve",
                new=AsyncMock(return_value={"call_id": "call-unsubmitted"}),
            ),
            patch.object(
                self.agent, "_credit_mark_submitted", new=AsyncMock(return_value=None)
            ),
            patch.object(self.agent, "_credit_release", new=release),
            patch.object(self.agent, "_do_openrouter_call", new=provider),
            patch("chat_agent_openrouter.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "submission authorization"):
                await self.agent._call_openrouter(
                    SimpleNamespace(),
                    [{"role": "user", "content": "hello"}],
                    session_id="s-test",
                )

        provider.assert_not_awaited()
        release.assert_awaited_once()

    async def test_only_first_navigate_in_hosted_task_focuses_client_browser(self):
        self.agent.sessions["s-focus"] = []
        tool_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "nav-1",
                            "function": {
                                "name": "navigate",
                                "arguments": '{"url":"https://example.com/one"}',
                            },
                        },
                        {
                            "id": "nav-2",
                            "function": {
                                "name": "navigate",
                                "arguments": '{"url":"https://example.com/two"}',
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
        }
        final_response = {
            "choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }],
        }
        execute_tool = AsyncMock(side_effect=["Navigated one", "Navigated two"])

        with (
            patch.object(
                self.agent,
                "_call_openrouter",
                new=AsyncMock(side_effect=[tool_response, final_response]),
            ),
            patch.object(self.agent, "_execute_tool", new=execute_tool),
            patch.object(self.agent, "_emit_live_preview", new=AsyncMock()),
            patch.object(
                self.agent,
                "_sanitize_user_output",
                new=AsyncMock(return_value="done"),
            ),
            patch.object(self.agent, "_send", new=AsyncMock()),
            patch.object(self.agent, "_save_session"),
        ):
            await self.agent._handle_message({
                "session_id": "s-focus",
                "agent_id": "client-browser",
                "tab_id": "tab-1",
                "user_id": "u-focus",
                "message": "visit both pages",
            })

        self.assertEqual(execute_tool.await_count, 2)
        first, second = execute_tool.await_args_list
        self.assertTrue(first.kwargs["bring_to_front"])
        self.assertFalse(second.kwargs["bring_to_front"])

    def test_prepare_hosted_context_bounds_messages_and_chars_in_place(self):
        messages = [{"role": "system", "content": "system" * 100}]
        for index in range(20):
            messages.extend(
                [
                    {"role": "user", "content": f"request {index}"},
                    {"role": "assistant", "content": "click" * 5000},
                ]
            )
        messages.append({"role": "user", "content": "current request"})

        stats = _prepare_hosted_context(
            messages,
            max_messages=30,
            max_chars=25_000,
            emergency_keep=10,
        )

        self.assertLessEqual(
            len(json.dumps(messages, ensure_ascii=False, default=str)),
            25_000,
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "current request"})
        self.assertGreater(stats["message_trimmed"], 0)
        self.assertTrue(stats["emergency_trimmed"])

    async def test_handle_message_bounds_oversized_live_session_before_provider(self):
        sid = "s-context-bound"
        cached = [{"role": "system", "content": "old system"}]
        for index in range(22):
            cached.extend(
                [
                    {"role": "user", "content": f"old request {index}"},
                    {"role": "assistant", "content": "click" * 5000},
                ]
            )
        self.agent.sessions[sid] = cached
        captured = []

        async def provider(_client, messages, *_args, **_kwargs):
            captured.append(json.loads(json.dumps(messages)))
            return {
                "choices": [{
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }],
            }

        with (
            patch.object(self.agent, "_call_openrouter", side_effect=provider),
            patch.object(self.agent, "_send", new=AsyncMock()),
            patch.object(self.agent, "_save_session"),
            patch.object(
                self.agent,
                "_sanitize_user_output",
                new=AsyncMock(return_value="done"),
            ),
        ):
            await self.agent._handle_message(
                {
                    "session_id": sid,
                    "agent_id": "client-browser",
                    "user_id": "u-context",
                    "message": "current request",
                }
            )

        self.assertEqual(len(captured), 1)
        provider_messages = captured[0]
        self.assertLessEqual(
            len(json.dumps(provider_messages, ensure_ascii=False, default=str)),
            HOSTED_MAX_INPUT_CHARS,
        )
        self.assertEqual(
            provider_messages[-1],
            {"role": "user", "content": "current request"},
        )
        self.assertLessEqual(
            sum(1 for message in provider_messages if message.get("role") != "system"),
            MAX_SESSION_MESSAGES,
        )

    async def test_background_navigation_policy_reaches_cloud_tools(self):
        with patch(
            "chat_agent_openrouter.cloud_tools.navigate",
            new=AsyncMock(return_value="Navigated"),
        ) as navigate:
            result = await self.agent._dispatch_tool(
                "client-browser",
                "tab-1",
                "navigate",
                {"url": "https://example.com"},
                bring_to_front=False,
            )

        self.assertEqual(result, "Navigated")
        self.assertFalse(navigate.await_args.kwargs["bring_to_front"])

    async def test_hosted_provider_error_is_one_attempt(self):
        class ErrorResponse:
            status_code = 503
            text = "temporary"
            is_success = False

            def raise_for_status(self):
                request = httpx.Request("POST", "https://openrouter.ai")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError("503", request=request, response=response)

        client = SimpleNamespace(post=AsyncMock(return_value=ErrorResponse()))
        with self.assertRaises(httpx.HTTPStatusError):
            await self.agent._do_openrouter_call(
                client,
                {"model": self.agent.model, "messages": []},
                self.agent.model,
                "s-test",
                allow_unmetered_retries=False,
            )
        self.assertEqual(client.post.await_count, 1)

    def test_provider_error_copy_omits_httpx_url_boilerplate(self):
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(
            404,
            request=request,
            json={"error": {"message": "No endpoints found for vendor/model."}},
        )
        exc = httpx.HTTPStatusError("404", request=request, response=response)
        message = _openrouter_user_error(exc, "vendor/model")

        self.assertEqual(
            message,
            "OpenRouter model vendor/model is currently unavailable: "
            "No endpoints found for vendor/model.",
        )
        self.assertNotIn("chat/completions", message)

    def test_websocket_key_is_not_callback_token_fallback(self):
        os.environ.pop("HOSTED_AGENT_SERVICE_TOKEN", None)
        self.assertEqual(self.agent._hosted_service_token(), "")


if __name__ == "__main__":
    unittest.main()
