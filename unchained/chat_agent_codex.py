"""chat_agent_codex.py — Chat agent using OpenAI/Codex API (per-user key).

Mirrors chat_agent_openrouter.py but calls OpenAI's chat completions endpoint
with a per-user API key provisioned and stored by signup_agent.py.

Architecture:
    Phone → EC2 web.py → /chat/ws → chat_agent_codex.py → OpenAI/Codex API
                                        ↕
                                  cloud_tools → relay → Chrome

Usage:
    uv run chat_agent_codex.py --key uc_live_... --agent codexsdk-12345678 --codex-key sk-...

The API key can also come per-session from web.py (via provider_keys table).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field

import httpx
import websockets

import cloud_tools
from chat_event_transport import CHAT_WS_MAX_MESSAGE_BYTES, send_agent_event
from context_compact import compact_messages, emergency_trim
from nudge import (
    NudgeState,
    _is_base64_png_blob,
    _extract_domain,
    _hash_sig,
    _normalize_for_progress,
    _tool_progress_sig,
    intervention_runtime_available,
    should_emit_intervention,
    _severity_rank,
    _INTERVENTION_IMPORT_ERROR,
    STALL_SCORE_THRESHOLD,
    STALL_FORCE_FINAL_STRIKES,
    STALL_NAV_GRACE_TURNS,
    LOOP_SHORT_CIRCUIT_REPEAT_THRESHOLD,
    STALL_VARIETY_WINDOW,
    STALL_FIND_WINDOW,
    STALL_FIND_DISTINCT_MAX,
    INTERVENTION_ENABLED,
    INTERVENTION_MIN_SEVERITY,
    INTERVENTION_MIN_TOOL_STEPS,
    INTERVENTION_COOLDOWN_TURNS,
    INTERVENTION_MAX_EVENTS,
    INTERVENTION_NUDGE_STALL_DECAY,
    INTERVENTION_NUDGE_RESET_PROGRESS,
    INTERVENTION_SCREENSHOT_ON_NUDGE,
    INTERVENTION_SCREENSHOT_TIMEOUT,
)

# Reuse system prompt, tools, and helpers from the OpenRouter agent
from chat_agent_openrouter import (
    SYSTEM_PROMPT,
    TOOLS,
    _truncate,
    _decode_tool_arguments,
    _ui_tool_name,
    _ui_tool_input,
    _message_content_as_text,
    _looks_like_internal_tool_payload,
    _strip_internal_tool_payload,
)
from scheduler_agent import (
    SCHEDULER_TOOL_NAMES,
    build_openai_tools,
    build_system_prompt,
    execute_scheduler_tool,
)

try:
    from reflex import ReflexState, REFLEX_ENABLED
except ImportError:
    REFLEX_ENABLED = False
    ReflexState = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CODEX_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
DEFAULT_MODEL = "codex-mini-latest"
DEFAULT_SERVER = "wss://api.unchainedsky.com"

RELAY_HOST = os.environ.get("RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "443"))

MAX_TURNS = 50
EXTENSION_BLOCK = 25
MAX_ABSOLUTE_TURNS = 200
SLOT_COUNT = 3

SESSION_DIR = os.environ.get(
    "SESSION_DIR",
    os.path.join(
        os.environ.get("UNCHAINED_DATA_DIR", os.path.expanduser("~/.unchained")),
        "sessions",
    ),
)
MAX_SESSION_MESSAGES = 30
TRIM_ON_ERROR = 10
TOOL_EXEC_TIMEOUT = int(os.environ.get("TOOL_EXEC_TIMEOUT", "45"))
FORCE_FINAL_TIMEOUT = int(os.environ.get("FORCE_FINAL_TIMEOUT", "35"))

# Prefer these when the requested Codex model is unavailable for a key.
FALLBACK_MODEL_CANDIDATES = (
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-4o-mini",
    "gpt-4o",
)


# ---------------------------------------------------------------------------
# Codex Chat Agent
# ---------------------------------------------------------------------------

class CodexChatAgent:
    """Chat agent backed by OpenAI/Codex chat completions."""

    def __init__(self, api_key: str, agent_id: str, server: str,
                 codex_key: str, model: str = DEFAULT_MODEL, mode: str = "codex-sdk"):
        self.api_key = api_key          # Unchained relay auth key
        self.agent_id = agent_id
        self.server = server
        self.codex_key = codex_key
        self.model = model
        self.mode = mode
        self.ws = None
        self.sessions: dict[str, list] = {}
        self.active_tasks: dict[str, asyncio.Task] = {}
        self._model_fallbacks: dict[str, str] = {}

    # --- Session persistence (same as OpenRouter agent) ---

    def _session_path(self, session_id: str) -> str:
        os.makedirs(SESSION_DIR, exist_ok=True)
        safe_id = session_id.replace("/", "_").replace("..", "").replace(" ", "_")
        mode_tag = "codexcli" if self.mode == "codex-cli" else "codexsdk"
        return os.path.join(SESSION_DIR, f"{mode_tag}-{safe_id}.json")

    def _resolve_model(self, model: str) -> str:
        """Normalize optional routing prefixes from web.py model selectors."""
        m = (model or "").strip()
        for prefix in ("codex-sdk:", "codex-cli:"):
            if m.startswith(prefix):
                return (m.split(":", 1)[1] or self.model).strip() or self.model
        return m or self.model

    def _load_session(self, session_id: str) -> list:
        path = self._session_path(session_id)
        try:
            with open(path) as f:
                data = json.load(f)
            msgs = data.get("messages", [])
            print(f"[{session_id}] Loaded {len(msgs)} messages from disk")
        except FileNotFoundError:
            msgs = []
        except Exception as e:
            print(f"[{session_id}] Failed to load session: {e}")
            msgs = []
        return [{"role": "system", "content": SYSTEM_PROMPT}] + msgs

    def _save_session(self, session_id: str, messages: list, max_messages: int | None = None):
        path = self._session_path(session_id)
        non_system = [m for m in messages if m.get("role") != "system"]
        cap = max_messages if isinstance(max_messages, int) and max_messages > 0 else MAX_SESSION_MESSAGES
        if len(non_system) > cap:
            non_system = non_system[-cap:]
        try:
            with open(path, "w") as f:
                json.dump({"messages": non_system}, f)
        except Exception as e:
            print(f"[{session_id}] Failed to save session: {e}")

    def _slot_meta_path(self) -> str:
        os.makedirs(SESSION_DIR, exist_ok=True)
        mode_tag = "codexcli" if self.mode == "codex-cli" else "codexsdk"
        return os.path.join(SESSION_DIR, f"{mode_tag}-{self.agent_id}-slots.json")

    def _load_slot_meta(self) -> dict:
        try:
            with open(self._slot_meta_path()) as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        try:
            active_slot = int(raw.get("active_slot", 1))
        except (TypeError, ValueError):
            active_slot = 1
        if active_slot not in range(1, SLOT_COUNT + 1):
            active_slot = 1
        raw_sessions = raw.get("slot_sessions", {})
        slot_sessions = {}
        for slot in range(1, SLOT_COUNT + 1):
            session_id = ""
            if isinstance(raw_sessions, dict):
                session_id = str(raw_sessions.get(str(slot), raw_sessions.get(slot, "")) or "").strip()
            slot_sessions[str(slot)] = session_id
        return {"active_slot": active_slot, "slot_sessions": slot_sessions}

    def _save_slot_meta(self, meta: dict):
        normalized = self._load_slot_meta()
        try:
            active_slot = int(meta.get("active_slot", normalized["active_slot"]))
        except (TypeError, ValueError):
            active_slot = normalized["active_slot"]
        if active_slot not in range(1, SLOT_COUNT + 1):
            active_slot = 1
        normalized["active_slot"] = active_slot
        raw_sessions = meta.get("slot_sessions", {})
        if isinstance(raw_sessions, dict):
            for slot in range(1, SLOT_COUNT + 1):
                normalized["slot_sessions"][str(slot)] = str(
                    raw_sessions.get(str(slot), raw_sessions.get(slot, normalized["slot_sessions"][str(slot)]))
                    or ""
                ).strip()
        with open(self._slot_meta_path(), "w") as f:
            json.dump(normalized, f)

    def _active_slot(self) -> int:
        return self._load_slot_meta().get("active_slot", 1)

    def _session_for_slot(self, slot: int | None = None) -> str:
        current_slot = self._active_slot() if slot is None else int(slot)
        if current_slot not in range(1, SLOT_COUNT + 1):
            current_slot = 1
        meta = self._load_slot_meta()
        return str(meta.get("slot_sessions", {}).get(str(current_slot), "") or "").strip()

    def _set_slot_session(self, slot: int, session_id: str):
        current_slot = int(slot)
        if current_slot not in range(1, SLOT_COUNT + 1):
            current_slot = 1
        meta = self._load_slot_meta()
        meta["slot_sessions"][str(current_slot)] = str(session_id or "").strip()
        self._save_slot_meta(meta)

    def _new_session_id(self) -> str:
        return f"s-{self.agent_id}-{int(time.time() * 1000):x}"

    def _delete_session(self, session_id: str):
        sid = str(session_id or "").strip()
        if not sid:
            return
        self.sessions.pop(sid, None)
        try:
            os.remove(self._session_path(sid))
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[{sid}] Failed to delete session file: {e}")

    def _clear_slot(self, slot: int | None = None):
        current_slot = self._active_slot() if slot is None else int(slot)
        if current_slot not in range(1, SLOT_COUNT + 1):
            current_slot = 1
        existing_session = self._session_for_slot(current_slot)
        if existing_session:
            self._delete_session(existing_session)
        meta = self._load_slot_meta()
        meta["slot_sessions"][str(current_slot)] = ""
        self._save_slot_meta(meta)

    def _get_slots_info(self) -> dict:
        meta = self._load_slot_meta()
        slots = []
        for slot in range(1, SLOT_COUNT + 1):
            session_id = str(meta["slot_sessions"].get(str(slot), "") or "").strip()
            preview = ""
            empty = True
            if session_id:
                messages = self.sessions.get(session_id)
                if messages is None:
                    messages = self._load_session(session_id)
                visible_messages = [m for m in messages if m.get("role") != "system"]
                empty = len(visible_messages) == 0
                for message in visible_messages:
                    if message.get("role") == "user":
                        preview = str(message.get("content", "") or "")[:40]
                        break
            slots.append({"slot": slot, "empty": empty, "preview": preview})
        return {"active_slot": meta.get("active_slot", 1), "slots": slots}

    # --- Sanitization ---

    async def _sanitize_user_output(self, draft_text: str, session_id: str = "") -> str:
        text = (draft_text or "").strip()
        if not text:
            return ""
        if not _looks_like_internal_tool_payload(text):
            return text
        if session_id:
            print(f"[{session_id}] Sanitizing internal tool payload from final response")
        cleaned = _strip_internal_tool_payload(text)
        if cleaned and not _looks_like_internal_tool_payload(cleaned):
            return cleaned
        return (
            "I hit an internal formatting issue while preparing the response. "
            "I can continue and provide a clean summary if you want me to proceed."
        )

    # --- Intervention events ---

    async def _emit_intervention_event(
        self, session_id: str, agent_id: str, severity: str, prompt: str,
        messages: list | None = None, tab_id: str | None = None,
    ):
        await self._send(session_id, {
            "type": "tool_start", "name": "intervention", "input": severity,
        })
        await self._send(session_id, {
            "type": "tool_result", "name": "intervention",
            "data": (prompt or "")[:1500], "is_screenshot": False,
        })
        if severity != "nudge" or not INTERVENTION_SCREENSHOT_ON_NUDGE:
            return
        await self._send(session_id, {
            "type": "tool_start", "name": "intervention_screenshot", "input": "current page",
        })
        try:
            screenshot = await asyncio.wait_for(
                self._execute_tool(agent_id, "screenshot", {}, tab_id=tab_id),
                timeout=INTERVENTION_SCREENSHOT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            screenshot = f"Intervention screenshot timed out after {INTERVENTION_SCREENSHOT_TIMEOUT}s."
        except Exception as e:
            screenshot = f"Intervention screenshot failed: {e}"
        if not isinstance(screenshot, str):
            screenshot = str(screenshot)
        is_screenshot = _is_base64_png_blob(screenshot)
        await self._send(session_id, {
            "type": "tool_result", "name": "intervention_screenshot",
            "data": screenshot if is_screenshot else screenshot[:3000],
            "is_screenshot": is_screenshot, "visible": False,
        })
        if is_screenshot and messages is not None:
            messages.append({
                "role": "system",
                "content": "Intervention context: a fresh screenshot was captured. "
                           "Re-orient on the current page state before choosing the next action.",
            })

    # --- Connection ---

    async def connect(self):
        url = f"{self.server}/chat/ws"
        print(f"Connecting to {url} ...")
        self.ws = await websockets.connect(
            url, ping_interval=20, ping_timeout=30, max_size=CHAT_WS_MAX_MESSAGE_BYTES
        )
        await self.ws.send(json.dumps({"key": self.api_key, "agent_id": self.agent_id}))
        resp = json.loads(await self.ws.recv())
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {resp}")
        print(f"Authenticated. Model: {self.model}. Waiting for messages...")

    async def run(self):
        while True:
            try:
                await self.connect()
                async for raw in self.ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "user_message":
                        sid = msg.get("session_id", "")
                        if sid:
                            self._set_slot_session(self._active_slot(), sid)
                        if sid:
                            old_task = self.active_tasks.pop(sid, None)
                            if old_task and not old_task.done():
                                old_task.cancel()
                                print(f"[{sid}] Auto-cancelled previous task (new message arrived)")
                        task = asyncio.create_task(self._handle_message(msg))
                        if sid:
                            self.active_tasks[sid] = task
                            task.add_done_callback(
                                lambda t, s=sid: self.active_tasks.pop(s, None)
                            )
                    elif msg.get("type") == "new_chat":
                        req_id = msg.get("req_id", "")
                        current_slot = self._active_slot()
                        self._clear_slot(current_slot)
                        new_sid = self._new_session_id()
                        self._set_slot_session(current_slot, new_sid)
                        print(f"[new_chat] Cleared slot {current_slot}, new session: {new_sid}")
                        await send_agent_event(self.ws, {
                            "type": "new_chat_ok",
                            "req_id": req_id,
                            "session_id": new_sid,
                            "active_slot": current_slot,
                        })
                    elif msg.get("type") == "switch_slot":
                        req_id = msg.get("req_id", "")
                        try:
                            slot = int(msg.get("slot", 1))
                        except (TypeError, ValueError):
                            slot = 1
                        if slot not in range(1, SLOT_COUNT + 1):
                            slot = 1
                        meta = self._load_slot_meta()
                        meta["active_slot"] = slot
                        self._save_slot_meta(meta)
                        print(f"[slot] Switched to slot {slot}")
                        await send_agent_event(self.ws, {
                            "type": "switch_slot_ok",
                            "req_id": req_id,
                            "active_slot": slot,
                        })
                    elif msg.get("type") == "get_slots":
                        req_id = msg.get("req_id", "")
                        info = self._get_slots_info()
                        info["type"] = "slots_response"
                        info["req_id"] = req_id
                        await send_agent_event(self.ws, info)
                    elif msg.get("type") == "get_history":
                        req_id = msg.get("req_id", "")
                        sid = msg.get("session_id", "") or self._session_for_slot()
                        raw_msgs = []
                        if sid and sid in self.sessions:
                            raw_msgs = [
                                m for m in self.sessions[sid]
                                if m.get("role") != "system"
                            ]
                        elif sid:
                            loaded = self._load_session(sid)
                            raw_msgs = [m for m in loaded if m.get("role") != "system"]
                        # Filter to only displayable messages (skip tool role, null content)
                        messages = [
                            m for m in raw_msgs
                            if m.get("role") in ("user", "assistant")
                            and isinstance(m.get("content"), str)
                            and m.get("content")
                        ]
                        await send_agent_event(self.ws, {
                            "type": "history_response",
                            "req_id": req_id,
                            "messages": messages,
                        })
                    elif msg.get("type") == "cancel":
                        sid = msg.get("session_id", "")
                        task = self.active_tasks.pop(sid, None)
                        if task and not task.done():
                            task.cancel()
                            print(f"[{sid}] Cancelled")
                            await self._send(sid, {"type": "cancelled"})
                            await self._send(sid, {"type": "done"})
            except websockets.ConnectionClosed:
                print("Connection lost. Reconnecting in 3s...")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"Error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _send(self, session_id: str, event: dict):
        event["session_id"] = session_id
        try:
            await send_agent_event(self.ws, event)
        except Exception as e:
            print(f"Send error: {e}")

    # --- OpenAI/Codex API call ---

    @staticmethod
    def _extract_api_error(resp: httpx.Response) -> tuple[str, str]:
        """Return (code, message) from OpenAI-style error payload."""
        try:
            payload = resp.json()
        except Exception:
            return "", (resp.text or "")[:400]
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")
        return "", str(err or "")[:400]

    async def _pick_fallback_model(self, client: httpx.AsyncClient, key: str) -> str | None:
        headers = {"Authorization": f"Bearer {key}"}
        try:
            resp = await client.get(OPENAI_MODELS_URL, headers=headers, timeout=30.0)
            if not resp.is_success:
                return None
            payload = resp.json()
            data = payload.get("data", []) if isinstance(payload, dict) else []
            model_ids = {
                str(m.get("id", "")).strip()
                for m in data
                if isinstance(m, dict) and m.get("id")
            }
            for candidate in FALLBACK_MODEL_CANDIDATES:
                if candidate in model_ids:
                    return candidate
            return None
        except Exception:
            return None

    async def _call_codex(self, client: httpx.AsyncClient, messages: list,
                           model: str = "", tool_choice: str = "auto",
                           codex_key: str = "", tools: list[dict] | None = None) -> dict:
        """Call OpenAI/Codex via chat completions."""
        requested_model = model or self.model
        active_model = self._model_fallbacks.get(requested_model, requested_model)
        body: dict = {
            "model": active_model,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.2,
        }
        if tool_choice == "none":
            body["tool_choice"] = "none"
        else:
            body["tools"] = tools or TOOLS
            body["tool_choice"] = "auto"

        key = codex_key or self.codex_key
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        resp = await client.post(CODEX_URL, json=body, headers=headers, timeout=60.0)

        if resp.status_code == 404:
            err_code, err_msg = self._extract_api_error(resp)
            if err_code == "model_not_found":
                fallback = await self._pick_fallback_model(client, key)
                if fallback and fallback != active_model:
                    self._model_fallbacks[requested_model] = fallback
                    body["model"] = fallback
                    print(
                        f"[codex] model '{active_model}' unavailable; "
                        f"falling back to '{fallback}' for this key"
                    )
                    resp = await client.post(CODEX_URL, json=body, headers=headers, timeout=60.0)
                else:
                    raise RuntimeError(
                        f"OpenAI model '{active_model}' is not available for this API key "
                        f"({err_msg or 'model_not_found'}). "
                        "Please use a key with Codex model access, or choose a model your key can access."
                    )

        # Retry on 500/503
        if resp.status_code in (500, 502, 503):
            for attempt in range(1, 3):
                print(f"[codex] {resp.status_code} — retry {attempt}/2 after {2 * attempt}s")
                await asyncio.sleep(2 * attempt)
                resp = await client.post(CODEX_URL, json=body, headers=headers, timeout=60.0)
                if resp.status_code not in (500, 502, 503):
                    break

        if not resp.is_success:
            err_code, err_msg = self._extract_api_error(resp)
            print(f"[codex] {resp.status_code} error ({err_code}): {resp.text[:400]}")
            if resp.status_code == 429:
                raise RuntimeError(
                    "OpenAI API rate limit reached. "
                    "Please wait a few minutes, or check your usage at "
                    "https://platform.openai.com/settings/organization/billing"
                )
            if resp.status_code == 401:
                raise RuntimeError(
                    "OpenAI API key is invalid or revoked. "
                    "Please update your key at /setup?provider=codex-sdk"
                )
            low = (err_msg or "").lower()
            if "billing" in low or "quota" in low or "insufficient" in low or "credit" in low:
                raise RuntimeError(
                    "OpenAI API quota or billing limit reached. "
                    "Please check your billing at "
                    "https://platform.openai.com/settings/organization/billing"
                )
            if err_msg:
                raise RuntimeError(err_msg)
            resp.raise_for_status()

        data = resp.json()
        if "choices" not in data:
            err_msg = data.get("error", {})
            if isinstance(err_msg, dict):
                err_msg = err_msg.get("message", str(data)[:200])
            print(f"[codex] Provider error: {err_msg} — retrying")
            for attempt in range(1, 3):
                await asyncio.sleep(2 * attempt)
                resp = await client.post(CODEX_URL, json=body, headers=headers, timeout=60.0)
                if resp.is_success:
                    data = resp.json()
                    if "choices" in data:
                        break
            if "choices" not in data:
                raise RuntimeError(f"Codex provider error: {err_msg}")
        return data

    # --- Tool execution (reuses cloud_tools dispatch from openrouter agent) ---

    async def _execute_tool(self, agent_id: str, name: str, args: dict,
                            tab_id: str | None = None, session_id: str = "",
                            scheduler_grant_id: str = "") -> str:
        if name in SCHEDULER_TOOL_NAMES:
            return await execute_scheduler_tool(
                server_url=self.server,
                api_key=self.api_key,
                session_id=session_id,
                scheduler_grant_id=scheduler_grant_id,
                tool_name=name,
                args=args,
            )

        args_tab = args.get("tab_id", "")
        if args_tab and args_tab != "auto" and "://" not in args_tab and "/" not in args_tab:
            effective_tab = args_tab
        elif tab_id:
            effective_tab = tab_id
        else:
            effective_tab = "auto"

        flags_str = args.get("flags", "")
        if name == "ddm" and any(f in flags_str for f in ("--new", "--tabs", "--close")):
            effective_tab = "auto"

        result = await self._dispatch_tool(agent_id, effective_tab, name, args)

        if effective_tab != "auto" and ("BROWSER_UNAVAILABLE" in result or "4000" in result):
            result = await self._dispatch_tool(agent_id, "auto", name, args)

        return result

    async def _dispatch_tool(self, agent_id: str, tab_id: str, name: str, args: dict) -> str:
        try:
            if name == "ddm":
                flags = args.get("flags", "--llm-2pass --cols 60").split()
                return await cloud_tools.run_ddm(
                    agent_id, tab_id, flags, RELAY_HOST, RELAY_PORT)
            elif name == "intel_probe":
                return await cloud_tools.run_intel(
                    agent_id, tab_id, ["--probe"], RELAY_HOST, RELAY_PORT)
            elif name == "intel_extract":
                flags = ["--extract"]
                if strategy := args.get("strategy"):
                    flags += ["--strategy", strategy]
                return await cloud_tools.run_intel(
                    agent_id, tab_id, flags, RELAY_HOST, RELAY_PORT)
            elif name == "intel_stores":
                return await cloud_tools.run_intel(
                    agent_id, tab_id, ["--stores"], RELAY_HOST, RELAY_PORT)
            elif name == "intel_find_paths":
                return await cloud_tools.run_intel(
                    agent_id, tab_id,
                    ["--find-paths", args["global_name"], args["key"]],
                    RELAY_HOST, RELAY_PORT)
            elif name == "navigate":
                return await cloud_tools.navigate(
                    agent_id, tab_id, args["url"], RELAY_HOST, RELAY_PORT)
            elif name == "click":
                return await cloud_tools.click(
                    agent_id, tab_id, args["x"], args["y"], RELAY_HOST, RELAY_PORT)
            elif name == "type_text":
                return await cloud_tools.type_text(
                    agent_id, tab_id, args["text"], RELAY_HOST, RELAY_PORT)
            elif name == "submit_form":
                return await cloud_tools.submit_form(
                    agent_id, tab_id, RELAY_HOST, RELAY_PORT)
            elif name == "press_enter":
                return await cloud_tools.press_enter(
                    agent_id, tab_id, RELAY_HOST, RELAY_PORT)
            elif name == "js_eval":
                return await cloud_tools.run_js(
                    agent_id, tab_id, args["expression"], RELAY_HOST, RELAY_PORT)
            elif name == "screenshot":
                return await cloud_tools.screenshot(
                    agent_id, tab_id, RELAY_HOST, RELAY_PORT)
            else:
                return f"Unknown tool: {name}"
        except (asyncio.TimeoutError, TimeoutError):
            return "BROWSER_UNAVAILABLE: Chrome connector timed out — it may not be running."
        except Exception as e:
            err = str(e)
            if any(k in err.lower() for k in
                   ("404", "403", "invalid status", "connection refused",
                    "connection closed", "no agent", "not found", "websocket",
                    "timed out", "timeout")):
                return ("BROWSER_UNAVAILABLE: Chrome connector is not running. "
                        "Answer from knowledge — do not retry browser tools.")
            return f"Tool error ({name}): {err}"

    # --- Message handling (tool-use loop) ---

    async def _handle_message(self, msg: dict):
        session_id = msg["session_id"]
        agent_id = msg.get("agent_id", self.agent_id)
        session_tab_id = msg.get("tab_id")
        user_text = msg["message"]
        model = self._resolve_model(msg.get("model") or self.model)
        codex_key = self.codex_key
        scheduler_armed = bool(msg.get("scheduler_armed"))
        scheduler_grant_id = str(msg.get("scheduler_grant_id", "") or "").strip()
        system_prompt = build_system_prompt(
            SYSTEM_PROMPT,
            scheduler_armed=scheduler_armed,
            scheduler_grant_id=scheduler_grant_id,
        )
        tools = build_openai_tools(
            TOOLS,
            scheduler_armed=scheduler_armed,
            scheduler_grant_id=scheduler_grant_id,
        )

        print(f"[{session_id}] User ({agent_id}): {user_text[:80]} (model={model})")

        if session_id not in self.sessions:
            self.sessions[session_id] = self._load_session(session_id)
        messages = self.sessions[session_id]
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})

        ns = NudgeState()
        reflex = ReflexState() if ReflexState else None
        if reflex:
            reflex.set_user_goal(user_text)

        try:
            async with httpx.AsyncClient() as client:
                async def _force_final_response(reason_log: str, fallback: str):
                    print(f"[{session_id}] {reason_log}")
                    try:
                        final_resp = await asyncio.wait_for(
                            self._call_codex(
                                client, messages, model,
                                tool_choice="none", codex_key=codex_key, tools=tools,
                            ),
                            timeout=FORCE_FINAL_TIMEOUT,
                        )
                        final_msg = final_resp["choices"][0]["message"]
                        messages.append(final_msg)
                        text = final_msg.get("content") or ""
                        text = await self._sanitize_user_output(text, session_id=session_id)
                    except asyncio.TimeoutError:
                        print(f"[{session_id}] Forced-final model timeout after {FORCE_FINAL_TIMEOUT}s")
                        text = ""
                    except Exception as e:
                        print(f"[{session_id}] Forced-final model error: {e}")
                        text = ""
                    if not text:
                        text = fallback
                    await self._send(session_id, {"type": "text", "data": text})
                    self._save_session(session_id, messages)
                    await self._send(session_id, {"type": "done"})

                turn_cap = MAX_TURNS

                for turn in range(MAX_ABSOLUTE_TURNS):
                    # Dynamic extension
                    if turn >= turn_cap:
                        if ns.should_extend_turns():
                            turn_cap = min(turn + EXTENSION_BLOCK, MAX_ABSOLUTE_TURNS)
                            ns.intervention_events = max(0, ns.intervention_events - 1)
                            print(f"[{session_id}] Dynamic extension: cap now {turn_cap} (turn {turn})")
                        else:
                            await _force_final_response(
                                f"Dynamic cap: extension denied at turn {turn}",
                                "I've completed my research. Here's what I found so far.",
                            )
                            return

                    # Periodic context compaction (every 5 turns)
                    if turn > 0 and turn % 5 == 0:
                        messages, cstats = compact_messages(messages, fmt="openai")
                        if cstats["compacted"]:
                            print(f"[{session_id}] Compacted {cstats['compacted']} tool results "
                                  f"({cstats['tokens_before']}→{cstats['tokens_after']} est tokens)")

                    try:
                        next_tool_choice = "none" if (ns.hard_stop_guard and ns.hard_stop_recovery_used >= 1) else "auto"
                        if next_tool_choice == "none":
                            print(f"[{session_id}] Hard-stop guard: forcing final response")
                        response = await self._call_codex(
                            client, messages, model,
                            tool_choice=next_tool_choice, codex_key=codex_key, tools=tools,
                        )
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 400 and len(messages) > TRIM_ON_ERROR + 1:
                            messages = emergency_trim(messages, fmt="openai", keep_tail=TRIM_ON_ERROR)
                            self.sessions[session_id] = messages
                            print(f"[{session_id}] 400 on turn {turn} — emergency trim to {len(messages)} msgs")
                            response = await self._call_codex(
                                client, messages, model, codex_key=codex_key, tools=tools,
                            )
                        else:
                            raise

                    choice = response["choices"][0]
                    message = choice["message"]
                    finish_reason = choice.get("finish_reason", "")
                    tool_calls = message.get("tool_calls") or []
                    messages.append(message)

                    # Loop detection
                    loop_detected = False
                    if tool_calls:
                        sig = json.dumps([
                            {"name": tc.get("function", {}).get("name"),
                             "args": tc.get("function", {}).get("arguments")}
                            for tc in tool_calls
                        ], sort_keys=True)
                        loop_detected, nudge_text, feedback = ns.check_loop(sig)

                        if loop_detected:
                            if feedback and getattr(feedback, "should_intervene", False):
                                await self._emit_intervention_event(
                                    session_id=session_id, agent_id=agent_id,
                                    severity=getattr(feedback, "severity", "hard_stop"),
                                    prompt=nudge_text, messages=messages,
                                    tab_id=session_tab_id,
                                )
                            for tc in tool_calls:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc.get("id", "loop"),
                                    "content": nudge_text,
                                })
                            await _force_final_response(
                                "Loop detected — forcing final response",
                                "I got stuck in a loop and couldn't complete the task. "
                                "Please try rephrasing your request.",
                            )
                            return

                    # No tool calls → final answer
                    if not tool_calls:
                        text = message.get("content") or ""
                        text = await self._sanitize_user_output(text, session_id=session_id)
                        if text:
                            await self._send(session_id, {"type": "text", "data": text})
                        else:
                            await self._send(session_id, {"type": "text",
                                "data": "[Agent completed the task but returned no text response. "
                                        "Try asking it to summarize what it found.]"})
                        self._save_session(session_id, messages)
                        await self._send(session_id, {"type": "done"})
                        print(f"[{session_id}] Done ({turn + 1} turns)")
                        return

                    # Execute tool calls
                    tool_results = []
                    reflex_hints: list[str] = []
                    turn_step_sigs: list[str] = []
                    turn_find_queries: list[str] = []
                    turn_had_navigation = False
                    turn_had_interaction = False
                    turn_domain_switch = False

                    for idx, tc in enumerate(tool_calls):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args = _decode_tool_arguments(fn.get("arguments"))
                        fn["arguments"] = json.dumps(args, separators=(",", ":"))

                        if name == "navigate":
                            nav_url = str(args.get("url", "")).strip()
                            if nav_url and nav_url != ns.last_nav_url:
                                turn_had_navigation = True
                            ns.last_nav_url = nav_url
                            domain = _extract_domain(nav_url)
                            if domain:
                                if ns.recent_domains and domain != ns.recent_domains[-1]:
                                    turn_domain_switch = True
                                ns.recent_domains.append(domain)
                        elif name in {"click", "type_text", "press_enter", "submit_form"}:
                            turn_had_interaction = True

                        ui_name = _ui_tool_name(name)
                        ui_input = _truncate(_ui_tool_input(name, args), 200)
                        await self._send(session_id, {
                            "type": "tool_start", "name": ui_name, "input": ui_input,
                        })

                        print(f"[{session_id}] Tool {turn + 1}.{idx + 1} start: "
                              f"{name} args={_truncate(json.dumps(args, sort_keys=True), 200)}")
                        try:
                            result = await asyncio.wait_for(
                                self._execute_tool(
                                    agent_id,
                                    name,
                                    args,
                                    tab_id=session_tab_id,
                                    session_id=session_id,
                                    scheduler_grant_id=scheduler_grant_id,
                                ),
                                timeout=TOOL_EXEC_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            result = ("BROWSER_UNAVAILABLE: Tool execution timed out — "
                                      "Chrome connector may be offline or unresponsive.")

                        if not isinstance(result, str):
                            result = str(result)
                        print(f"[{session_id}] Tool {turn + 1}.{idx + 1} done: "
                              f"{name} -> {_truncate(result.replace(chr(10), ' '), 180)}")

                        turn_step_sigs.append(_tool_progress_sig(name, args, result))
                        if name == "ddm":
                            flags = str(args.get("flags", "")).strip()
                            if flags.startswith("--text --find"):
                                query = flags[len("--text --find"):].strip().lower()
                                if query:
                                    turn_find_queries.append(query)

                        is_screenshot = name == "screenshot" and _is_base64_png_blob(result)
                        show_user = args.get("show_user", False)
                        await self._send(session_id, {
                            "type": "tool_result", "name": ui_name,
                            "data": result if is_screenshot else result[:3000],
                            "is_screenshot": is_screenshot,
                            "visible": is_screenshot and bool(show_user),
                        })

                        ns.live_tool_log.append({
                            "turn": turn + 1, "tool": name,
                            "args": args, "output_preview": result[:3000],
                        })

                        tool_call_id = tc.get("id") or f"tc-{turn + 1}-{idx + 1}"
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result[:8000],
                        })

                        if reflex:
                            _rh = reflex.on_tool_result(name, args, result)
                            if _rh:
                                reflex_hints.append(_rh)

                    messages.extend(tool_results)

                    for _rh in reflex_hints:
                        messages.append({"role": "system", "content": _rh})

                    # Stagnation tracking
                    ns.update_stagnation(
                        turn_step_sigs, turn_find_queries,
                        turn_had_navigation, turn_domain_switch,
                        turn_had_interaction,
                    )

                    # Progress-based intervention
                    _should_emit, feedback = ns.run_intervention(turn + 1)
                    if _should_emit and feedback:
                        prompt = (feedback.feedback_prompt or "").strip()
                        if prompt:
                            messages.append({"role": "system", "content": prompt})
                            ns.intervention_events += 1
                            ns.last_intervention_model_turn = turn + 1
                            await self._emit_intervention_event(
                                session_id=session_id, agent_id=agent_id,
                                severity=feedback.severity, prompt=prompt,
                                messages=messages, tab_id=session_tab_id,
                            )
                            if feedback.severity == "nudge":
                                ns.apply_nudge_decay()
                                ns.apply_nudge_reset()
                            if feedback.severity == "hard_stop":
                                ns.hard_stop_guard = True
                                ns.hard_stop_recovery_used = 0

                    # Stall threshold
                    action, guidance = ns.check_stall_threshold()
                    if action == "guidance":
                        messages.append({"role": "system", "content": guidance})
                    elif action == "force":
                        await _force_final_response(
                            f"Progress stalled (score={ns.stagnation_score}) — forcing final response",
                            guidance,
                        )
                        return

                    if ns.hard_stop_guard and tool_calls:
                        ns.hard_stop_recovery_used += 1

                # Absolute ceiling
                print(f"[{session_id}] Reached absolute max turns ({MAX_ABSOLUTE_TURNS})")
                await _force_final_response(
                    f"Absolute max turns ({MAX_ABSOLUTE_TURNS}) reached",
                    "I've reached the maximum number of research turns. Here's what I found.",
                )

        except asyncio.CancelledError:
            print(f"[{session_id}] Task cancelled")
            self._save_session(session_id, messages)
        except Exception as e:
            print(f"[{session_id}] Error: {e}")
            await self._send(session_id, {"type": "error", "data": str(e)})
            self._save_session(session_id, messages)
            await self._send(session_id, {"type": "done"})


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Unchained Codex Chat Agent")
    parser.add_argument("--key", default=os.environ.get("UNCHAINED_API_KEY", ""),
                        help="Unchained API key (uc_live_...)")
    parser.add_argument("--agent", help="Default agent ID (codexsdk-12345678)")
    parser.add_argument("--server",
                        default=os.environ.get("UNCHAINED_SERVER", DEFAULT_SERVER),
                        help=f"WebSocket server URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--model",
                        default=os.environ.get("CODEX_MODEL", DEFAULT_MODEL),
                        help=f"Codex model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--mode", choices=["codex-sdk", "codex-cli"],
                        default=os.environ.get("CODEX_MODE", "codex-sdk"),
                        help="Agent mode tag (for session namespace only)")
    parser.add_argument("--codex-key",
                        default=os.environ.get("CODEX_API_KEY", "") or os.environ.get("OPENAI_API_KEY", ""),
                        help="Codex/OpenAI API key (sk-...)")
    args = parser.parse_args()

    if not args.codex_key:
        print("ERROR: --codex-key or CODEX_API_KEY/OPENAI_API_KEY env var required.", file=sys.stderr)
        sys.exit(1)

    if not args.key or not args.agent:
        parser.error("--key and --agent are required.")

    agent = CodexChatAgent(
        api_key=args.key,
        agent_id=args.agent,
        server=args.server,
        codex_key=args.codex_key,
        model=args.model,
        mode=args.mode,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: loop.stop())

    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        print("\nCodex agent stopped.")


if __name__ == "__main__":
    main()
