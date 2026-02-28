"""agent_cli.py — Claude CLI agent executor for benchmark.

Spawns `claude -p --output-format stream-json` for each task, parses
stream-json stdout for tool_use events and final result. Collects metrics
(tool count, turns, wall time, final answer).

Uses cdp_tool.py commands (the deployed HTTP split path) instead of
direct ddm.py/intel.py subprocess calls.

Requires: `claude` CLI installed and authenticated (Claude Code).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

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

CWD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _build_system_prompt() -> str:
    """Build CLI agent system prompt using cdp_tool.py commands."""
    return f"""You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
You MUST use browser tools to answer ANY factual question — never answer from memory.
Your training data is outdated. Always browse to get live, current data.

IMPORTANT: Your working directory is already set. Run cdp_tool.py commands directly.
NEVER use `cd` — it is not available. All commands run from the correct directory automatically.

## Browser Tools (via Bash)

uv run python cdp_tool.py ddm --llm-2pass --cols 60    # Map page layout + interactive elements (~500 tok)
uv run python cdp_tool.py ddm --text                    # Extract page text (~3000 chars)
uv run python cdp_tool.py ddm --text --find keyword     # Search text on page
uv run python cdp_tool.py ddm --text --max 5000         # More text (custom char limit)
uv run python cdp_tool.py ddm --at 694,584              # Element details at pixel coordinates
uv run python cdp_tool.py ddm --js "expression"         # Execute JS on page, return JSON
uv run python cdp_tool.py navigate https://example.com  # Go to URL (returns page layout — no ddm needed)
uv run python cdp_tool.py click 500 300                 # Click at coordinates (returns page layout — no ddm needed)
uv run python cdp_tool.py type "search query"           # Type into focused input (click first!)
uv run python cdp_tool.py js "document.title"           # Run JavaScript on page
uv run python cdp_tool.py intel --probe                 # Page fingerprint + Bayesian strategy ranking
uv run python cdp_tool.py intel --extract               # Extract structured data (auto strategy)
uv run python cdp_tool.py intel --stores                # List JS data store globals
uv run python cdp_tool.py intel --find-paths GLOBAL key # Find data arrays in a global
uv run python cdp_tool.py screenshot                    # Screenshot (CAPTCHAs only, ~2100 tok)

## DDM-First Methodology

1. **ORIENT**: `navigate` and `click` return DDM page layout in their output (under "=== Page Layout ==="). Read that section — do NOT call `ddm` separately after navigate or click. If "=== Page Layout ===" is missing, run `ddm --llm-2pass --cols 60` as fallback. Only call `ddm` separately after `type`, for `--text`, `--at x,y`, `--find`, or `--js`.
2. **IDENTIFY**: `ddm --at x,y` to get href, class, text for elements you want to interact with
3. **CLASSIFY**: `intel --probe` on unknown SPAs — identifies framework and best extraction strategy
4. **ACT**: Use coordinates from DDM to click, or navigate to URLs. For SPA widgets, use `js` with .click()
5. **VERIFY**: After `navigate` or `click`, check the "=== Page Layout ===" section in the tool output. If it's missing, run `ddm --llm-2pass --cols 60`. After `type`, `press_enter`, or `submit_form`, always run `ddm` to verify.
6. **EXTRACT**: Choose by page type:
   - Simple text: `ddm --text --max 5000`
   - Shadow DOM (Reddit): `intel --extract`
   - SPA data store (Nuxt/YouTube): `intel --stores` → `intel --find-paths` → `js`
   - Structured data: `js` with querySelectorAll
   - data-testid (GitHub): `intel --extract --strategy data_testid`

## Key Rules
- ALWAYS use tools. NEVER answer from memory or fabricate data.
- navigate and click already return DDM page layout — do NOT call ddm separately after them. Only call ddm after type, or for --text, --at, --find, --js flags.
- Click input fields before typing.
- SPA widgets: CDP clicks often fail — use `js` with .click() instead.
- DDM only sees current viewport — scroll + remap for content below fold.
- Be concise — report findings, not process."""


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


async def run_task(
    task: dict,
    model: str | None = None,
    max_turns: int = 25,
    intermediate_goal_mode: str = "off",
    agent_id: str | None = None,
    relay_host: str | None = None,
    relay_port: int | None = None,
    private_core_url: str | None = None,
    private_core_token: str | None = None,
) -> TaskResult:
    """Run a single benchmark task through the Claude CLI agent.

    Spawns `claude -p` with stream-json output, feeds task prompt via stdin,
    and parses streaming events for metrics.

    Args:
        task: Task dict from tasks.jsonl.
        model: Claude model name ('opus', 'sonnet', 'haiku'). Default: 'sonnet'.
        max_turns: Maximum tool-use turns.
        intermediate_goal_mode: 'off', 'on', or 'auto'.
        agent_id: CDP agent ID (overrides CDP_AGENT_ID env var).
        relay_host: Relay host (overrides CDP_RELAY_HOST env var).
        relay_port: Relay port (overrides CDP_RELAY_PORT env var).
        private_core_url: Private core URL (overrides PRIVATE_CORE_URL env var).
        private_core_token: Private core token (overrides PRIVATE_CORE_TOKEN env var).

    Returns:
        TaskResult with answer, metrics, and any errors.
    """
    cli_model = model or "sonnet"
    result = TaskResult()
    timeout = task.get("timeout_seconds", 120)
    system_prompt = _build_system_prompt()

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

    # Build command
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", cli_model,
        "--max-turns", str(max_turns),
        "--allowedTools",
        'Bash(uv run python cdp_tool.py:*)',
        "--system-prompt", system_prompt,
        "--tools", "Bash",
    ]

    # Build subprocess env — strip CLAUDECODE, inject CDP config
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    # Resolve CDP config: CLI args > env vars > defaults
    _agent_id = agent_id or env.get("CDP_AGENT_ID", "")
    _relay_host = relay_host or env.get("CDP_RELAY_HOST", "127.0.0.1")
    _relay_port = str(relay_port) if relay_port else env.get("CDP_RELAY_PORT", "8765")
    _private_core_url = private_core_url or env.get("PRIVATE_CORE_URL", "")
    _private_core_token = private_core_token or env.get("PRIVATE_CORE_TOKEN", "")

    if _agent_id:
        env["CDP_AGENT_ID"] = _agent_id
    if _relay_host:
        env["CDP_RELAY_HOST"] = _relay_host
    if _relay_port:
        env["CDP_RELAY_PORT"] = _relay_port
    if _private_core_url:
        env["PRIVATE_CORE_URL"] = _private_core_url
    if _private_core_token:
        env["PRIVATE_CORE_TOKEN"] = _private_core_token

    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=CWD,
        )

        # Send task and close stdin
        proc.stdin.write(task_prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # Track tool names for loop detection
        recent_tools: list[str] = []

        # Parse stream-json events
        async def read_stream():
            async for raw_line in proc.stdout:
                line = raw_line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_input = block.get("input", {})
                            block_name = block.get("name", "")

                            if block_name == "Bash":
                                cmd_str = tool_input.get("command", "")
                                tool_name = _extract_tool_name(cmd_str)
                                call_sig = f"{tool_name}:{cmd_str}"
                            else:
                                tool_name = block_name.lower()
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

                elif etype == "usage":
                    # Per-turn usage events — accumulate
                    result.prompt_tokens += event.get("input_tokens", 0)
                    result.completion_tokens += event.get("output_tokens", 0)
                    result.total_tokens += (
                        event.get("input_tokens", 0) + event.get("output_tokens", 0)
                    )

                elif etype == "result":
                    result.answer = event.get("result", "")
                    result.turns = event.get("num_turns", 0)
                    # Final usage from result event (use if no per-turn usage was emitted)
                    usage = event.get("usage", {})
                    if usage and result.total_tokens == 0:
                        result.prompt_tokens = usage.get("input_tokens", 0)
                        result.completion_tokens = usage.get("output_tokens", 0)
                        result.total_tokens = result.prompt_tokens + result.completion_tokens

        # Run with timeout
        try:
            await asyncio.wait_for(read_stream(), timeout=timeout)
        except asyncio.TimeoutError:
            result.error = f"Timeout after {timeout}s"
            proc.terminate()

        await proc.wait()

        # Check for errors
        if proc.returncode and proc.returncode != 0 and not result.answer:
            stderr_text = (await proc.stderr.read()).decode().strip()
            result.error = f"Exit code {proc.returncode}: {stderr_text[:200]}"

    except FileNotFoundError:
        result.error = "claude CLI not found — install Claude Code first"
    except Exception as e:
        result.error = f"Exception: {e}"

    result.wall_seconds = time.monotonic() - start
    return result


def _extract_tool_name(cmd_str: str) -> str:
    """Extract a logical tool name from a cdp_tool.py Bash command.

    Examples:
        'uv run python cdp_tool.py navigate ...' -> 'navigate'
        'uv run python cdp_tool.py ddm --text'   -> 'ddm'
        'uv run python cdp_tool.py intel --probe' -> 'intel'
        'uv run python cdp_tool.py click 500 300' -> 'click'
        'uv run python cdp_tool.py screenshot'    -> 'screenshot'
    """
    if "cdp_tool.py" not in cmd_str:
        return "bash"

    # Find the subcommand after cdp_tool.py
    parts = cmd_str.split()
    try:
        idx = next(i for i, p in enumerate(parts) if "cdp_tool.py" in p)
        if idx + 1 < len(parts):
            subcmd = parts[idx + 1]
            # Known subcommands
            if subcmd in ("ddm", "intel", "navigate", "click", "type",
                          "js", "screenshot", "tabs", "new-tab", "close-tab"):
                return subcmd
    except StopIteration:
        pass

    return "cdp_tool"
