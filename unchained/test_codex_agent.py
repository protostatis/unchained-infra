#!/usr/bin/env python3
"""Targeted tests for Codex model fallback behavior."""

import unittest

import httpx

from chat_agent_codex import CodexChatAgent


class _FakeClient:
    def __init__(self, post_responses, get_response):
        self._post_responses = list(post_responses)
        self._get_response = get_response
        self.posts = []
        self.gets = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if not self._post_responses:
            raise AssertionError("Unexpected extra POST")
        return self._post_responses.pop(0)

    async def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return self._get_response


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


class TestCodexFallback(unittest.IsolatedAsyncioTestCase):
    async def test_model_not_found_falls_back_to_available_model(self):
        agent = CodexChatAgent(
            api_key="uc_live_x",
            agent_id="codexsdk-test",
            server="wss://example.invalid",
            codex_key="sk-test",
            model="codex-mini-latest",
            mode="codex-sdk",
        )

        first = _json_response(404, {
            "error": {
                "code": "model_not_found",
                "message": "The model `codex-mini-latest` does not exist or you do not have access to it.",
            }
        })
        second = _json_response(200, {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        })
        models = httpx.Response(
            status_code=200,
            json={"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-4.1"}]},
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        client = _FakeClient([first, second], models)

        out = await agent._call_codex(
            client=client,
            messages=[{"role": "user", "content": "hi"}],
            model="codex-mini-latest",
            codex_key="sk-test",
        )

        self.assertIn("choices", out)
        self.assertEqual(agent._model_fallbacks.get("codex-mini-latest"), "gpt-4.1-mini")
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.posts[1]["json"]["model"], "gpt-4.1-mini")

    async def test_model_not_found_without_fallback_raises_clear_error(self):
        agent = CodexChatAgent(
            api_key="uc_live_x",
            agent_id="codexsdk-test",
            server="wss://example.invalid",
            codex_key="sk-test",
            model="codex-mini-latest",
            mode="codex-sdk",
        )

        first = _json_response(404, {
            "error": {
                "code": "model_not_found",
                "message": "The model `codex-mini-latest` does not exist or you do not have access to it.",
            }
        })
        models = httpx.Response(
            status_code=200,
            json={"data": [{"id": "gpt-image-1"}, {"id": "whisper-1"}]},
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        client = _FakeClient([first], models)

        with self.assertRaises(RuntimeError) as ctx:
            await agent._call_codex(
                client=client,
                messages=[{"role": "user", "content": "hi"}],
                model="codex-mini-latest",
                codex_key="sk-test",
            )
        self.assertIn("not available", str(ctx.exception))
        self.assertEqual(len(client.posts), 1)


if __name__ == "__main__":
    unittest.main()
