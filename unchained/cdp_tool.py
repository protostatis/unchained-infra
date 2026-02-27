"""cdp_tool.py — CLI wrapper for cloud_tools over WSS relay.

Exposes CDP browser tools as simple CLI commands so `claude -p` can
drive the browser via its built-in Bash tool. One subprocess, one agent
loop — no manual turn management needed.

Usage:
    uv run python cdp_tool.py ddm --llm-2pass --cols 60
    uv run python cdp_tool.py ddm --text
    uv run python cdp_tool.py ddm --text --find keyword
    uv run python cdp_tool.py ddm --at 500,300
    uv run python cdp_tool.py navigate https://example.com
    uv run python cdp_tool.py click 500 300
    uv run python cdp_tool.py type "search query"
    uv run python cdp_tool.py js "document.title"
    uv run python cdp_tool.py screenshot
    uv run python cdp_tool.py intel --probe
    uv run python cdp_tool.py intel --extract

Environment variables (set by chat_agent_cli.py):
    CDP_AGENT_ID   — Agent ID (default: a-7fba49f4)
    CDP_RELAY_HOST — Relay hostname (default: api.unchainedsky.com)
    CDP_RELAY_PORT — Relay port (default: 443)
    CDP_TAB_ID     — Tab ID (default: auto)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_tools  # noqa: E402

AGENT_ID = os.environ.get("CDP_AGENT_ID", "claude-7fba49f4")
RELAY_HOST = os.environ.get("CDP_RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("CDP_RELAY_PORT", "443"))
TAB_ID = os.environ.get("CDP_TAB_ID", "auto")


async def main():
    if len(sys.argv) < 2:
        print("Usage: cdp_tool.py <command> [--tab <id>] [args...]")
        print("Commands: ddm, navigate, click, type, js, screenshot, intel, pdf")
        print("Use --tab <id> to target a specific tab (default: auto = first tab)")
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    # Parse --tab <id> from args (works on any command)
    tab_id = TAB_ID
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--tab" and i + 1 < len(args):
            tab_id = args[i + 1]
            i += 2
        else:
            filtered.append(args[i])
            i += 1
    args = filtered

    try:
        if cmd == "ddm":
            flags = args if args else ["--llm-2pass", "--cols", "60"]
            result = await cloud_tools.run_ddm(
                AGENT_ID, tab_id, flags, RELAY_HOST, RELAY_PORT)

        elif cmd == "navigate":
            if not args:
                print("Usage: cdp_tool.py navigate <url>", file=sys.stderr)
                sys.exit(1)
            result = await cloud_tools.navigate(
                AGENT_ID, tab_id, args[0], RELAY_HOST, RELAY_PORT)

        elif cmd == "click":
            if len(args) < 2:
                print("Usage: cdp_tool.py click <x> <y>", file=sys.stderr)
                sys.exit(1)
            result = await cloud_tools.click(
                AGENT_ID, tab_id, int(args[0]), int(args[1]),
                RELAY_HOST, RELAY_PORT)

        elif cmd == "type":
            if not args:
                print("Usage: cdp_tool.py type <text>", file=sys.stderr)
                sys.exit(1)
            result = await cloud_tools.type_text(
                AGENT_ID, tab_id, " ".join(args), RELAY_HOST, RELAY_PORT)

        elif cmd == "js":
            if not args:
                print("Usage: cdp_tool.py js <expression>", file=sys.stderr)
                sys.exit(1)
            result = await cloud_tools.run_js(
                AGENT_ID, tab_id, " ".join(args), RELAY_HOST, RELAY_PORT)

        elif cmd == "screenshot":
            data = await cloud_tools.screenshot(
                AGENT_ID, tab_id, RELAY_HOST, RELAY_PORT)
            # Save raw base64 to temp file for UI display;
            # print summary for Claude (raw base64 would waste tokens).
            import tempfile
            sc_path = os.path.join(tempfile.gettempdir(), "unchained_last_screenshot.b64")
            with open(sc_path, "w") as f:
                f.write(data)
            result = f"[screenshot captured: {len(data)} bytes, saved:{sc_path}]"

        elif cmd == "intel":
            flags = args if args else ["--probe"]
            result = await cloud_tools.run_intel(
                AGENT_ID, tab_id, flags, RELAY_HOST, RELAY_PORT)

        elif cmd == "pdf":
            flags = ["--pdf"] + args
            result = await cloud_tools.run_ddm(
                AGENT_ID, tab_id, flags, RELAY_HOST, RELAY_PORT)

        else:
            print(f"Unknown command: {cmd}", file=sys.stderr)
            sys.exit(1)

        print(result)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


asyncio.run(main())
