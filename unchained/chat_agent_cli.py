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
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import socket
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse
import uuid

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


def _resolve_local_cli_binary(env_var: str, default_name: str) -> str:
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        return explicit
    discovered = shutil.which(default_name)
    if discovered:
        return discovered
    fallback_dirs: list[str] = []
    if sys.platform == "darwin":
        # launchd often starts the agent with a stripped PATH on macOS.
        fallback_dirs.extend(["/opt/homebrew/bin", "/usr/local/bin"])
    fallback_dirs.append(os.path.expanduser("~/.local/bin"))
    for fallback_dir in fallback_dirs:
        fallback = os.path.join(fallback_dir, default_name)
        if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
            return fallback
    return default_name


def _cli_binary_available(path_or_name: str) -> bool:
    candidate = str(path_or_name or "").strip()
    if not candidate:
        return False
    if os.path.sep in candidate:
        return os.path.isfile(candidate) and os.access(candidate, os.X_OK)
    return shutil.which(candidate) is not None


KEY = os.environ.get("UNCHAINED_API_KEY", "")
SERVER = os.environ.get("UNCHAINED_SERVER", "wss://api.unchainedsky.com/chat/ws")
RELAY_HOST = os.environ.get("UNCHAINED_RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("UNCHAINED_RELAY_PORT", "443"))
CWD = os.path.expanduser("~/unchained-agent/unchained")
CLAUDE_BIN = _resolve_local_cli_binary("CLAUDE_BIN", "claude")
CODEX_BIN = _resolve_local_cli_binary("CODEX_BIN", "codex")
DEFAULT_CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.1-codex-mini")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "low").strip().lower()
CODEX_MAX_RUNTIME_S = int(os.environ.get("CODEX_MAX_RUNTIME_S", "0"))

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
ARCHIVE_DIR = os.path.join(CHAT_DIR, "archive")
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


def _chat_session_id_for_data(data: dict, fallback: str = "") -> str:
    """Return the persisted chat session id for a slot, if any."""
    for key in ("claude_session", "codex_session"):
        saved = data.get(key)
        if not isinstance(saved, dict):
            continue
        chat_session_id = str(saved.get("chat_session_id", "") or "").strip()
        if chat_session_id:
            return chat_session_id
    return fallback


def _archive_slot(slot: int | None = None):
    """Archive a slot's chat data before clearing. No-op if slot is empty."""
    if slot is None:
        slot = _active_slot()
    data = _load_chat(slot)
    msgs = data.get("messages", [])
    if not msgs:
        return None
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ts = int(time.time())
    archive_id = f"{int(time.time() * 1000)}_{slot}_{uuid.uuid4().hex[:8]}"
    preview = ""
    for m in msgs:
        if m.get("role") == "user":
            preview = m.get("content", "")[:80]
            break
    archive = {
        "slot_data": data,
        "archived_at": ts,
        "slot": slot,
        "preview": preview,
        "message_count": len(msgs),
    }
    archive_path = os.path.join(ARCHIVE_DIR, f"{archive_id}.json")
    with open(archive_path, "w") as f:
        json.dump(archive, f)
    log.info("[archive] Archived slot %d as %s (%d messages)", slot, archive_id, len(msgs))
    return archive_id


def _list_archives(limit: int = 50) -> list[dict]:
    """List archived chats, newest first."""
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    entries = []
    for fname in os.listdir(ARCHIVE_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(ARCHIVE_DIR, fname)
        try:
            with open(fpath, "r") as f:
                arc = json.load(f)
            entries.append({
                "id": fname[:-5],  # strip .json
                "preview": arc.get("preview", ""),
                "message_count": arc.get("message_count", 0),
                "archived_at": arc.get("archived_at", 0),
                "slot": arc.get("slot", 0),
            })
        except (json.JSONDecodeError, OSError):
            continue
    entries.sort(key=lambda e: e["archived_at"], reverse=True)
    return entries[:limit]


def _safe_archive_path(archive_id: str) -> str | None:
    """Validate archive_id and return safe path, or None if invalid."""
    if not archive_id or "/" in archive_id or "\\" in archive_id or ".." in archive_id:
        return None
    fpath = os.path.join(ARCHIVE_DIR, f"{archive_id}.json")
    if not os.path.realpath(fpath).startswith(os.path.realpath(ARCHIVE_DIR) + os.sep):
        return None
    return fpath


def _restore_archive(archive_id: str) -> dict | None:
    """Load an archive file and return its slot_data, or None."""
    fpath = _safe_archive_path(archive_id)
    if fpath is None:
        return None
    try:
        with open(fpath, "r") as f:
            arc = json.load(f)
        return arc.get("slot_data")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _delete_archive(archive_id: str) -> bool:
    """Delete an archive file. Returns True if deleted."""
    fpath = _safe_archive_path(archive_id)
    if fpath is None:
        return False
    try:
        os.remove(fpath)
        return True
    except OSError:
        return False


def _restore_archive_into_slot(archive_id: str, slot: int | None = None) -> tuple[dict | None, str]:
    """Archive the current slot, replace it with an archived chat, and restore runtime mappings."""
    slot_data = _restore_archive(archive_id)
    if slot_data is None:
        return None, ""
    if slot is None:
        slot = _active_slot()
    _archive_slot(slot)
    _save_chat(slot_data, slot)

    # Session resume state is currently modeled as a single active mapping per
    # agent process. Restore runs on the serialized agent control path, so we
    # rebuild those maps wholesale for the restored slot.
    claude_sessions.clear()
    codex_sessions.clear()
    _context_injected.clear()

    saved = slot_data.get("claude_session", {})
    if saved.get("session_id") and saved.get("chat_session_id"):
        claude_sessions[saved["chat_session_id"]] = saved["session_id"]
    saved_codex = slot_data.get("codex_session", {})
    if saved_codex.get("session_id") and saved_codex.get("chat_session_id"):
        codex_sessions[saved_codex["chat_session_id"]] = saved_codex["session_id"]

    return slot_data, _chat_session_id_for_data(slot_data)


def _clear_slot(slot: int | None = None):
    """Archive then clear chat history and claude session for a slot."""
    if slot is None:
        slot = _active_slot()
    _archive_slot(slot)
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


def _agent_root() -> str:
    """Return the packaged agent root containing start/update scripts."""
    candidates = [
        Path(CWD).resolve().parent,
        Path(CWD).resolve(),
        Path.cwd().resolve(),
    ]
    for candidate in candidates:
        if (candidate / "update.sh").exists() or (candidate / "update.ps1").exists():
            return str(candidate)
    return str(candidates[0])


def _local_version_path() -> str | None:
    """Return the packaged version.txt path if present."""
    roots = [
        os.path.join(CWD, "..", "version.txt"),
        os.path.join(CWD, "version.txt"),
        os.path.join(_agent_root(), "version.txt"),
    ]
    for path in roots:
        if os.path.exists(path):
            return path
    return None


def _local_version() -> str:
    """Return the packaged local version string when available."""
    version_path = _local_version_path()
    if not version_path:
        return ""
    try:
        with open(version_path) as f:
            return f.read().strip()
    except OSError:
        return ""


def check_for_updates() -> str | None:
    """Check for agent updates. Returns a warning string if outdated, else None."""
    import urllib.request
    import urllib.error

    # Read local version
    local_version = _local_version()
    if not local_version:
        return None  # No version file — skip check (dev environment)

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


def _pid_exists(pid: int) -> bool:
    """Return True if PID appears to be alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_supervisor_alive(agent_root: str) -> bool:
    """Return True if the daemon supervisor PID file points at a live process."""
    if os.name == "nt":
        pid_path = os.path.join(agent_root, ".agent.pid.json")
        if not os.path.exists(pid_path):
            return False
        try:
            with open(pid_path, "r") as f:
                pid_state = json.load(f)
            agent_pid = int(pid_state.get("agent_pid") or 0)
            bridge_pid = int(pid_state.get("bridge_pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return _pid_exists(agent_pid) or _pid_exists(bridge_pid)

    pid_path = os.path.join(agent_root, ".agent.pid")
    if not os.path.exists(pid_path):
        return False
    try:
        with open(pid_path, "r") as f:
            pid = int((f.read() or "").strip())
    except (OSError, ValueError):
        return False
    return _pid_exists(pid)


def _ps_output(*args: str) -> str:
    """Run ps and return decoded stdout, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["ps", *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return ""
    return proc.stdout or ""


def _launched_by_launchd() -> bool:
    """Best-effort detection for launchd-managed foreground start.sh jobs."""
    if sys.platform != "darwin":
        return False
    try:
        pid = os.getppid()
    except Exception:
        return False
    for _ in range(5):
        if pid <= 1:
            break
        cmd = _ps_output("-o", "comm=", "-p", str(pid)).strip()
        if os.path.basename(cmd) == "launchd":
            return True
        parent_raw = _ps_output("-o", "ppid=", "-p", str(pid)).strip()
        try:
            pid = int(parent_raw)
        except ValueError:
            break
    return False


def _current_run_hint(agent_root: str) -> str:
    """Classify the current runtime so post-update restart can avoid duplicates."""
    if _daemon_supervisor_alive(agent_root):
        return "daemon"
    if _launched_by_launchd():
        return "launchd"
    return "manual"


def _run_logged(cmd: list[str], *, cwd: str, timeout_seconds: float | None = None) -> int:
    """Run a subprocess and mirror combined output into the agent log."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = str(getattr(exc, "stdout", "") or "").strip()
        if output:
            for line in output.splitlines():
                log.info("[self-update] %s", line)
        log.error("[self-update] command timed out after %ss: %s", timeout_seconds, shlex.join(cmd))
        return 124
    output = (proc.stdout or "").strip()
    if output:
        for line in output.splitlines():
            log.info("[self-update] %s", line)
    return proc.returncode


def _localhost_port_open(host: str, port: int, *, timeout_seconds: float = 0.75) -> bool:
    """Return True when a local TCP port accepts a connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _spawn_detached(cmd: list[str], *, cwd: str, log_path: str | None = None) -> None:
    """Start a background process detached from the current terminal."""
    stdout_target: object = subprocess.DEVNULL
    stderr_target: object = subprocess.DEVNULL
    stdout_fd: int | None = None
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        stdout_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        stdout_target = stdout_fd
        stderr_target = stdout_fd
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdout": stdout_target,
        "stderr": stderr_target,
    }
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    finally:
        if stdout_fd is not None:
            os.close(stdout_fd)


_RESEARCH_DESK_LOCAL_HOST = "127.0.0.1"
_RESEARCH_DESK_LOCAL_PORT = 8766
_RESEARCH_DESK_BRIDGE_PORT = 9333
# Setup runs synchronously, then each network-visible bootstrap step gets this
# bounded reachability window. The total install-plus-bootstrap flow can exceed
# this value because setup + bridge + serve each consume separate time.
_RESEARCH_DESK_BOOTSTRAP_TIMEOUT_SECONDS = 12.0
_RESEARCH_DESK_BOOTSTRAP_POLL_INTERVAL_SECONDS = 0.5
_RESEARCH_DESK_BRIDGE_START_TIMEOUT_SECONDS = 20.0
_RESEARCH_DESK_AUTO_OPEN_URL = f"http://{_RESEARCH_DESK_LOCAL_HOST}:{_RESEARCH_DESK_LOCAL_PORT}/"


def _research_desk_bootstrap_log_path(name: str) -> str:
    return os.path.join(_log_dir, name)


def _path_within_prefix(path: str, prefix: str) -> bool:
    if not path or not prefix:
        return False
    abs_path = os.path.abspath(path)
    real_path = os.path.realpath(path)
    prefix = os.path.realpath(prefix)
    prefix_sep = prefix + os.sep
    return abs_path.startswith(prefix_sep) or real_path.startswith(prefix_sep)


def _wait_for_local_port(host: str, port: int, *, timeout_seconds: float) -> bool:
    """Poll until a local TCP port opens or the timeout expires."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _localhost_port_open(host, port):
            return True
        time.sleep(_RESEARCH_DESK_BOOTSTRAP_POLL_INTERVAL_SECONDS)
    return _localhost_port_open(host, port)


def _ensure_research_desk_bridge_running(python_bin: str) -> None:
    """Start the default Research Desk bridge unless it is already reachable."""
    if _localhost_port_open(_RESEARCH_DESK_LOCAL_HOST, _RESEARCH_DESK_BRIDGE_PORT):
        log.info(
            "[research-desk-install] bridge already reachable on %s:%s",
            _RESEARCH_DESK_LOCAL_HOST,
            _RESEARCH_DESK_BRIDGE_PORT,
        )
        return
    bridge_dir = os.path.join(_agent_root(), "unchained")
    bridge_cmd = [python_bin, "-m", "unchained_pyreplab", "bridge-start", "--daemon"]
    if os.path.isfile(os.path.join(bridge_dir, "chrome_bridge.py")):
        bridge_cmd.extend(["--bridge-dir", bridge_dir])
    log.info("[research-desk-install] starting bridge: %s", shlex.join(bridge_cmd))
    rc = _run_logged(
        bridge_cmd,
        cwd=_agent_root(),
        timeout_seconds=_RESEARCH_DESK_BRIDGE_START_TIMEOUT_SECONDS,
    )
    if rc != 0:
        raise SystemExit(rc)
    if not _wait_for_local_port(
        _RESEARCH_DESK_LOCAL_HOST,
        _RESEARCH_DESK_BRIDGE_PORT,
        timeout_seconds=_RESEARCH_DESK_BOOTSTRAP_TIMEOUT_SECONDS,
    ):
        log.error(
            "[research-desk-install] bridge did not become reachable on %s:%s",
            _RESEARCH_DESK_LOCAL_HOST,
            _RESEARCH_DESK_BRIDGE_PORT,
        )
        raise SystemExit(1)


def _ensure_research_desk_server_running(python_bin: str) -> None:
    """Start the local Research Desk web server unless it is already serving."""
    if _localhost_port_open(_RESEARCH_DESK_LOCAL_HOST, _RESEARCH_DESK_LOCAL_PORT):
        log.info(
            "[research-desk-install] local desk already reachable on %s:%s",
            _RESEARCH_DESK_LOCAL_HOST,
            _RESEARCH_DESK_LOCAL_PORT,
        )
        return
    serve_cmd = [
        python_bin,
        "-m",
        "unchained_pyreplab",
        "serve",
        "--host",
        _RESEARCH_DESK_LOCAL_HOST,
        "--port",
        str(_RESEARCH_DESK_LOCAL_PORT),
    ]
    log.info("[research-desk-install] starting local desk: %s", shlex.join(serve_cmd))
    _spawn_detached(
        serve_cmd,
        cwd=_agent_root(),
        log_path=_research_desk_bootstrap_log_path("research-desk-serve.log"),
    )
    if not _wait_for_local_port(
        _RESEARCH_DESK_LOCAL_HOST,
        _RESEARCH_DESK_LOCAL_PORT,
        timeout_seconds=_RESEARCH_DESK_BOOTSTRAP_TIMEOUT_SECONDS,
    ):
        log.error(
            "[research-desk-install] local desk did not become reachable on %s:%s",
            _RESEARCH_DESK_LOCAL_HOST,
            _RESEARCH_DESK_LOCAL_PORT,
        )
        raise SystemExit(1)


def _open_research_desk_local() -> None:
    """Open the local Research Desk in the user's default browser."""
    url = _RESEARCH_DESK_AUTO_OPEN_URL
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
            return
        open_cmd = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
        _spawn_detached(open_cmd, cwd=_agent_root())
    except Exception as exc:
        log.warning("[research-desk-install] could not auto-open local desk: %s", exc)


def _terminate_windows_runtime(helper_pid: int):
    """Stop current chat/bridge processes without touching Startup autostart."""
    cmd = (
        "$helperPid=[int]$env:UNCHAINED_HELPER_PID; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $helperPid -and $_.CommandLine -and ("
        "$_.CommandLine -like '*chat_agent_cli.py*' -or "
        "$_.CommandLine -like '*chrome_bridge.py*'"
        ") } | ForEach-Object { "
        "try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} "
        "}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        check=False,
        env={**os.environ, "UNCHAINED_HELPER_PID": str(helper_pid)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_windows_daemon(agent_root: str):
    """Start the packaged Windows agent in background mode."""
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "start.ps1", "-Daemon"],
        cwd=agent_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def _other_posix_chat_agent_running(helper_pid: int) -> bool:
    """Return True if another chat_agent_cli process is already running."""
    listing = _ps_output("-ax", "-o", "pid=,command=")
    if not listing:
        return False
    for line in listing.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1]
        if pid == helper_pid:
            continue
        if "chat_agent_cli.py" not in command:
            continue
        if "--self-update-helper" in command:
            continue
        return True
    return False


def _run_self_update_helper():
    """Detached helper that updates the package and restarts it if needed."""
    agent_root = os.environ.get("UNCHAINED_AGENT_ROOT", "").strip() or _agent_root()
    run_hint = os.environ.get("UNCHAINED_UPDATE_RUN_HINT", "").strip() or "manual"
    helper_pid = os.getpid()
    log.info("[self-update] helper starting (root=%s mode=%s)", agent_root, run_hint)
    time.sleep(1.0)
    before_version = _local_version()

    if os.name == "nt":
        update_cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "update.ps1"]
    else:
        update_cmd = ["bash", "update.sh"]
    rc = _run_logged(update_cmd, cwd=agent_root)
    if rc != 0:
        log.error("[self-update] update command failed with exit code %s", rc)
        raise SystemExit(rc)
    after_version = _local_version()
    if before_version and after_version and before_version == after_version:
        log.info("[self-update] local version unchanged at %s; skipping restart", after_version)
        return

    if os.name == "nt":
        _terminate_windows_runtime(helper_pid)
        time.sleep(2.0)
        _start_windows_daemon(agent_root)
        log.info("[self-update] windows daemon restart started")
        return

    trigger_pid_raw = os.environ.get("UNCHAINED_UPDATE_TRIGGER_PID", "").strip()
    try:
        trigger_pid = int(trigger_pid_raw)
    except ValueError:
        trigger_pid = 0

    if trigger_pid > 0:
        try:
            os.kill(trigger_pid, signal.SIGTERM)
        except OSError:
            pass
    subprocess.run(
        ["pkill", "-f", "chrome_bridge.py start"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if run_hint == "daemon":
        log.info("[self-update] waiting for daemon supervisor to restart runtime")
        return

    time.sleep(4.0)
    if _other_posix_chat_agent_running(helper_pid):
        log.info("[self-update] detected restarted chat agent; skipping manual daemon start")
        return

    subprocess.Popen(
        ["bash", "start.sh", "--daemon"],
        cwd=agent_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.info("[self-update] started daemon after update")


def _spawn_remote_update() -> tuple[bool, str]:
    """Launch a detached self-update helper and return (ok, error_message)."""
    agent_root = _agent_root()
    helper_env = dict(os.environ)
    helper_env["UNCHAINED_AGENT_ROOT"] = agent_root
    helper_env["UNCHAINED_UPDATE_TRIGGER_PID"] = str(os.getpid())
    helper_env["UNCHAINED_UPDATE_RUN_HINT"] = _current_run_hint(agent_root)
    helper_cmd = [sys.executable, os.path.abspath(__file__), "--self-update-helper"]
    kwargs = {
        "cwd": agent_root,
        "env": helper_env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(helper_cmd, **kwargs)
    except Exception as e:
        return False, str(e)
    return True, ""


def _research_desk_python_binary() -> str:
    python3_bin = shutil.which("python3")
    current_prefix = os.path.realpath(getattr(sys, "prefix", "") or "")
    base_prefix = os.path.realpath(getattr(sys, "base_prefix", "") or "")
    use_python3_bin = True
    if python3_bin:
        if (
            not current_prefix
            or current_prefix == base_prefix
            or not _path_within_prefix(python3_bin, current_prefix)
        ):
            return python3_bin
        log.info(
            "[research-desk-install] ignoring venv python3 for --user install: %s",
            python3_bin,
        )
        use_python3_bin = False
    if current_prefix and current_prefix != base_prefix:
        base_python = shutil.which("python3", path=os.path.join(base_prefix, "bin"))
        if base_python:
            return base_python
        fallback_candidates = [
            os.path.join(base_prefix, "bin", "python3"),
            os.path.join(base_prefix, "python3"),
            "/usr/bin/python3",
        ]
        for candidate in fallback_candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    if python3_bin and use_python3_bin:
        return python3_bin
    return sys.executable


def _research_desk_launcher_prefix() -> str:
    python_bin = _research_desk_python_binary()
    if os.path.basename(python_bin) == "python3" and " " not in python_bin:
        return "python3 -m unchained_pyreplab"
    return f"{shlex.quote(python_bin)} -m unchained_pyreplab"


_RESEARCH_DESK_PACKAGE_PATH = "/web/research-desk/files"
_RESEARCH_DESK_PACKAGE_URL_RE = re.compile(r"^/web/research-desk/files$")


def _is_allowed_research_desk_package_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {
            "api.unchainedsky.com",
            "unchainedsky.com",
            "127.0.0.1:8080",
            "127.0.0.1:8088",
            "localhost:8080",
            "localhost:8088",
        }
        and bool(_RESEARCH_DESK_PACKAGE_URL_RE.match(parsed.path))
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _research_desk_package_url() -> str:
    api_url = os.environ.get("UNCHAINED_API_URL", f"https://{RELAY_HOST}").strip()
    if not api_url:
        api_url = f"https://{RELAY_HOST}"
    default_url = f"{api_url.rstrip('/')}{_RESEARCH_DESK_PACKAGE_PATH}"
    override = os.environ.get("UNCHAINED_RESEARCH_DESK_PACKAGE_URL", "").strip()
    if not override:
        return default_url
    if _is_allowed_research_desk_package_url(override):
        return override
    log.warning(
        "[research-desk-install] ignoring invalid package override: %s",
        override,
    )
    return default_url


def _download_research_desk_package(package_url: str) -> str:
    req = urllib.request.Request(
        package_url,
        headers={"Authorization": f"Bearer {KEY}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        fd, temp_path = tempfile.mkstemp(prefix="research-desk-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(resp.read())
        except Exception:
            os.unlink(temp_path)
            raise
    return temp_path


def _remote_research_desk_install_supported() -> bool:
    return bool(_research_desk_python_binary())


def _run_research_desk_install_helper():
    """Detached helper that installs and bootstraps Research Desk locally."""
    python_bin = _research_desk_python_binary()
    package_url = _research_desk_package_url()
    log.info(
        "[research-desk-install] helper starting (python=%s package=%s)",
        python_bin,
        package_url,
    )
    package_path = _download_research_desk_package(package_url)
    try:
        install_cmd = [python_bin, "-m", "pip", "install", "--user", "--upgrade", package_path]
        rc = _run_logged(install_cmd, cwd=_agent_root())
        if rc != 0:
            log.error("[research-desk-install] install command failed with exit code %s", rc)
            raise SystemExit(rc)
        setup_cmd = [python_bin, "-m", "unchained_pyreplab", "setup"]
        log.info("[research-desk-install] running setup: %s", shlex.join(setup_cmd))
        rc = _run_logged(setup_cmd, cwd=_agent_root())
        if rc != 0:
            log.error("[research-desk-install] setup command failed with exit code %s", rc)
            raise SystemExit(rc)
        _ensure_research_desk_bridge_running(python_bin)
        _ensure_research_desk_server_running(python_bin)
        _open_research_desk_local()
        log.info("[research-desk-install] install and bootstrap completed")
    finally:
        try:
            os.unlink(package_path)
        except OSError:
            pass


def _spawn_remote_research_desk_install() -> tuple[bool, str, str]:
    """Launch a detached helper that installs Research Desk locally."""
    helper_env = dict(os.environ)
    helper_env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    helper_env.setdefault("PYTHONUTF8", "1")
    helper_cmd = [sys.executable, os.path.abspath(__file__), "--research-desk-install-helper"]
    kwargs = {
        "cwd": _agent_root(),
        "env": helper_env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(helper_cmd, **kwargs)
    except Exception as e:
        return False, str(e), ""
    return True, "", _research_desk_launcher_prefix()


SYSTEM_PROMPT = f"""You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
You MUST use browser tools to answer ANY factual question — never answer from memory.
Your training data is outdated. Always browse to get live, current data.
{_claude_md_warning}
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
uv run python cdp_tool.py press_enter                   # Press Enter in the focused element
uv run python cdp_tool.py submit_form                   # Submit the focused form
uv run python cdp_tool.py js "document.title"           # Run JavaScript on page
uv run python cdp_tool.py intel --probe                 # Page fingerprint + Bayesian strategy ranking
uv run python cdp_tool.py intel --extract               # Extract structured data (auto strategy)
uv run python cdp_tool.py intel --stores                # List JS data store globals
uv run python cdp_tool.py intel --find-paths GLOBAL key # Find data arrays in a global
uv run python cdp_tool.py screenshot                    # Screenshot (CAPTCHAs only, ~2100 tok)
uv run python cdp_tool.py pdf                           # Save current page as PDF
uv run python cdp_tool.py tabs                          # List open Chrome tabs
uv run python cdp_tool.py new-tab https://example.com   # Open URL in a new tab
uv run python cdp_tool.py close-tab <tab_id>            # Close a tab by ID

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

## Tab Management

uv run python cdp_tool.py tabs                              # List all open tabs (ID, title, URL)
uv run python cdp_tool.py new-tab https://example.com       # Open new tab at URL
uv run python cdp_tool.py close-tab <tab_id>                # Close a tab by ID prefix

Add --tab <id> to ANY command to target a specific tab:
uv run python cdp_tool.py navigate https://example.com --tab <tab_id>
uv run python cdp_tool.py ddm --llm-2pass --cols 60 --tab <tab_id>
uv run python cdp_tool.py js "document.title" --tab <tab_id>

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
bash ../update.sh

This checks the server for a newer version, downloads code updates, and prints
"restart required" if an update was applied. Never touch .env or .venv.

## Key Rules
- ALWAYS use tools. NEVER answer from memory or fabricate data.
- navigate and click already return DDM page layout — do NOT call ddm separately after them. Only call ddm after type, or for --text, --at, --find, --js flags.
- Click input fields before typing.
- To submit a search or form, use `press_enter` or `submit_form` instead of typing `\\n` or `/n`.
- SPA widgets: CDP clicks often fail — use `js` with .click() instead.
- DDM only sees current viewport — scroll + remap for content below fold.
- Be concise — report findings, not process.
"""


SCHEDULER_TOOL_PROMPT = """## Scheduler Tool (this turn only)

The user explicitly armed scheduler access by starting the message with `/schedule`.
Use the scheduler tool only for scheduled-task management. It reads and writes the
same local task list shown in `/scheduler`.

Commands:
- uv run python scheduler_tool.py list
- uv run python scheduler_tool.py preview --id "task-id" --prompt "full prompt" --daily-at 09:00
- uv run python scheduler_tool.py upsert --id "task-id" --prompt "full prompt" --daily-at 09:00
- uv run python scheduler_tool.py delete --id "task-id"

Rules:
- If modifying an existing task, list it first and preserve fields the user did not ask to change.
- Scheduled prompts must be self-contained. Do not save "do this again" style prompts.
- If the user is ambiguous about the schedule or desired task, ask a concise follow-up question.
"""


def _claude_system_prompt(*, scheduler_armed: bool) -> str:
    if not scheduler_armed:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{SCHEDULER_TOOL_PROMPT}"


# Codex CLI does not currently support a dedicated `--system-prompt` flag like
# Claude CLI. Inject equivalent browser-agent instructions into the prompt text.
CODEX_RESUME_REMINDER = """You are an autonomous browser agent controlling a real Chrome browser via CDP tools.
Always use browser tools for factual requests.
Your working directory is already set. NEVER use `cd` — run commands directly.

Use CDP tools via Bash:
- uv run python cdp_tool.py ddm --llm-2pass --cols 60
- uv run python cdp_tool.py ddm --text --max 5000
- uv run python cdp_tool.py navigate "https://example.com"
- uv run python cdp_tool.py click X Y
- uv run python cdp_tool.py type "text here"
- uv run python cdp_tool.py press_enter
- uv run python cdp_tool.py submit_form
- uv run python cdp_tool.py js "document.title"
- uv run python cdp_tool.py intel --probe

IMPORTANT shell rules:
- NEVER use `cd` — your working directory is already correct.
- Always double-quote URLs: uv run python cdp_tool.py navigate "https://site.com/path?a=1&b=2"
- Always double-quote text arguments: uv run python cdp_tool.py type "my search query"
- Never use newlines inside commands — keep each command on one line.
- To submit a search or form, use `press_enter` or `submit_form` instead of typing `\\n` or `/n`.

Never answer factual browser tasks from memory when tool use is available.
"""


def _build_codex_prompt(user_text: str, *, is_resume: bool, scheduler_armed: bool = False, history_context: str = "") -> str:
    """Build Codex input text with browser-agent instructions."""
    req = (user_text or "").strip()
    if not req:
        return ""
    scheduler_block = f"\n{SCHEDULER_TOOL_PROMPT}\n" if scheduler_armed else ""
    if is_resume:
        return (
            "[AGENT REMINDER]\n"
            f"{CODEX_RESUME_REMINDER}{scheduler_block}\n"
            "[/AGENT REMINDER]\n\n"
            f"User request:\n{req}\n"
        )
    history_block = f"\n{history_context}\n" if history_context else ""
    return (
        "[SYSTEM PROMPT]\n"
        f"{_claude_system_prompt(scheduler_armed=scheduler_armed)}\n"
        "[/SYSTEM PROMPT]\n"
        f"{history_block}\n"
        f"User request:\n{req}\n"
    )


# Map chat session_id → claude session_id for conversation persistence
claude_sessions: dict[str, str] = {}
# Map chat session_id → codex thread/session id for exec resume continuity
codex_sessions: dict[str, str] = {}

# Track which (session_id, lane) pairs already received context injection.
# Keyed by (sid, "claude") or (sid, "codex") so a fresh Codex thread after
# a Claude turn (or after a Codex model switch) still gets its own injection.
_context_injected: set[tuple[str, str]] = set()

# Track active subprocesses and tasks for cancel support
active_procs: dict[str, asyncio.subprocess.Process] = {}
active_tasks: dict[str, asyncio.Task] = {}
# Process PIDs explicitly cancelled by the user via /web/chat/cancel.
# Using PID avoids race conditions when a new turn starts on the same session_id.
user_cancelled_pids: set[int] = set()


def _make_emitter(ws, sid: str, req_id: str):
    """Return an async helper that tags every event with session_id and req_id."""
    async def emit(evt: dict):
        evt["session_id"] = sid
        if req_id:
            evt["req_id"] = req_id
        await ws.send(json.dumps(evt))
    return emit


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
    # Strip /bin/zsh -lc '...' wrapper that Codex CLI adds
    shell_m = re.match(r"^/bin/(?:zsh|bash)\s+-lc\s+'(.+)'$", cmd, re.DOTALL)
    if shell_m:
        cmd = shell_m.group(1).strip()
    if "scheduler_tool.py" in cmd:
        return "scheduler", cmd
    m = re.search(r"cdp_tool\.py\s+([a-zA-Z0-9_-]+)", cmd)
    if m:
        return m.group(1).lower(), cmd
    return "bash", cmd


async def handle_message_claude(
    ws,
    sid: str,
    user_text: str,
    model: str = "",
    tab_id: str = "auto",
    scheduler_armed: bool = False,
    scheduler_grant_id: str = "",
    req_id: str = "",
):
    """Single claude -p call with streaming tool events and session resume."""
    emit = _make_emitter(ws, sid, req_id)
    cli_model = _MODEL_CLI_MAP.get(model, "opus")

    # Save user message locally
    _append_message("user", user_text)

    claude_sid = claude_sessions.get(sid)
    is_resume = claude_sid is not None

    # Context fallback: if no resume session but slot has history, inject summary
    effective_text = user_text
    if not is_resume and (sid, "claude") not in _context_injected:
        _context_injected.add((sid, "claude"))
        data = _load_chat()
        prev_msgs = data.get("messages", [])
        # Exclude the message we just appended (last one)
        prev_msgs = prev_msgs[:-1] if prev_msgs else []
        if prev_msgs:
            # Format last 20 messages as context; strip brackets from content
            # to prevent bracket-tagged prompt fragments from confusing the model.
            history_lines = []
            for m in prev_msgs[-20:]:
                role = m.get("role", "unknown")
                content = m.get("content", "").replace("[", "(").replace("]", ")")
                if len(content) > 300:
                    content = content[:300] + "..."
                history_lines.append(f"{role}: {content}")
            history_summary = "\n".join(history_lines)
            effective_text = (
                f"[Previous conversation context — the session was restarted, "
                f"here is the recent history for continuity]\n"
                f"{history_summary}\n\n"
                f"[New message]\n{user_text}"
            )
            log.info("  Injected %d previous messages as context", len(prev_msgs[-20:]))

    log.info("  Calling Claude (model=%s)%s...", cli_model, f"  (resume {claude_sid[:12]})" if is_resume else " (new)")

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CDP_AGENT_ID"] = AGENT_ID
    env["CDP_RELAY_HOST"] = RELAY_HOST
    env["CDP_RELAY_PORT"] = str(RELAY_PORT)
    env["CDP_TAB_ID"] = tab_id or "auto"
    env["UNCHAINED_CHAT_SESSION_ID"] = sid
    if scheduler_armed and scheduler_grant_id:
        env["UNCHAINED_SCHEDULER_GRANT_ID"] = scheduler_grant_id
    else:
        env.pop("UNCHAINED_SCHEDULER_GRANT_ID", None)

    # Build command with stream-json for real-time tool events
    allowed = "Bash(uv run python cdp_tool.py:*) Bash(bash ../update.sh)"
    if scheduler_armed:
        allowed += " Bash(uv run python scheduler_tool.py:*)"
    tools = ["Bash"]
    # Optional: set CLAUDE_ENABLE_WEB_TOOLS=1 to enable WebFetch/WebSearch
    if os.environ.get("CLAUDE_ENABLE_WEB_TOOLS"):
        allowed += " WebFetch WebSearch"
        tools += ["WebFetch", "WebSearch"]
    cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose",
           "--model", cli_model,
           "--allowedTools", allowed,
           "--system-prompt", _claude_system_prompt(scheduler_armed=scheduler_armed),
           "--tools"] + tools
    if is_resume:
        cmd += ["--resume", claude_sid]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=CWD,
            start_new_session=True,
        )
    except FileNotFoundError:
        await emit({
            "type": "error",
            "data": (
                f"Claude CLI is not installed or not on PATH. "
                f"Expected `{CLAUDE_BIN}`. Install Claude Code or set `CLAUDE_BIN`."
            ),
        })
        await emit({"type": "done"})
        return
    active_procs[sid] = proc

    # Send input and close stdin
    proc.stdin.write(effective_text.encode())
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
                    await emit({
                        "type": "tool_start",
                        "name": "intervention", "input": severity,
                    })
                    await emit({
                        "type": "tool_result",
                        "name": "intervention",
                        "data": (prompt or f"Intervention: {severity}")[:1500],
                        "is_screenshot": False,
                    })
                    # Decay stagnation after nudge
                    nudge_state.apply_nudge_decay()

                    if severity == "hard_stop":
                        # Terminate on hard_stop
                        proc.terminate()
                        stop_msg = prompt or (
                            "I kept browsing but stopped making meaningful progress. "
                            "Please try a different approach or rephrase the request."
                        )
                        await emit({"type": "text", "data": stop_msg})
                        await emit({"type": "done"})
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
                    await emit({
                        "type": "tool_start",
                        "name": "intervention", "input": "progress stalled",
                    })
                    await emit({
                        "type": "tool_result",
                        "name": "intervention", "data": stall_msg,
                        "is_screenshot": False,
                    })
                    await emit({"type": "text", "data": stall_msg})
                    await emit({"type": "done"})
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
                        if "scheduler_tool.py" in cmd_str:
                            tool_name = "scheduler"
                        elif "cdp_tool.py" in cmd_str:
                            parts = cmd_str.split("cdp_tool.py", 1)
                            after = parts[1].strip().split() if len(parts) > 1 else []
                            tool_name = after[0] if after else "cdp"
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
                    await emit({
                        "type": "tool_start",
                        "name": tool_name,
                        "input": display,
                    })

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
                    await emit({
                        "type": "tool_start",
                        "name": "intervention", "input": "loop detected",
                    })
                    await emit({
                        "type": "tool_result",
                        "name": "intervention", "data": nudge_msg,
                        "is_screenshot": False,
                    })
                    await emit({"type": "text", "data": nudge_msg})
                    await emit({"type": "done"})
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
                    await emit({
                        "type": "tool_result",
                        "name": "result",
                        "data": screenshot_data,
                        "is_screenshot": True,
                        "visible": True,
                    })
                else:
                    await emit({
                        "type": "tool_result",
                        "name": "result",
                        "data": result_text[:3000],
                        "is_screenshot": False,
                    })

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
            # Server-side cancel injection handles the cancelled+done events
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
            return await handle_message_claude(
                ws,
                sid,
                user_text,
                model,
                tab_id=tab_id,
                scheduler_armed=scheduler_armed,
                scheduler_grant_id=scheduler_grant_id,
                req_id=req_id,
            )
        if is_resume and proc.returncode != 0:
            log.info("[%s] Resume failed (exit %d): %s", sid, proc.returncode, stderr_text[:200])
        response = f"Error: {stderr_text}" if stderr_text else f"Error: exit code {proc.returncode}"

    if response:
        # Save assistant response locally
        _append_message("assistant", response)
        await emit({"type": "text", "data": response})
    await emit({"type": "done"})


async def handle_message_codex(
    ws,
    sid: str,
    user_text: str,
    model: str = "",
    tab_id: str = "auto",
    scheduler_armed: bool = False,
    scheduler_grant_id: str = "",
    req_id: str = "",
):
    """Single codex exec call with JSON event parsing and session resume."""
    emit = _make_emitter(ws, sid, req_id)
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
            _context_injected.discard((sid, "codex"))
    is_resume = bool(codex_sid)

    # Context fallback: if no resume session but slot has history, inject summary
    history_context = ""
    if not is_resume and (sid, "codex") not in _context_injected:
        _context_injected.add((sid, "codex"))
        data = _load_chat()
        prev_msgs = data.get("messages", [])
        prev_msgs = prev_msgs[:-1] if prev_msgs else []
        if prev_msgs:
            # Strip brackets from content to prevent bracket-tagged prompt
            # fragments in history from confusing the model.
            history_lines = []
            for m in prev_msgs[-20:]:
                role = m.get("role", "unknown")
                content = m.get("content", "").replace("[", "(").replace("]", ")")
                if len(content) > 300:
                    content = content[:300] + "..."
                history_lines.append(f"{role}: {content}")
            history_context = (
                "[Previous conversation context — the session was restarted, "
                "here is the recent history for continuity]\n"
                + "\n".join(history_lines)
            )
            log.info("  Injected %d previous messages as context (codex)", len(prev_msgs[-20:]))

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
    env["CDP_TAB_ID"] = tab_id or "auto"
    env["UNCHAINED_CHAT_SESSION_ID"] = sid
    env.pop("UNCHAINED_INSTALL_TOKEN", None)
    if scheduler_armed and scheduler_grant_id:
        env["UNCHAINED_SCHEDULER_GRANT_ID"] = scheduler_grant_id
    else:
        env.pop("UNCHAINED_SCHEDULER_GRANT_ID", None)

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

    codex_input = _build_codex_prompt(user_text, is_resume=is_resume, scheduler_armed=scheduler_armed, history_context=history_context)
    proc.stdin.write(codex_input.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    response = ""
    streamed_text = ""
    error_text = ""
    codex_tool_items: dict[str, tuple[str, str]] = {}
    codex_tool_start_times: dict[str, float] = {}  # item_id -> monotonic start
    SEARCH_TIMEOUT_S = float(os.environ.get("SEARCH_TIMEOUT_S", "60"))
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
        # Check for stalled search tools
        if SEARCH_TIMEOUT_S > 0 and codex_tool_start_times:
            now = time.monotonic()
            for _tid, _tstart in list(codex_tool_start_times.items()):
                elapsed = now - _tstart
                if elapsed > SEARCH_TIMEOUT_S:
                    tool_name = codex_tool_items.get(_tid, ("search", ""))[0]
                    log.info("[%s] Search tool %s stalled after %.0fs, killing subprocess", sid, tool_name, elapsed)
                    timed_out = True
                    error_text = f"Search timed out after {int(elapsed)}s — please try again"
                    break
            if timed_out:
                break
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
                await emit({
                    "type": "tool_start",
                    "name": tool_name, "input": tool_input,
                })
            else:
                # Track non-Bash tools (WebSearch, WebFetch, etc.)
                log.info("  Codex item.started: id=%s type=%s", item_id, item_type)
                if item_id and "search" in item_type:
                    codex_tool_items[item_id] = ("websearch", str(item.get("query", ""))[:100])
                    codex_tool_start_times[item_id] = time.monotonic()
                    await emit({
                        "type": "tool_start",
                        "name": "websearch", "input": str(item.get("query", ""))[:100],
                    })
            continue

        if etype == "item.completed":
            item = event.get("item") or {}
            item_id = str(item.get("id", "")).strip()
            item_type = (item.get("type") or "").lower()
            codex_tool_start_times.pop(item_id, None)  # clear search timer
            # Send tool_result for non-command_execution items that had a tool_start
            _tracked = codex_tool_items.pop(item_id, None)
            if _tracked and item_type != "command_execution":
                await emit({
                    "type": "tool_result",
                    "name": _tracked[0], "data": "completed",
                    "is_screenshot": False, "visible": False,
                })
            if item_type == "command_execution":
                tool_name, tool_input = _tracked or _codex_tool_name_and_input(str(item.get("command", "")))
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
                    await emit({
                        "type": "tool_result",
                        "name": tool_name, "data": codex_screenshot_data,
                        "is_screenshot": True, "visible": True,
                    })
                else:
                    await emit({
                        "type": "tool_result",
                        "name": tool_name, "data": out[:12000],
                        "is_screenshot": False, "visible": None,
                    })
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
        if not error_text:
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
        response = error_text or f"Codex CLI timed out after {CODEX_MAX_RUNTIME_S}s"

    if proc.returncode and proc.returncode < 0:
        proc_pid = getattr(proc, "pid", None)
        was_user_cancel = isinstance(proc_pid, int) and proc_pid in user_cancelled_pids
        if isinstance(proc_pid, int):
            user_cancelled_pids.discard(proc_pid)
        if was_user_cancel:
            # Server-side cancel injection handles the cancelled+done events
            try:
                if os.path.exists(output_file):
                    os.remove(output_file)
            except Exception:
                pass
            return
        if not response:
            if timed_out:
                response = error_text or f"Codex CLI timed out after {CODEX_MAX_RUNTIME_S}s"
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
            return await handle_message_codex(
                ws,
                sid,
                user_text,
                model,
                tab_id=tab_id,
                scheduler_armed=scheduler_armed,
                scheduler_grant_id=scheduler_grant_id,
                req_id=req_id,
            )
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
    await emit({"type": "text", "data": response})
    await emit({"type": "done"})
    try:
        if os.path.exists(output_file):
            os.remove(output_file)
    except Exception:
        pass


async def handle_message(
    ws,
    sid: str,
    user_text: str,
    model: str = "",
    tab_id: str = "auto",
    scheduler_armed: bool = False,
    scheduler_grant_id: str = "",
    req_id: str = "",
):
    """Dispatch to local Claude CLI or Codex CLI handler by model prefix."""
    emit = _make_emitter(ws, sid, req_id)
    if _is_codex_cli_model(model):
        if not _cli_binary_available(CODEX_BIN):
            await emit({
                "type": "error",
                "data": "Codex CLI is not installed. Install codex and run `codex login`.",
            })
            await emit({"type": "done"})
            return
        return await handle_message_codex(
            ws,
            sid,
            user_text,
            model,
            tab_id=tab_id,
            scheduler_armed=scheduler_armed,
            scheduler_grant_id=scheduler_grant_id,
            req_id=req_id,
        )
    if not _cli_binary_available(CLAUDE_BIN):
        await emit({
            "type": "error",
            "data": (
                f"Claude CLI is not installed or not on PATH. "
                f"Expected `{CLAUDE_BIN}`. Install Claude Code or set `CLAUDE_BIN`."
            ),
        })
        await emit({"type": "done"})
        return
    return await handle_message_claude(
        ws,
        sid,
        user_text,
        model,
        tab_id=tab_id,
        scheduler_armed=scheduler_armed,
        scheduler_grant_id=scheduler_grant_id,
        req_id=req_id,
    )


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
    if not _cli_binary_available(CLAUDE_BIN):
        print(
            f"WARNING: {CLAUDE_BIN} not found — Claude CLI lane will fail until Claude Code is installed "
            "or CLAUDE_BIN points to it."
        )
    if not _cli_binary_available(CODEX_BIN):
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
                    "claude_cli": bool(_cli_binary_available(CLAUDE_BIN)),
                    "codex_cli": bool(_cli_binary_available(CODEX_BIN)),
                    "client_version": _local_version(),
                    "remote_update": True,
                    "remote_research_desk_install": _remote_research_desk_install_supported(),
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
                    msg_tab_id = msg.get("tab_id", "auto")
                    # Sync active slot from the UI so messages go to the
                    # conversation the user is actually viewing.
                    msg_slot = msg.get("slot")
                    if msg_slot is not None and msg_slot in (1, 2, 3):
                        meta = _load_meta()
                        if meta.get("active_slot") != msg_slot:
                            meta["active_slot"] = msg_slot
                            _save_meta(meta)
                            log.info("[%s] Synced active slot to %s from user_message", sid, msg_slot)
                    msg_scheduler_armed = bool(msg.get("scheduler_armed"))
                    msg_scheduler_grant_id = str(msg.get("scheduler_grant_id", "") or "").strip()
                    msg_req_id = str(msg.get("req_id", "") or "").strip()
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
                    task = asyncio.create_task(
                        handle_message(
                            ws,
                            sid,
                            user_text,
                            msg_model,
                            tab_id=msg_tab_id,
                            scheduler_armed=msg_scheduler_armed,
                            scheduler_grant_id=msg_scheduler_grant_id,
                            req_id=msg_req_id,
                        )
                    )
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
                    payload = {
                        "type": "history_response",
                        "req_id": req_id,
                        "messages": data.get("messages", []),
                    }
                    chat_session_id = _chat_session_id_for_data(data)
                    if chat_session_id:
                        payload["session_id"] = chat_session_id
                    await ws.send(json.dumps(payload))
                elif msg.get("type") == "new_chat":
                    req_id = msg.get("req_id", "")
                    current = _active_slot()
                    _clear_slot(current)
                    claude_sessions.clear()
                    codex_sessions.clear()
                    _context_injected.clear()
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
                    _context_injected.clear()
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
                elif msg.get("type") == "get_archives":
                    req_id = msg.get("req_id", "")
                    archives = _list_archives()
                    await ws.send(json.dumps({
                        "type": "archives_response",
                        "req_id": req_id,
                        "archives": archives,
                    }))
                elif msg.get("type") == "restore_archive":
                    req_id = msg.get("req_id", "")
                    archive_id = msg.get("archive_id", "")
                    slot_data, chat_session_id = _restore_archive_into_slot(archive_id)
                    if slot_data is None:
                        await ws.send(json.dumps({
                            "type": "restore_archive_error",
                            "req_id": req_id,
                            "error": "Archive not found",
                        }))
                    else:
                        current = _active_slot()
                        print(f"[chat] Restored archive {archive_id} into slot {current}")
                        payload = {
                            "type": "restore_archive_ok",
                            "req_id": req_id,
                            "active_slot": current,
                        }
                        if chat_session_id:
                            payload["session_id"] = chat_session_id
                        await ws.send(json.dumps(payload))
                elif msg.get("type") == "delete_archive":
                    req_id = msg.get("req_id", "")
                    archive_id = msg.get("archive_id", "")
                    ok = _delete_archive(archive_id)
                    await ws.send(json.dumps({
                        "type": "delete_archive_ok" if ok else "delete_archive_error",
                        "req_id": req_id,
                    }))
                elif msg.get("type") == "update_client":
                    req_id = msg.get("req_id", "")
                    ok, err = _spawn_remote_update()
                    if ok:
                        await ws.send(json.dumps({
                            "type": "update_client_ok",
                            "req_id": req_id,
                            "status": "updating",
                        }))
                    else:
                        await ws.send(json.dumps({
                            "type": "update_client_error",
                            "req_id": req_id,
                            "error": err or "Could not start update helper.",
                        }))
                elif msg.get("type") == "install_research_desk":
                    req_id = msg.get("req_id", "")
                    ok, err, launcher_prefix = _spawn_remote_research_desk_install()
                    if ok:
                        await ws.send(json.dumps({
                            "type": "install_research_desk_ok",
                            "req_id": req_id,
                            "status": "installing",
                            "launcher_prefix": launcher_prefix,
                        }))
                    else:
                        await ws.send(json.dumps({
                            "type": "install_research_desk_error",
                            "req_id": req_id,
                            "error": err or "Could not start Research Desk install helper.",
                        }))

        except Exception as e:
            log.error("WebSocket error: %s. Reconnecting in 3s...", e, exc_info=True)
            await asyncio.sleep(3)


def _run():
    """Top-level entry with signal and crash logging."""
    import atexit

    def _on_exit():
        log.info("Agent process exiting (atexit)")

    def _on_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        log.warning("Agent received %s (signal %d) — exiting", sig_name, signum)
        sys.exit(128 + signum)

    atexit.register(_on_exit)
    sigs = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        sigs.append(signal.SIGHUP)
    if hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    for sig in sigs:
        signal.signal(sig, _on_signal)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Agent stopped by KeyboardInterrupt")
    except SystemExit as e:
        log.info("Agent SystemExit (code=%s)", e.code)
        raise
    except BaseException:
        log.critical("Agent crashed with unhandled exception", exc_info=True)
        raise


if __name__ == "__main__":
    if "--self-update-helper" in sys.argv[1:]:
        _run_self_update_helper()
    elif "--research-desk-install-helper" in sys.argv[1:]:
        _run_research_desk_install_helper()
    else:
        _run()
