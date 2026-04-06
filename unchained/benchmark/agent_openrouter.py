"""OpenRouter benchmark backend."""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx

from benchmark.common import BenchmarkBrowser, BenchmarkConfig, TaskResult
from chat_agent_openrouter import DEFAULT_MODEL, OPENROUTER_URL, SYSTEM_PROMPT, TOOLS
from context_compact import compact_messages

try:
    from benchmark.intermediate_goal import (
        build_intermediate_goal_contract,
        estimate_hardness,
        should_enable_intermediate_goal,
    )

    _HAS_INTERMEDIATE_GOAL = True
except RuntimeError:
    _HAS_INTERMEDIATE_GOAL = False


def _extract_text_content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "") or ""))
        return "\n".join(parts).strip()
    return ""


_RATE_RAMP_KEYWORDS = (
    "rate increased too quickly", "rate limit", "too many requests",
    "slow down", "throttle", "quota", "overloaded", "server busy",
)


async def _call_openrouter(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
) -> dict:
    _body = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 4096,
    }
    _headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://unchainedsky.com",
        "X-Title": "Unchained Benchmark",
    }
    response = await client.post(OPENROUTER_URL, json=_body, headers=_headers, timeout=90.0)
    response.raise_for_status()
    data = response.json()
    if "choices" not in data:
        err_msg = data.get("error", {})
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", str(data)[:200])
        err_str = str(err_msg).lower()
        _is_rate_ramp = any(k in err_str for k in _RATE_RAMP_KEYWORDS)
        delays = [5, 10, 20, 30] if _is_rate_ramp else [2, 4]
        print(f"[openrouter/bench] Provider error: {err_msg} — retrying ({len(delays)} attempts)")
        for attempt, delay in enumerate(delays, 1):
            await asyncio.sleep(delay)
            response = await client.post(OPENROUTER_URL, json=_body, headers=_headers, timeout=90.0)
            if response.is_success:
                data = response.json()
                if "choices" in data:
                    break
            print(f"[openrouter/bench] Provider error retry {attempt}/{len(delays)} still no choices")
        if "choices" not in data:
            raise RuntimeError(f"OpenRouter provider error: {err_msg}")
    return data


async def run_task(
    task: dict,
    *,
    config: BenchmarkConfig,
    model: str | None = None,
    max_turns: int = 30,
    intermediate_goal_mode: str = "off",
) -> TaskResult:
    """Run one task through an OpenRouter model."""
    result = TaskResult()
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    timeout = task.get("timeout_seconds", 120)
    or_model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    if not api_key:
        result.error = "OPENROUTER_API_KEY is required for openrouter benchmarks"
        return result
    if not config.agent_id:
        result.error = "agent_id is required for openrouter benchmarks"
        return result

    task_prompt = task["task"]
    if _HAS_INTERMEDIATE_GOAL:
        hardness_estimate = estimate_hardness(task)
        enable_intermediate_goal = should_enable_intermediate_goal(
            intermediate_goal_mode,
            hardness_estimate.score,
        )
        result.intermediate_goal_mode = (intermediate_goal_mode or "off").lower()
        result.intermediate_goal_enabled = enable_intermediate_goal
        result.hardness = hardness_estimate.to_dict()
        if enable_intermediate_goal:
            contract = build_intermediate_goal_contract(task, hardness_estimate)
            task_prompt = f"{task_prompt}\n\n{contract}"
    else:
        result.intermediate_goal_mode = "off"

    browser = BenchmarkBrowser(config)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_prompt},
    ]
    start = time.monotonic()

    try:
        async with httpx.AsyncClient() as client:
            async def _run_loop():
                for turn in range(1, max_turns + 1):
                    if turn > 1 and turn % 5 == 0:
                        compact_messages(messages, fmt="openai")

                    payload = await _call_openrouter(client, api_key, or_model, messages)
                    usage = payload.get("usage") or {}
                    prompt_tokens = int(
                        usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
                    )
                    completion_tokens = int(
                        usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                    )
                    total_tokens = int(usage.get("total_tokens", 0) or 0)
                    result.prompt_tokens += prompt_tokens
                    result.completion_tokens += completion_tokens
                    result.total_tokens += total_tokens or (prompt_tokens + completion_tokens)
                    result.turns = turn

                    choice = (payload.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    tool_calls = message.get("tool_calls") or []

                    if tool_calls:
                        for tool_call in tool_calls:
                            function = tool_call.get("function") or {}
                            if function.get("arguments") is None:
                                function["arguments"] = "{}"

                    messages.append(message)

                    if not tool_calls:
                        result.answer = _extract_text_content(message)
                        return

                    tool_results = []
                    for tool_call in tool_calls:
                        function = tool_call.get("function") or {}
                        tool_name = str(function.get("name", "") or "")
                        try:
                            tool_input = json.loads(function.get("arguments", "{}") or "{}")
                        except json.JSONDecodeError:
                            tool_input = {}
                        if not isinstance(tool_input, dict):
                            tool_input = {}
                        tool_output = await browser.execute_tool(
                            tool_name,
                            tool_input,
                            default_tab_id=config.tab_id,
                        )
                        result.record_tool_call(
                            tool_name,
                            tool_input,
                            call_signature=f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}",
                            output_preview=tool_output,
                        )
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": tool_output[:4000],
                            }
                        )
                    messages.extend(tool_results)

                result.error = f"Exceeded {max_turns} turns"

            await asyncio.wait_for(_run_loop(), timeout=timeout)
    except asyncio.TimeoutError:
        result.error = f"Timeout after {timeout}s"
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        result.error = f"HTTP {exc.response.status_code}: {body}"
    except Exception as exc:
        result.error = f"Exception: {exc}"
    finally:
        await browser.close()

    result.wall_seconds = time.monotonic() - start
    return result
