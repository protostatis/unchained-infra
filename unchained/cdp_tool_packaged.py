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
    python cdp_tool.py rhythm_train https://example.com
    python cdp_tool.py rhythm_catch https://example.com "find prices" "price,bed,sqft"
    python cdp_tool.py rhythm_execute https://example.com '[{"action":"click","text":"Next"}]'
    python cdp_tool.py rhythm_query list_all
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
BRIDGE_AGENT_ID = os.environ.get("CDP_AGENT_ID", "")
CHAT_SESSION_ID = os.environ.get("UNCHAINED_CHAT_SESSION_ID", "")
CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
DATA_DIR = os.environ.get("UNCHAINED_DATA_DIR",
                          os.path.join(os.path.expanduser("~"), ".unchained"))


def _parse_prov_slot(tab_id: str = TAB_ID):
    """Extract the provision slot from a prov-<slot>-<id> tab id."""
    if not tab_id.startswith("prov-"):
        return ""
    parts = tab_id.split("-", 2)
    if len(parts) < 3 or not parts[1]:
        return ""
    return parts[1]


def _load_prov_slot_state(slot: str):
    """Load provision slot state, returning {} when the slot is stale/missing."""
    if not slot:
        return {}
    state_file = os.path.join(DATA_DIR, "provision_slots", f"{slot}.json")
    try:
        with open(state_file) as f:
            state = json.loads(f.read())
    except Exception:
        return {}
    try:
        port = int(state.get("port", 0))
    except Exception:
        port = 0
    if port <= 0:
        return {}
    return state


def _active_prov_slot():
    """Return the provisioned-Chrome slot id when the session is bound to one.

    The chat agent exports CDP_TAB_ID="prov-<slot>-<real_id>" for provisioned
    sessions. Returns "" when not in a provisioned session.
    """
    slot = _parse_prov_slot(TAB_ID)
    if not slot:
        return ""
    # A stale prov-* CDP_TAB_ID can leak into a later default-profile turn.
    # Treat it as default unless the slot still has live local state.
    return slot if _load_prov_slot_state(slot) else ""


def _resolve_cdp_port():
    """Return the CDP port for the active Chrome.

    When running with a provisioned profile (CDP_TAB_ID starts with prov-),
    read the provisioned Chrome's port from its state file instead of using
    the default port (9222).
    """
    slot = _active_prov_slot()
    if not slot:
        return CDP_PORT
    state = _load_prov_slot_state(slot)
    port = int(state.get("port", 0)) if state else 0
    if port > 0:
        return port
    return CDP_PORT


def _format_tab_id_for_display(real_id: str, slot: str) -> str:
    """Produce the tab id form the agent should pass back via --tab.

    For provisioned sessions, the bridge expects "prov-<slot>-<real_id>" so
    it routes commands to the correct Chrome instance. Default sessions use
    the bare 12-char prefix.
    """
    short = real_id[:12]
    if slot:
        return f"prov-{slot}-{short}"
    return short


def cmd(action, **kwargs):
    payload = {"action": action, "tab_id": TAB_ID, **kwargs}
    # session_id lets the server reuse the bridge chosen for this chat turn;
    # bridge_agent_id is a validated fallback when no session map exists yet.
    if CHAT_SESSION_ID:
        payload["session_id"] = CHAT_SESSION_ID
    if BRIDGE_AGENT_ID:
        payload["bridge_agent_id"] = BRIDGE_AGENT_ID
    body = json.dumps(payload).encode()
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
        print("Commands: ddm, navigate, click, type, press_enter, submit_form, js, screenshot, intel, pdf, tabs, new-tab, close-tab, rhythm_train, rhythm_catch, rhythm_execute, rhythm_query")
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

    # When the session uses a provisioned Chrome, the bridge routes by
    # tab id prefix: bare ids hit default Chrome (9222), prov-prefixed ids
    # hit the provisioned Chrome's port. Rewrite any non-prov-prefixed
    # value the agent passes so it stays inside the active slot.
    prov_slot = _active_prov_slot()
    if not prov_slot and tab_id.startswith("prov-"):
        # The chat/session may have retained a provisioned tab id after that
        # provisioned Chrome was cleaned up.  Falling back to auto lets the
        # default-profile bridge select its current tab instead of sending an
        # impossible prov-* tab id to the server.
        tab_id = "auto"
    if prov_slot and tab_id and not tab_id.startswith("prov-"):
        tab_id = f"prov-{prov_slot}-{tab_id}"

    try:
        # --- Tab management ---
        if command == "tabs":
            tabs = _chrome_tabs()
            print(f"=== Open Tabs ({len(tabs)}) ===")
            for t in tabs:
                tid = _format_tab_id_for_display(t["id"], prov_slot)
                title = (t.get("title") or "(no title)")[:50]
                url = (t.get("url") or "")[:80]
                print(f"  {tid}  {title}  {url}")
            return

        elif command == "new-tab":
            url = args[0] if args else f"{API_URL.rstrip('/')}{DEFAULT_NEW_TAB_PATH}"
            result = cmd("new_tab", tab_id=tab_id, url=url)
            print(result.get("data", "Created tab"))
            return

        elif command == "close-tab":
            if not args:
                print("Usage: cdp_tool.py close-tab <tab_id>", file=sys.stderr)
                sys.exit(1)
            tabs = _chrome_tabs()
            # Accept either bare ids or the prov-<slot>-<id> form printed by
            # `tabs` / `new-tab` when in a provisioned session.
            target = args[0]
            if target.startswith("prov-"):
                parts = target.split("-", 2)
                if len(parts) >= 3:
                    target = parts[2]
            matches = [t for t in tabs if t["id"].startswith(target)]
            if not matches:
                print(f"Tab {args[0]} not found", file=sys.stderr)
                sys.exit(1)
            if len(matches) > 1:
                print(f"Ambiguous prefix '{args[0]}'", file=sys.stderr)
                sys.exit(1)
            close_id = matches[0]["id"]
            req = urllib.request.Request(
                f"http://{CDP_HOST}:{_resolve_cdp_port()}/json/close/{close_id}", method="PUT")
            urllib.request.urlopen(req, timeout=5)
            print(f"Closed tab {_format_tab_id_for_display(close_id, prov_slot)}")
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
        elif command == "rhythm_train":
            if not args:
                print("Usage: cdp_tool.py rhythm_train <url> [--click <text>]", file=sys.stderr)
                sys.exit(1)
            url = args[0]
            click_text = ""
            j = 1
            while j < len(args):
                if args[j] == "--click" and j + 1 < len(args):
                    click_text = args[j + 1]
                    j += 2
                else:
                    j += 1
            result = cmd("rhythm_train", tab_id=tab_id, url=url, click_link_text=click_text)
        elif command == "rhythm_catch":
            if len(args) < 3:
                print("Usage: cdp_tool.py rhythm_catch <url> <task> <catch_terms> [--click <text>]", file=sys.stderr)
                sys.exit(1)
            url, task, catch_terms = args[0], args[1], args[2]
            click_text = ""
            j = 3
            while j < len(args):
                if args[j] == "--click" and j + 1 < len(args):
                    click_text = args[j + 1]
                    j += 2
                else:
                    j += 1
            result = cmd("rhythm_catch", tab_id=tab_id, url=url, task=task, catch_terms=catch_terms, click_text=click_text)
        elif command == "rhythm_execute":
            if len(args) < 2:
                print("Usage: cdp_tool.py rhythm_execute <url> <targets_json>", file=sys.stderr)
                sys.exit(1)
            result = cmd("rhythm_execute", tab_id=tab_id, url=args[0], targets=args[1])
        elif command == "rhythm_query":
            if not args:
                print("Usage: cdp_tool.py rhythm_query <action> [--url <url>] [--domain <domain>]", file=sys.stderr)
                sys.exit(1)
            action = args[0]
            url = ""
            domain = ""
            j = 1
            while j < len(args):
                if args[j] == "--url" and j + 1 < len(args):
                    url = args[j + 1]
                    j += 2
                elif args[j] == "--domain" and j + 1 < len(args):
                    domain = args[j + 1]
                    j += 2
                else:
                    j += 1
            result = cmd("rhythm_query", tab_id=tab_id, query_action=action, url=url, domain=domain)
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
