"""Claude CLI agent executor for benchmark."""

from __future__ import annotations

import asyncio
import json
import os
import time

from benchmark.common import BenchmarkConfig, TaskResult, extract_cdp_tool_name

try:
    from benchmark.intermediate_goal import (
        build_intermediate_goal_contract,
        estimate_hardness,
        should_enable_intermediate_goal,
    )

    _HAS_INTERMEDIATE_GOAL = True
except RuntimeError:
    _HAS_INTERMEDIATE_GOAL = False

CWD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _build_system_prompt() -> str:
    return """You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
You MUST use browser tools to answer ANY factual question and never answer from memory.
Your training data is outdated. Always browse to get live, current data.

IMPORTANT: Your working directory is already set. Run cdp_tool.py commands directly.
NEVER use `cd` and never try to manage tabs manually unless needed for the task.

## Browser Tools

uv run python cdp_tool.py ddm --llm-2pass --cols 60
uv run python cdp_tool.py ddm --text
uv run python cdp_tool.py ddm --text --find keyword
uv run python cdp_tool.py ddm --at 694,584
uv run python cdp_tool.py ddm --js "expression"
uv run python cdp_tool.py navigate "https://example.com"
uv run python cdp_tool.py click 500 300
uv run python cdp_tool.py type "search query"
uv run python cdp_tool.py js "document.title"
uv run python cdp_tool.py intel --probe
uv run python cdp_tool.py intel --extract
uv run python cdp_tool.py intel --stores
uv run python cdp_tool.py intel --find-paths GLOBAL key
uv run python cdp_tool.py screenshot

## DDM-First Methodology

1. ORIENT: navigate and click already return page layout inline. Only call ddm separately for --text, --at, --find, --js, or after type.
2. IDENTIFY: use ddm --at x,y for href/class/text.
3. CLASSIFY: use intel --probe on the first page of a new domain.
4. ACT: click with DDM coordinates or navigate directly to URLs.
5. VERIFY: after navigate/click, read the returned layout before issuing another ddm call.
6. EXTRACT: prefer direct URL patterns, then DDM/intel/JS as needed.

## Key Rules

- ALWAYS use tools.
- Click inputs before typing.
- SPA widgets often need js click handlers.
- Keep answers concise and factual.
"""


async def _terminate_process(proc: asyncio.subprocess.Process, grace_seconds: float = 5.0):
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def run_task(
    task: dict,
    *,
    config: BenchmarkConfig,
    model: str | None = None,
    max_turns: int = 25,
    intermediate_goal_mode: str = "off",
) -> TaskResult:
    """Run one task through Claude CLI."""
    result = TaskResult()
    cli_model = model or "sonnet"
    timeout = task.get("timeout_seconds", 120)
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

    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        cli_model,
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        "Bash(uv run python cdp_tool.py:*)",
        "--system-prompt",
        _build_system_prompt(),
        "--tools",
        "Bash",
    ]

    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=config.subprocess_env(),
            cwd=CWD,
        )

        assert proc.stdin is not None
        proc.stdin.write(task_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async def _read_stream():
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                if etype == "assistant":
                    msg = event.get("message", {}) or {}
                    for block in msg.get("content", []):
                        if block.get("type") != "tool_use":
                            continue
                        tool_input = block.get("input", {}) or {}
                        block_name = block.get("name", "")
                        if block_name == "Bash":
                            command = str(tool_input.get("command", "") or "")
                            tool_name = extract_cdp_tool_name(command)
                            call_sig = f"{tool_name}:{command}"
                        else:
                            tool_name = block_name.lower()
                            call_sig = None
                        result.record_tool_call(
                            tool_name,
                            tool_input if isinstance(tool_input, dict) else {"raw": str(tool_input)},
                            call_signature=call_sig,
                        )
                elif etype == "usage":
                    result.prompt_tokens += int(event.get("input_tokens", 0) or 0)
                    result.completion_tokens += int(event.get("output_tokens", 0) or 0)
                    result.total_tokens += int(event.get("input_tokens", 0) or 0)
                    result.total_tokens += int(event.get("output_tokens", 0) or 0)
                elif etype == "result":
                    result.answer = str(event.get("result", "") or "")
                    result.turns = int(event.get("num_turns", 0) or 0)
                    usage = event.get("usage") or {}
                    if usage and result.total_tokens == 0:
                        result.prompt_tokens = int(usage.get("input_tokens", 0) or 0)
                        result.completion_tokens = int(usage.get("output_tokens", 0) or 0)
                        result.total_tokens = result.prompt_tokens + result.completion_tokens

        try:
            await asyncio.wait_for(_read_stream(), timeout=timeout)
        except asyncio.TimeoutError:
            result.error = f"Timeout after {timeout}s"
            await _terminate_process(proc)

        await proc.wait()

        if proc.returncode and proc.returncode != 0 and not result.answer:
            assert proc.stderr is not None
            stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
            result.error = f"Exit code {proc.returncode}: {stderr_text[:400]}"

    except FileNotFoundError:
        result.error = "claude CLI not found"
    except Exception as exc:
        result.error = f"Exception: {exc}"

    result.wall_seconds = time.monotonic() - start
    return result
