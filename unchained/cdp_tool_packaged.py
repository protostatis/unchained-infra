"""cdp_tool.py — CLI wrapper for browser tools via server API.

Calls the server's /web/cmd endpoint. DDM, page intelligence, and CDP
run on the relay server — this is a thin HTTP client.
Tab management (tabs, new-tab, close-tab) calls Chrome's local HTTP API
directly since Chrome always runs on the same machine.

Usage (called by Claude via Bash):
    python cdp_tool.py ddm --llm-2pass --cols 60
    python cdp_tool.py navigate https://example.com
    python cdp_tool.py type "search query"
    python cdp_tool.py press_enter
    python cdp_tool.py submit_form
    python cdp_tool.py pdf
    python cdp_tool.py tabs
    python cdp_tool.py new-tab https://example.com
    python cdp_tool.py close-tab <tab_id>
"""
from __future__ import annotations


import json
import os
import sys
import urllib.request
import urllib.error

API_KEY = os.environ.get("UNCHAINED_API_KEY", "")
API_URL = os.environ.get("UNCHAINED_API_URL", "https://api.unchainedsky.com")
DEFAULT_NEW_TAB_PATH = "/tab"
TAB_ID = os.environ.get("CDP_TAB_ID", "auto")
CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
DATA_DIR = os.environ.get("UNCHAINED_DATA_DIR",
                          os.path.join(os.path.expanduser("~"), ".unchained"))


def _resolve_cdp_port():
    """Return the CDP port for the active Chrome.

    When running with a provisioned profile (CDP_TAB_ID starts with prov-),
    read the provisioned Chrome's port from its state file instead of using
    the default port (9222).
    """
    if not TAB_ID.startswith("prov-"):
        return CDP_PORT
    parts = TAB_ID.split("-", 2)
    if len(parts) < 3:
        return CDP_PORT
    slot = parts[1]
    state_file = os.path.join(DATA_DIR, "provision_slots", f"{slot}.json")
    try:
        with open(state_file) as f:
            state = json.loads(f.read())
        port = int(state.get("port", 0))
        if port > 0:
            return port
    except Exception:
        pass
    return CDP_PORT


def cmd(action, **kwargs):
    body = json.dumps({"action": action, "tab_id": TAB_ID, **kwargs}).encode()
    req = urllib.request.Request(
        f"{API_URL}/web/cmd",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            err = json.loads(err).get("error", err)
        except Exception:
            pass
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError):
        print("Error: Cannot reach the server. Check your internet connection.", file=sys.stderr)
        sys.exit(1)


def _chrome_tabs():
    """List page tabs from local Chrome's HTTP API."""
    port = _resolve_cdp_port()
    req = urllib.request.Request(f"http://{CDP_HOST}:{port}/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        tabs = json.loads(resp.read())
    return [t for t in tabs if t.get("type") == "page"]


def _decode_type_text_arg(text):
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


def main():
    if len(sys.argv) < 2:
        print("Usage: cdp_tool.py <command> [--tab <id>] [args...]")
        print("Commands: ddm, navigate, click, type, press_enter, submit_form, js, screenshot, intel, pdf, tabs, new-tab, close-tab")
        print("Use --tab <id> to target a specific tab (default: auto = first tab)")
        sys.exit(1)

    command = sys.argv[1]
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
        parts = TAB_ID.split("-", 2)
        if len(parts) >= 3 and parts[1]:
            tab_id = f"prov-{parts[1]}-auto"

    try:
        # --- Tab management: call Chrome's local HTTP API directly ---
        if command == "tabs":
            tabs = _chrome_tabs()
            print(f"=== Open Tabs ({len(tabs)}) ===")
            for t in tabs:
                tid = t["id"][:12]
                title = (t.get("title") or "(no title)")[:50]
                url = (t.get("url") or "")[:80]
                print(f"  {tid}  {title}  {url}")
            return

        elif command == "new-tab":
            url = args[0] if args else f"{API_URL.rstrip('/')}{DEFAULT_NEW_TAB_PATH}"
            req = urllib.request.Request(
                f"http://{CDP_HOST}:{_resolve_cdp_port()}/json/new?{url}", method="PUT")
            with urllib.request.urlopen(req, timeout=5) as resp:
                tab_info = json.loads(resp.read())
            new_id = tab_info["id"][:12]
            print(f"Created tab {new_id}")
            tabs = _chrome_tabs()
            for t in tabs:
                tid = t["id"][:12]
                title = (t.get("title") or "(no title)")[:50]
                marker = " *" if t["id"].startswith(new_id) else ""
                print(f"  {tid}  {title}{marker}")
            return

        elif command == "close-tab":
            if not args:
                print("Usage: cdp_tool.py close-tab <tab_id>", file=sys.stderr)
                sys.exit(1)
            tabs = _chrome_tabs()
            matches = [t for t in tabs if t["id"].startswith(args[0])]
            if not matches:
                print(f"Tab {args[0]} not found", file=sys.stderr)
                sys.exit(1)
            if len(matches) > 1:
                print(f"Ambiguous prefix '{args[0]}'", file=sys.stderr)
                sys.exit(1)
            close_id = matches[0]["id"]
            req = urllib.request.Request(
                f"http://{CDP_HOST}:{CDP_PORT}/json/close/{close_id}", method="PUT")
            urllib.request.urlopen(req, timeout=5)
            print(f"Closed tab {close_id[:12]}")
            return

        # --- Browser commands: go through server API ---
        if command == "ddm":
            result = cmd("ddm", tab_id=tab_id, flags=args or ["--llm-2pass", "--cols", "60"])
        elif command == "navigate":
            if not args:
                print("Usage: cdp_tool.py navigate <url>", file=sys.stderr)
                sys.exit(1)
            result = cmd("navigate", tab_id=tab_id, url=args[0])
        elif command == "click":
            if len(args) < 2:
                print("Usage: cdp_tool.py click <x> <y>", file=sys.stderr)
                sys.exit(1)
            result = cmd("click", tab_id=tab_id, x=int(args[0]), y=int(args[1]))
        elif command == "type":
            if not args:
                print("Usage: cdp_tool.py type <text>", file=sys.stderr)
                sys.exit(1)
            result = cmd("type", tab_id=tab_id, text=_decode_type_text_arg(" ".join(args)))
        elif command == "press_enter":
            result = cmd("press_enter", tab_id=tab_id)
        elif command == "submit_form":
            result = cmd("submit_form", tab_id=tab_id)
        elif command == "js":
            if not args:
                print("Usage: cdp_tool.py js <expression>", file=sys.stderr)
                sys.exit(1)
            result = cmd("js", tab_id=tab_id, expression=" ".join(args))
        elif command == "screenshot":
            result = cmd("screenshot", tab_id=tab_id)
        elif command == "intel":
            result = cmd("intel", tab_id=tab_id, flags=args or ["--probe"])
        elif command == "pdf":
            result = cmd("ddm", tab_id=tab_id, flags=["--pdf"] + args)
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            sys.exit(1)

        if result.get("type") == "image":
            import tempfile
            data = result.get("data", "")
            sc_path = os.path.join(tempfile.gettempdir(), "unchained_last_screenshot.b64")
            with open(sc_path, "w") as f:
                f.write(data)
            print(f"[screenshot captured: {len(data)} bytes, saved:{sc_path}]")
        else:
            print(result.get("data", ""))

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
