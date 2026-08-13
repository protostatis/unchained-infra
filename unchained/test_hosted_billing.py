"""Hosted-provider billing boundary and retry-safety tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from chat_agent_openrouter import (
    HOSTED_MAX_INTERNAL_CONTEXT_CHARS,
    HOSTED_MAX_SESSION_MESSAGES,
    TrialAgent,
    _append_tool_followup_guidance,
    _hosted_user_error,
    _load_hosted_internal_context_configuration,
    _openrouter_user_error,
    _prepare_hosted_context,
    _recover_deepseek_dsml_tool_calls,
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
        with self.assertRaisesRegex(RuntimeError, "must be at least 10000"):
            _load_hosted_internal_context_configuration(
                {"HOSTED_MAX_INTERNAL_CONTEXT_CHARS": "5000"}
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

    async def test_openrouter_settle_uses_provider_cost_without_pricing(self):
        """OpenRouter settlements keep cost math and send no DeepSeek pricing."""
        settle = AsyncMock(return_value={"call_id": "call-test", "status": "settled"})
        credit_client = SimpleNamespace(aclose=AsyncMock())

        async def provider(*_args, **_kwargs):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.00123},
            }

        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(
                self.agent, "_credit_reserve",
                new=AsyncMock(return_value={"call_id": "call-test"}),
            ),
            patch.object(
                self.agent, "_credit_mark_submitted",
                new=AsyncMock(return_value={"status": "submitted"}),
            ),
            patch.object(self.agent, "_credit_settle", new=settle),
            patch.object(self.agent, "_credit_release", new=AsyncMock()),
            patch.object(self.agent, "_do_openrouter_call", side_effect=provider),
            patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
        ):
            await self.agent._call_openrouter(
                SimpleNamespace(),
                [{"role": "user", "content": "hello"}],
                session_id="s-test",
            )

        settle.assert_awaited_once()
        kwargs = settle.await_args.kwargs
        self.assertEqual(kwargs["actual_cost_micro_usd"], 1_230)
        self.assertFalse(kwargs["cost_absent"])
        self.assertEqual(kwargs["pricing_tier"], "")
        self.assertEqual(kwargs["pricing_schedule_version"], "")

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

    async def test_hosted_agent_keeps_followup_tools_on_new_provisioned_tab(self):
        sid = "s-new-tab"
        raw_new_tab_id = "A" * 32
        initial_tab_id = "prov-slot-original-tab"
        self.agent.sessions[sid] = []
        tool_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "new-tab",
                            "function": {
                                "name": "ddm",
                                "arguments": json.dumps(
                                    {"flags": "--new https://example.test"}
                                ),
                            },
                        },
                        {
                            "id": "inspect-new-tab",
                            "function": {
                                "name": "js_eval",
                                "arguments": json.dumps(
                                    {"expression": "document.title"}
                                ),
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
        execute_tool = AsyncMock(side_effect=[
            f"Tab: {raw_new_tab_id}\nCreated tab",
            "new tab title",
        ])
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
                "tab_id": initial_tab_id,
                "user_id": "u-new-tab",
                "message": "open a new tab and inspect it",
            })

        self.assertEqual(execute_tool.await_count, 2)
        first, second = execute_tool.await_args_list
        self.assertEqual(first.kwargs["tab_id"], initial_tab_id)
        self.assertEqual(
            second.kwargs["tab_id"],
            f"prov-slot-{raw_new_tab_id}",
        )
        tool_results = [
            call.args[1]
            for call in send.await_args_list
            if len(call.args) > 1 and call.args[1].get("type") == "tool_result"
        ]
        self.assertEqual(tool_results[0]["new_tab_id"], raw_new_tab_id)

    async def test_hosted_provisioned_ddm_new_keeps_its_slot_target(self):
        dispatch = AsyncMock(return_value="ok")

        with patch.object(self.agent, "_dispatch_tool", new=dispatch):
            await self.agent._execute_tool(
                "client-browser",
                "ddm",
                {"flags": "--new https://example.test"},
                tab_id="prov-slot-original-tab",
            )
            await self.agent._execute_tool(
                "client-browser",
                "ddm",
                {"flags": "--tabs"},
                tab_id="prov-slot-original-tab",
            )
            await self.agent._execute_tool(
                "client-browser",
                "ddm",
                {"flags": "--new https://example.test"},
                tab_id="regular-tab",
            )

        provisioned_new, provisioned_tabs, default_new = dispatch.await_args_list
        self.assertEqual(provisioned_new.args[1], "prov-slot-original-tab")
        self.assertEqual(provisioned_tabs.args[1], "auto")
        self.assertEqual(default_new.args[1], "auto")

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

    def test_prepare_hosted_context_uses_default_count_cap_before_char_trim(self):
        messages = [{"role": "system", "content": "system"}]
        for index in range(HOSTED_MAX_SESSION_MESSAGES + 20):
            messages.extend([
                {"role": "user", "content": f"request {index}"},
                {"role": "assistant", "content": f"answer {index}"},
            ])
        messages.append({"role": "user", "content": "current request"})

        stats = _prepare_hosted_context(messages, max_chars=400_000)

        self.assertTrue(stats["count_trimmed"])
        self.assertEqual(stats["message_limit"], HOSTED_MAX_SESSION_MESSAGES)
        self.assertLess(stats["chars_after"], stats["chars_before"])
        self.assertLessEqual(
            sum(1 for message in messages if message.get("role") != "system"),
            HOSTED_MAX_SESSION_MESSAGES,
        )
        self.assertEqual(messages[-1], {"role": "user", "content": "current request"})
        self.assertFalse(stats["emergency_trimmed"])

    def test_prepare_hosted_context_reports_char_only_trim(self):
        messages = [{"role": "system", "content": "system"}]
        for index in range(10):
            messages.extend([
                {"role": "user", "content": f"request {index}"},
                {"role": "assistant", "content": "x" * 20_000},
            ])
        messages.append({"role": "user", "content": "current request"})

        stats = _prepare_hosted_context(
            messages,
            max_messages=HOSTED_MAX_SESSION_MESSAGES,
            max_chars=50_000,
            emergency_keep=10,
        )

        self.assertFalse(stats["count_trimmed"])
        self.assertEqual(stats["message_limit"], HOSTED_MAX_SESSION_MESSAGES)
        self.assertTrue(stats["emergency_trimmed"])
        self.assertLessEqual(
            len(json.dumps(messages, ensure_ascii=False, default=str)), 50_000
        )

    def test_session_save_uses_default_count_cap(self):
        sid = "s-default-context-cap"
        messages = [{"role": "system", "content": "system"}]
        for index in range(HOSTED_MAX_SESSION_MESSAGES + 20):
            messages.append({"role": "user", "content": f"request {index}"})

        with tempfile.TemporaryDirectory() as session_dir:
            with patch("chat_agent_openrouter.SESSION_DIR", session_dir):
                self.agent._save_session(sid, messages)
                with open(os.path.join(session_dir, f"{sid}.json")) as f:
                    saved = json.load(f)

        self.assertEqual(len(saved["messages"]), HOSTED_MAX_SESSION_MESSAGES)
        self.assertEqual(
            saved["messages"][-1],
            {
                "role": "user",
                "content": f"request {HOSTED_MAX_SESSION_MESSAGES + 19}",
            },
        )

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
            HOSTED_MAX_SESSION_MESSAGES,
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


class DeepSeekCostTests(unittest.TestCase):
    """DeepSeek direct provider detection + time-versioned pricing.

    Official legacy schedule (in effect until the exact cutoff) vs the new
    time-of-use schedule, priced by the local provider-submission timestamp.
    """

    EFFECTIVE = 1_786_896_000  # 2026-08-16T16:00:00Z

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k, "__UNSET__") for k in ("DEEPSEEK_API_KEY",)
        }

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v == "__UNSET__":
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _utc_ts(year, month, day, hour, minute=0, second=0):
        import datetime
        dt = datetime.datetime(
            year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc
        )
        return dt.timestamp()

    def test_deepseek_model_detection(self):
        from chat_agent_openrouter import _is_deepseek_model
        self.assertTrue(_is_deepseek_model("deepseek-v4-flash"))
        self.assertTrue(_is_deepseek_model("deepseek-v4-pro"))
        self.assertFalse(_is_deepseek_model("google/gemini-3.1-flash-lite"))
        self.assertFalse(_is_deepseek_model(""))

    def test_legacy_schedule_applies_before_cutoff(self):
        from credit import deepseek_pricing_for_timestamp
        ts = self.EFFECTIVE - 1
        flash = deepseek_pricing_for_timestamp("deepseek-v4-flash", ts)
        pro = deepseek_pricing_for_timestamp("deepseek-v4-pro", ts)
        self.assertEqual(flash["tier"], "legacy")
        self.assertEqual(flash["input_cache_hit_micro_usd_per_million"], 2_800)
        self.assertEqual(flash["input_cache_miss_micro_usd_per_million"], 140_000)
        self.assertEqual(flash["output_micro_usd_per_million"], 280_000)
        self.assertEqual(pro["tier"], "legacy")
        self.assertEqual(pro["input_cache_hit_micro_usd_per_million"], 3_625)
        self.assertEqual(pro["input_cache_miss_micro_usd_per_million"], 435_000)
        self.assertEqual(pro["output_micro_usd_per_million"], 870_000)
        self.assertEqual(flash["pricing_basis_ts"], ts)
        self.assertIn("2026-08-16", flash["schedule_version"])

    def test_exact_cutoff_switches_to_new_schedule(self):
        from credit import deepseek_pricing_for_timestamp
        # 2026-08-16T16:00:00Z is off-peak (hour 16).
        pricing = deepseek_pricing_for_timestamp(
            "deepseek-v4-flash", self.EFFECTIVE
        )
        self.assertEqual(pricing["tier"], "offpeak")
        self.assertEqual(pricing["input_cache_hit_micro_usd_per_million"], 7_000)
        self.assertEqual(pricing["input_cache_miss_micro_usd_per_million"], 220_000)
        self.assertEqual(pricing["output_micro_usd_per_million"], 660_000)

    def test_peak_window_boundary_hours(self):
        from credit import deepseek_pricing_for_timestamp
        cases = {
            # (hour) -> tier
            0: "offpeak",
            1: "peak",    # [01:00, 04:00) starts inclusive
            3: "peak",
            4: "offpeak",  # [01:00, 04:00) ends exclusive
            5: "offpeak",
            6: "peak",    # [06:00, 10:00) starts inclusive
            9: "peak",
            10: "offpeak",  # [06:00, 10:00) ends exclusive
            12: "offpeak",
            16: "offpeak",
        }
        for hour, expected_tier in cases.items():
            ts = self._utc_ts(2026, 8, 17, hour)
            pricing = deepseek_pricing_for_timestamp("deepseek-v4-flash", ts)
            self.assertEqual(
                pricing["tier"], expected_tier,
                f"hour {hour:02d} expected {expected_tier}, got {pricing['tier']}",
            )

    def test_peak_rates_for_both_models(self):
        from credit import deepseek_pricing_for_timestamp
        ts = self._utc_ts(2026, 8, 17, 2)  # peak
        flash = deepseek_pricing_for_timestamp("deepseek-v4-flash", ts)
        pro = deepseek_pricing_for_timestamp("deepseek-v4-pro", ts)
        self.assertEqual(flash["tier"], "peak")
        self.assertEqual(flash["input_cache_hit_micro_usd_per_million"], 14_000)
        self.assertEqual(flash["input_cache_miss_micro_usd_per_million"], 440_000)
        self.assertEqual(flash["output_micro_usd_per_million"], 1_320_000)
        self.assertEqual(pro["tier"], "peak")
        self.assertEqual(pro["input_cache_hit_micro_usd_per_million"], 44_000)
        self.assertEqual(pro["input_cache_miss_micro_usd_per_million"], 1_320_000)
        self.assertEqual(pro["output_micro_usd_per_million"], 3_960_000)

    def test_offpeak_rates_for_both_models(self):
        from credit import deepseek_pricing_for_timestamp
        ts = self._utc_ts(2026, 8, 17, 16)  # off-peak
        flash = deepseek_pricing_for_timestamp("deepseek-v4-flash", ts)
        pro = deepseek_pricing_for_timestamp("deepseek-v4-pro", ts)
        self.assertEqual(flash["tier"], "offpeak")
        self.assertEqual(flash["input_cache_hit_micro_usd_per_million"], 7_000)
        self.assertEqual(flash["input_cache_miss_micro_usd_per_million"], 220_000)
        self.assertEqual(flash["output_micro_usd_per_million"], 660_000)
        self.assertEqual(pro["tier"], "offpeak")
        self.assertEqual(pro["input_cache_hit_micro_usd_per_million"], 22_000)
        self.assertEqual(pro["input_cache_miss_micro_usd_per_million"], 660_000)
        self.assertEqual(pro["output_micro_usd_per_million"], 1_980_000)

    def test_extract_deepseek_usage_legacy_flash_cache_aware(self):
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "id": "ds-1",
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 900_000,
                "prompt_cache_miss_tokens": 100_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            },
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-flash", submitted_at_ts=self.EFFECTIVE - 1
        )
        # 0.9M×$0.0028 + 0.1M×$0.14 + 0.1M×$0.28 per 1M tokens
        expected = 0.9 * 0.0028 + 0.1 * 0.14 + 0.1 * 0.28
        self.assertAlmostEqual(usage["cost_usd"], expected, places=9)
        self.assertEqual(usage["cost_micro_usd"], 44_520)
        self.assertEqual(usage["prompt_cache_hit_tokens"], 900_000)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 100_000)
        self.assertTrue(usage["cost_present"])
        self.assertEqual(usage["pricing"]["tier"], "legacy")

    def test_extract_deepseek_usage_falls_back_to_all_miss(self):
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            }
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-flash", submitted_at_ts=self.EFFECTIVE - 1
        )
        # No breakdown → all prompt tokens billed as cache miss (conservative).
        expected = 1.0 * 0.14 + 0.1 * 0.28
        self.assertAlmostEqual(usage["cost_usd"], expected, places=9)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 1_000_000)

    def test_extract_deepseek_usage_peak_flash(self):
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 500_000,
                "prompt_cache_miss_tokens": 500_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            }
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-flash",
            submitted_at_ts=self._utc_ts(2026, 8, 17, 2),
        )
        # Peak: 0.5M×$0.014 + 0.5M×$0.44 + 0.1M×$1.32
        expected = 0.5 * 0.014 + 0.5 * 0.44 + 0.1 * 1.32
        self.assertAlmostEqual(usage["cost_usd"], expected, places=9)
        self.assertEqual(usage["pricing"]["tier"], "peak")

    def test_extract_deepseek_usage_prices_unaccounted_remainder_as_miss(self):
        """If hit+miss < prompt_tokens, the remainder is billed as cache miss."""
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 400_000,
                "prompt_cache_miss_tokens": 400_000,
                "completion_tokens": 0,
                "total_tokens": 1_000_000,
            }
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-flash", submitted_at_ts=self.EFFECTIVE - 1
        )
        # 400k hit, 600k miss (200k unaccounted priced as miss).
        expected = 0.4 * 0.0028 + 0.6 * 0.14
        self.assertAlmostEqual(usage["cost_usd"], expected, places=9)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 600_000)

    def test_pro_uses_pro_legacy_tier(self):
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 1_000_000,
                "prompt_cache_miss_tokens": 0,
                "completion_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            }
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-pro", submitted_at_ts=self.EFFECTIVE - 1
        )
        expected = 1.0 * 0.003625 + 0.0 + 1.0 * 0.87
        self.assertAlmostEqual(usage["cost_usd"], expected, places=9)
        self.assertEqual(usage["pricing"]["tier"], "legacy")

    def test_extract_deepseek_usage_returns_audit_metadata(self):
        """Pricing audit metadata (schedule/tier/basis/rates) is returned."""
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 1_000_000,
                "prompt_cache_miss_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 1_000_000,
            }
        }
        ts = self._utc_ts(2026, 8, 17, 8)  # peak
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-flash", submitted_at_ts=ts
        )
        pricing = usage["pricing"]
        self.assertIsNotNone(pricing)
        self.assertEqual(pricing["schedule_version"], "2026-08-16T16:00:00Z")
        self.assertEqual(pricing["tier"], "peak")
        self.assertEqual(pricing["pricing_basis_ts"], ts)
        self.assertEqual(pricing["input_cache_hit_micro_usd_per_million"], 14_000)
        self.assertEqual(pricing["input_cache_miss_micro_usd_per_million"], 440_000)
        self.assertEqual(pricing["output_micro_usd_per_million"], 1_320_000)

    def test_unknown_deepseek_model_reports_no_cost(self):
        """A model with no price entry reports cost_present=False so the
        ledger falls back to the conservative full-reservation settle."""
        from chat_agent_openrouter import _extract_deepseek_usage
        payload = {
            "usage": {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 1_000_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            }
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v9-unknown", submitted_at_ts=self.EFFECTIVE + 1
        )
        self.assertEqual(usage["cost_usd"], 0.0)
        self.assertFalse(usage["cost_present"])
        self.assertIsNone(usage["pricing"])

    def test_round_cost_micro_boundaries(self):
        from credit import round_cost_micro
        self.assertEqual(round_cost_micro(0), 0)
        self.assertEqual(round_cost_micro(499_999), 0)   # sub-0.5 micro → down
        self.assertEqual(round_cost_micro(500_000), 1)   # exactly half → up
        self.assertEqual(round_cost_micro(500_001), 1)   # above half → up
        self.assertEqual(round_cost_micro(44_520_000_000), 44_520)
        self.assertEqual(round_cost_micro(1_334_000_000_000), 1_334_000)

    def test_extract_deepseek_usage_sub_half_micro_rounds_zero_but_present(self):
        """A known-priced request with nonzero usage that rounds to zero micro
        must stay cost_present (settle zero, not full-reservation)."""
        from chat_agent_openrouter import _extract_deepseek_usage
        # One cache-hit token at legacy flash hit rate (2800 micro/1M) →
        # 0.0028 micro, rounds to 0.
        payload = {
            "usage": {
                "prompt_tokens": 1,
                "prompt_cache_hit_tokens": 1,
                "prompt_cache_miss_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 1,
            }
        }
        usage = _extract_deepseek_usage(
            payload, "deepseek-v4-flash", submitted_at_ts=self.EFFECTIVE - 1
        )
        self.assertEqual(usage["cost_micro_usd"], 0)
        self.assertEqual(usage["cost_usd"], 0.0)
        self.assertTrue(usage["cost_present"])
        self.assertEqual(usage["pricing"]["tier"], "legacy")

    def test_worker_and_control_plane_costs_match_all_pricing_tiers(self):
        """Worker extraction and authoritative settlement stay byte-for-byte
        equivalent across legacy, off-peak, and peak schedules."""
        from chat_agent_openrouter import _extract_deepseek_usage
        from credit import CreditLedger

        cases = (
            ("legacy", self.EFFECTIVE - 1),
            ("offpeak", self._utc_ts(2026, 8, 17, 16)),
            ("peak", self._utc_ts(2026, 8, 17, 2)),
        )
        usage_payload = {
            "prompt_tokens": 1_000_000,
            "prompt_cache_hit_tokens": 400_000,
            # Deliberately leave 200k unaccounted; both paths must conservatively
            # assign that remainder to cache misses.
            "prompt_cache_miss_tokens": 400_000,
            "completion_tokens": 123_456,
            "total_tokens": 1_123_456,
        }

        for model in ("deepseek-v4-flash", "deepseek-v4-pro"):
            for expected_tier, submitted_at in cases:
                with self.subTest(model=model, tier=expected_tier):
                    worker = _extract_deepseek_usage(
                        {"usage": usage_payload}, model,
                        submitted_at_ts=submitted_at,
                    )
                    with tempfile.TemporaryDirectory() as temp_dir:
                        ledger = CreditLedger(os.path.join(temp_dir, "credit.db"))
                        user_id = f"u-{model}-{expected_tier}"
                        ledger.grant(
                            user_id, 10_000_000,
                            idempotency_key=f"grant-{model}-{expected_tier}",
                        )
                        run = ledger.create_run(
                            user_id,
                            idempotency_key=f"run-{model}-{expected_tier}",
                        )
                        call = ledger.reserve_call(
                            run["run_id"], model=model,
                            idempotency_key=f"call-{model}-{expected_tier}",
                        )
                        with patch("credit._now_ts", return_value=submitted_at):
                            ledger.mark_call_submitted(call["call_id"])
                        settled = ledger.settle_call(
                            call["call_id"],
                            actual_cost_micro_usd=0,
                            provider_cost_micro_usd=0,
                            prompt_tokens=usage_payload["prompt_tokens"],
                            prompt_cache_hit_tokens=usage_payload[
                                "prompt_cache_hit_tokens"
                            ],
                            prompt_cache_miss_tokens=usage_payload[
                                "prompt_cache_miss_tokens"
                            ],
                            completion_tokens=usage_payload["completion_tokens"],
                            total_tokens=usage_payload["total_tokens"],
                        )
                        CreditLedger._instances.pop(
                            os.path.join(temp_dir, "credit.db"), None
                        )

                    self.assertEqual(settled["pricing_tier"], expected_tier)
                    self.assertEqual(
                        settled["provider_cost_micro_usd"],
                        worker["cost_micro_usd"],
                    )
                    self.assertEqual(
                        settled["prompt_cache_miss_tokens"],
                        worker["prompt_cache_miss_tokens"],
                    )


class DeepSeekProviderCallTests(unittest.IsolatedAsyncioTestCase):
    """DeepSeek direct API call routing (URL/key/headers, body mapping)."""

    def setUp(self):
        self._saved = {
            k: os.environ.get(k, "__UNSET__")
            for k in ("DEEPSEEK_API_KEY", "HOSTED_AGENT_SERVICE_TOKEN")
        }
        os.environ["DEEPSEEK_API_KEY"] = "sk-ds-test"
        os.environ["HOSTED_AGENT_SERVICE_TOKEN"] = "hosted-worker-test-token"
        self.agent = TrialAgent(
            api_key="trial-websocket-key",
            agent_id="trial-agent",
            server="ws://web:8080",
            model="deepseek-v4-flash",
        )

    def tearDown(self):
        for k, v in self._saved.items():
            if v == "__UNSET__":
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def test_do_deepseek_call_hits_deepseek_url_with_key(self):
        from chat_agent_openrouter import DEEPSEEK_URL
        client = SimpleNamespace(post=AsyncMock())
        client.post.return_value = SimpleNamespace(
            is_success=True,
            status_code=200,
            json=lambda: {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )
        data = await self.agent._do_deepseek_call(
            client, {"model": "deepseek-v4-flash", "messages": []}, "deepseek-v4-flash"
        )
        self.assertIn("choices", data)
        args, kwargs = client.post.call_args
        self.assertEqual(args[0], DEEPSEEK_URL)
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer sk-ds-test"
        )
        self.assertNotIn("HTTP-Referer", kwargs["headers"])

    async def test_do_deepseek_call_requires_key(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self.agent = TrialAgent(
            api_key="k", agent_id="a", server="ws://web:8080", model="deepseek-v4-flash"
        )
        client = SimpleNamespace(post=AsyncMock())
        with self.assertRaises(RuntimeError):
            await self.agent._do_deepseek_call(
                client, {"model": "deepseek-v4-flash"}, "deepseek-v4-flash"
            )

    async def test_credit_settle_forwards_pricing_audit_metadata(self):
        """The settle callback POSTs the immutable pricing audit fields."""
        client = SimpleNamespace(post=AsyncMock())
        client.post.return_value = SimpleNamespace(
            is_success=True,
            status_code=200,
            json=lambda: {"call_id": "call-1", "status": "settled"},
        )
        await self.agent._credit_settle(
            client,
            "call-1",
            actual_cost_micro_usd=44_520,
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            total_tokens=1_100_000,
            provider_cost_micro_usd=44_520,
            prompt_cache_hit_tokens=900_000,
            prompt_cache_miss_tokens=100_000,
            pricing_schedule_version="2026-08-16T16:00:00Z",
            pricing_tier="legacy",
            pricing_basis_ts=1_786_895_999.0,
            input_cache_hit_rate_micro_usd_per_million=2_800,
            input_cache_miss_rate_micro_usd_per_million=140_000,
            output_rate_micro_usd_per_million=280_000,
        )
        args, kwargs = client.post.call_args
        body = kwargs["json"]
        self.assertEqual(body["pricing_schedule_version"], "2026-08-16T16:00:00Z")
        self.assertEqual(body["pricing_tier"], "legacy")
        self.assertEqual(body["pricing_basis_ts"], 1_786_895_999.0)
        self.assertEqual(
            body["input_cache_hit_rate_micro_usd_per_million"], 2_800
        )
        self.assertEqual(
            body["input_cache_miss_rate_micro_usd_per_million"], 140_000
        )
        self.assertEqual(body["output_rate_micro_usd_per_million"], 280_000)

    async def test_deepseek_settle_prices_by_submission_timestamp(self):
        """A successful DeepSeek call is priced by the callback-returned
        authoritative ``submitted_at`` (not the worker's local clock)."""
        settle = AsyncMock(return_value={"call_id": "call-ds", "status": "settled"})
        credit_client = SimpleNamespace(aclose=AsyncMock())
        peak_ts = 1_786_932_000.0  # 2026-08-17T02:00:00Z — peak window
        offpeak_ts = 1_786_896_000.0  # 2026-08-16T16:00:00Z — off-peak
        self.agent._session_billing_runs["s-ds-billing"] = "run-ds-billing"

        async def provider(*_args, **_kwargs):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1_000_000,
                    "prompt_cache_hit_tokens": 1_000_000,
                    "prompt_cache_miss_tokens": 0,
                    "completion_tokens": 1_000_000,
                    "total_tokens": 2_000_000,
                },
            }

        with (
            patch("chat_agent_openrouter.httpx.AsyncClient", return_value=credit_client),
            patch.object(
                self.agent, "_credit_reserve",
                new=AsyncMock(return_value={"call_id": "call-ds"}),
            ),
            patch.object(
                self.agent, "_credit_mark_submitted",
                new=AsyncMock(
                    return_value={"status": "submitted", "submitted_at": peak_ts}
                ),
            ),
            patch.object(self.agent, "_credit_settle", new=settle),
            patch.object(self.agent, "_credit_release", new=AsyncMock()),
            patch.object(self.agent, "_do_openrouter_call", side_effect=provider),
            patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
            # Worker's local clock is off-peak; the authoritative submitted_at is
            # peak. The settle must follow submitted_at, not the local clock.
            patch("chat_agent_openrouter.time.time", return_value=offpeak_ts),
        ):
            await self.agent._call_openrouter(
                SimpleNamespace(),
                [{"role": "user", "content": "hello"}],
                model="deepseek-v4-flash",
                session_id="s-ds-billing",
            )

        settle.assert_awaited_once()
        kwargs = settle.await_args.kwargs
        self.assertEqual(kwargs["pricing_tier"], "peak")
        self.assertEqual(kwargs["pricing_schedule_version"], "2026-08-16T16:00:00Z")
        self.assertEqual(kwargs["pricing_basis_ts"], peak_ts)
        self.assertEqual(kwargs["input_cache_hit_rate_micro_usd_per_million"], 14_000)
        self.assertEqual(kwargs["input_cache_miss_rate_micro_usd_per_million"], 440_000)
        self.assertEqual(kwargs["output_rate_micro_usd_per_million"], 1_320_000)
        # Peak flash: 1M hit × $0.014 + 1M out × $1.32 = $1.334 → 1_334_000 micro
        self.assertEqual(kwargs["actual_cost_micro_usd"], 1_334_000)
        self.assertEqual(kwargs["provider_cost_micro_usd"], 1_334_000)

    async def test_do_openrouter_call_dispatches_to_deepseek(self):
        client = SimpleNamespace(post=AsyncMock())
        client.post.return_value = SimpleNamespace(
            is_success=True,
            status_code=200,
            json=lambda: {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )
        data = await self.agent._do_openrouter_call(
            client, {"model": "deepseek-v4-flash"}, "deepseek-v4-flash"
        )
        from chat_agent_openrouter import DEEPSEEK_URL
        args, _ = client.post.call_args
        self.assertEqual(args[0], DEEPSEEK_URL)
        self.assertIn("choices", data)

    def test_deepseek_http_errors_are_not_labeled_as_openrouter(self):
        request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "tool message ordering is invalid"}},
        )
        error = httpx.HTTPStatusError("400", request=request, response=response)

        message = _hosted_user_error(error, "deepseek-v4-flash")

        self.assertEqual(
            message,
            "DeepSeek rejected model deepseek-v4-flash: tool message ordering is invalid",
        )
        self.assertNotIn("OpenRouter", message)

    def test_direct_deepseek_skips_optional_tool_followup_system_guidance(self):
        messages = [{"role": "tool", "tool_call_id": "call-1", "content": "done"}]

        appended = _append_tool_followup_guidance(
            messages, "deepseek-v4-flash", "internal nudge"
        )

        self.assertFalse(appended)
        self.assertEqual(len(messages), 1)

    async def test_direct_deepseek_nudge_screenshot_preserves_tool_adjacency(self):
        session_id = "s-deepseek-loop"
        self.agent.sessions[session_id] = []
        tool_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Need browser evidence.",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "ddm", "arguments": '{"flags":"--text"}'},
                    }],
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
        loop_feedback = SimpleNamespace(
            should_intervene=True,
            severity="nudge",
            reason_codes=[],
        )
        loop_state = SimpleNamespace(
            hard_stop_guard=False,
            hard_stop_recovery_used=0,
            check_loop=lambda _signature: (True, "Stop retrying.", loop_feedback),
        )
        call_model = AsyncMock(side_effect=[tool_response, final_response])

        with (
            patch("chat_agent_openrouter.NudgeState", return_value=loop_state),
            patch("chat_agent_openrouter.INTERVENTION_SCREENSHOT_ON_NUDGE", True),
            patch.object(self.agent, "_call_openrouter", new=call_model),
            patch.object(self.agent, "_execute_tool", new=AsyncMock(return_value="iVBOR" + "A" * 64)),
            patch.object(self.agent, "_sanitize_user_output", new=AsyncMock(return_value="done")),
            patch.object(self.agent, "_send", new=AsyncMock()),
            patch.object(self.agent, "_save_session"),
        ):
            await self.agent._handle_message({
                "session_id": session_id,
                "agent_id": "client-browser",
                "message": "inspect this page",
                "model": "deepseek-v4-flash",
            })

        forced_final_messages = call_model.await_args_list[1].args[1]
        tool_call_index = next(
            index
            for index, message in enumerate(forced_final_messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        self.assertEqual(forced_final_messages[tool_call_index + 1]["role"], "tool")
        self.assertEqual(
            forced_final_messages[tool_call_index + 1]["tool_call_id"], "call-1"
        )

    async def test_direct_deepseek_strips_escaped_tool_call_xml(self):
        payload = '{"name":"navigate","arguments":{"url":"https://example.test"}}'
        for opening, closing in (
            ("<tool_call>", "</tool_call>"),
            ("&lt;tool_call&gt;", "&lt;/tool_call&gt;"),
            ("&amp;lt;tool_call&amp;gt;", "&amp;lt;/tool_call&amp;gt;"),
        ):
            with self.subTest(opening=opening):
                text = f"Navigation complete. {opening}{payload}{closing}"
                cleaned = await self.agent._sanitize_user_output(
                    SimpleNamespace(), "deepseek-v4-flash", text
                )

                self.assertEqual(cleaned, "Navigation complete.")
                self.assertNotIn("tool_call", cleaned.lower())

        for opening in ("<tool_call>", "&lt;tool_call&gt;", "&amp;lt;tool_call&amp;gt;"):
            with self.subTest(opening=opening, unclosed=True):
                text = f"Navigation complete. {opening}{payload}"
                cleaned = await self.agent._sanitize_user_output(
                    SimpleNamespace(), "deepseek-v4-flash", text
                )

                self.assertEqual(cleaned, "Navigation complete.")
                self.assertNotIn("tool_call", cleaned.lower())

        dsml = (
            "Still at step 11. "
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
            "document.body.innerText"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
        )
        cleaned = await self.agent._sanitize_user_output(
            SimpleNamespace(), "deepseek-v4-flash", dsml
        )

        self.assertEqual(cleaned, "Still at step 11.")
        self.assertNotIn("dsml", cleaned.lower())
        self.assertNotIn("tool_call", cleaned.lower())

    async def test_direct_deepseek_recovers_dsml_tool_call_and_drops_pre_tool_prose(self):
        session_id = "s-deepseek-dsml"
        self.agent.sessions[session_id] = []
        dsml = (
            "I'll inspect the page first. "
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
            "document.body.innerText"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"tab_id\" string=\"true\">"
            "tab-1"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
        )
        tool_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": dsml,
                    "reasoning_content": "Inspect the page.",
                },
                "finish_reason": "stop",
            }],
        }
        final_response = {
            "choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }],
        }
        call_model = AsyncMock(side_effect=[tool_response, final_response])
        execute_tool = AsyncMock(return_value="page text")

        with (
            patch.object(self.agent, "_call_openrouter", new=call_model),
            patch.object(self.agent, "_execute_tool", new=execute_tool),
            patch.object(self.agent, "_sanitize_user_output", new=AsyncMock(return_value="done")),
            patch.object(self.agent, "_send", new=AsyncMock()),
            patch.object(self.agent, "_save_session"),
        ):
            await self.agent._handle_message({
                "session_id": session_id,
                "agent_id": "client-browser",
                "message": "inspect the page",
                "model": "deepseek-v4-flash",
            })

        self.assertEqual(execute_tool.await_count, 1)
        execute_call = execute_tool.await_args_list[0]
        self.assertEqual(execute_call.args[1], "js_eval")
        self.assertEqual(
            execute_call.args[2],
            {"expression": "document.body.innerText", "tab_id": "tab-1"},
        )
        followup_messages = call_model.await_args_list[1].args[1]
        recovered = next(
            message
            for message in followup_messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        self.assertIsNone(recovered["content"])
        self.assertEqual(recovered["reasoning_content"], "Inspect the page.")
        self.assertEqual(recovered["tool_calls"][0]["function"]["name"], "js_eval")
        self.assertEqual(
            json.loads(recovered["tool_calls"][0]["function"]["arguments"]),
            {"expression": "document.body.innerText", "tab_id": "tab-1"},
        )

    def test_dsml_recovery_rejects_unknown_tools(self):
        message = {
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"unknown_tool\">"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        }

        self.assertIs(_recover_deepseek_dsml_tool_calls(message), message)

    def test_dsml_recovery_rejects_malformed_trailing_invoke(self):
        message = {
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"navigate\">"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        }

        self.assertIs(_recover_deepseek_dsml_tool_calls(message), message)

    def test_dsml_recovery_rejects_residual_markup_outside_a_valid_block(self):
        message = {
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"navigate\">"
            ),
        }

        self.assertIs(_recover_deepseek_dsml_tool_calls(message), message)

    def test_dsml_recovery_rejects_malformed_trailing_parameter(self):
        message = {
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
                "document.title"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"tab_id\" string=\"true\">"
                "tab-1"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        }

        self.assertIs(_recover_deepseek_dsml_tool_calls(message), message)

    def test_dsml_recovery_rejects_nested_markup_inside_a_parameter(self):
        message = {
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
                "document.title"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"tab_id\" string=\"true\">"
                "tab-1"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        }

        self.assertIs(_recover_deepseek_dsml_tool_calls(message), message)

    def test_dsml_recovery_rejects_unparseable_attributes(self):
        message = {
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\" unexpected>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        }

        self.assertIs(_recover_deepseek_dsml_tool_calls(message), message)

    def test_dsml_recovery_accepts_entity_escaped_wrappers(self):
        raw = (
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
            "document.title"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
        )
        for opening, closing in (("&lt;", "&gt;"), ("&amp;lt;", "&amp;gt;")):
            with self.subTest(opening=opening):
                escaped = raw.replace("<", opening).replace(">", closing)
                recovered = _recover_deepseek_dsml_tool_calls(
                    {"role": "assistant", "content": escaped}
                )

                self.assertIsNone(recovered["content"])
                self.assertEqual(recovered["tool_calls"][0]["function"]["name"], "js_eval")
                self.assertEqual(
                    json.loads(recovered["tool_calls"][0]["function"]["arguments"]),
                    {"expression": "document.title"},
                )

    def test_dsml_recovery_keeps_a_valid_large_batch(self):
        invokes = "".join(
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
            "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
            f"document.title + {index}"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
            "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
            for index in range(17)
        )
        recovered = _recover_deepseek_dsml_tool_calls({
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                f"{invokes}"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        })

        self.assertEqual(len(recovered["tool_calls"]), 17)
        self.assertEqual(
            json.loads(recovered["tool_calls"][-1]["function"]["arguments"]),
            {"expression": "document.title + 16"},
        )

    def test_dsml_recovery_decodes_parameter_entities_once(self):
        recovered = _recover_deepseek_dsml_tool_calls({
            "role": "assistant",
            "content": (
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke name=\"js_eval\">"
                "<\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter name=\"expression\" string=\"true\">"
                "document.querySelector(\'&amp;lt;sample&amp;gt;\')"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cparameter>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Cinvoke>"
                "</\uFF5C\uFF5CDSML\uFF5C\uFF5Ctool_calls>"
            ),
        })

        self.assertEqual(
            json.loads(recovered["tool_calls"][0]["function"]["arguments"]),
            {"expression": "document.querySelector('&lt;sample&gt;')"},
        )

    async def test_deepseek_body_keeps_thinking_consistent(self):
        """DeepSeek thinking mode must be enabled for EVERY turn (including the
        fast first turn) so the reasoning_content echo requirement stays
        consistent and follow-ups do not 400."""
        for reasoning in (False, True):
            with self.subTest(reasoning=reasoning):
                client = SimpleNamespace(post=AsyncMock())
                client.post.return_value = SimpleNamespace(
                    is_success=True,
                    status_code=200,
                    json=lambda: {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                            "total_tokens": 2}},
                )
                with (
                    patch.object(self.agent, "_do_deepseek_call", new=AsyncMock(return_value={
                        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    })),
                    patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
                ):
                    await self.agent._call_openrouter(
                        client,
                        [{"role": "user", "content": "hi"}],
                        model="deepseek-v4-flash",
                        session_id="s-test",
                        user_id="u-test",
                        reasoning=reasoning,
                    )
                    sent = self.agent._do_deepseek_call.call_args[0][1]
                    self.assertEqual(sent["thinking"], {"type": "enabled"})
                    self.assertNotIn("reasoning", sent)

    async def test_deepseek_echoes_reasoning_content_verbatim(self):
        """A prior thinking-mode assistant message must be passed to the
        follow-up request unchanged (including reasoning_content) — this is the
        guarantee that prevents the DeepSeek 400 on multi-turn conversations."""
        reasoning_content = "Need to check the market data before answering."
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Why is oil up?"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": reasoning_content,
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "navigate", "arguments": "{\"url\":\"x\"}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "user", "content": "continue"},
        ]
        client = SimpleNamespace(post=AsyncMock())
        client.post.return_value = SimpleNamespace(
            is_success=True,
            status_code=200,
            json=lambda: {"choices": [{"message": {"role": "assistant", "content": "done"}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        )
        with (
            patch.object(self.agent, "_do_deepseek_call", new=AsyncMock(return_value={
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })),
            patch.object(self.agent, "_emit_openrouter_usage_event", new=AsyncMock()),
        ):
            await self.agent._call_openrouter(
                client,
                history,
                model="deepseek-v4-flash",
                session_id="s-test",
                user_id="u-test",
            )
            sent = self.agent._do_deepseek_call.call_args[0][1]
            # The full conversation (including the assistant message with
            # content:None, reasoning_content, tool_calls) must be forwarded
            # verbatim and in order — that is the echo guarantee.
            self.assertEqual(sent["messages"], history)
            assistant_msgs = [
                m for m in sent["messages"]
                if m.get("role") == "assistant" and m.get("tool_calls")
            ]
            self.assertTrue(assistant_msgs, "assistant tool-call message should be in the request")
            self.assertEqual(
                assistant_msgs[0].get("reasoning_content"), reasoning_content
            )
            self.assertEqual(assistant_msgs[0]["tool_calls"], history[2]["tool_calls"])


if __name__ == "__main__":
    unittest.main()
