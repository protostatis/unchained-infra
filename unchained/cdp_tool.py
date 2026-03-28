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
    uv run python cdp_tool.py press_enter
    uv run python cdp_tool.py submit_form
    uv run python cdp_tool.py js "document.title"
    uv run python cdp_tool.py screenshot
    uv run python cdp_tool.py intel --probe
    uv run python cdp_tool.py intel --extract
    uv run python cdp_tool.py rhythm_train https://example.com
    uv run python cdp_tool.py rhythm_catch https://example.com "find prices" "price,bed,sqft"
    uv run python cdp_tool.py rhythm_execute https://example.com '[{"action":"click","text":"Next"}]'
    uv run python cdp_tool.py rhythm_query list_all

Environment variables (set by chat_agent_cli.py):
    CDP_AGENT_ID   — Agent ID (default: a-7fba49f4)
    CDP_RELAY_HOST — Relay hostname (default: api.unchainedsky.com)
    CDP_RELAY_PORT — Relay port (default: 443)
    CDP_TAB_ID     — Tab ID (default: auto)
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_tools  # noqa: E402

AGENT_ID = os.environ.get("CDP_AGENT_ID", "claude-7fba49f4")
RELAY_HOST = os.environ.get("CDP_RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("CDP_RELAY_PORT", "443"))
TAB_ID = os.environ.get("CDP_TAB_ID", "auto")


def _decode_type_text_arg(text: str) -> str:
    """Decode common newline escape aliases used by CLI agents."""
    aliases = {
        "/n": "\n",
        "/r": "\r",
        "/r/n": "\r\n",
        "/rn": "\r\n",
        r"\n": "\n",
        r"\r": "\r",
        r"\r\n": "\r\n",
    }
    if text in aliases:
        return aliases[text]
    return text.replace(r"\r\n", "\r\n").replace(r"\n", "\n").replace(r"\r", "\r")


def _parse_flags(args: list, known: dict, positional: int = 0) -> dict:
    """Parse known --flags from args, warn on unknown ones.

    Args:
        args: Argument list after positional args are consumed.
        known: Map of flag name (without --) to default value.
        positional: Number of leading positional args to skip.
    Returns:
        dict with parsed values. Positional args under '_pos'.
    """
    result = {k: v for k, v in known.items()}
    result["_pos"] = args[:positional]
    i = positional
    while i < len(args):
        flag = args[i]
        if flag.startswith("--"):
            name = flag[2:]
            if name in known and i + 1 < len(args):
                result[name] = args[i + 1]
                i += 2
            else:
                print(f"Warning: unknown flag '{flag}'", file=sys.stderr)
                i += 1
        else:
            print(f"Warning: unexpected argument '{args[i]}'", file=sys.stderr)
            i += 1
    return result


async def main():
    if len(sys.argv) < 2:
        print("Usage: cdp_tool.py <command> [--tab <id>] [args...]")
        print("Commands: ddm, navigate, click, type, press_enter, submit_form, js, screenshot, intel, pdf, rhythm_train, rhythm_catch, rhythm_execute, rhythm_query")
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

    # When the session uses a provisioned Chrome, keep "auto" within
    # the same provision slot so the agent doesn't fall back to the
    # default/guest Chrome.
    if tab_id == "auto" and TAB_ID.startswith("prov-"):
        # TAB_ID format: prov-{slot}-{real_id}
        # split("-", 2) → ["prov", slot, real_id]
        parts = TAB_ID.split("-", 2)
        if len(parts) >= 3 and parts[1]:
            tab_id = f"prov-{parts[1]}-auto"

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
                AGENT_ID,
                tab_id,
                _decode_type_text_arg(" ".join(args)),
                RELAY_HOST,
                RELAY_PORT,
            )

        elif cmd == "press_enter":
            result = await cloud_tools.press_enter(
                AGENT_ID, tab_id, RELAY_HOST, RELAY_PORT)

        elif cmd == "submit_form":
            result = await cloud_tools.submit_form(
                AGENT_ID, tab_id, RELAY_HOST, RELAY_PORT)

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

        elif cmd == "rhythm_train":
            if not args:
                print("Usage: cdp_tool.py rhythm_train <url> [--click <text>]", file=sys.stderr)
                sys.exit(1)
            parsed = _parse_flags(args, {"click": ""}, positional=1)
            result = await cloud_tools.run_rhythm_train(
                AGENT_ID, tab_id, parsed["_pos"][0],
                click_link_text=parsed["click"],
                relay_host=RELAY_HOST, relay_port=RELAY_PORT)

        elif cmd == "rhythm_catch":
            if len(args) < 3:
                print("Usage: cdp_tool.py rhythm_catch <url> <task> <catch_terms> [--click <text>]", file=sys.stderr)
                sys.exit(1)
            parsed = _parse_flags(args, {"click": ""}, positional=3)
            url, task, catch_terms = parsed["_pos"]
            terms = [t.strip() for t in catch_terms.split(",") if t.strip()]
            result = await cloud_tools.run_rhythm_catch(
                AGENT_ID, tab_id, url, task, terms,
                click_text=parsed["click"],
                relay_host=RELAY_HOST, relay_port=RELAY_PORT)

        elif cmd == "rhythm_execute":
            if len(args) < 2:
                print("Usage: cdp_tool.py rhythm_execute <url> <targets_json>", file=sys.stderr)
                sys.exit(1)
            _parse_flags(args, {}, positional=2)  # warn on unknown flags
            url = args[0]
            try:
                target_list = json.loads(args[1])
            except json.JSONDecodeError:
                print("Invalid JSON in targets parameter.", file=sys.stderr)
                sys.exit(1)
            if not isinstance(target_list, list):
                print("targets must be a JSON array of step objects.", file=sys.stderr)
                sys.exit(1)
            result = await cloud_tools.run_rhythm_execute(
                AGENT_ID, tab_id, url, target_list,
                relay_host=RELAY_HOST, relay_port=RELAY_PORT)

        elif cmd == "rhythm_query":
            if not args:
                print("Usage: cdp_tool.py rhythm_query <action> [--url <url>] [--domain <domain>]", file=sys.stderr)
                sys.exit(1)
            parsed = _parse_flags(args, {"url": "", "domain": ""}, positional=1)
            result = await cloud_tools.run_rhythm_query(
                parsed["_pos"][0], url=parsed["url"], domain=parsed["domain"])

        else:
            print(f"Unknown command: {cmd}", file=sys.stderr)
            sys.exit(1)

        print(result)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


asyncio.run(main())
