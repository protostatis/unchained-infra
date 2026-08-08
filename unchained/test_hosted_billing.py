"""Hosted-provider billing boundary and retry-safety tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from chat_agent_openrouter import (
    HOSTED_MAX_INTERNAL_CONTEXT_CHARS,
    MAX_SESSION_MESSAGES,
    TrialAgent,
    _load_hosted_internal_context_configuration,
    _openrouter_user_error,
    _prepare_hosted_context,
    _resolve_hosted_internal_context_chars,
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

    def _render_compose(self, overrides: dict[str, str] | None = None) -> dict:
        repo_root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        for name in (
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS",
            "HOSTED_MAX_INPUT_CHARS",
            "HOSTED_MAX_USER_PROMPT_CHARS",
        ):
            env.pop(name, None)
        env.update({
            "FIN_TERMINAL_PROXY_TOKEN": "test",
            "FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN": "test",
            "PRIVATE_CORE_TOKEN": "test",
            "TRIAL_AGENT_KEY": "test",
            "HOSTED_AGENT_SERVICE_TOKEN": "test",
            "OPENROUTER_API_KEY": "test",
        })
        env.update(overrides or {})
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_compose_and_worker_resolve_hosted_limits_without_nesting(self):
        rendered = self._render_compose()
        web_env = rendered["services"]["web"]["environment"]
        trial_env = rendered["services"]["trial-agent"]["environment"]
        self.assertEqual(web_env["HOSTED_MAX_USER_PROMPT_CHARS"], "20000")
        self.assertEqual(trial_env["HOSTED_MAX_INTERNAL_CONTEXT_CHARS"], "")
        self.assertEqual(trial_env["HOSTED_MAX_INPUT_CHARS"], "")
        self.assertEqual(_resolve_hosted_internal_context_chars({}), (400_000, "default"))

        legacy_rendered = self._render_compose({"HOSTED_MAX_INPUT_CHARS": "250000"})
        legacy_env = legacy_rendered["services"]["trial-agent"]["environment"]
        self.assertEqual(legacy_env["HOSTED_MAX_INPUT_CHARS"], "250000")
        self.assertEqual(
            _resolve_hosted_internal_context_chars(legacy_env), (250_000, "legacy")
        )

        canonical_rendered = self._render_compose({
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS": "400000",
            "HOSTED_MAX_INPUT_CHARS": "250000",
        })
        canonical_env = canonical_rendered["services"]["trial-agent"]["environment"]
        self.assertEqual(canonical_env["HOSTED_MAX_INTERNAL_CONTEXT_CHARS"], "400000")
        self.assertEqual(
            _resolve_hosted_internal_context_chars(canonical_env),
            (400_000, "canonical"),
        )

    def test_invalid_hosted_context_configuration_has_actionable_error(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "HOSTED_MAX_INTERNAL_CONTEXT_CHARS must be an integer",
        ):
            _load_hosted_internal_context_configuration(
                {"HOSTED_MAX_INTERNAL_CONTEXT_CHARS": "not-a-number"}
            )
        with self.assertRaisesRegex(
            RuntimeError,
            "exceeds the credit-hold certification",
        ):
            _load_hosted_internal_context_configuration(
                {"HOSTED_MAX_INTERNAL_CONTEXT_CHARS": "400001"}
            )

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

    async def test_expanded_internal_context_budget_reaches_provider(self):
        """The 400k working-context budget must not retain the legacy 200k cap."""
        provider = AsyncMock(return_value={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001},
        })
        credit_client = SimpleNamespace(aclose=AsyncMock())
        messages = [{"role": "user", "content": "x" * 250_000}]

        self.assertGreater(HOSTED_MAX_INTERNAL_CONTEXT_CHARS, 250_000)
        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(
                self.agent,
                "_credit_reserve",
                new=AsyncMock(return_value={"call_id": "call-expanded-context"}),
            ),
            patch.object(
                self.agent,
                "_credit_mark_submitted",
                new=AsyncMock(return_value={"status": "submitted"}),
            ),
            patch.object(
                self.agent,
                "_credit_settle",
                new=AsyncMock(return_value={"status": "settled"}),
            ),
            patch.object(self.agent, "_do_openrouter_call", new=provider),
            patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
        ):
            await self.agent._call_openrouter(
                SimpleNamespace(), messages, session_id="s-test"
            )

        provider.assert_awaited_once()

    async def test_hosted_task_navigations_stay_in_background(self):
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
        self.assertFalse(first.kwargs["bring_to_front"])
        self.assertFalse(second.kwargs["bring_to_front"])

    async def test_hosted_agent_blocks_third_broad_link_scan_on_page(self):
        sid = "s-link-scan"
        self.agent.sessions[sid] = []
        expression = (
            "Array.from(document.querySelectorAll('a'))"
            ".filter(a => a.href).map(a => a.href)"
        )
        expressions = [f"{expression}.slice(0, {limit})" for limit in (25, 50, 75)]
        tool_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"links-{index}",
                            "function": {
                                "name": "js_eval",
                                "arguments": json.dumps({"expression": expressions[index]}),
                            },
                        }
                        for index in range(3)
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
        execute_tool = AsyncMock(return_value="https://example.test/article")
        send = AsyncMock()

        with (
            patch.object(
                self.agent,
                "_call_openrouter",
                new=AsyncMock(side_effect=[tool_response, final_response]),
            ),
            patch.object(self.agent, "_execute_tool", new=execute_tool),
            patch.object(
                self.agent,
                "_sanitize_user_output",
                new=AsyncMock(return_value="done"),
            ),
            patch.object(self.agent, "_send", new=send),
            patch.object(self.agent, "_save_session"),
        ):
            await self.agent._handle_message({
                "session_id": sid,
                "agent_id": "client-browser",
                "tab_id": "tab-1",
                "user_id": "u-link-scan",
                "message": "find articles",
            })

        self.assertEqual(execute_tool.await_count, 2)
        tool_results = [
            call.args[1]
            for call in send.await_args_list
            if len(call.args) > 1 and call.args[1].get("type") == "tool_result"
        ]
        self.assertTrue(
            any(
                result["data"].startswith("LINK_SCAN_REPEAT_BLOCKED")
                for result in tool_results
            )
        )

    async def test_hosted_agent_tracks_actual_pages_after_redirects_and_clicks(self):
        sid = "s-page-tracking"
        self.agent.sessions[sid] = []
        expression = (
            "Array.from(document.querySelectorAll('a'))"
            ".filter(a => a.href).map(a => a.href)"
        )
        expressions = [f"{expression}.slice(0, {limit})" for limit in (25, 50, 75, 100)]
        redirected_url = "https://example.test/redirected"
        article_url = "https://example.test/article"

        def tool_call(call_id, name, args):
            return {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(args)},
            }

        tool_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        tool_call("nav-start", "navigate", {"url": "https://example.test/start"}),
                        tool_call("links-1", "js_eval", {"expression": expressions[0]}),
                        tool_call("nav-canonical", "navigate", {"url": redirected_url}),
                        tool_call("links-2", "js_eval", {"expression": expressions[1]}),
                        tool_call("links-3", "js_eval", {"expression": expressions[2]}),
                        tool_call("open-article", "click", {"x": 100, "y": 200}),
                        tool_call("article-links", "js_eval", {"expression": expressions[3]}),
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
        execute_tool = AsyncMock(side_effect=[
            f"Navigated to: {redirected_url}\nTitle: Redirect destination",
            "redirected links one",
            f"Navigated to: {redirected_url}\nTitle: Redirect destination",
            "redirected links two",
            f"Clicked A\n--- changed ---\nurl: {article_url}",
            "article links",
        ])
        send = AsyncMock()

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
            patch.object(self.agent, "_send", new=send),
            patch.object(self.agent, "_save_session"),
        ):
            await self.agent._handle_message({
                "session_id": sid,
                "agent_id": "client-browser",
                "tab_id": "tab-1",
                "user_id": "u-page-tracking",
                "message": "find articles",
            })

        self.assertEqual(execute_tool.await_count, 6)
        tool_results = [
            call.args[1]
            for call in send.await_args_list
            if len(call.args) > 1 and call.args[1].get("type") == "tool_result"
        ]
        blocked = [
            result for result in tool_results
            if result["data"].startswith("LINK_SCAN_REPEAT_BLOCKED")
        ]
        self.assertEqual(len(blocked), 1)
        self.assertIn("article links", [result["data"] for result in tool_results])

    async def test_hosted_agent_stops_distinct_not_found_url_guesses(self):
        sid = "s-not-found-stall"
        self.agent.sessions[sid] = []

        def navigation_response(call_id, url):
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": call_id,
                            "function": {
                                "name": "navigate",
                                "arguments": json.dumps({"url": url}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }

        final_response = {
            "choices": [{
                "message": {"role": "assistant", "content": "I could not verify an article."},
                "finish_reason": "stop",
            }],
        }
        urls = [
            "https://example.test/guessed-one",
            "https://example.test/guessed-two",
            "https://example.test/guessed-three",
        ]
        execute_tool = AsyncMock(side_effect=[
            f"Navigated to: {url}\nTitle: Page Not Found" for url in urls
        ])
        provider = AsyncMock(side_effect=[
            *(navigation_response(f"missing-{index}", url) for index, url in enumerate(urls, 1)),
            final_response,
        ])
        send = AsyncMock()

        with (
            patch.object(self.agent, "_call_openrouter", new=provider),
            patch.object(self.agent, "_execute_tool", new=execute_tool),
            patch.object(
                self.agent,
                "_sanitize_user_output",
                new=AsyncMock(return_value="I could not verify an article."),
            ),
            patch.object(self.agent, "_send", new=send),
            patch.object(self.agent, "_save_session"),
            patch("nudge.intervention_runtime_available", return_value=False),
        ):
            await self.agent._handle_message({
                "session_id": sid,
                "agent_id": "client-browser",
                "tab_id": "tab-1",
                "user_id": "u-not-found-stall",
                "message": "find the article",
            })

        self.assertEqual(execute_tool.await_count, 3)
        self.assertEqual(provider.await_count, 4)
        self.assertEqual(provider.await_args_list[-1].kwargs["tool_choice"], "none")
        tool_results = [
            call.args[1]
            for call in send.await_args_list
            if len(call.args) > 1 and call.args[1].get("type") == "tool_result"
        ]
        self.assertEqual(len(tool_results), 3)
        self.assertTrue(all("NAVIGATION_NOT_FOUND" in result["data"] for result in tool_results))

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
            HOSTED_MAX_INTERNAL_CONTEXT_CHARS,
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
