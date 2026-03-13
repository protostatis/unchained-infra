"""Anthropic API orchestrator benchmark backend."""

from __future__ import annotations

import asyncio
import json
import time

import anthropic

from benchmark.common import BenchmarkBrowser, BenchmarkConfig, TaskResult
from context_compact import compact_messages
from orchestrator import SYSTEM_PROMPT, TOOLS

try:
    from benchmark.intermediate_goal import (
        build_intermediate_goal_contract,
        estimate_hardness,
        should_enable_intermediate_goal,
    )

    _HAS_INTERMEDIATE_GOAL = True
except RuntimeError:
    _HAS_INTERMEDIATE_GOAL = False


async def run_task(
    task: dict,
    *,
    config: BenchmarkConfig,
    model: str | None = None,
    max_turns: int = 50,
    intermediate_goal_mode: str = "off",
) -> TaskResult:
    """Run one task through the Anthropic API orchestrator."""
    result = TaskResult()
    timeout = task.get("timeout_seconds", 120)
    orch_model = model or "claude-sonnet-4-6"

    if not config.agent_id:
        result.error = "agent_id is required for orchestrator benchmarks"
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
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": task_prompt}]
    start = time.monotonic()

    try:
        async def _run_loop():
            for turn in range(max_turns):
                if turn > 0 and turn % 5 == 0:
                    compact_messages(messages, fmt="anthropic")

                response = await client.messages.create(
                    model=orch_model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )

                usage = getattr(response, "usage", None)
                if usage:
                    result.prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                    result.completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)
                    result.total_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                    result.total_tokens += int(getattr(usage, "output_tokens", 0) or 0)

                result.turns = turn + 1

                if response.stop_reason == "end_turn":
                    text_parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
                    result.answer = "\n".join(text_parts).strip()
                    return

                tool_results = []
                for block in response.content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    tool_name = block.name
                    tool_input = block.input if isinstance(block.input, dict) else {}
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
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool_output,
                        }
                    )

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            result.error = f"Exceeded {max_turns} turns"

        await asyncio.wait_for(_run_loop(), timeout=timeout)
    except asyncio.TimeoutError:
        result.error = f"Timeout after {timeout}s"
    except Exception as exc:
        result.error = f"Exception: {exc}"
    finally:
        await browser.close()

    result.wall_seconds = time.monotonic() - start
    return result
