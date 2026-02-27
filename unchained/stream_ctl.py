#!/usr/bin/env python3
"""
stream_ctl.py — AI-powered live stream control terminal

Natural language interface to agent_stream.py. Talk to Trinity (OpenRouter free)
to control the stream — reads live state and issues commands directly.

Usage:
    uv run stream_ctl.py          # auto-launched by agent_stream.py
    uv run stream_ctl.py --raw    # skip Claude, send raw commands only

Commands Claude can issue:
    go <place>        — navigate to a destination immediately
    say <text>        — override overlay narration
    website <url>     — trigger a web visit
    skip              — force the AI orchestrator to pick next destination

Raw commands (type directly when Claude is overkill):
    > go Tokyo Tower
    > say Walking through Akihabara's electric town
    > website https://en.wikipedia.org/wiki/Tokyo
    > skip
    > status
"""

import json
import os
import readline
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

SOCKET_PATH  = os.path.expanduser("~/.unchained/stream.sock")
HISTORY_FILE = os.path.expanduser("~/.unchained/ctl_history")
LOG_FILE     = os.path.expanduser("~/.unchained/stream.log")   # matches agent_stream.py

# Load .env.obs for OPENROUTER_API_KEY and other secrets
_env_obs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.obs")
if os.path.exists(_env_obs):
    with open(_env_obs) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

CTL_SYSTEM = """\
You are the live director of a travel show on unchainedsky.com — an AI that \
controls a real Chrome browser in real-time via CDP (Chrome DevTools Protocol).

The browser is navigating Google Maps Street View and occasionally other websites \
to entertain a live audience and showcase unchainedsky.com's browser agent capabilities.

You have full control over the stream. When you want to take an action, \
embed ONE <action> tag anywhere in your response:

  <action>{"cmd": "go", "dest": "Place Name, Country"}</action>
  <action>{"cmd": "say", "text": "Overlay narration ≤90 chars"}</action>
  <action>{"cmd": "website", "url": "https://en.wikipedia.org/wiki/..."}</action>
  <action>{"cmd": "skip"}</action>   ← force the background AI to pick next destination

Guidelines:
- Respond naturally and conversationally — this is a creative collaboration.
- Include an <action> whenever the user's intent maps to a stream command.
- "say" sets the live overlay text viewers see — keep it punchy and present tense.
- "go" jumps the queue and forces navigation immediately.
- You can suggest destinations, explain what's on screen, or just chat about the show.
- Keep responses concise — the user is watching a live stream while talking to you.
"""


# ── Socket helpers ─────────────────────────────────────────────────────────────
def send_cmd(cmd: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(SOCKET_PATH)
            s.sendall((json.dumps(cmd) + "\n").encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            return json.loads(data.decode().strip())
    except Exception as e:
        return {"error": str(e)}


def get_status() -> dict:
    return send_cmd({"cmd": "status"})


def fmt_status(s: dict) -> str:
    if "error" in s:
        return f"[error: {s['error']}]"
    paused      = s.get("agent_paused", False)
    obs_conn    = s.get("obs_connected", False)
    lines = [
        f"  location : {s.get('current', '?')}",
        f"  mode     : {s.get('mode', '?')}",
        f"  narration: {s.get('narration', '')}",
        f"  status   : {s.get('status', '')}",
        f"  visited  : {len(s.get('visited', []))} places",
        f"  queue    : {s.get('queue', [])}",
        f"  agent    : {'⏸  PAUSED' if paused else '▶  running'}",
        f"  obs      : {'connected' if obs_conn else 'not connected'}",
    ]
    return "\n".join(lines)


# ── Claude call ────────────────────────────────────────────────────────────────
def call_claude(history: list, user_msg: str, stream_state: dict) -> str:
    state_block = (
        f"[Live stream state]\n"
        f"  location  : {stream_state.get('current', 'unknown')}\n"
        f"  mode      : {stream_state.get('mode', 'unknown')}\n"
        f"  narration : {stream_state.get('narration', '')}\n"
        f"  step      : {stream_state.get('step', 0)}\n"
        f"  visited   : {', '.join(stream_state.get('visited', [])[-5:]) or 'none'}\n"
        f"  queue     : {stream_state.get('queue', [])[:3]}\n"
    )

    turns = "\n\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in history[-12:]
    )
    full_prompt = f"{CTL_SYSTEM}\n\n{state_block}\n---\n\n{turns}\nUser: {user_msg}"

    openrouter_bin = os.environ.get("OPENROUTER_BIN", shutil.which("openrouter-agent") or "openrouter-agent")
    model = os.environ.get("OPENROUTER_MODEL", "arcee-ai/trinity-large-preview:free")
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    cmd = [openrouter_bin, "--no-tools", "--model", model, "--prompt", full_prompt]
    if api_key:
        cmd += ["--api-key", api_key]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        return result.stdout.strip() or f"[no response — stderr: {result.stderr.strip()[:200]}]"
    except subprocess.TimeoutExpired:
        return "[timeout — try again]"
    except FileNotFoundError:
        return "[openrouter-agent not found — check OPENROUTER_BIN]"
    except Exception as e:
        return f"[error: {e}]"


def parse_and_execute(response: str) -> str | None:
    m = re.search(r"<action>(.*?)</action>", response, re.DOTALL)
    if not m:
        return None
    try:
        cmd = json.loads(m.group(1).strip())
        result = send_cmd(cmd)
        return f"  → {cmd} — {result}"
    except Exception as e:
        return f"  → [action error: {e}]"


# ── Raw command parser (no Claude needed) ─────────────────────────────────────
def handle_raw(line: str) -> bool:
    """Handle typed commands directly. Returns True if handled."""
    parts = line.strip().split(None, 1)
    if not parts:
        return False
    cmd, rest = parts[0].lower(), parts[1] if len(parts) > 1 else ""

    if cmd == "status":
        print(fmt_status(get_status()))
        return True
    elif cmd == "go" and rest:
        r = send_cmd({"cmd": "go", "dest": rest})
        print(f"  → {r}")
        return True
    elif cmd == "say" and rest:
        r = send_cmd({"cmd": "say", "text": rest})
        print(f"  → {r}")
        return True
    elif cmd == "website" and rest:
        r = send_cmd({"cmd": "website", "url": rest})
        print(f"  → {r}")
        return True
    elif cmd == "skip":
        r = send_cmd({"cmd": "skip"})
        print(f"  → {r}")
        return True
    elif cmd == "agent" and rest in ("pause", "resume", "status"):
        r = send_cmd({"cmd": "agent", "action": rest})
        print(f"  → {r}")
        return True
    elif cmd == "obs" and rest in ("start", "stop", "status"):
        r = send_cmd({"cmd": "obs", "action": rest})
        print(f"  → {r}")
        return True
    elif cmd in ("q", "quit", "exit"):
        raise SystemExit(0)
    return False


# ── Event tail ────────────────────────────────────────────────────────────────
_ICONS = {
    "thinking":      "🤔",
    "agent_plan":    "🤖",
    "nav":           "🗺 ",
    "arrived":       "📍",
    "website":       "🌐",
    "ctl":           "⌨ ",
    "chat":          "💬",
    "agent_paused":  "⏸ ",
    "agent_resumed": "▶️ ",
}

_CTL_CMD_LABELS = {
    "go":        "director → go",
    "say":       "director → say",
    "website":   "director → website",
    "skip":      "director → skip",
    "obs_start": "director → obs start",
    "obs_stop":  "director → obs stop",
}


def _fmt_event(evt: dict) -> str | None:
    t = evt.get("type", "")
    icon = _ICONS.get(t, "·")

    if t == "thinking":
        return f"\n{icon} agent thinking…"
    elif t == "agent_plan":
        action  = evt.get("action", "?")
        dest    = evt.get("dest", "")
        narr    = evt.get("narration", "")
        reason  = evt.get("reason", "")
        lines = [f"\n{icon} agent decided: {action.upper()} → {dest}"]
        if narr:
            lines.append(f'   narration : "{narr}"')
        if reason:
            lines.append(f"   reason    : {reason}")
        return "\n".join(lines)
    elif t == "nav":
        return f"{icon} navigating → {evt.get('place', '?')}"
    elif t == "arrived":
        return f"{icon} arrived: {evt.get('place', '?')}  ({evt.get('total', '?')} visited)"
    elif t == "website":
        return f"{icon} visiting: {evt.get('url', '?')}"
    elif t == "ctl":
        cmd   = evt.get("cmd", "?")
        label = _CTL_CMD_LABELS.get(cmd, f"ctl:{cmd}")
        detail = evt.get("dest") or evt.get("text") or evt.get("url") or ""
        return f"{icon} {label}{': ' + detail if detail else ''}"
    elif t == "chat":
        return f"{icon} chat request: !go {evt.get('dest', '?')}"
    elif t == "agent_paused":
        return f"{icon} agent PAUSED — AI will not make decisions until resumed"
    elif t == "agent_resumed":
        return f"{icon} agent RESUMED — AI orchestration active"
    return None


def _tail_thread():
    """Background thread: connects to agent_stream tail and prints events."""
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(SOCKET_PATH)
                s.sendall((json.dumps({"cmd": "tail"}) + "\n").encode())
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            evt = json.loads(line.decode())
                            msg = _fmt_event(evt)
                            if msg:
                                # Erase current prompt line, print event, leave
                                # readline to repaint its own prompt on next keystroke.
                                # Never write "you > " from a background thread —
                                # that corrupts readline's cursor state and eats input.
                                sys.stdout.write(f"\r\033[K{msg}\n")
                                sys.stdout.flush()
                        except Exception:
                            pass
        except Exception:
            pass
        time.sleep(3)   # reconnect after disconnect


# ── Main REPL ──────────────────────────────────────────────────────────────────
def main():
    raw_mode = "--raw" in sys.argv

    # Wait for socket to be ready (agent_stream may still be starting)
    for _ in range(20):
        if os.path.exists(SOCKET_PATH):
            break
        time.sleep(0.5)

    status = get_status()
    if "error" in status:
        print(f"[ctl] cannot connect to agent_stream: {status['error']}")
        print(f"[ctl] socket: {SOCKET_PATH}")
        print("[ctl] is agent_stream.py running?")
        input("Press Enter to retry or Ctrl+C to quit…")
        status = get_status()

    # Start background event tail
    t = threading.Thread(target=_tail_thread, daemon=True)
    t.start()

    print("\n" + "─" * 60)
    print("  stream_ctl · unchainedsky.com live stream director")
    print("─" * 60)
    print(fmt_status(status))
    print("─" * 60)
    if raw_mode:
        print("  Raw mode — commands: go / say / website / skip / status")
    else:
        print("  Talk naturally. Claude controls the stream.")
        print("  Raw: go / say / website / skip / status")
    print("  /help    — show all commands")
    print("  quit     — exit ctl (stream keeps running)\n")

    # readline history
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    try:
        readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError:
        pass
    readline.set_history_length(500)

    history: list = []

    while True:
        try:
            user_input = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[ctl] exiting — stream keeps running")
            break

        if not user_input:
            continue

        # ── Slash commands — purely local, instant, no socket/Claude needed ──
        if user_input.startswith("/"):
            if user_input == "/help":
                print("""
  ── stream_ctl commands ──────────────────────────────────────────

  Natural language (default)
    Type anything — Claude reads live state and controls the stream.
    Example: "go somewhere cold and remote"
             "say something funny about this place"
             "show me the Wikipedia page for here"

  Navigation
    go <place>       Navigate to a destination immediately
    say <text>       Override overlay narration (≤90 chars)
    website <url>    Trigger a web visit (~15s then back to Maps)
    skip             Force the AI to pick the next destination now

  Agent orchestrator
    agent pause      Freeze AI decisions (keeps stepping in place)
    agent resume     Resume AI decisions
    agent status     Show whether agent is paused

  OBS
    obs start        Start streaming in OBS
    obs stop         Stop streaming in OBS
    obs status       Show OBS stream status (active, timecode)

  Info
    status           Print full stream state
    /help            Show this help
    /log             Tail raw agent log — Ctrl+C to return
    /events          Raw JSON event stream — Ctrl+C to return

  Other
    quit / q         Exit ctl (stream keeps running)
    Ctrl+C           Exit ctl (stream keeps running)

  YouTube chat (audience)
    Viewers type  !go <place>  in live chat to queue destinations.

  ─────────────────────────────────────────────────────────────────
""")
            elif user_input == "/log":
                if not os.path.exists(LOG_FILE):
                    print(f"  [log] no log file yet: {LOG_FILE}\n")
                else:
                    print(f"\n  [log] tailing {LOG_FILE}  — Ctrl+C to return\n")
                    try:
                        proc = subprocess.Popen(["tail", "-f", "-n", "80", LOG_FILE])
                        proc.wait()
                    except KeyboardInterrupt:
                        proc.terminate()
                        proc.wait()
                    print("\n  [log] back to director\n")
            elif user_input == "/events":
                print("\n  [events] raw JSON event stream — Ctrl+C to return\n")
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.connect(SOCKET_PATH)
                        s.sendall((json.dumps({"cmd": "tail"}) + "\n").encode())
                        buf = b""
                        while True:
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                print(line.decode())
                except KeyboardInterrupt:
                    pass
                except Exception as e:
                    print(f"  [events] error: {e}")
                print("\n  [events] back to director\n")
            else:
                print(f"  unknown slash command: {user_input}  (try /help)\n")
            continue

        # ── Raw commands — fast socket calls, no Claude ─────────────────────
        if handle_raw(user_input):
            try:
                readline.write_history_file(HISTORY_FILE)
            except Exception:
                pass
            continue

        if raw_mode:
            print("  unknown command")
            continue

        # Trinity-powered response
        print("  …", end="", flush=True)
        stream_state = get_status()
        response = call_claude(history, user_input, stream_state)

        # Strip action tags for display
        display = re.sub(r"\s*<action>.*?</action>", "", response, flags=re.DOTALL).strip()
        print(f"\rtrinity > {display}\n")

        # Execute any embedded action
        action_result = parse_and_execute(response)
        if action_result:
            print(action_result + "\n")

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": response})
        if len(history) > 24:
            history[:] = history[-24:]

        try:
            readline.write_history_file(HISTORY_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()
