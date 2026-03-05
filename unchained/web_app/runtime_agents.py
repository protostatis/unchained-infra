"""Provider agent process lifecycle helpers extracted from web.py."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time


def _core():
    import web as core

    return core


def spawn_gemini_agent(user_id: str, api_key: str, gemini_key: str):
    """Spawn a per-user chat_agent_gemini.py subprocess if not already running."""
    core = _core()
    agent_id = core._agent_id("gemini", api_key)

    with core._gemini_spawn_lock:
        proc = core._gemini_procs.get(agent_id)
        if proc and proc.poll() is None:
            core._gemini_last_active[agent_id] = time.time()
            return

        port = int(os.environ.get("WEB_PORT", "8080"))
        if os.path.exists("/.dockerenv"):
            server_url = f"ws://web:{port}"
        else:
            server_url = f"ws://127.0.0.1:{port}"

        relay_host, relay_port = core._parse_relay()
        env = {
            **os.environ,
            "RELAY_HOST": relay_host,
            "RELAY_PORT": str(relay_port),
            "UNCHAINED_API_KEY": api_key,
            "GEMINI_API_KEY": gemini_key,
        }

        script = os.path.join(os.path.dirname(core.__file__), "chat_agent_gemini.py")
        log_path = os.path.join(tempfile.gettempdir(), f"gemini-{agent_id}.log")
        log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            [sys.executable, script, "--agent", agent_id, "--server", server_url],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
        core._gemini_procs[agent_id] = proc
        core._gemini_log_fhs[agent_id] = log_fh
        core._gemini_last_active[agent_id] = time.time()
    core.log.info("[gemini] Spawned agent %s for user %s (pid %d)", agent_id, user_id, proc.pid)


def spawn_codex_agent(mode: str, user_id: str, api_key: str, codex_key: str):
    """Spawn a per-user chat_agent_codex.py subprocess in sdk/cli mode."""
    core = _core()
    if mode == "codex-cli":
        prefix = "codexcli"
        procs = core._codex_cli_procs
        log_fhs = core._codex_cli_log_fhs
        last_active = core._codex_cli_last_active
        spawn_lock = core._codex_cli_spawn_lock
    else:
        prefix = "codexsdk"
        procs = core._codex_sdk_procs
        log_fhs = core._codex_sdk_log_fhs
        last_active = core._codex_sdk_last_active
        spawn_lock = core._codex_sdk_spawn_lock

    agent_id = core._agent_id(prefix, api_key)
    with spawn_lock:
        proc = procs.get(agent_id)
        if proc and proc.poll() is None:
            last_active[agent_id] = time.time()
            return

        port = int(os.environ.get("WEB_PORT", "8080"))
        if os.path.exists("/.dockerenv"):
            server_url = f"ws://web:{port}"
        else:
            server_url = f"ws://127.0.0.1:{port}"

        relay_host, relay_port = core._parse_relay()
        env = {
            **os.environ,
            "RELAY_HOST": relay_host,
            "RELAY_PORT": str(relay_port),
            "UNCHAINED_API_KEY": api_key,
            "CODEX_API_KEY": codex_key,
            "OPENAI_API_KEY": codex_key,
            "CODEX_MODE": mode,
        }

        script = os.path.join(os.path.dirname(core.__file__), "chat_agent_codex.py")
        log_path = os.path.join(tempfile.gettempdir(), f"{agent_id}.log")
        log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            [sys.executable, script, "--agent", agent_id, "--server", server_url, "--mode", mode],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
        procs[agent_id] = proc
        log_fhs[agent_id] = log_fh
        last_active[agent_id] = time.time()
    core.log.info("[%s] Spawned agent %s for user %s (pid %d)", prefix, agent_id, user_id, proc.pid)


def spawn_codex_sdk_agent(user_id: str, api_key: str, codex_key: str):
    spawn_codex_agent("codex-sdk", user_id, api_key, codex_key)


def spawn_codex_cli_agent(user_id: str, api_key: str, codex_key: str):
    spawn_codex_agent("codex-cli", user_id, api_key, codex_key)


def spawn_claude_sdk_agent(user_id: str, api_key: str, claude_key: str):
    """Spawn a per-user chat_agent_sdk.py subprocess if not already running."""
    core = _core()
    agent_id = core._agent_id("claudesdk", api_key)
    with core._claude_sdk_spawn_lock:
        proc = core._claude_sdk_procs.get(agent_id)
        if proc and proc.poll() is None:
            core._claude_sdk_last_active[agent_id] = time.time()
            return

        port = int(os.environ.get("WEB_PORT", "8080"))
        if os.path.exists("/.dockerenv"):
            server_url = f"ws://web:{port}"
        else:
            server_url = f"ws://127.0.0.1:{port}"

        relay_host, relay_port = core._parse_relay()
        env = {
            **os.environ,
            "RELAY_HOST": relay_host,
            "RELAY_PORT": str(relay_port),
            "UNCHAINED_API_KEY": api_key,
            "ANTHROPIC_API_KEY": claude_key,
        }

        script = os.path.join(os.path.dirname(core.__file__), "chat_agent_sdk.py")
        log_path = os.path.join(tempfile.gettempdir(), f"{agent_id}.log")
        log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            [sys.executable, script, "--agent", agent_id, "--server", server_url],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
        core._claude_sdk_procs[agent_id] = proc
        core._claude_sdk_log_fhs[agent_id] = log_fh
        core._claude_sdk_last_active[agent_id] = time.time()
    core.log.info("[claudesdk] Spawned agent %s for user %s (pid %d)", agent_id, user_id, proc.pid)


async def cleanup_idle_gemini_agents():
    """Periodically terminate idle provider agents and expired pending provisions."""
    core = _core()
    while True:
        await asyncio.sleep(60)
        now = time.time()

        def _reap_idle(
            label: str,
            timeout_s: int,
            procs: dict[str, subprocess.Popen],
            last_active: dict[str, float],
            log_fhs: dict[str, object],
        ):
            for aid in list(procs):
                proc = procs[aid]
                if proc.poll() is not None:
                    core.log.info("[%s] Agent %s exited (code %s), removing", label, aid, proc.returncode)
                    procs.pop(aid, None)
                    last_active.pop(aid, None)
                    fh = log_fhs.pop(aid, None)
                    if fh:
                        fh.close()
                    continue

                last = last_active.get(aid, 0)
                if now - last > timeout_s:
                    core.log.info("[%s] Agent %s idle for >%ds, terminating", label, aid, timeout_s)
                    proc.terminate()
                    procs.pop(aid, None)
                    last_active.pop(aid, None)
                    fh = log_fhs.pop(aid, None)
                    if fh:
                        fh.close()

        _reap_idle(
            "gemini",
            core._GEMINI_IDLE_TIMEOUT,
            core._gemini_procs,
            core._gemini_last_active,
            core._gemini_log_fhs,
        )
        _reap_idle(
            "codexsdk",
            core._CODEX_IDLE_TIMEOUT,
            core._codex_sdk_procs,
            core._codex_sdk_last_active,
            core._codex_sdk_log_fhs,
        )
        _reap_idle(
            "codexcli",
            core._CODEX_IDLE_TIMEOUT,
            core._codex_cli_procs,
            core._codex_cli_last_active,
            core._codex_cli_log_fhs,
        )
        _reap_idle(
            "claudesdk",
            core._CLAUDE_SDK_IDLE_TIMEOUT,
            core._claude_sdk_procs,
            core._claude_sdk_last_active,
            core._claude_sdk_log_fhs,
        )

        for uid in list(core._pending_provision):
            _, _, ts = core._pending_provision[uid]
            if now - ts > core._PENDING_PROVISION_TTL:
                core.log.info("[provision] Expired pending key for user %s", uid)
                core._pending_provision.pop(uid, None)

        for uid in list(core._provision_cooldowns):
            if now - core._provision_cooldowns[uid] > 300:
                core._provision_cooldowns.pop(uid, None)

        try:
            import signup_agent

            signup_agent.cleanup_provision_locks()
        except Exception:
            pass
