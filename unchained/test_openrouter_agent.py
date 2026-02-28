"""test_openrouter_agent.py — Test OpenRouter LLM with CDP/DDM browser toolset.

Tests whether a given OpenRouter model supports function calling and can
drive Chrome via CDP tools (ddm, navigate, click, js_eval, etc.).

Two modes:
  --local  Direct local Chrome via CDP (default). Requires Chrome running
           with --remote-debugging-port=9222. No relay needed.
  --relay  Cloud relay mode. Requires chrome_bridge / agent connected
           and CDP_AGENT_ID / CDP_RELAY_HOST / CDP_RELAY_PORT env vars.

Requires:
- OPENROUTER_API_KEY env var (or source the .env from openrouter_test/)
- Local Chrome running with CDP (--remote-debugging-port=9222) for --local mode

Usage:
    cd ~/Projects/unchained/unchained
    OPENROUTER_API_KEY=sk-or-... uv run python test_openrouter_agent.py
    OPENROUTER_API_KEY=... uv run python test_openrouter_agent.py --task "What's on HN?"
    OPENROUTER_API_KEY=... uv run python test_openrouter_agent.py --model google/gemma-3-27b-it:free
    OPENROUTER_API_KEY=... uv run python test_openrouter_agent.py --model meta-llama/llama-3.3-70b-instruct:free
"""

import argparse
import asyncio
import json
import os
import sys

import httpx

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "arcee-ai/trinity-large-preview:free"
DEFAULT_TASK = (
    "Navigate to https://news.ycombinator.com and list the top 5 story titles using DDM."
)
CWD = os.path.dirname(os.path.abspath(__file__))
MAX_TURNS = 20

# ---------------------------------------------------------------------------
# System prompt — same DDM-first methodology as orchestrator.py
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
You MUST use browser tools to answer ANY factual question — never answer from memory.
Your training data is outdated. Always browse to get live, current data.

## DDM-First Methodology
1. ORIENT: Call ddm (flags: "--llm-2pass --cols 60") on every new page
2. CLASSIFY: Call intel_probe on first page of every new domain
3. IDENTIFY: Call ddm with "--at x,y" to get href/class/text for specific elements
4. ACT: Use coordinates from DDM to click, or navigate to URLs
5. VERIFY: navigate and click return page layout inline — only call ddm after type_text or for --text/--at/--find
6. EXTRACT: Choose method based on page type

## Key Rules
- ALWAYS use tools. NEVER answer from memory or fabricate data.
- navigate and click return page layout inline — do NOT call ddm separately after them.
- Click input fields before typing.
- SPA widgets: CDP clicks often fail — use js_eval with .click() instead.
- DDM only sees current viewport — scroll + remap for content below fold.
- Be concise — report findings, not process."""

# ---------------------------------------------------------------------------
# Tool definitions in OpenAI function format
# (converted from Anthropic input_schema used in orchestrator.py)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ddm",
            "description": (
                "DOM Density Map — page layout + interactive elements (~500 tokens). "
                "Use FIRST on every page. Default flags: '--llm-2pass --cols 60'. "
                "Other flags: '--text' (page text), '--text --find keyword' (search), "
                "'--at 694,584' (element at pixel coords), '--js expression' (run JS), "
                "'--text --max 5000' (more text)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flags": {
                        "type": "string",
                        "description": "DDM flags as a single string",
                        "default": "--llm-2pass --cols 60",
                    },
                    "tab_id": {
                        "type": "string",
                        "description": "Tab ID prefix for targeting a specific tab (default: auto)",
                        "default": "auto",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intel_probe",
            "description": (
                "Page intelligence probe — DOM fingerprint + Bayesian strategy ranking (~100 tokens). "
                "Identifies framework (Nuxt/Next/React), data stores, shadow DOM. "
                "Run on first visit to every new domain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intel_extract",
            "description": (
                "Extract structured data using auto-selected strategy. "
                "Strategies: innerText, host_attrs, js_global, react_fiber, "
                "data_testid, heading_hier, img_alt, shadow_pierce."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "description": "Force a specific strategy (optional, leave blank for auto)",
                        "default": "",
                    },
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": (
                "Click at pixel coordinates. Get coordinates from DDM output. "
                "Returns page layout inline — no separate DDM call needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X pixel coordinate"},
                    "y": {"type": "integer", "description": "Y pixel coordinate"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the focused element. Click on an input field first to give it focus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "js_eval",
            "description": (
                "Execute JavaScript on the page. Returns JSON for objects/arrays, "
                "raw for primitives. Use for SPA widget interaction, querySelectorAll "
                "data extraction, reading JS data stores."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "JavaScript expression"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Capture screenshot (~2100 tokens). Use ONLY for CAPTCHAs, "
                "visual state verification, or image content. Prefer DDM for everything else."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution — two modes
# ---------------------------------------------------------------------------


async def _run_subprocess(cmd: list, timeout: int = 30) -> str:
    """Run a subprocess in CWD and return stdout (or stderr on failure)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CWD,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode().strip()
        err = stderr.decode().strip()
        if proc.returncode != 0 and not output:
            return f"Tool error (exit {proc.returncode}): {err[:300] or '(no stderr)'}"
        return output or err or "(no output)"
    except asyncio.TimeoutError:
        return f"Tool timeout: command took >{timeout}s"
    except Exception as e:
        return f"Tool exception: {e}"


async def execute_tool_local(name: str, args: dict) -> str:
    """Execute a CDP tool using ddm.py / intel.py directly (local Chrome, no relay).

    Local Chrome must be running with --remote-debugging-port=9222.
    Navigate returns the DDM map of the destination page.
    Click and type_text use JS injection via ddm.py --js.
    """
    tab_id = args.get("tab_id", "auto")
    tab_flags = ["--tab", tab_id] if tab_id and tab_id != "auto" else []

    if name == "ddm":
        flags_str = args.get("flags", "--llm-2pass --cols 60")
        flags = flags_str.split()
        cmd = ["uv", "run", "python", "ddm.py"] + flags + tab_flags

    elif name == "intel_probe":
        cmd = ["uv", "run", "python", "intel.py", "--probe"] + tab_flags

    elif name == "intel_extract":
        cmd = ["uv", "run", "python", "intel.py", "--extract"]
        if strategy := args.get("strategy"):
            cmd += ["--strategy", strategy]
        cmd += tab_flags

    elif name == "intel_stores":
        cmd = ["uv", "run", "python", "intel.py", "--stores"] + tab_flags

    elif name == "navigate":
        url = args.get("url", "")
        if not url:
            return "Error: url is required for navigate"
        # ddm.py navigates then maps the page; --new opens a tab if none exists
        cmd = ["uv", "run", "python", "ddm.py", "--new", "--llm-2pass", "--cols", "60", url] + tab_flags

    elif name == "click":
        x, y = args.get("x", 0), args.get("y", 0)
        expr = (
            f"(()=>{{const el=document.elementFromPoint({x},{y});"
            f"if(el){{el.dispatchEvent(new MouseEvent('click',{{bubbles:true,cancelable:true}}))"
            f";return 'clicked '+el.tagName+' at {x},{y}'}}return 'no element at {x},{y}'}})()"
        )
        cmd = ["uv", "run", "python", "ddm.py", "--js", expr] + tab_flags

    elif name == "type_text":
        # Escape single quotes in text
        text = args.get("text", "").replace("\\", "\\\\").replace("'", "\\'")
        expr = (
            f"(()=>{{const el=document.activeElement;"
            f"if(el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA')){{"
            f"el.value='{text}';"
            f"el.dispatchEvent(new Event('input',{{bubbles:true}}));"
            f"el.dispatchEvent(new Event('change',{{bubbles:true}}));"
            f"return 'typed into '+el.tagName}}return 'no input focused'}})()"
        )
        cmd = ["uv", "run", "python", "ddm.py", "--js", expr] + tab_flags

    elif name == "js_eval":
        expr = args.get("expression", "")
        if not expr:
            return "Error: expression is required for js_eval"
        cmd = ["uv", "run", "python", "ddm.py", "--js", expr] + tab_flags

    elif name == "screenshot":
        cmd = ["uv", "run", "python", "engage_cdp.py", "screenshot"] + tab_flags

    else:
        return f"Unknown tool: {name}"

    return await _run_subprocess(cmd, timeout=30)


async def execute_tool_relay(name: str, args: dict) -> str:
    """Execute a CDP tool via cdp_tool.py through the relay.

    Requires agent connected via chrome_bridge + CDP_AGENT_ID / CDP_RELAY_HOST /
    CDP_RELAY_PORT env vars (or defaults to a-7fba49f4 / api.unchainedsky.com).
    """
    tab_id = args.get("tab_id", "auto")
    tab_flags = ["--tab", tab_id] if tab_id and tab_id != "auto" else []

    if name == "ddm":
        flags_str = args.get("flags", "--llm-2pass --cols 60")
        flags = flags_str.split()
        cmd = ["uv", "run", "python", "cdp_tool.py", "ddm"] + flags + tab_flags

    elif name == "intel_probe":
        cmd = ["uv", "run", "python", "cdp_tool.py", "intel", "--probe"] + tab_flags

    elif name == "intel_extract":
        cmd = ["uv", "run", "python", "cdp_tool.py", "intel", "--extract"]
        if strategy := args.get("strategy"):
            cmd += ["--strategy", strategy]
        cmd += tab_flags

    elif name == "intel_stores":
        cmd = ["uv", "run", "python", "cdp_tool.py", "intel", "--stores"] + tab_flags

    elif name == "navigate":
        url = args.get("url", "")
        if not url:
            return "Error: url is required for navigate"
        cmd = ["uv", "run", "python", "cdp_tool.py", "navigate", url] + tab_flags

    elif name == "click":
        x, y = args.get("x", 0), args.get("y", 0)
        cmd = ["uv", "run", "python", "cdp_tool.py", "click", str(x), str(y)] + tab_flags

    elif name == "type_text":
        text = args.get("text", "")
        cmd = ["uv", "run", "python", "cdp_tool.py", "type", text] + tab_flags

    elif name == "js_eval":
        expr = args.get("expression", "")
        if not expr:
            return "Error: expression is required for js_eval"
        cmd = ["uv", "run", "python", "cdp_tool.py", "js", expr] + tab_flags

    elif name == "screenshot":
        cmd = ["uv", "run", "python", "cdp_tool.py", "screenshot"] + tab_flags

    else:
        return f"Unknown tool: {name}"

    return await _run_subprocess(cmd, timeout=30)


# Populated at startup based on --local / --relay flag
_use_local_mode: bool = True


async def execute_tool(name: str, args: dict) -> str:
    if _use_local_mode:
        return await execute_tool_local(name, args)
    return await execute_tool_relay(name, args)


# ---------------------------------------------------------------------------
# OpenRouter API call
# ---------------------------------------------------------------------------


async def call_openrouter(
    client: httpx.AsyncClient,
    model: str,
    messages: list,
) -> dict:
    """Call OpenRouter chat completions with tool definitions."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 2048,
    }
    resp = await client.post(
        OPENROUTER_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/unchainedsky/unchained",
            "X-Title": "Unchained Browser Agent Test",
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------


async def run_agent(model: str, task: str):
    """Run the OpenRouter agent loop for a given task."""
    print(f"\nModel  : {model}")
    print(f"Task   : {task}")
    print("-" * 60)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    tool_calls_made = 0

    async with httpx.AsyncClient() as client:
        for turn in range(1, MAX_TURNS + 1):
            print(f"\n[Turn {turn}] Calling {model}...")

            try:
                response = await call_openrouter(client, model, messages)
            except httpx.HTTPStatusError as e:
                body = e.response.text[:400]
                print(f"API error {e.response.status_code}: {body}")
                return
            except Exception as e:
                print(f"Request failed: {e}")
                return

            # Debug: show raw response structure on first turn if no tool calls
            choice = response["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "")
            tool_calls = message.get("tool_calls") or []

            # Normalize tool_calls: ensure function.arguments is always a string.
            # Some models omit it for no-arg tools, which causes OpenRouter 400 errors
            # when the message is echoed back in subsequent requests.
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if fn.get("arguments") is None:
                        fn["arguments"] = "{}"

            # Append assistant message to history (with truncated tool results to cap context)
            messages.append(message)

            if not tool_calls:
                content = message.get("content") or ""
                print(f"\n[Final Answer] (finish_reason={finish_reason})")
                print(content)

                if tool_calls_made == 0:
                    print("\n" + "=" * 60)
                    print("WARNING: Model returned text without calling any tools.")
                    print("This model may not support function/tool calling.")
                    print(f"finish_reason: {finish_reason!r}")
                    usage = response.get("usage", {})
                    if usage:
                        print(f"Usage: {usage}")
                    # Show raw choice for diagnosis
                    print("\nRaw choice (for diagnosis):")
                    print(json.dumps(choice, indent=2)[:800])
                    print("=" * 60)
                else:
                    print(f"\nCompleted in {turn} turns, {tool_calls_made} tool calls.")
                return

            # Execute tool calls
            tool_results = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                args_preview = json.dumps(tool_args)
                print(f"  → {tool_name}({args_preview[:120]}{'...' if len(args_preview) > 120 else ''})")

                result = await execute_tool(tool_name, tool_args)
                result_preview = result[:200] + ("..." if len(result) > 200 else "")
                print(f"  ← {result_preview}")

                tool_calls_made += 1
                # Truncate large results to keep context manageable
                result_for_history = result if len(result) <= 4000 else result[:4000] + "\n...[truncated]"
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_for_history,
                })

            messages.extend(tool_results)

    print(f"\n[Max turns ({MAX_TURNS}) reached without final answer]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    global _use_local_mode

    parser = argparse.ArgumentParser(
        description="Test OpenRouter LLM with Unchained CDP/DDM browser toolset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Local Chrome mode (default) — requires Chrome with --remote-debugging-port=9222
  OPENROUTER_API_KEY=sk-or-... uv run python test_openrouter_agent.py

  # Relay mode — requires chrome_bridge connected to relay
  OPENROUTER_API_KEY=... CDP_AGENT_ID=a-xxx uv run python test_openrouter_agent.py --relay

  # Custom task
  OPENROUTER_API_KEY=... uv run python test_openrouter_agent.py --task "Go to reddit.com and get the top post title"

  # Try a different free model that supports function calling
  OPENROUTER_API_KEY=... uv run python test_openrouter_agent.py --model meta-llama/llama-3.3-70b-instruct:free
  OPENROUTER_API_KEY=... uv run python test_openrouter_agent.py --model google/gemma-3-27b-it:free

Note: Not all free models support function/tool calling. If the model returns
text without calling any tools, try a different model.
""",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter model ID (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--task",
        default=DEFAULT_TASK,
        help="Task to give the agent",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--local",
        action="store_true",
        default=True,
        help="Use local Chrome via CDP directly — ddm.py/intel.py (default)",
    )
    mode_group.add_argument(
        "--relay",
        action="store_true",
        help="Use relay mode via cdp_tool.py (requires chrome_bridge connected)",
    )
    args = parser.parse_args()

    _use_local_mode = not args.relay

    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY env var not set.", file=sys.stderr)
        print("  export OPENROUTER_API_KEY=sk-or-...", file=sys.stderr)
        print("  # or: export $(cat ~/Projects/openrouter_test/.env | xargs)", file=sys.stderr)
        sys.exit(1)

    mode = "local Chrome (ddm.py/intel.py)" if _use_local_mode else "relay (cdp_tool.py)"
    print(f"CDP mode: {mode}")
    asyncio.run(run_agent(args.model, args.task))


if __name__ == "__main__":
    main()
