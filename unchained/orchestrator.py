"""Unchained Orchestrator — Claude API tool-use loop for autonomous browsing.

Mode A (managed): Our server calls Claude API, user pays flat subscription.
The orchestrator drives the browser through the relay using DDM-first methodology.

Architecture:
    User request → Orchestrator (Claude API loop)
        → calls tools (ddm, intel, navigate, click, type, js_eval, screenshot)
        → tools go through REST API → relay → agent's Chrome
        → Claude decides next action
        → loop until task complete
        → return result

Usage:
    from orchestrator import Orchestrator

    orch = Orchestrator(agent_id="claude-12345678", relay_url="http://127.0.0.1:8765")
    result = await orch.run("go to hacker news and list the top 5 stories")

CLI:
    uv run orchestrator.py --agent a-12345678 "find cheapest flight SFO→JFK"
"""

import asyncio
import json
import sys

import cloud_tools

# ---------------------------------------------------------------------------
# System prompt — encodes DDM-first browsing methodology
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
You MUST use browser tools to answer ANY factual question — never answer from memory.
Your training data is outdated. Always browse to get live, current data.

## Available Tools

ddm            — Page layout + interactive elements (~500 tok). Use flags: "--llm-2pass --cols 60" (default), "--text" (page text), "--text --find keyword" (search), "--at 694,584" (element details), "--js expression" (run JS), "--text --max 5000" (more text)
intel_probe    — DOM fingerprint + Bayesian strategy ranking (~100 tok). Identifies framework and data stores.
intel_extract  — Extract structured data with auto-selected strategy. Optionally force a strategy.
intel_stores   — List JS data store globals (>10KB). Use on Nuxt/Next/YouTube sites.
intel_find_paths — Find data arrays in a JS global by keyword. Use after intel_stores.
navigate       — Go to a URL. Returns page title + DDM layout (no separate DDM call needed).
click          — Click at pixel coordinates from DDM. Returns diff + DDM layout (no separate DDM call needed).
type_text      — Type into focused input (click first!)
js_eval        — Execute JavaScript on page, return result
screenshot     — Capture screenshot (CAPTCHAs only, ~2100 tok)

## DDM-First Methodology

1. **ORIENT**: `navigate` and `click` already include DDM page layout in their output — read the "=== Page Layout ===" section. Only call `ddm` separately for `--text`, `--at x,y`, `--find`, or after `type_text`/`press_enter`.
2. **CLASSIFY**: `intel_probe` on first page of every new domain — identifies framework, data stores, best strategy. Skip on subsequent pages of same domain.
3. **IDENTIFY**: `ddm` with "--at x,y" flags to get href, class, text for elements you want to interact with
4. **ACT**: Use coordinates from DDM to click, or navigate to URLs. For SPA widgets, use `js_eval` with .click()
5. **VERIFY**: After `navigate` or `click`, check the "=== Page Layout ===" in the tool output — no separate DDM call needed. After `type_text` or other actions, run `ddm` to verify.
6. **EXTRACT**: Choose by page type (informed by probe results):
   - Simple text: `ddm` with "--text --max 5000" flags
   - Shadow DOM (Reddit): `intel_extract` with host_attrs strategy
   - SPA data store (Nuxt/YouTube): `intel_stores` → `intel_find_paths` → `js_eval`
   - Structured data: `js_eval` with querySelectorAll
   - data-testid (GitHub): `intel_extract` with data_testid strategy

## Key Rules
- ALWAYS use tools. NEVER answer from memory or fabricate data.
- navigate and click already return DDM layout — do NOT call ddm separately after them. Only call ddm after type_text, press_enter, or submit_form.
- type_text auto-focuses the single visible input. Click first if multiple inputs exist.
- SPA widgets: CDP clicks often fail — use `js_eval` with .click() instead.
- DDM only sees current viewport — scroll + remap for content below fold.
- NEVER take a screenshot unless DDM can't answer the question (CAPTCHAs, visual state, images).
- Wait at least 1 second between interactive actions (anti-bot protection).
- If an action fails silently (DDM shows same page), try a different approach.
- On the FIRST page of every new domain, run `intel_probe` right after ddm. If js_global >50%, use intel_stores → intel_find_paths → js_eval. If host_attrs >50%, use intel_extract. If data_testid >40%, use intel_extract with data_testid strategy. Otherwise stick with DDM. Skip probe on subsequent pages of the same domain.
- Be concise — report findings, not process."""

# ---------------------------------------------------------------------------
# Tool definitions for Claude API
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "ddm",
        "description": (
            "DOM Density Map — page layout + interactive elements (~500 tokens). "
            "Use FIRST on every page. Default flags: '--llm-2pass --cols 60'. "
            "Other useful flags: '--text' (page text), '--text --find keyword' "
            "(search text), '--at 694,584' (element at coords), '--forms' "
            "(detect forms), '--js expression' (run JavaScript)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flags": {
                    "type": "string",
                    "description": "DDM flags as a single string",
                    "default": "--llm-2pass --cols 60",
                },
                "tab_id": {
                    "type": "string",
                    "description": "Tab ID (default: auto)",
                    "default": "auto",
                },
            },
        },
    },
    {
        "name": "intel_probe",
        "description": (
            "Page intelligence probe — DOM fingerprint + Bayesian strategy "
            "ranking (~100 tokens). Identifies framework, data stores, shadow DOM. "
            "Run on first visit to unknown SPAs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "default": "auto"},
            },
        },
    },
    {
        "name": "intel_extract",
        "description": (
            "Extract structured data using auto-selected strategy. "
            "Strategies: innerText, host_attrs, js_global, react_fiber, "
            "data_testid, heading_hier, img_alt, shadow_pierce."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "description": "Force a specific strategy (optional)",
                    "default": "",
                },
                "tab_id": {"type": "string", "default": "auto"},
            },
        },
    },
    {
        "name": "intel_stores",
        "description": (
            "List JS data store globals (>10KB) on the current page. "
            "Use on Nuxt/Next/YouTube sites to discover data stores "
            "before extracting with intel_find_paths + js_eval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "default": "auto"},
            },
        },
    },
    {
        "name": "intel_find_paths",
        "description": (
            "Find data arrays in a JS global by keyword. "
            "Example: global='__NUXT__', key='deals' finds paths to deal arrays. "
            "Use after intel_stores to locate data, then js_eval to extract values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "global_name": {
                    "type": "string",
                    "description": "JS global variable name (e.g. '__NUXT__', 'ytInitialData')",
                },
                "key": {
                    "type": "string",
                    "description": "Keyword to search for in the data tree",
                },
                "tab_id": {"type": "string", "default": "auto"},
            },
            "required": ["global_name", "key"],
        },
    },
    {
        "name": "navigate",
        "description": (
            "Navigate the browser to a URL. Returns page title plus DDM "
            "page layout with all interactive elements and coordinates. "
            "No separate ddm call needed after navigate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "tab_id": {"type": "string", "default": "auto"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click at pixel coordinates. Returns what was clicked, a "
            "'--- changed ---' diff, plus DDM page layout with all interactive "
            "elements and coordinates. No separate ddm call needed after click. "
            "'--- no change ---' means the click had no effect."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X pixel coordinate"},
                "y": {"type": "integer", "description": "Y pixel coordinate"},
                "tab_id": {"type": "string", "default": "auto"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text into the focused input. Auto-focuses the single visible "
            "input if none is focused. Returns '--- changed ---' diff with new elements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "tab_id": {"type": "string", "default": "auto"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "js_eval",
        "description": (
            "Execute JavaScript on the page. Returns JSON for objects/arrays, "
            "raw for primitives. Use for: SPA widget interaction, structured data "
            "extraction with querySelectorAll, reading data stores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "JavaScript expression"},
                "tab_id": {"type": "string", "default": "auto"},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Capture screenshot (~2100 tokens). Use ONLY for: CAPTCHAs, visual "
            "state verification, image content. Prefer DDM for everything else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "default": "auto"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class Orchestrator:
    """Claude API tool-use loop for autonomous browser tasks."""

    def __init__(self, agent_id: str, relay_url: str = "http://127.0.0.1:8765",
                 model: str = "claude-sonnet-4-6",
                 max_turns: int = 50):
        """Initialize the orchestrator.

        Args:
            agent_id: Agent ID connected to the relay.
            relay_url: Relay server URL (not used directly — tools use cloud_tools).
            model: Claude model to use for the tool-use loop.
            max_turns: Maximum number of tool-use turns before stopping.
        """
        import anthropic
        self.client = anthropic.Anthropic()
        self.agent_id = agent_id
        self.relay_url = relay_url
        self.model = model
        self.max_turns = max_turns

    async def run(self, task: str) -> str:
        """Execute a browser task using Claude tool-use loop.

        Returns the final text response from Claude.
        """
        messages = [{"role": "user", "content": task}]

        for _ in range(self.max_turns):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # If Claude is done (no tool calls), return the text
            if response.stop_reason == "end_turn":
                text_parts = [b.text for b in response.content
                              if hasattr(b, "text")]
                return "\n".join(text_parts) if text_parts else ""

            # Execute tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await self._execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        return "Task exceeded maximum turns. Partial results may be in the conversation."

    async def _execute_tool(self, name: str, input_data: dict) -> str:
        """Execute a tool call against the agent's Chrome."""
        tab_id = input_data.get("tab_id", "auto")

        try:
            if name == "ddm":
                flags = input_data.get("flags", "--llm-2pass --cols 60")
                return await cloud_tools.run_ddm(
                    self.agent_id, tab_id, flags.split())

            elif name == "intel_probe":
                return await cloud_tools.run_intel(
                    self.agent_id, tab_id, ["--probe"])

            elif name == "intel_extract":
                flags = ["--extract"]
                strategy = input_data.get("strategy", "")
                if strategy:
                    flags += ["--strategy", strategy]
                return await cloud_tools.run_intel(
                    self.agent_id, tab_id, flags)

            elif name == "intel_stores":
                return await cloud_tools.run_intel(
                    self.agent_id, tab_id, ["--stores"])

            elif name == "intel_find_paths":
                global_name = input_data["global_name"]
                key = input_data["key"]
                return await cloud_tools.run_intel(
                    self.agent_id, tab_id,
                    ["--find-paths", global_name, key])

            elif name == "navigate":
                return await cloud_tools.navigate(
                    self.agent_id, tab_id, input_data["url"])

            elif name == "click":
                return await cloud_tools.click(
                    self.agent_id, tab_id,
                    input_data["x"], input_data["y"])

            elif name == "type_text":
                return await cloud_tools.type_text(
                    self.agent_id, tab_id, input_data["text"])

            elif name == "js_eval":
                return await cloud_tools.run_js(
                    self.agent_id, tab_id, input_data["expression"])

            elif name == "screenshot":
                data = await cloud_tools.screenshot(self.agent_id, tab_id)
                # Return truncated indicator — actual image would go via content block
                return f"[screenshot captured, {len(data)} bytes base64]"

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            return f"Tool error ({name}): {e}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    agent_id = None
    model = "claude-sonnet-4-6"
    max_turns = 50
    task_parts = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--agent" and i + 1 < len(args):
            agent_id = args[i + 1]
            i += 2
        elif args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--max-turns" and i + 1 < len(args):
            max_turns = int(args[i + 1])
            i += 2
        elif args[i] in ("--help", "-h"):
            print("""Usage: uv run orchestrator.py --agent <agent_id> "task description"

Options:
    --agent <id>         Agent ID (required)
    --model <model>      Claude model (default: claude-sonnet-4-6)
    --max-turns <n>      Max tool-use turns (default: 50)
""")
            return
        else:
            task_parts.append(args[i])
            i += 1

    if not agent_id:
        print("Error: --agent is required", file=sys.stderr)
        sys.exit(1)

    task = " ".join(task_parts)
    if not task:
        print("Error: task description is required", file=sys.stderr)
        sys.exit(1)

    orch = Orchestrator(agent_id=agent_id, model=model, max_turns=max_turns)
    result = asyncio.run(orch.run(task))
    print(result)


if __name__ == "__main__":
    main()
