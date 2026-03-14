"""CLI agent executor for benchmark."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
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
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.1-codex-mini")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "low").strip().lower()


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


def _build_codex_prompt(task_prompt: str) -> str:
    return (
        "[SYSTEM PROMPT]\n"
        f"{_build_system_prompt()}\n"
        "[/SYSTEM PROMPT]\n\n"
        f"User request:\n{(task_prompt or '').strip()}\n"
    )


def _is_codex_cli_model(model: str | None) -> bool:
    return (model or "").startswith("codex-cli:")


def _resolve_codex_model(model: str | None) -> str:
    raw = (model or "").strip()
    if raw.startswith("codex-cli:"):
        resolved = raw.split(":", 1)[1].strip()
        return resolved or DEFAULT_CODEX_MODEL
    return DEFAULT_CODEX_MODEL


def _collect_text_strings(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            out.append(s)
        return out
    if isinstance(obj, list):
        for value in obj:
            out.extend(_collect_text_strings(value))
        return out
    if not isinstance(obj, dict):
        return out

    text = obj.get("text")
    if isinstance(text, str) and text.strip():
        out.append(text.strip())
    for key in ("content", "output_text", "parts"):
        if key in obj:
            out.extend(_collect_text_strings(obj.get(key)))
    return out


def _codex_tool_name_and_command(command: str) -> tuple[str, str]:
    cmd = (command or "").strip()
    shell_match = re.match(r"^/bin/(?:zsh|bash)\s+-lc\s+'(.+)'$", cmd, re.DOTALL)
    if shell_match:
        cmd = shell_match.group(1).strip()
    return extract_cdp_tool_name(cmd), cmd


async def _terminate_process(proc: asyncio.subprocess.Process, grace_seconds: float = 5.0):
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def _run_task_claude(
    task_prompt: str,
    *,
    config: BenchmarkConfig,
    model: str | None,
    max_turns: int,
    timeout: int | float,
) -> TaskResult:
    """Run one task through Claude CLI."""
    result = TaskResult()
    cli_model = model or "sonnet"

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
        "--mcp-config",
        '{"mcpServers":{}}',
        "--strict-mcp-config",
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

        # Map tool_use block id → index in result.tool_log for correct
        # output_preview attribution when a turn has multiple tool calls.
        _tool_use_id_to_log_idx: dict[str, int] = {}
        _pending_tool_use_ids: list[str] = []

        def _claim_tool_log_idx(use_id: str = "") -> int | None:
            if use_id:
                try:
                    _pending_tool_use_ids.remove(use_id)
                except ValueError:
                    pass
                return _tool_use_id_to_log_idx.get(use_id)
            while _pending_tool_use_ids:
                pending_id = _pending_tool_use_ids.pop(0)
                log_idx = _tool_use_id_to_log_idx.get(pending_id)
                if log_idx is not None:
                    return log_idx
            return None

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
                        use_id = block.get("id", "")
                        if use_id:
                            _tool_use_id_to_log_idx[use_id] = len(result.tool_log) - 1
                            _pending_tool_use_ids.append(use_id)
                elif etype == "user":
                    # Tool results arrive in user events (chat_agent_cli.py:1314).
                    # Bash stdout may be in event["tool_use_result"] directly,
                    # or in message.content tool_result blocks for other tools.
                    raw_result = event.get("tool_use_result")
                    if raw_result is not None:
                        if isinstance(raw_result, str):
                            result_text = raw_result
                        elif isinstance(raw_result, dict):
                            result_text = str(raw_result.get("stdout", "") or "")
                        else:
                            result_text = ""
                        if result_text:
                            log_idx = _claim_tool_log_idx()
                            if log_idx is None and len(result.tool_log) == 1:
                                log_idx = 0
                            if log_idx is not None and log_idx < len(result.tool_log):
                                result.tool_log[log_idx].setdefault(
                                    "output_preview", result_text[:600]
                                )
                    msg = event.get("message", {}) or {}
                    for block in msg.get("content", []):
                        if block.get("type") != "tool_result":
                            continue
                        content = block.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        preview = str(content)[:600] if content else None
                        use_id = block.get("tool_use_id", "")
                        log_idx = _claim_tool_log_idx(use_id) if use_id else None
                        if log_idx is None and not use_id and len(result.tool_log) == 1:
                            log_idx = 0
                        if preview and log_idx is not None and log_idx < len(result.tool_log):
                            result.tool_log[log_idx].setdefault("output_preview", preview)
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


async def _run_task_codex(
    task_prompt: str,
    *,
    config: BenchmarkConfig,
    model: str | None,
    timeout: int | float,
) -> TaskResult:
    """Run one task through Codex CLI."""
    result = TaskResult()
    codex_model = _resolve_codex_model(model)
    output_file = os.path.join(
        tempfile.gettempdir(),
        f"benchmark_codex_last_{os.getpid()}_{int(time.time() * 1000)}",
    )
    config_args: list[str] = []
    if CODEX_REASONING_EFFORT in {"low", "medium", "high"}:
        config_args = ["-c", f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"']

    cmd = [
        CODEX_BIN,
        "exec",
        *config_args,
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--output-last-message",
        output_file,
        "-C",
        CWD,
        "-m",
        codex_model,
        "-",
    ]

    start = time.monotonic()
    response = ""
    streamed_text = ""
    error_text = ""

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=config.subprocess_env(),
            cwd=CWD,
            start_new_session=True,
        )

        assert proc.stdin is not None
        proc.stdin.write(_build_codex_prompt(task_prompt).encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        # Map item id → index in result.tool_log for correct
        # output_preview attribution when a turn has multiple tool calls.
        _item_id_to_log_idx: dict[str, int] = {}

        async def _read_stream():
            nonlocal response, streamed_text, error_text
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
                if etype == "item.started":
                    item = event.get("item") or {}
                    item_type = str(item.get("type", "") or "").lower()
                    item_id = str(item.get("id", "") or "")
                    if item_type == "command_execution":
                        tool_name, command = _codex_tool_name_and_command(str(item.get("command", "") or ""))
                        result.record_tool_call(
                            tool_name,
                            {"command": command},
                            call_signature=f"{tool_name}:{command}",
                        )
                        if item_id:
                            _item_id_to_log_idx[item_id] = len(result.tool_log) - 1
                    elif "search" in item_type:
                        result.record_tool_call(
                            "websearch",
                            {"query": str(item.get("query", "") or "")[:200]},
                        )
                        if item_id:
                            _item_id_to_log_idx[item_id] = len(result.tool_log) - 1
                    continue

                if etype == "item.completed":
                    item = event.get("item") or {}
                    item_type = str(item.get("type", "") or "").lower()
                    if item_type in ("assistant_message", "message", "output_text"):
                        text_bits = _collect_text_strings(item)
                        if text_bits:
                            response = "\n".join(text_bits).strip()
                    elif item_type in ("command_execution", "web_search_call"):
                        output = str(item.get("aggregated_output") or item.get("output") or "")[:600]
                        item_id = str(item.get("id", "") or "")
                        log_idx = _item_id_to_log_idx.get(item_id)
                        if output and log_idx is not None and log_idx < len(result.tool_log):
                            result.tool_log[log_idx].setdefault("output_preview", output)
                    elif item_type == "error":
                        msg = item.get("message", "")
                        if isinstance(msg, str) and msg.strip():
                            error_text = msg.strip()
                    continue

                if etype == "item.delta":
                    delta_bits = _collect_text_strings(event.get("delta"))
                    if delta_bits:
                        chunk = "\n".join(delta_bits).strip()
                        if chunk:
                            if streamed_text:
                                streamed_text += "\n"
                            streamed_text += chunk
                    continue

                if etype == "turn.completed":
                    usage = event.get("usage") or {}
                    result.prompt_tokens += int(usage.get("input_tokens", 0) or 0)
                    result.completion_tokens += int(usage.get("output_tokens", 0) or 0)
                    result.total_tokens += int(usage.get("input_tokens", 0) or 0)
                    result.total_tokens += int(usage.get("output_tokens", 0) or 0)
                    bits = _collect_text_strings(event)
                    if bits:
                        maybe = "\n".join(bits).strip()
                        if maybe and len(maybe) > len(response):
                            response = maybe
                    continue

                if etype == "turn.failed":
                    err = event.get("error") or {}
                    msg = err.get("message", "") if isinstance(err, dict) else ""
                    if isinstance(msg, str) and msg.strip():
                        error_text = msg.strip()
                    continue

                if etype == "error":
                    msg = event.get("message", "")
                    if isinstance(msg, str) and msg.strip():
                        error_text = msg.strip()
                    continue

        try:
            await asyncio.wait_for(_read_stream(), timeout=timeout)
        except asyncio.TimeoutError:
            result.error = f"Timeout after {timeout}s"
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                await proc.wait()
        else:
            await proc.wait()

        if not response and os.path.exists(output_file):
            try:
                with open(output_file, "r", encoding="utf-8", errors="replace") as handle:
                    response = handle.read().strip()
            except Exception:
                pass

        if not response and streamed_text:
            response = streamed_text.strip()
        if response:
            result.answer = response
        elif error_text:
            result.error = error_text[:400]
        elif proc.returncode and proc.returncode != 0:
            assert proc.stderr is not None
            stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
            result.error = stderr_text[:400] or f"Exit code {proc.returncode}"

    except FileNotFoundError:
        result.error = "codex CLI not found"
    except Exception as exc:
        result.error = f"Exception: {exc}"
    finally:
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except Exception:
            pass

    result.wall_seconds = time.monotonic() - start
    return result


async def run_task(
    task: dict,
    *,
    config: BenchmarkConfig,
    model: str | None = None,
    max_turns: int = 25,
    intermediate_goal_mode: str = "off",
) -> TaskResult:
    """Run one task through Claude CLI or Codex CLI."""
    timeout = task.get("timeout_seconds", 120)
    task_prompt = task["task"]
    intermediate_goal_enabled = False
    intermediate_goal_mode_resolved = (intermediate_goal_mode or "off").lower()
    hardness: dict[str, object] = {}

    if _HAS_INTERMEDIATE_GOAL:
        hardness_estimate = estimate_hardness(task)
        intermediate_goal_enabled = should_enable_intermediate_goal(
            intermediate_goal_mode,
            hardness_estimate.score,
        )
        hardness = hardness_estimate.to_dict()
        if intermediate_goal_enabled:
            contract = build_intermediate_goal_contract(task, hardness_estimate)
            task_prompt = f"{task_prompt}\n\n{contract}"

    if _is_codex_cli_model(model):
        result = await _run_task_codex(
            task_prompt,
            config=config,
            model=model,
            timeout=timeout,
        )
    else:
        result = await _run_task_claude(
            task_prompt,
            config=config,
            model=model,
            max_turns=max_turns,
            timeout=timeout,
        )

    result.intermediate_goal_mode = intermediate_goal_mode_resolved if _HAS_INTERMEDIATE_GOAL else "off"
    result.intermediate_goal_enabled = intermediate_goal_enabled
    result.hardness = hardness
    return result
