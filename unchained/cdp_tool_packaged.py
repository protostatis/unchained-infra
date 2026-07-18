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
import platform
import re
import subprocess
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


def _pid_is_running(pid: int) -> bool:
    """Return whether a local process with ``pid`` still exists."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            output = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'running' }}",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            return output.strip() == "running"
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _process_cmdline(pid: int) -> str:
    """Return a process command line, or an empty string when unavailable."""
    if pid <= 0:
        return ""
    if platform.system() == "Windows":
        ps_cmd = (
            f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}" '
            "-ErrorAction SilentlyContinue; "
            'if ($p) { [string]$p.CommandLine }'
        )
        try:
            return subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            ).strip()
        except Exception:
            return ""
    proc_cmdline = f"/proc/{pid}/cmdline"
    if os.path.exists(proc_cmdline):
        try:
            with open(proc_cmdline, "rb") as f:
                return f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            return ""
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
    except Exception:
        return ""


def _parse_prov_slot(tab_id: str = TAB_ID):
    """Extract the provision slot from a prov-<slot>-<id> tab id."""
    if not tab_id.startswith("prov-"):
        return ""
    parts = tab_id.split("-", 2)
    if len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{4}", parts[1]):
        return ""
    return parts[1]


def _provision_slot_status(slot: str):
    """Classify a persisted provision slot without taking ownership of it.

    The bridge owns lifecycle cleanup. This client only decides whether it is
    safe to route this command to a provisioned Chrome instead of port 9222.
    """
    if not re.fullmatch(r"[0-9a-f]{4}", slot):
        return "stale", {}
    state_file = os.path.join(DATA_DIR, "provision_slots", f"{slot}.json")
    try:
        with open(state_file) as f:
            state = json.load(f)
    except Exception:
        return "stale", {}
    if not isinstance(state, dict):
        return "stale", {}
    try:
        port = int(state.get("port", 0))
        pid = int(state.get("pid", 0))
    except Exception:
        return "stale", state
    temp_dir = str(state.get("temp_dir") or "")
    if port <= 0 or pid <= 0 or not temp_dir:
        return "stale", state
    state_agent_id = str(state.get("agent_id") or "")
    if BRIDGE_AGENT_ID and state_agent_id and state_agent_id != BRIDGE_AGENT_ID:
        return "unavailable", state
    if not _pid_is_running(pid):
        return "stale", state
    cmdline = _process_cmdline(pid)
    if not cmdline:
        return "unavailable", state
    user_data_arg = f"--user-data-dir={temp_dir}"
    port_marker = f"--remote-debugging-port={port}"
    if not _has_process_arg(cmdline, user_data_arg) or not _has_process_arg(cmdline, port_marker):
        return "stale", state
    if not bool(state.get("ready", True)):
        return "starting", state
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
        with urllib.request.urlopen(req, timeout=1) as resp:
            version = json.loads(resp.read())
    except Exception:
        return "unavailable", state
    if not isinstance(version, dict) or not str(version.get("webSocketDebuggerUrl") or "").startswith("ws://"):
        return "unavailable", state
    return "active", state


def _has_process_arg(cmdline: str, arg: str) -> bool:
    """Match one complete quoted or unquoted command-line argument."""
    return bool(re.search(rf"(?<!\S)[\"']?{re.escape(arg)}[\"']?(?=\s|$)", cmdline))


def _load_prov_slot_state(slot: str):
    """Load state only when the saved Chrome identity is healthy."""
    status, state = _provision_slot_status(slot)
    if status != "active":
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


def _provision_slot_error(slot: str, status: str) -> str:
    """Describe a non-routable provision slot without risking profile crossover."""
    if status == "starting":
        return f"Provisioned Chrome slot '{slot}' is still starting. Retry shortly."
    if status == "stale":
        return f"Provisioned Chrome slot '{slot}' is no longer running. Re-provision to continue."
    return f"Provisioned Chrome slot '{slot}' is unavailable. Retry or re-provision to continue."


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


def _chrome_tabs(port=None):
    """List page tabs from local Chrome's HTTP API."""
    if port is None:
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

    # When the session uses a provisioned Chrome, the bridge routes by tab id
    # prefix: bare ids hit default Chrome (9222), prov-prefixed ids hit the
    # provisioned Chrome's port. A non-routable provision slot always fails
    # closed so an action cannot silently cross into the default profile.
    env_slot = _parse_prov_slot(TAB_ID)
    prov_slot = ""
    prov_state = {}
    if env_slot:
        env_status, env_state = _provision_slot_status(env_slot)
        if env_status == "active":
            prov_slot = env_slot
            prov_state = env_state
        else:
            print(f"Error: {_provision_slot_error(env_slot, env_status)}", file=sys.stderr)
            sys.exit(1)

    if not prov_slot and tab_id.startswith("prov-"):
        print("Error: Provision tab is not bound to this chat session.", file=sys.stderr)
        sys.exit(1)
    elif prov_slot and tab_id.startswith("prov-") and _parse_prov_slot(tab_id) != prov_slot:
        print(
            f"Error: Provision tab targets slot '{_parse_prov_slot(tab_id)}', "
            f"but this session is bound to slot '{prov_slot}'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if prov_slot and tab_id and not tab_id.startswith("prov-"):
        tab_id = f"prov-{prov_slot}-{tab_id}"
    cdp_port = int(prov_state.get("port", CDP_PORT)) if prov_state else CDP_PORT

    try:
        # --- Tab management ---
        if command == "tabs":
            tabs = _chrome_tabs(cdp_port)
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
            tabs = _chrome_tabs(cdp_port)
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
                f"http://{CDP_HOST}:{cdp_port}/json/close/{close_id}", method="PUT")
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
