"""agent_orchestrator.py — Claude API orchestrator agent for benchmark.

Runs an instrumented Claude API tool-use loop using the same SYSTEM_PROMPT
and TOOLS from orchestrator.py, dispatching tool calls through cloud_tools.

This tests the full deployed path:
    Claude API → cloud_tools → private_core_client → HTTP → private_core_engine
    → CDP via relay → Chrome

Requires: ANTHROPIC_API_KEY env var.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import anthropic

import cloud_tools
from orchestrator import SYSTEM_PROMPT, TOOLS

# Graceful import — benchmark works without private core overlay
try:
    from benchmark.intermediate_goal import (
        build_intermediate_goal_contract,
        estimate_hardness,
        should_enable_intermediate_goal,
    )
    _HAS_INTERMEDIATE_GOAL = True
except RuntimeError:
    _HAS_INTERMEDIATE_GOAL = False


class TaskResult:
    def __init__(self):
        self.answer: str = ""
        self.tool_calls: int = 0
        self.turns: int = 0
        self.loops: int = 0
        self.wall_seconds: float = 0.0
        self.error: str = ""
        self.tool_log: list[dict] = []
        self.intermediate_goal_mode: str = "off"
        self.intermediate_goal_enabled: bool = False
        self.hardness: dict = {}
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "tool_calls": self.tool_calls,
            "turns": self.turns,
            "loops": self.loops,
            "wall_seconds": round(self.wall_seconds, 1),
            "error": self.error,
            "intermediate_goal_mode": self.intermediate_goal_mode,
            "intermediate_goal_enabled": self.intermediate_goal_enabled,
            "hardness": self.hardness,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


async def _execute_tool(agent_id: str, name: str, input_data: dict) -> str:
    """Execute a tool call against the agent's Chrome via cloud_tools."""
    tab_id = input_data.get("tab_id", "auto")

    try:
        if name == "ddm":
            flags = input_data.get("flags", "--llm-2pass --cols 60")
            return await cloud_tools.run_ddm(agent_id, tab_id, flags.split())

        elif name == "intel_probe":
            return await cloud_tools.run_intel(agent_id, tab_id, ["--probe"])

        elif name == "intel_extract":
            flags = ["--extract"]
            strategy = input_data.get("strategy", "")
            if strategy:
                flags += ["--strategy", strategy]
            return await cloud_tools.run_intel(agent_id, tab_id, flags)

        elif name == "intel_stores":
            return await cloud_tools.run_intel(agent_id, tab_id, ["--stores"])

        elif name == "intel_find_paths":
            global_name = input_data["global_name"]
            key = input_data["key"]
            return await cloud_tools.run_intel(
                agent_id, tab_id,
                ["--find-paths", global_name, key])

        elif name == "navigate":
            return await cloud_tools.navigate(agent_id, tab_id, input_data["url"])

        elif name == "click":
            return await cloud_tools.click(
                agent_id, tab_id,
                input_data["x"], input_data["y"])

        elif name == "type_text":
            return await cloud_tools.type_text(
                agent_id, tab_id, input_data["text"])

        elif name == "js_eval":
            return await cloud_tools.run_js(
                agent_id, tab_id, input_data["expression"])

        elif name == "screenshot":
            data = await cloud_tools.screenshot(agent_id, tab_id)
            return f"[screenshot captured, {len(data)} bytes base64]"

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"


async def run_task(
    task: dict,
    model: str | None = None,
    max_turns: int = 50,
    intermediate_goal_mode: str = "off",
    agent_id: str | None = None,
) -> TaskResult:
    """Run a single benchmark task through the Claude API orchestrator.

    Args:
        task: Task dict from tasks.jsonl.
        model: Claude model ID (default: 'claude-sonnet-4-6').
        max_turns: Maximum tool-use turns.
        intermediate_goal_mode: 'off', 'on', or 'auto'.
        agent_id: CDP agent ID (required — from CLI args or env).

    Returns:
        TaskResult with answer, metrics, and any errors.
    """
    orch_model = model or "claude-sonnet-4-6"
    result = TaskResult()
    timeout = task.get("timeout_seconds", 120)

    if not agent_id:
        result.error = "agent_id is required for orchestrator mode"
        return result

    # Intermediate goal support (graceful degradation)
    if _HAS_INTERMEDIATE_GOAL:
        hardness_estimate = estimate_hardness(task)
        enable_intermediate_goal = should_enable_intermediate_goal(
            intermediate_goal_mode,
            hardness_estimate.score,
        )
        result.intermediate_goal_mode = (intermediate_goal_mode or "off").lower()
        result.intermediate_goal_enabled = enable_intermediate_goal
        result.hardness = hardness_estimate.to_dict()
    else:
        enable_intermediate_goal = False
        result.intermediate_goal_mode = "off"

    task_prompt = task["task"]
    if enable_intermediate_goal and _HAS_INTERMEDIATE_GOAL:
        contract = build_intermediate_goal_contract(task, hardness_estimate)
        task_prompt = f"{task_prompt}\n\n{contract}"

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task_prompt}]

    # Track tool calls for loop detection
    recent_tools: list[str] = []

    start = time.monotonic()

    try:
        async def _run_loop():
            for turn in range(max_turns):
                response = client.messages.create(
                    model=orch_model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )

                # Accumulate token usage
                if hasattr(response, "usage") and response.usage:
                    result.prompt_tokens += response.usage.input_tokens
                    result.completion_tokens += response.usage.output_tokens
                    result.total_tokens += (
                        response.usage.input_tokens + response.usage.output_tokens
                    )

                result.turns = turn + 1

                # If Claude is done (no tool calls), extract text
                if response.stop_reason == "end_turn":
                    text_parts = [b.text for b in response.content
                                  if hasattr(b, "text")]
                    result.answer = "\n".join(text_parts) if text_parts else ""
                    return

                # Execute tool calls
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input if isinstance(block.input, dict) else {}
                        call_sig = f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"

                        result.tool_calls += 1
                        result.tool_log.append({
                            "turn": result.tool_calls,
                            "tool": tool_name,
                            "input": str(tool_input)[:200],
                        })

                        # Loop detection
                        recent_tools.append(call_sig)
                        if len(recent_tools) > 6:
                            recent_tools.pop(0)
                        if recent_tools.count(call_sig) >= 3:
                            result.loops += 1

                        tool_result = await _execute_tool(
                            agent_id, tool_name, tool_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool_result,
                        })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            # Exceeded max turns
            result.answer = ""
            result.error = f"Exceeded {max_turns} turns"

        await asyncio.wait_for(_run_loop(), timeout=timeout)

    except asyncio.TimeoutError:
        result.error = f"Timeout after {timeout}s"
    except Exception as e:
        result.error = f"Exception: {e}"

    result.wall_seconds = time.monotonic() - start
    return result
