"""chat_agent_cli.py — Local chat agent for Claude CLI and Codex CLI lanes.

No provider API key needed for these local CLI lanes.
- Claude lane uses `claude -p` with `--resume` session continuity.
- Codex lane uses `codex exec --json` with `exec resume` continuity.

Architecture:
    Phone → EC2 web server (POST /web/chat, SSE response)
         → WebSocket bridge
         → This script (runs on your Mac)
         → `claude -p` OR `codex exec` subprocess (selected by model prefix)
            → Bash → cdp_tool.py → cloud_tools → WSS to EC2 relay → Chrome

Usage:
    cd ~/Projects/unchained/unchained
    PYTHONUNBUFFERED=1 uv run python chat_agent_cli.py

See also: chat_agent_sdk.py (production Anthropic SDK lane)
"""
from __future__ import annotations  # Python 3.9 compat for int | None hints

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.expanduser("~/Projects/unchained/unchained"))

import subprocess
import websockets  # noqa: E402

from nudge import (
    NudgeState,
    _is_base64_png_blob,
    _extract_domain,
    _tool_progress_sig,
    LOOP_SHORT_CIRCUIT_REPEAT_THRESHOLD,
)

# File logging — writes to ~/.unchained/agent.log
_log_dir = os.environ.get("UNCHAINED_DATA_DIR", os.path.expanduser("~/.unchained"))
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(_log_dir, "agent.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

KEY = os.environ.get("UNCHAINED_API_KEY", "")
SERVER = os.environ.get("UNCHAINED_SERVER", "wss://api.unchainedsky.com/chat/ws")
RELAY_HOST = os.environ.get("UNCHAINED_RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("UNCHAINED_RELAY_PORT", "443"))
CWD = os.path.expanduser("~/Projects/unchained/unchained")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.1-codex-mini")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "low").strip().lower()
CODEX_MAX_RUNTIME_S = int(os.environ.get("CODEX_MAX_RUNTIME_S", "300"))

# Derive stable agent ID from API key
AGENT_ID = ""
if KEY:
    AGENT_ID = f"claude-{hashlib.sha256(KEY.encode()).hexdigest()[:8]}"

# Check if CLAUDE.md exists (Claude Code auto-loads it from CWD or parent dirs)
_claude_md_found = (
    os.path.exists(os.path.join(CWD, "CLAUDE.md"))
    or os.path.exists(os.path.join(CWD, "..", "CLAUDE.md"))
)
_claude_md_warning = "" if _claude_md_found else """
WARNING: CLAUDE.md not found in working directory or parent. You are running WITHOUT
the full browsing methodology (Rule #5: probe on first domain visit, per-site strategy
tables, DDM vs JS decision rules, gotchas). The system prompt below has basic instructions
only. For full capability, ensure CLAUDE.md is present in the agent package directory.
"""

# ---------------------------------------------------------------------------
# Local chat history — stored on the user's machine
# ---------------------------------------------------------------------------

CHAT_DIR = os.path.join(
    os.environ.get("UNCHAINED_DATA_DIR", os.path.expanduser("~/.unchained")),
    "chats",
)
META_FILE = os.path.join(CHAT_DIR, "meta.json")


def _slot_file(n: int) -> str:
    """Return path to slot N's chat file (1-3)."""
    return os.path.join(CHAT_DIR, f"slot_{n}.json")


def _load_meta() -> dict:
    """Load slot metadata. Migrates legacy chat.json on first call."""
    os.makedirs(CHAT_DIR, exist_ok=True)
    # Migrate legacy chat.json → slot_1.json
    legacy = os.path.join(CHAT_DIR, "chat.json")
    if not os.path.exists(META_FILE) and os.path.exists(legacy):
        try:
            os.rename(legacy, _slot_file(1))
        except OSError:
            pass
        meta = {"active_slot": 1}
        with open(META_FILE, "w") as f:
            json.dump(meta, f)
        return meta
    try:
        with open(META_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"active_slot": 1}


def _save_meta(meta: dict):
    """Save slot metadata."""
    os.makedirs(CHAT_DIR, exist_ok=True)
    with open(META_FILE, "w") as f:
        json.dump(meta, f)


def _active_slot() -> int:
    """Return the currently active slot number (1-3)."""
    return _load_meta().get("active_slot", 1)


def _load_chat(slot: int | None = None) -> dict:
    """Load chat data from a slot file. Defaults to active slot."""
    if slot is None:
        slot = _active_slot()
    try:
        with open(_slot_file(slot), "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"messages": [], "claude_session": {}, "codex_session": {}}


def _save_chat(data: dict, slot: int | None = None):
    """Save chat data to a slot file. Defaults to active slot."""
    if slot is None:
        slot = _active_slot()
    os.makedirs(CHAT_DIR, exist_ok=True)
    with open(_slot_file(slot), "w") as f:
        json.dump(data, f)


def _append_message(role: str, content: str, tools: list | None = None):
    """Append a message to the active slot's chat history."""
    data = _load_chat()
    msg = {"role": role, "content": content, "created_at": time.time()}
    if tools:
        msg["tools"] = tools
    data["messages"].append(msg)
    # Keep last 200 messages to prevent unbounded growth
    if len(data["messages"]) > 200:
        data["messages"] = data["messages"][-200:]
    _save_chat(data)


def _save_claude_session(chat_session_id: str, claude_sid: str):
    """Persist claude session mapping in the active slot."""
    data = _load_chat()
    data["claude_session"] = {
        "chat_session_id": chat_session_id,
        "session_id": claude_sid,
        "updated_at": time.time(),
    }
    _save_chat(data)


def _load_claude_session() -> dict:
    """Load saved claude session mapping from the active slot."""
    data = _load_chat()
    return data.get("claude_session", {})


def _save_codex_session(chat_session_id: str, codex_sid: str, model: str = ""):
    """Persist codex session mapping in the active slot."""
    data = _load_chat()
    data["codex_session"] = {
        "chat_session_id": chat_session_id,
        "session_id": codex_sid,
        "model": model,
        "updated_at": time.time(),
    }
    _save_chat(data)


def _load_codex_session() -> dict:
    """Load saved codex session mapping from the active slot."""
    data = _load_chat()
    return data.get("codex_session", {})


def _clear_slot(slot: int | None = None):
    """Clear chat history and claude session for a slot."""
    if slot is None:
        slot = _active_slot()
    _save_chat({"messages": [], "claude_session": {}, "codex_session": {}}, slot)


def _get_slots_info() -> dict:
    """Return info about all 3 slots for the UI."""
    active = _active_slot()
    slots = []
    for n in range(1, 4):
        data = _load_chat(n)
        msgs = data.get("messages", [])
        preview = ""
        for m in msgs:
            if m.get("role") == "user":
                preview = m.get("content", "")[:40]
                break
        slots.append({"slot": n, "empty": len(msgs) == 0, "preview": preview})
    return {"active_slot": active, "slots": slots}


def _parse_version(v: str) -> tuple:
    """Parse version string like "0.2.0" into a comparable tuple (0, 2, 0)."""
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def check_for_updates() -> str | None:
    """Check for agent updates. Returns a warning string if outdated, else None."""
    import urllib.request
    import urllib.error

    # Read local version
    version_path = os.path.join(CWD, "..", "version.txt")
    if not os.path.exists(version_path):
        version_path = os.path.join(CWD, "version.txt")
    if not os.path.exists(version_path):
        return None  # No version file — skip check (dev environment)

    try:
        with open(version_path) as f:
            local_version = f.read().strip()
    except OSError:
        return None

    # Check remote version
    api_url = os.environ.get("UNCHAINED_API_URL", f"https://{RELAY_HOST}")
    req = urllib.request.Request(
        f"{api_url}/web/agent/version",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _json
            data = _json.loads(resp.read())
    except Exception:
        return None  # Network error — skip silently

    remote_version = data.get("version", "")
    min_version = data.get("min_version", "")

    local_t = _parse_version(local_version)
    remote_t = _parse_version(remote_version)
    min_t = _parse_version(min_version)

    if local_t < min_t:
        return (
            f"CRITICAL: Agent version {local_version} is below minimum {min_version}. "
            f"Update required: bash update.sh"
        )
    if local_t < remote_t:
        return (
            f"Update available: {local_version} -> {remote_version}. "
            f"Run: bash update.sh"
        )
    return None


SYSTEM_PROMPT = f"""You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
You MUST use browser tools to answer ANY factual question — never answer from memory.
Your training data is outdated. Always browse to get live, current data.
{_claude_md_warning}
IMPORTANT: Your working directory is {CWD}. Always run cdp_tool.py from this directory.

## Browser Tools (via Bash)

cd {CWD} && uv run python cdp_tool.py ddm --llm-2pass --cols 60    # Map page layout + interactive elements (~500 tok)
cd {CWD} && uv run python cdp_tool.py ddm --text                    # Extract page text (~3000 chars)
cd {CWD} && uv run python cdp_tool.py ddm --text --find keyword     # Search text on page
cd {CWD} && uv run python cdp_tool.py ddm --text --max 5000         # More text (custom char limit)
cd {CWD} && uv run python cdp_tool.py ddm --at 694,584              # Element details at pixel coordinates
cd {CWD} && uv run python cdp_tool.py ddm --js "expression"         # Execute JS on page, return JSON
cd {CWD} && uv run python cdp_tool.py navigate https://example.com  # Go to URL
cd {CWD} && uv run python cdp_tool.py click 500 300                 # Click at pixel coordinates from ddm
cd {CWD} && uv run python cdp_tool.py type "search query"           # Type into focused input (click first!)
cd {CWD} && uv run python cdp_tool.py js "document.title"           # Run JavaScript on page
cd {CWD} && uv run python cdp_tool.py intel --probe                 # Page fingerprint + Bayesian strategy ranking
cd {CWD} && uv run python cdp_tool.py intel --extract               # Extract structured data (auto strategy)
cd {CWD} && uv run python cdp_tool.py intel --stores                # List JS data store globals
cd {CWD} && uv run python cdp_tool.py intel --find-paths GLOBAL key # Find data arrays in a global
cd {CWD} && uv run python cdp_tool.py screenshot                    # Screenshot (CAPTCHAs only, ~2100 tok)

## DDM-First Methodology

1. **ORIENT**: `ddm --llm-2pass --cols 60` on every new page — shows all interactive elements with coordinates
2. **IDENTIFY**: `ddm --at x,y` to get href, class, text for elements you want to interact with
3. **CLASSIFY**: `intel --probe` on unknown SPAs — identifies framework and best extraction strategy
4. **ACT**: Use coordinates from DDM to click, or navigate to URLs. For SPA widgets, use `js` with .click()
5. **VERIFY**: DDM again after every action to confirm the page changed
6. **EXTRACT**: Choose by page type:
   - Simple text: `ddm --text --max 5000`
   - Shadow DOM (Reddit): `intel --extract`
   - SPA data store (Nuxt/YouTube): `intel --stores` → `intel --find-paths` → `js`
   - Structured data: `js` with querySelectorAll
   - data-testid (GitHub): `intel --extract --strategy data_testid`

## Tab Management

cd {CWD} && uv run python cdp_tool.py tabs                              # List all open tabs (ID, title, URL)
cd {CWD} && uv run python cdp_tool.py new-tab https://example.com       # Open new tab at URL
cd {CWD} && uv run python cdp_tool.py close-tab <tab_id>                # Close a tab by ID prefix

Add --tab <id> to ANY command to target a specific tab:
cd {CWD} && uv run python cdp_tool.py navigate https://example.com --tab <tab_id>
cd {CWD} && uv run python cdp_tool.py ddm --llm-2pass --cols 60 --tab <tab_id>
cd {CWD} && uv run python cdp_tool.py js "document.title" --tab <tab_id>

When to use multiple tabs:
- Parallel research: open each source in its own tab, switch between them
- Reference: keep docs/pricing open while working in another tab
- Comparison: open two product pages side by side
- Default (no --tab) always targets the first tab — use --tab for others

## Direct Web Tools (no browser needed)

WebSearch — search the web. Use ONLY for finding URLs or quick facts before browsing.
WebFetch — fetch a URL directly. Use ONLY for simple text pages (news articles, API endpoints).

IMPORTANT: After navigating the browser to a page, ALWAYS use DDM to read it — never WebFetch.
WebFetch cannot see what the browser sees (cookies, login state, JS-rendered content).
If you navigated to a site, you MUST use ddm/js to extract data from it, not WebFetch.

## Agent Update

If the user asks to update the agent, run:
bash {CWD}/../update.sh

This checks the server for a newer version, downloads code updates, and prints
"restart required" if an update was applied. Never touch .env or .venv.

## Key Rules
- ALWAYS use tools. NEVER answer from memory or fabricate data.
- Run ddm after every navigate/click to verify the page changed.
- Click input fields before typing.
- SPA widgets: CDP clicks often fail — use `js` with .click() instead.
- DDM only sees current viewport — scroll + remap for content below fold.
- Be concise — report findings, not process.
"""

# Codex CLI does not currently support a dedicated `--system-prompt` flag like
# Claude CLI. Inject equivalent browser-agent instructions into the prompt text.
CODEX_RESUME_REMINDER = f"""You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
Always use browser tools for factual requests.
Working directory: {CWD}

Use CDP tools via Bash:
- cd {CWD} && uv run python cdp_tool.py ddm --llm-2pass --cols 60
- cd {CWD} && uv run python cdp_tool.py ddm --text --max 5000
- cd {CWD} && uv run python cdp_tool.py navigate https://example.com
- cd {CWD} && uv run python cdp_tool.py click X Y
- cd {CWD} && uv run python cdp_tool.py type "text"
- cd {CWD} && uv run python cdp_tool.py js "document.title"
- cd {CWD} && uv run python cdp_tool.py intel --probe

Web search and fetch (no browser needed):
- curl -sL "https://html.duckduckgo.com/html/?q=QUERY" | sed -n 's/.*href="\\([^"]*\\)".*/\\1/p' | head -10
- curl -sL URL | head -200

Never answer factual browser tasks from memory when tool use is available.
"""


def _build_codex_prompt(user_text: str, *, is_resume: bool) -> str:
    """Build Codex input text with browser-agent instructions."""
    req = (user_text or "").strip()
    if not req:
        return ""
    if is_resume:
        return (
            "[AGENT REMINDER]\n"
            f"{CODEX_RESUME_REMINDER}\n"
            "[/AGENT REMINDER]\n\n"
            f"User request:\n{req}\n"
        )
    return (
        "[SYSTEM PROMPT]\n"
        f"{SYSTEM_PROMPT}\n"
        "[/SYSTEM PROMPT]\n\n"
        f"User request:\n{req}\n"
    )


# Map chat session_id → claude session_id for conversation persistence
claude_sessions: dict[str, str] = {}
# Map chat session_id → codex thread/session id for exec resume continuity
codex_sessions: dict[str, str] = {}

# Track active subprocesses and tasks for cancel support
active_procs: dict[str, asyncio.subprocess.Process] = {}
active_tasks: dict[str, asyncio.Task] = {}
# Process PIDs explicitly cancelled by the user via /web/chat/cancel.
# Using PID avoids race conditions when a new turn starts on the same session_id.
user_cancelled_pids: set[int] = set()


def check_chrome_bridge() -> bool:
    """Test that chrome_bridge.py is connected to the relay."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CDP_AGENT_ID"] = AGENT_ID
    env["CDP_RELAY_HOST"] = RELAY_HOST
    env["CDP_RELAY_PORT"] = str(RELAY_PORT)
    try:
        r = subprocess.run(
            ["uv", "run", "python", "cdp_tool.py", "js", "document.title"],
            capture_output=True, text=True, timeout=15, env=env, cwd=CWD,
        )
        if r.returncode == 0:
            print(f"  Chrome bridge OK: {r.stdout.strip()[:60]}")
            return True
        err = r.stderr.strip()
        if "not connected" in err:
            print(f"  ERROR: Chrome bridge not connected (agent {AGENT_ID})")
            print("  Start it: export UNCHAINED_API_KEY=uc_live_... && cd unchained && uv run chrome_bridge.py start --relay wss://api.unchainedsky.com/tunnel")
        else:
            print(f"  ERROR: Chrome bridge check failed: {err[:100]}")
        return False
    except subprocess.TimeoutExpired:
        print("  ERROR: Chrome bridge check timed out")
        return False


# Map Anthropic model IDs to Claude Code CLI model names
_MODEL_CLI_MAP = {
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
}


def _is_codex_cli_model(model: str) -> bool:
    return (model or "").startswith("codex-cli:")


def _resolve_codex_model(model: str) -> str:
    m = (model or "").strip()
    if m.startswith("codex-cli:"):
        resolved = m.split(":", 1)[1].strip()
        return resolved or DEFAULT_CODEX_MODEL
    return DEFAULT_CODEX_MODEL


def _collect_text_strings(obj) -> list[str]:
    """Collect likely text fragments from nested codex event objects."""
    out: list[str] = []
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            out.append(s)
        return out
    if isinstance(obj, list):
        for v in obj:
            out.extend(_collect_text_strings(v))
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


def _codex_tool_name_and_input(command: str) -> tuple[str, str]:
    """Map codex command_execution payloads into UI tool card fields."""
    cmd = (command or "").strip()
    m = re.search(r"cdp_tool\.py\s+([a-zA-Z0-9_-]+)", cmd)
    if m:
        return m.group(1).lower(), cmd
    return "bash", cmd


async def handle_message_claude(ws, sid: str, user_text: str, model: str = ""):
    """Single claude -p call with streaming tool events and session resume."""
    cli_model = _MODEL_CLI_MAP.get(model, "opus")

    # Save user message locally
    _append_message("user", user_text)

    claude_sid = claude_sessions.get(sid)
    is_resume = claude_sid is not None

    log.info("  Calling Claude (model=%s)%s...", cli_model, f"  (resume {claude_sid[:12]})" if is_resume else " (new)")

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CDP_AGENT_ID"] = AGENT_ID
    env["CDP_RELAY_HOST"] = RELAY_HOST
    env["CDP_RELAY_PORT"] = str(RELAY_PORT)

    # Build command with stream-json for real-time tool events
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--model", cli_model, "--max-turns", "100",
           "--allowedTools", "Bash(cd:*) Bash(uv run:*) Bash(bash:*) Bash(sleep:*) Bash(echo:*) WebFetch WebSearch",
           "--system-prompt", SYSTEM_PROMPT,
           "--tools", "Bash", "WebFetch", "WebSearch"]
    if is_resume:
        cmd += ["--resume", claude_sid]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=CWD,
        start_new_session=True,
    )
    active_procs[sid] = proc

    # Send input and close stdin
    proc.stdin.write(user_text.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    # Stream events from claude -p
    response = ""
    turn = 0
    nudge_state = NudgeState()
    # Track pending tool calls from the current assistant turn
    pending_tool_calls: list[dict] = []
    # Track per-turn state for stagnation
    turn_step_sigs: list[str] = []
    turn_find_queries: list[str] = []
    turn_had_navigation = False
    turn_had_interaction = False
    turn_domain_switch = False

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
            # Finalize stagnation for the PREVIOUS turn before resetting
            if turn_step_sigs:
                prev_score = nudge_state.stagnation_score
                nudge_state.update_stagnation(
                    turn_step_sigs, turn_find_queries,
                    turn_had_navigation, turn_domain_switch,
                    turn_had_interaction,
                )

                # --- progress_critic intervention ---
                should_emit, feedback = nudge_state.run_intervention(turn)
                if feedback:
                    log.info(
                        "[%s] ProgressCritic: emit=%s sev=%s intervene=%s reasons=%s tool_log=%d",
                        sid, should_emit,
                        getattr(feedback, "severity", "?"),
                        getattr(feedback, "should_intervene", "?"),
                        getattr(feedback, "reason_codes", [])[:3],
                        len(nudge_state.live_tool_log),
                    )
                elif turn >= 4 and turn % 5 == 0:
                    # Periodic debug: why no feedback?
                    from nudge import intervention_runtime_available as _ira
                    log.info(
                        "[%s] ProgressCritic: no feedback (runtime=%s, tool_log=%d)",
                        sid, _ira(), len(nudge_state.live_tool_log),
                    )
                if should_emit and feedback:
                    severity = getattr(feedback, "severity", "nudge")
                    prompt = (getattr(feedback, "feedback_prompt", "") or "").strip()
                    reasons = getattr(feedback, "reason_codes", [])[:3]
                    nudge_state.intervention_events += 1
                    nudge_state.last_intervention_model_turn = turn
                    log.info(
                        "[%s] Intervention %s (reasons=%s, turn=%d, stag=%d)",
                        sid, severity, ",".join(reasons), turn,
                        nudge_state.stagnation_score,
                    )
                    # Emit intervention card to UI
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_start",
                        "name": "intervention", "input": severity,
                    }))
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": "intervention",
                        "data": (prompt or f"Intervention: {severity}")[:1500],
                        "is_screenshot": False,
                    }))
                    # Decay stagnation after nudge
                    nudge_state.apply_nudge_decay()

                    if severity == "hard_stop":
                        # Terminate on hard_stop
                        proc.terminate()
                        stop_msg = prompt or (
                            "I kept browsing but stopped making meaningful progress. "
                            "Please try a different approach or rephrase the request."
                        )
                        await ws.send(json.dumps({
                            "session_id": sid, "type": "text", "data": stop_msg,
                        }))
                        await ws.send(json.dumps({"session_id": sid, "type": "done"}))
                        return
                    elif severity == "nudge":
                        nudge_state.apply_nudge_reset()

                # --- stagnation-based stall check (fallback) ---
                log.info(
                    "[%s] Nudge: turn=%d stag=%d (was %d) strikes=%d loops=%d nav=%s",
                    sid, turn, nudge_state.stagnation_score, prev_score,
                    nudge_state.stall_force_strikes, nudge_state.loop_events,
                    turn_had_navigation or turn_domain_switch,
                )
                action, guidance = nudge_state.check_stall_threshold()
                if action == "guidance":
                    log.info("[%s] Nudge: guidance issued (score=%d, strikes=%d)",
                             sid, nudge_state.stagnation_score, nudge_state.stall_force_strikes)
                if action == "force":
                    log.info(
                        "[%s] Progress stalled (score=%d) — terminating subprocess",
                        sid, nudge_state.stagnation_score,
                    )
                    proc.terminate()
                    stall_msg = (
                        "I kept browsing but stopped making meaningful progress. "
                        "Please try a different approach or rephrase the request."
                    )
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_start",
                        "name": "intervention", "input": "progress stalled",
                    }))
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": "intervention", "data": stall_msg,
                        "is_screenshot": False,
                    }))
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "text", "data": stall_msg,
                    }))
                    await ws.send(json.dumps({"session_id": sid, "type": "done"}))
                    return

            # New assistant turn — reset per-turn tracking
            pending_tool_calls = []
            turn_step_sigs = []
            turn_find_queries = []
            turn_had_navigation = False
            turn_had_interaction = False
            turn_domain_switch = False

            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    tool_input = block.get("input", {})
                    block_name = block.get("name", "")
                    # Track pending tool call for nudge
                    pending_tool_calls.append({
                        "name": block_name,
                        "input": tool_input,
                    })
                    # Track navigation/interaction for stagnation
                    if block_name == "Bash":
                        cmd_str = tool_input.get("command", "")
                        if "navigate" in cmd_str:
                            turn_had_navigation = True
                            # Try to extract URL for domain tracking
                            parts = cmd_str.split("navigate", 1)
                            if len(parts) > 1:
                                url_part = parts[1].strip().split()[0] if parts[1].strip() else ""
                                domain = _extract_domain(url_part)
                                if domain:
                                    if nudge_state.recent_domains and domain != nudge_state.recent_domains[-1]:
                                        turn_domain_switch = True
                                    nudge_state.recent_domains.append(domain)
                        elif any(k in cmd_str for k in ("click ", "type ", "press_enter", "submit_form")):
                            turn_had_interaction = True
                    # Detect tool type and extract display info
                    if block_name == "Bash":
                        cmd_str = tool_input.get("command", "")
                        tool_name = "bash"
                        if "cdp_tool.py" in cmd_str:
                            parts = cmd_str.split("cdp_tool.py", 1)
                            tool_name = parts[1].strip().split()[0] if len(parts) > 1 else "cdp"
                        display = cmd_str[:200]
                    elif block_name == "WebFetch":
                        tool_name = "webfetch"
                        display = tool_input.get("url", "")[:200]
                    elif block_name == "WebSearch":
                        tool_name = "websearch"
                        display = tool_input.get("query", "")[:200]
                    else:
                        tool_name = block_name.lower()
                        display = str(tool_input)[:200]
                    turn += 1
                    print(f"    [{turn}] {tool_name}: {display[:100]}")
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_start",
                        "name": tool_name,
                        "input": display,
                    }))

            # Loop detection on tool call signatures
            if pending_tool_calls:
                sig = json.dumps([
                    {"name": tc["name"], "args": json.dumps(tc["input"], sort_keys=True)}
                    for tc in pending_tool_calls
                ], sort_keys=True)
                loop_detected, loop_nudge, _ = nudge_state.check_loop(sig)
                if nudge_state.repeated_count > 0:
                    log.info("[%s] Nudge: repeat=%d (threshold=%d)",
                             sid, nudge_state.repeated_count, LOOP_SHORT_CIRCUIT_REPEAT_THRESHOLD)
                if loop_detected:
                    log.info("[%s] Loop detected — terminating subprocess", sid)
                    proc.terminate()
                    nudge_msg = (
                        "I got stuck repeating the same actions without making progress. "
                        "Please try rephrasing your request or asking me to take a different approach."
                    )
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_start",
                        "name": "intervention", "input": "loop detected",
                    }))
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": "intervention", "data": nudge_msg,
                        "is_screenshot": False,
                    }))
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "text", "data": nudge_msg,
                    }))
                    await ws.send(json.dumps({"session_id": sid, "type": "done"}))
                    return

        elif etype == "user":
            # Tool result — check both Bash stdout and content blocks
            result_text = ""
            tool_result = event.get("tool_use_result", {})
            if isinstance(tool_result, str):
                result_text = tool_result
            elif isinstance(tool_result, dict):
                result_text = tool_result.get("stdout", "")
            if not result_text:
                # WebFetch/WebSearch/other tools return content blocks
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_result":
                        content = block.get("content", "")
                        if isinstance(content, str):
                            result_text = content
                        elif isinstance(content, list):
                            result_text = " ".join(
                                b.get("text", "") for b in content
                                if b.get("type") == "text"
                            )
                        if result_text:
                            break
            if result_text:
                # Check if this is a screenshot result with temp file
                is_screenshot = False
                screenshot_data = None
                if "saved:" in result_text and "screenshot captured" in result_text:
                    # cdp_tool.py saves base64 to temp file; read it for the UI
                    try:
                        sc_path = result_text.split("saved:")[1].rstrip("]").strip()
                        with open(sc_path, "r") as f:
                            screenshot_data = f.read()
                        is_screenshot = _is_base64_png_blob(screenshot_data)
                    except Exception:
                        pass
                elif _is_base64_png_blob(result_text):
                    is_screenshot = True
                    screenshot_data = result_text

                if is_screenshot and screenshot_data:
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": "result",
                        "data": screenshot_data,
                        "is_screenshot": True,
                        "visible": True,
                    }))
                else:
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": "result",
                        "data": result_text[:3000],
                        "is_screenshot": False,
                    }))

                # Stagnation tracking: build progress signature from last pending tool call
                if pending_tool_calls:
                    last_tc = pending_tool_calls[-1]
                    step_sig = _tool_progress_sig(
                        last_tc["name"],
                        last_tc["input"],
                        result_text[:1500],
                    )
                    turn_step_sigs.append(step_sig)
                    # Track --text --find queries for find-repetition detection
                    if last_tc["name"] == "Bash":
                        cmd_str = last_tc["input"].get("command", "")
                        if "--text --find" in cmd_str:
                            parts = cmd_str.split("--text --find", 1)
                            if len(parts) > 1:
                                query = parts[1].strip().split('"')[0].strip().lower()
                                if not query and len(parts[1].strip()) > 0:
                                    query = parts[1].strip().split()[0].lower()
                                if query:
                                    turn_find_queries.append(query)

                    nudge_state.live_tool_log.append({
                        "turn": turn,
                        "tool": last_tc["name"],
                        "args": last_tc["input"],
                        "output_preview": result_text[:3000],
                    })

        elif etype == "result":
            response = event.get("result", "")
            new_claude_sid = event.get("session_id")
            if new_claude_sid:
                claude_sessions[sid] = new_claude_sid
                _save_claude_session(sid, new_claude_sid)
                if not is_resume:
                    print(f"  Session: {new_claude_sid[:12]}...")
            cost = event.get("total_cost_usd", 0)
            turns = event.get("num_turns", 0)
            log.info("  Done: %d turns, $%.4f (model=%s)", turns, cost, cli_model)

    await proc.wait()

    # Handle cancel (SIGKILL = -9, SIGTERM = -15)
    if proc.returncode and proc.returncode < 0:
        proc_pid = getattr(proc, "pid", None)
        was_user_cancel = isinstance(proc_pid, int) and proc_pid in user_cancelled_pids
        if isinstance(proc_pid, int):
            user_cancelled_pids.discard(proc_pid)
        if not was_user_cancel:
            stderr_text = (await proc.stderr.read()).decode(errors="replace").strip()
            sig = -proc.returncode
            response = (
                f"Claude CLI terminated unexpectedly (signal {sig})."
                + (f" {stderr_text[:300]}" if stderr_text else "")
            )
        else:
            await ws.send(json.dumps({
                "session_id": sid, "type": "cancelled",
            }))
            await ws.send(json.dumps({"session_id": sid, "type": "done"}))
            return

    # Handle errors
    if proc.returncode != 0 and not response:
        stderr_text = (await proc.stderr.read()).decode().strip()
        # Only retry on stale/invalid session — not on API errors (400, rate limits, etc.)
        _stale_signals = ("session not found", "invalid session", "session_id", "ENOENT", "does not exist")
        if is_resume and any(s in stderr_text.lower() for s in _stale_signals):
            print(f"  Stale session ({stderr_text[:80]}), starting fresh...")
            del claude_sessions[sid]
            await asyncio.sleep(1)  # let API state settle
            return await handle_message_claude(ws, sid, user_text, model)
        if is_resume and proc.returncode != 0:
            log.info("[%s] Resume failed (exit %d): %s", sid, proc.returncode, stderr_text[:200])
        response = f"Error: {stderr_text}" if stderr_text else f"Error: exit code {proc.returncode}"

    if response:
        # Save assistant response locally
        _append_message("assistant", response)
        await ws.send(json.dumps({
            "session_id": sid, "type": "text", "data": response,
        }))
    await ws.send(json.dumps({"session_id": sid, "type": "done"}))


async def handle_message_codex(ws, sid: str, user_text: str, model: str = ""):
    """Single codex exec call with JSON event parsing and session resume."""
    codex_model = _resolve_codex_model(model)

    # Save user message locally
    _append_message("user", user_text)

    codex_sid = codex_sessions.get(sid)
    # Invalidate session if model changed — thread is model-specific
    if codex_sid:
        saved = _load_codex_session()
        saved_model = saved.get("model", "")
        if saved_model and saved_model != codex_model:
            log.info("  Codex model switched (%s → %s), starting fresh session", saved_model, codex_model)
            codex_sessions.pop(sid, None)
            codex_sid = None
    is_resume = bool(codex_sid)
    log.info(
        "  Calling Codex CLI (model=%s, effort=%s)%s...",
        codex_model,
        CODEX_REASONING_EFFORT if CODEX_REASONING_EFFORT in {"low", "medium", "high"} else "default",
        f" (resume {codex_sid[:12]})" if is_resume else " (new)",
    )

    env = dict(os.environ)
    env["CDP_AGENT_ID"] = AGENT_ID
    env["CDP_RELAY_HOST"] = RELAY_HOST
    env["CDP_RELAY_PORT"] = str(RELAY_PORT)

    output_file = os.path.join(
        tempfile.gettempdir(),
        f"unchained_codex_last_{os.getpid()}_{int(time.time() * 1000)}_{sid[-8:]}",
    )
    config_args = []
    if CODEX_REASONING_EFFORT in {"low", "medium", "high"}:
        config_args = ["-c", f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"']

    if is_resume:
        cmd = [
            CODEX_BIN, "exec",
            *config_args,
            "--output-last-message", output_file,
            "resume",
            "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
            "-m", codex_model,
            codex_sid, "-",
        ]
        cmd_cwd = None
    else:
        cmd = [
            CODEX_BIN, "exec",
            *config_args,
            "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
            "--output-last-message", output_file,
            "-C", CWD, "-m", codex_model,
            "-",
        ]
        cmd_cwd = CWD

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cmd_cwd,
        start_new_session=True,
    )
    active_procs[sid] = proc

    codex_input = _build_codex_prompt(user_text, is_resume=is_resume)
    proc.stdin.write(codex_input.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    response = ""
    streamed_text = ""
    error_text = ""
    codex_tool_items: dict[str, tuple[str, str]] = {}
    timed_out = False
    deadline = time.monotonic() + CODEX_MAX_RUNTIME_S if CODEX_MAX_RUNTIME_S > 0 else None

    while True:
        read_timeout = 1.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            read_timeout = min(read_timeout, max(0.05, remaining))
        try:
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
        except asyncio.TimeoutError:
            continue
        if not raw_line:
            break
        line = raw_line.decode(errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        if etype == "thread.started":
            thread_id = event.get("thread_id", "")
            if thread_id:
                codex_sessions[sid] = thread_id
                _save_codex_session(sid, thread_id, model=codex_model)
                if not is_resume:
                    print(f"  Codex session: {thread_id[:12]}...")
            continue

        if etype == "item.started":
            item = event.get("item") or {}
            item_id = str(item.get("id", "")).strip()
            item_type = (item.get("type") or "").lower()
            if item_type == "command_execution":
                tool_name, tool_input = _codex_tool_name_and_input(str(item.get("command", "")))
                if item_id:
                    codex_tool_items[item_id] = (tool_name, tool_input)
                await ws.send(json.dumps({
                    "session_id": sid, "type": "tool_start",
                    "name": tool_name, "input": tool_input,
                }))
            continue

        if etype == "item.completed":
            item = event.get("item") or {}
            item_id = str(item.get("id", "")).strip()
            item_type = (item.get("type") or "").lower()
            if item_type == "command_execution":
                tool_name, tool_input = codex_tool_items.pop(
                    item_id, _codex_tool_name_and_input(str(item.get("command", "")))
                )
                out = str(item.get("aggregated_output") or "").strip()
                if not out:
                    exit_code = item.get("exit_code")
                    status = str(item.get("status") or "").lower()
                    if status == "failed" or (isinstance(exit_code, int) and exit_code != 0):
                        out = f"command failed (exit_code={exit_code})" if exit_code is not None else "command failed"
                    else:
                        out = "command completed"
                # Detect screenshot results from cdp_tool.py
                codex_is_screenshot = False
                codex_screenshot_data = None
                log.info("  Codex tool output (first 200): %s", out[:200])
                if "saved:" in out and "screenshot captured" in out:
                    try:
                        sc_path = out.split("saved:")[1].rstrip("]").strip()
                        log.info("  Codex screenshot path: %s", sc_path)
                        with open(sc_path, "r") as f:
                            codex_screenshot_data = f.read()
                        codex_is_screenshot = _is_base64_png_blob(codex_screenshot_data)
                        log.info("  Codex screenshot valid: %s (len=%d)", codex_is_screenshot, len(codex_screenshot_data))
                    except Exception as e:
                        log.info("  Codex screenshot read failed: %s", e)
                else:
                    # Fallback: check if unchained_last_screenshot.b64 was updated
                    import tempfile as _tf
                    _sc_fallback = os.path.join(_tf.gettempdir(), "unchained_last_screenshot.b64")
                    if os.path.isfile(_sc_fallback):
                        _sc_age = time.time() - os.path.getmtime(_sc_fallback)
                        log.info("  Codex screenshot fallback: file age=%.1fs", _sc_age)
                        if _sc_age < 30:  # file updated within last 30 seconds
                            try:
                                with open(_sc_fallback, "r") as f:
                                    codex_screenshot_data = f.read()
                                codex_is_screenshot = _is_base64_png_blob(codex_screenshot_data)
                                log.info("  Codex screenshot fallback valid: %s", codex_is_screenshot)
                            except Exception as e:
                                log.info("  Codex screenshot fallback failed: %s", e)
                if codex_is_screenshot and codex_screenshot_data:
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": tool_name, "data": codex_screenshot_data,
                        "is_screenshot": True, "visible": True,
                    }))
                else:
                    await ws.send(json.dumps({
                        "session_id": sid, "type": "tool_result",
                        "name": tool_name, "data": out[:12000],
                        "is_screenshot": False, "visible": None,
                    }))
                continue
            if item_type in ("assistant_message", "message", "output_text"):
                text_bits = _collect_text_strings(item)
                if text_bits:
                    response = "\n".join(text_bits).strip()
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

        if etype == "turn.failed":
            err = event.get("error") or {}
            msg = err.get("message", "") if isinstance(err, dict) else ""
            if isinstance(msg, str) and msg.strip():
                error_text = msg.strip()
            continue

        if etype == "turn.completed":
            bits = _collect_text_strings(event)
            if bits:
                maybe = "\n".join(bits).strip()
                if maybe and len(maybe) > len(response):
                    response = maybe
            continue

        if etype == "error":
            msg = event.get("message", "")
            if isinstance(msg, str) and msg.strip():
                error_text = msg.strip()
            continue

    if timed_out:
        error_text = f"timed out after {CODEX_MAX_RUNTIME_S}s"
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

    # Preferred fallback: Codex CLI can write the final assistant text to a file.
    if not response:
        try:
            if os.path.exists(output_file):
                with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                    file_text = f.read().strip()
                if file_text:
                    response = file_text
        except Exception:
            pass

    if not response and streamed_text:
        response = streamed_text.strip()

    if timed_out and not response:
        response = (
            f"Codex CLI timed out after {CODEX_MAX_RUNTIME_S}s before returning a final response. "
            "Try a narrower prompt or switch to gpt-5.1-codex-mini for faster results."
        )

    if proc.returncode and proc.returncode < 0:
        proc_pid = getattr(proc, "pid", None)
        was_user_cancel = isinstance(proc_pid, int) and proc_pid in user_cancelled_pids
        if isinstance(proc_pid, int):
            user_cancelled_pids.discard(proc_pid)
        if was_user_cancel:
            await ws.send(json.dumps({"session_id": sid, "type": "cancelled"}))
            await ws.send(json.dumps({"session_id": sid, "type": "done"}))
            try:
                if os.path.exists(output_file):
                    os.remove(output_file)
            except Exception:
                pass
            return
        if not response:
            if timed_out:
                response = (
                    f"Codex CLI timed out after {CODEX_MAX_RUNTIME_S}s before returning a final response. "
                    "Try a narrower prompt or switch to gpt-5.1-codex-mini for faster results."
                )
            else:
                sig = -proc.returncode
                response = f"Codex CLI terminated unexpectedly (signal {sig})."

    if not response:
        stderr_text = (await proc.stderr.read()).decode(errors="replace").strip()
        # Only retry on stale/invalid thread — not on API errors or transient failures
        _stale_signals = ("thread not found", "invalid thread", "thread_id", "not found", "does not exist")
        combined_err = (error_text + " " + stderr_text).lower()
        if is_resume and not timed_out and any(s in combined_err for s in _stale_signals):
            log.info(
                "  Codex stale session (%s), starting fresh",
                (error_text or stderr_text or f"exit code {proc.returncode}")[:120],
            )
            codex_sessions.pop(sid, None)
            _save_codex_session("", "")
            await asyncio.sleep(1)
            return await handle_message_codex(ws, sid, user_text, model)
        if is_resume and not timed_out and (proc.returncode != 0 or error_text):
            log.info("[%s] Codex resume error (exit %d, keeping session): %s",
                     sid, proc.returncode or 0, (error_text or stderr_text)[:200])
        if error_text:
            response = f"Codex CLI error: {error_text}"
        elif stderr_text:
            response = f"Codex CLI error: {stderr_text}"
        elif proc.returncode != 0:
            response = f"Codex CLI error: exit code {proc.returncode}"
        else:
            response = "Codex CLI finished without a final response."

    _append_message("assistant", response)
    await ws.send(json.dumps({"session_id": sid, "type": "text", "data": response}))
    await ws.send(json.dumps({"session_id": sid, "type": "done"}))
    try:
        if os.path.exists(output_file):
            os.remove(output_file)
    except Exception:
        pass


async def handle_message(ws, sid: str, user_text: str, model: str = ""):
    """Dispatch to local Claude CLI or Codex CLI handler by model prefix."""
    if _is_codex_cli_model(model):
        if shutil.which(CODEX_BIN) is None:
            await ws.send(json.dumps({
                "session_id": sid,
                "type": "error",
                "data": "Codex CLI is not installed. Install codex and run `codex login`.",
            }))
            await ws.send(json.dumps({"session_id": sid, "type": "done"}))
            return
        return await handle_message_codex(ws, sid, user_text, model)
    return await handle_message_claude(ws, sid, user_text, model)


async def main():
    if not KEY:
        print("ERROR: UNCHAINED_API_KEY env var not set.", file=sys.stderr)
        print("  export UNCHAINED_API_KEY=uc_live_...", file=sys.stderr)
        sys.exit(1)

    # Preflight: verify CLAUDE.md, version, and chrome_bridge
    print(f"Agent: {AGENT_ID}")
    if _claude_md_found:
        print("CLAUDE.md: loaded")
    else:
        print("WARNING: CLAUDE.md not found — agent running with basic system prompt only.")
        print("  Place CLAUDE.md in the agent package directory for full browsing methodology.")

    update_msg = check_for_updates()
    if update_msg:
        print(f"\n  {update_msg}\n")

    print("Checking Chrome bridge connectivity...")
    if not check_chrome_bridge():
        print("WARNING: Chrome bridge offline — browser tools will fail.")
        print("The agent will still work with WebFetch/WebSearch only.\n")
    if shutil.which(CODEX_BIN) is None:
        print(f"WARNING: {CODEX_BIN} not found in PATH — codex-cli model lane will be unavailable.")

    # Restore claude session from local storage
    saved = _load_claude_session()
    if saved.get("session_id") and saved.get("chat_session_id"):
        claude_sessions[saved["chat_session_id"]] = saved["session_id"]
        print(f"Restored session: {saved['session_id'][:12]}... for {saved['chat_session_id']}")
    saved_codex = _load_codex_session()
    if saved_codex.get("session_id") and saved_codex.get("chat_session_id"):
        codex_sessions[saved_codex["chat_session_id"]] = saved_codex["session_id"]
        print(f"Restored Codex session: {saved_codex['session_id'][:12]}... for {saved_codex['chat_session_id']}")

    while True:
        try:
            print(f"Connecting to {SERVER} ...")
            ws = await websockets.connect(SERVER, ping_interval=20, ping_timeout=30)
            await ws.send(json.dumps({
                "key": KEY,
                "capabilities": {
                    "claude_cli": True,
                    "codex_cli": bool(shutil.which(CODEX_BIN)),
                },
            }))
            resp = json.loads(await ws.recv())
            assert resp["type"] == "auth_ok", f"Auth failed: {resp}"
            print("Authenticated. Waiting for messages...")

            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "user_message":
                    sid = msg["session_id"]
                    user_text = msg["message"]
                    msg_model = msg.get("model", "")
                    log.info("[%s] User: %s (model=%s)", sid, user_text, msg_model or "default")
                    # Kill any existing process for this session to avoid concurrent API calls
                    existing_proc = active_procs.get(sid)
                    if existing_proc and existing_proc.returncode is None:
                        log.info("[%s] Killing previous process %s before new message", sid, existing_proc.pid)
                        try:
                            os.killpg(existing_proc.pid, 9)
                        except OSError:
                            existing_proc.kill()
                    existing_task = active_tasks.get(sid)
                    if existing_task and not existing_task.done():
                        existing_task.cancel()
                        try:
                            await asyncio.wait_for(asyncio.shield(existing_task), timeout=2.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass
                    task = asyncio.create_task(handle_message(ws, sid, user_text, msg_model))
                    active_tasks[sid] = task
                    task.add_done_callback(
                        lambda t, s=sid: (
                            active_tasks.pop(s, None),
                            active_procs.pop(s, None),
                            log.info("[%s] Done", s),
                        )
                    )
                elif msg.get("type") == "cancel":
                    sid = msg.get("session_id", "")
                    proc = active_procs.get(sid)
                    if proc and proc.returncode is None:
                        if isinstance(proc.pid, int):
                            user_cancelled_pids.add(proc.pid)
                        print(f"\n[{sid}] CANCEL — killing process group {proc.pid}")
                        try:
                            os.killpg(proc.pid, 9)  # SIGKILL entire group
                        except OSError:
                            proc.kill()
                elif msg.get("type") == "get_history":
                    req_id = msg.get("req_id", "")
                    slot = msg.get("slot")
                    data = _load_chat(slot)
                    await ws.send(json.dumps({
                        "type": "history_response",
                        "req_id": req_id,
                        "messages": data.get("messages", []),
                    }))
                elif msg.get("type") == "new_chat":
                    req_id = msg.get("req_id", "")
                    current = _active_slot()
                    _clear_slot(current)
                    claude_sessions.clear()
                    codex_sessions.clear()
                    print(f"[chat] New chat — cleared slot {current}")
                    await ws.send(json.dumps({
                        "type": "new_chat_ok",
                        "req_id": req_id,
                        "active_slot": current,
                    }))
                elif msg.get("type") == "switch_slot":
                    req_id = msg.get("req_id", "")
                    slot = msg.get("slot", 1)
                    if slot not in (1, 2, 3):
                        slot = 1
                    meta = _load_meta()
                    meta["active_slot"] = slot
                    _save_meta(meta)
                    claude_sessions.clear()
                    codex_sessions.clear()
                    # Restore sessions for the new slot
                    saved = _load_claude_session()
                    if saved.get("session_id") and saved.get("chat_session_id"):
                        claude_sessions[saved["chat_session_id"]] = saved["session_id"]
                    saved_codex = _load_codex_session()
                    if saved_codex.get("session_id") and saved_codex.get("chat_session_id"):
                        codex_sessions[saved_codex["chat_session_id"]] = saved_codex["session_id"]
                    print(f"[chat] Switched to slot {slot}")
                    await ws.send(json.dumps({
                        "type": "switch_slot_ok",
                        "req_id": req_id,
                        "active_slot": slot,
                    }))
                elif msg.get("type") == "get_slots":
                    req_id = msg.get("req_id", "")
                    info = _get_slots_info()
                    info["type"] = "slots_response"
                    info["req_id"] = req_id
                    await ws.send(json.dumps(info))

        except Exception as e:
            print(f"Error: {e}. Reconnecting in 3s...")
            import traceback; traceback.print_exc()
            await asyncio.sleep(3)


asyncio.run(main())
