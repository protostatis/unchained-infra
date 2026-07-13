"""chat_agent_sdk.py — Production chat agent using Anthropic SDK directly.

Requires ANTHROPIC_API_KEY in your environment. Uses the SDK's native
tool_use API for proper multi-turn tool calling (no text parsing hacks).
This is the production version — use chat_agent_cli.py for testing
without an API key.

Architecture:
    Phone → EC2 web server (POST /web/chat, SSE response)
         → WebSocket bridge
         → This script (runs on your Mac)
         → Anthropic SDK (uses local ANTHROPIC_API_KEY)
         → cloud_tools → WSS to EC2 relay → Chrome on your Mac

    Two WSS connections from Mac to EC2:
    1. chrome_bridge.py → /tunnel  (Chrome CDP relay — existing)
    2. This script → /chat/ws   (chat messages — new)

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    uv run chat_agent_sdk.py --key uc_live_... --agent a-12345678
    uv run chat_agent_sdk.py --key uc_live_... --agent a-... --model claude-opus-4-7

See also: chat_agent_cli.py (test version using `claude -p` CLI, no API key needed)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time

import anthropic
import websockets

import cloud_tools
from chat_event_transport import CHAT_WS_MAX_MESSAGE_BYTES, send_agent_event
from orchestrator import (
    build_system_prompt as _build_orchestrator_system_prompt,
    build_tools as _build_orchestrator_tools,
)
from context_compact import compact_messages, emergency_trim
from nudge import (
    NudgeState,
    _is_base64_png_blob,
    _extract_domain,
    _tool_progress_sig,
    intervention_runtime_available,
    INTERVENTION_SCREENSHOT_ON_NUDGE,
    INTERVENTION_SCREENSHOT_TIMEOUT,
    INTERVENTION_NUDGE_RESET_PROGRESS,
)
from scheduler_agent import SCHEDULER_TOOL_NAMES, execute_scheduler_tool


DEFAULT_SERVER = "wss://api.unchainedsky.com"
RELAY_HOST = "api.unchainedsky.com"
RELAY_PORT = 443
MODEL = "claude-sonnet-4-6"
MAX_TURNS = 100

SESSION_DIR = os.environ.get(
    "SESSION_DIR",
    os.path.join(
        os.environ.get("UNCHAINED_DATA_DIR", os.path.expanduser("~/.unchained")),
        "sessions",
    ),
)
MAX_SESSION_MESSAGES = 30


def _resolve_model(model: str, default_model: str = MODEL) -> str:
    """Normalize prefixed model selectors from web UI."""
    m = (model or "").strip()
    if m.startswith("claude-sdk:"):
        return (m.split(":", 1)[1] or default_model).strip() or default_model
    return m or default_model


class ChatAgent:
    """Persistent chat agent connecting to the unchained relay."""

    def __init__(self, api_key: str, agent_id: str, server: str):
        self.api_key = api_key
        self.agent_id = agent_id
        self.server = server
        self.ws = None
        self.model = MODEL
        self.client = anthropic.AsyncAnthropic()
        self.sessions: dict[str, list] = {}  # session_id → messages
        self.active_tasks: dict[str, asyncio.Task] = {}

    async def connect(self):
        """Connect to the chat WebSocket and authenticate."""
        url = f"{self.server}/chat/ws"
        print(f"Connecting to {url} ...")
        self.ws = await websockets.connect(
            url, ping_interval=20, ping_timeout=30, max_size=CHAT_WS_MAX_MESSAGE_BYTES
        )

        # Authenticate — include agent_id so the server registers us correctly
        await self.ws.send(json.dumps({"key": self.api_key, "agent_id": self.agent_id}))
        resp = json.loads(await self.ws.recv())
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {resp}")
        print("Authenticated. Waiting for messages...")

    # --- Session persistence ---

    def _session_path(self, session_id: str) -> str:
        os.makedirs(SESSION_DIR, exist_ok=True)
        safe_id = session_id.replace("/", "_").replace("..", "").replace(" ", "_")
        return os.path.join(SESSION_DIR, f"claudesdk-{safe_id}.json")

    @staticmethod
    def _serialize_content(content):
        """Convert Anthropic ContentBlock objects to JSON-serializable form."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            result = []
            for item in content:
                if isinstance(item, dict):
                    result.append(item)
                elif hasattr(item, "model_dump"):
                    result.append(item.model_dump())
                else:
                    result.append({"type": "text", "text": str(item)})
            return result
        if hasattr(content, "model_dump"):
            return content.model_dump()
        return str(content)

    def _serialize_messages(self, messages: list) -> list:
        """Serialize full message list for JSON persistence."""
        result = []
        for msg in messages:
            serialized = {"role": msg["role"]}
            serialized["content"] = self._serialize_content(msg.get("content", ""))
            result.append(serialized)
        return result

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
        return msgs

    def _save_session(self, session_id: str, messages: list):
        path = self._session_path(session_id)
        serialized = self._serialize_messages(messages)
        if len(serialized) > MAX_SESSION_MESSAGES:
            serialized = serialized[-MAX_SESSION_MESSAGES:]
        try:
            with open(path, "w") as f:
                json.dump({"messages": serialized}, f)
        except Exception as e:
            print(f"[{session_id}] Failed to save session: {e}")

    def _extract_display_history(self, messages: list) -> list:
        """Extract user/assistant text messages for frontend display.

        Anthropic messages use content blocks (lists of dicts with type/text),
        so we need to flatten those to plain strings. Tool-result user messages
        and tool-use-only assistant messages are skipped.
        """
        history = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "user":
                if isinstance(content, str) and content:
                    history.append({"role": "user", "content": content})
            elif role == "assistant":
                if isinstance(content, str):
                    if content:
                        history.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif hasattr(block, "text") and hasattr(block, "type") and block.type == "text":
                            texts.append(block.text)
                    text = "\n".join(t for t in texts if t)
                    if text:
                        history.append({"role": "assistant", "content": text})
        return history

    async def run(self):
        """Main loop — listen for messages, process each one."""
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
                        for sid in list(self.sessions):
                            self.sessions.pop(sid, None)
                        new_sid = f"s-{self.agent_id}-{int(time.time() * 1000):x}"
                        print(f"[new_chat] Cleared sessions, new session: {new_sid}")
                        await send_agent_event(self.ws, {
                            "type": "new_chat_ok",
                            "req_id": req_id,
                            "session_id": new_sid,
                            "active_slot": 1,
                        })
                    elif msg.get("type") == "get_history":
                        req_id = msg.get("req_id", "")
                        sid = msg.get("session_id", "")
                        raw_msgs = []
                        if sid and sid in self.sessions:
                            raw_msgs = self.sessions[sid]
                        elif sid:
                            raw_msgs = self._load_session(sid)
                        history = self._extract_display_history(raw_msgs)
                        await send_agent_event(self.ws, {
                            "type": "history_response",
                            "req_id": req_id,
                            "messages": history,
                        })
                    elif msg.get("type") == "cancel":
                        sid = msg.get("session_id", "")
                        task = self.active_tasks.pop(sid, None)
                        if task and not task.done():
                            task.cancel()
                            print(f"[{sid}] Cancelled")
                            await self._send_event(sid, {"type": "cancelled"})
                            await self._send_event(sid, {"type": "done"})

            except websockets.ConnectionClosed:
                print("Connection lost. Reconnecting in 3s...")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"Error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _send_event(self, session_id: str, event: dict):
        """Send an event back through the WebSocket."""
        event["session_id"] = session_id
        try:
            await send_agent_event(self.ws, event)
        except Exception as e:
            print(f"Failed to send event: {e}")

    async def _emit_intervention_event(
        self,
        session_id: str,
        agent_id: str,
        severity: str,
        prompt: str,
        tab_id: str | None = None,
    ):
        """Emit intervention event and optional screenshot context."""
        await self._send_event(session_id, {
            "type": "tool_start",
            "name": "intervention",
            "input": severity,
        })
        await self._send_event(session_id, {
            "type": "tool_result",
            "name": "intervention",
            "data": (prompt or "")[:1500],
            "is_screenshot": False,
        })

        if severity != "nudge" or not INTERVENTION_SCREENSHOT_ON_NUDGE:
            return

        await self._send_event(session_id, {
            "type": "tool_start",
            "name": "intervention_screenshot",
            "input": "current page",
        })
        try:
            screenshot = await asyncio.wait_for(
                self._execute_tool(agent_id, "screenshot", {"tab_id": "auto"}, tab_id=tab_id),
                timeout=INTERVENTION_SCREENSHOT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            screenshot = f"Intervention screenshot timed out after {INTERVENTION_SCREENSHOT_TIMEOUT}s."
        except Exception as e:
            screenshot = f"Intervention screenshot failed: {e}"

        if not isinstance(screenshot, str):
            screenshot = str(screenshot)
        is_ss = _is_base64_png_blob(screenshot)
        await self._send_event(session_id, {
            "type": "tool_result",
            "name": "intervention_screenshot",
            "data": screenshot if is_ss else screenshot[:3000],
            "is_screenshot": is_ss,
        })

    async def _handle_message(self, msg: dict):
        """Process a user message through Claude API tool-use loop."""
        session_id = msg["session_id"]
        agent_id = msg.get("agent_id", self.agent_id)
        user_text = msg["message"]
        model = _resolve_model(msg.get("model") or self.model, self.model)
        session_tab_id = msg.get("tab_id")
        scheduler_armed = bool(msg.get("scheduler_armed"))
        scheduler_grant_id = str(msg.get("scheduler_grant_id", "") or "").strip()
        system_prompt = _build_orchestrator_system_prompt(
            scheduler_armed=scheduler_armed,
            scheduler_grant_id=scheduler_grant_id,
        )
        tools = _build_orchestrator_tools(
            scheduler_armed=scheduler_armed,
            scheduler_grant_id=scheduler_grant_id,
        )

        print(f"[{session_id}] User: {user_text[:80]} (model={model})")

        # Get or create conversation (load from disk if needed)
        if session_id not in self.sessions:
            self.sessions[session_id] = self._load_session(session_id)
        messages = self.sessions[session_id]
        messages.append({"role": "user", "content": user_text})

        nudge = NudgeState()

        try:
            for turn in range(MAX_TURNS):
                # Periodic context compaction (every 5 turns)
                if turn > 0 and turn % 5 == 0:
                    messages, cstats = compact_messages(messages, fmt="anthropic")
                    if cstats["compacted"]:
                        print(f"[{session_id}] Compacted {cstats['compacted']} tool results "
                              f"({cstats['tokens_before']}→{cstats['tokens_after']} est tokens)")

                # Hard-stop guard: force final after one recovery turn
                extra_kwargs = {}
                if nudge.hard_stop_guard and nudge.hard_stop_recovery_used >= 1:
                    extra_kwargs["tool_choice"] = {"type": "none"}
                    print(f"[{session_id}] Hard-stop guard: forcing final response")

                response = await self.client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                    **extra_kwargs,
                )

                # If done (no tool calls), send final text
                if response.stop_reason == "end_turn":
                    text_parts = [b.text for b in response.content
                                  if hasattr(b, "text")]
                    final_text = "\n".join(text_parts) if text_parts else ""
                    if final_text:
                        await self._send_event(session_id, {
                            "type": "text", "data": final_text,
                        })
                    messages.append({
                        "role": "assistant", "content": response.content,
                    })
                    self._save_session(session_id, messages)
                    await self._send_event(session_id, {"type": "done"})
                    print(f"[{session_id}] Done ({turn + 1} turns)")
                    return

                # Build tool-call signature for loop detection
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                if tool_use_blocks:
                    sig = json.dumps([
                        {"name": b.name, "args": json.dumps(b.input, sort_keys=True)}
                        for b in tool_use_blocks
                    ], sort_keys=True)
                    loop_detected, loop_nudge, loop_feedback = nudge.check_loop(sig)
                else:
                    loop_detected = False

                if loop_detected:
                    # Emit intervention event if feedback available
                    if loop_feedback and getattr(loop_feedback, "should_intervene", False):
                        print(
                            f"[{session_id}] Intervention {loop_feedback.severity} "
                            f"(reasons={','.join(loop_feedback.reason_codes[:3])}) [loop-short-circuit]"
                        )
                        await self._emit_intervention_event(
                            session_id, agent_id,
                            getattr(loop_feedback, "severity", "hard_stop"),
                            loop_nudge,
                            tab_id=session_tab_id,
                        )

                    # Inject nudge into tool results, force final
                    messages.append({
                        "role": "assistant", "content": response.content,
                    })
                    nudge_results = []
                    for b in tool_use_blocks:
                        nudge_results.append({
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": loop_nudge,
                        })
                    messages.append({"role": "user", "content": nudge_results})

                    print(f"[{session_id}] Loop detected — forcing final response")
                    try:
                        final_resp = await self.client.messages.create(
                            model=model,
                            max_tokens=4096,
                            system=system_prompt,
                            tools=tools,
                            messages=messages,
                            tool_choice={"type": "none"},
                        )
                        text_parts = [b.text for b in final_resp.content
                                      if hasattr(b, "text")]
                        final_text = "\n".join(text_parts) if text_parts else ""
                    except Exception as e:
                        print(f"[{session_id}] Forced-final error: {e}")
                        final_text = ""
                    if not final_text:
                        final_text = (
                            "I got stuck in a loop and couldn't complete the task. "
                            "Please try rephrasing your request."
                        )
                    await self._send_event(session_id, {
                        "type": "text", "data": final_text,
                    })
                    self._save_session(session_id, messages)
                    await self._send_event(session_id, {"type": "done"})
                    return

                # Execute tool calls
                tool_results = []
                turn_step_sigs = []
                turn_find_queries = []
                turn_had_navigation = False
                turn_had_interaction = False
                turn_domain_switch = False

                for block in response.content:
                    if block.type != "tool_use":
                        # Stream any text blocks before tools
                        if hasattr(block, "text") and block.text:
                            await self._send_event(session_id, {
                                "type": "text", "data": block.text,
                            })
                        continue

                    # Track navigation/domain for stagnation
                    if block.name == "navigate":
                        turn_had_navigation = True
                        domain = _extract_domain(str(block.input.get("url", "")))
                        if domain:
                            if nudge.recent_domains and domain != nudge.recent_domains[-1]:
                                turn_domain_switch = True
                            nudge.recent_domains.append(domain)
                    elif block.name in {"click", "type_text", "press_enter", "submit_form"}:
                        turn_had_interaction = True

                    await self._send_event(session_id, {
                        "type": "tool_start", "name": block.name,
                        "input": _safe_truncate(json.dumps(block.input), 200),
                    })

                    result = await self._execute_tool(
                        agent_id,
                        block.name,
                        block.input,
                        tab_id=session_tab_id,
                        session_id=session_id,
                        scheduler_grant_id=scheduler_grant_id,
                    )

                    # Progress signature for stagnation tracking
                    turn_step_sigs.append(
                        _tool_progress_sig(block.name, block.input, result)
                    )
                    # Track --text --find queries
                    if block.name == "ddm":
                        flags = str(block.input.get("flags", "")).strip()
                        if flags.startswith("--text --find"):
                            query = flags[len("--text --find"):].strip().lower()
                            if query:
                                turn_find_queries.append(query)

                    is_screenshot = block.name == "screenshot" and _is_base64_png_blob(result)
                    await self._send_event(session_id, {
                        "type": "tool_result",
                        "name": block.name,
                        "data": result if is_screenshot else result[:3000],
                        "is_screenshot": is_screenshot,
                    })

                    nudge.live_tool_log.append({
                        "turn": turn + 1,
                        "tool": block.name,
                        "args": block.input,
                        "output_preview": result[:3000],
                    })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                messages.append({
                    "role": "assistant", "content": response.content,
                })

                # Update stagnation scoring
                nudge.update_stagnation(
                    turn_step_sigs, turn_find_queries,
                    turn_had_navigation, turn_domain_switch,
                    turn_had_interaction,
                )

                # Run progress-based intervention
                should_emit, feedback = nudge.run_intervention(turn + 1)
                intervention_text = ""
                if should_emit and feedback:
                    prompt = (feedback.feedback_prompt or "").strip()
                    if prompt:
                        intervention_text = prompt
                        nudge.intervention_events += 1
                        nudge.last_intervention_model_turn = turn + 1
                        print(
                            f"[{session_id}] Intervention {feedback.severity} "
                            f"(reasons={','.join(feedback.reason_codes[:3])})"
                        )
                        await self._emit_intervention_event(
                            session_id, agent_id,
                            feedback.severity, prompt,
                            tab_id=session_tab_id,
                        )
                        if feedback.severity == "nudge":
                            prev_stagnation = nudge.stagnation_score
                            nudge.apply_nudge_decay()
                            print(
                                f"[{session_id}] Nudge decay: "
                                f"stall={prev_stagnation}->{nudge.stagnation_score}"
                            )
                            prev_steps = len(nudge.live_tool_log)
                            nudge.apply_nudge_reset()
                            if INTERVENTION_NUDGE_RESET_PROGRESS:
                                print(
                                    f"[{session_id}] Nudge reset: "
                                    f"live_steps={prev_steps}->0"
                                )
                        if feedback.severity == "hard_stop":
                            nudge.hard_stop_guard = True
                            nudge.hard_stop_recovery_used = 0

                # Assemble user message with tool results + optional intervention
                user_content = tool_results[:]
                if intervention_text:
                    user_content.append({
                        "type": "text",
                        "text": intervention_text,
                    })
                messages.append({"role": "user", "content": user_content})

                # Check stall threshold
                action, guidance = nudge.check_stall_threshold()
                if action == "guidance":
                    print(
                        f"[{session_id}] Stall strike "
                        f"{nudge.stall_force_strikes} "
                        f"(score={nudge.stagnation_score}) — continuing with guidance"
                    )
                    # Inject guidance as extra text in last user message
                    if isinstance(messages[-1].get("content"), list):
                        messages[-1]["content"].append({
                            "type": "text", "text": guidance,
                        })
                elif action == "force":
                    print(
                        f"[{session_id}] Progress stalled "
                        f"(score={nudge.stagnation_score}) — forcing final response"
                    )
                    try:
                        final_resp = await self.client.messages.create(
                            model=model,
                            max_tokens=4096,
                            system=system_prompt,
                            tools=tools,
                            messages=messages,
                            tool_choice={"type": "none"},
                        )
                        text_parts = [b.text for b in final_resp.content
                                      if hasattr(b, "text")]
                        final_text = "\n".join(text_parts) if text_parts else ""
                    except Exception as e:
                        print(f"[{session_id}] Forced-final error: {e}")
                        final_text = ""
                    if not final_text:
                        final_text = guidance
                    await self._send_event(session_id, {
                        "type": "text", "data": final_text,
                    })
                    self._save_session(session_id, messages)
                    await self._send_event(session_id, {"type": "done"})
                    return

                # Hard-stop guard tracking
                if nudge.hard_stop_guard and tool_use_blocks:
                    nudge.hard_stop_recovery_used += 1

            # Hit max turns
            await self._send_event(session_id, {
                "type": "text",
                "data": "Reached maximum tool-use turns. Task may be incomplete.",
            })
            self._save_session(session_id, messages)
            await self._send_event(session_id, {"type": "done"})

        except asyncio.CancelledError:
            print(f"[{session_id}] Task cancelled")
            self._save_session(session_id, messages)
        except anthropic.AuthenticationError as e:
            print(f"[{session_id}] Auth error: {e}")
            self._save_session(session_id, messages)
            await self._send_event(session_id, {
                "type": "error",
                "data": "Anthropic API key is invalid or revoked. "
                        "Please update your key at /setup?provider=claude-sdk",
            })
        except anthropic.RateLimitError as e:
            print(f"[{session_id}] Rate limit: {e}")
            self._save_session(session_id, messages)
            msg = str(e)
            if "credit balance" in msg.lower() or "billing" in msg.lower():
                await self._send_event(session_id, {
                    "type": "error",
                    "data": "Anthropic API credit balance is too low. "
                            "Please add credits at https://console.anthropic.com/settings/billing",
                })
            else:
                await self._send_event(session_id, {
                    "type": "error",
                    "data": "Anthropic API rate limit reached. "
                            "Please wait a few minutes, or upgrade your plan at "
                            "https://console.anthropic.com/settings/billing",
                })
        except anthropic.BadRequestError as e:
            print(f"[{session_id}] Bad request: {e}")
            self._save_session(session_id, messages)
            msg = str(e)
            low = msg.lower()
            if "credit balance" in low:
                await self._send_event(session_id, {
                    "type": "error",
                    "data": "Anthropic API credit balance is too low. "
                            "Please add credits at https://console.anthropic.com/settings/billing",
                })
            elif ("too long" in low or "too many tokens" in low or "context length" in low
                  or "max tokens" in low) and len(messages) > 12:
                # Context overflow — emergency trim for next request
                messages = emergency_trim(messages, fmt="anthropic", keep_tail=10)
                self.sessions[session_id] = messages
                self._save_session(session_id, messages)
                print(f"[{session_id}] Context overflow — emergency trim to {len(messages)} msgs")
                await self._send_event(session_id, {
                    "type": "error",
                    "data": "Context was too large — trimmed history. Please resend your last message.",
                })
            else:
                await self._send_event(session_id, {
                    "type": "error", "data": str(e),
                })
        except Exception as e:
            print(f"[{session_id}] Error: {e}")
            self._save_session(session_id, messages)
            msg = str(e)
            if "credit balance" in msg.lower() or "billing" in msg.lower():
                await self._send_event(session_id, {
                    "type": "error",
                    "data": "Anthropic API credit balance is too low. "
                            "Please add credits at https://console.anthropic.com/settings/billing",
                })
            else:
                await self._send_event(session_id, {
                    "type": "error", "data": msg,
                })

    async def _execute_tool(
        self,
        agent_id: str,
        name: str,
        input_data: dict,
        tab_id: str | None = None,
        session_id: str = "",
        scheduler_grant_id: str = "",
    ) -> str:
        """Execute a tool call against the agent's Chrome via cloud_tools."""
        if name in SCHEDULER_TOOL_NAMES:
            return await execute_scheduler_tool(
                server_url=self.server,
                api_key=self.api_key,
                session_id=session_id,
                scheduler_grant_id=scheduler_grant_id,
                tool_name=name,
                args=input_data,
            )

        args_tab = input_data.get("tab_id", "")
        if args_tab and args_tab != "auto":
            effective_tab = args_tab
        elif tab_id:
            effective_tab = tab_id
        else:
            effective_tab = "auto"

        try:
            if name == "ddm":
                flags = input_data.get("flags", "--llm-2pass --cols 60")
                return await cloud_tools.run_ddm(
                    agent_id, effective_tab, flags.split(),
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "intel_probe":
                return await cloud_tools.run_intel(
                    agent_id, effective_tab, ["--probe"],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "intel_extract":
                flags = ["--extract"]
                strategy = input_data.get("strategy", "")
                if strategy:
                    flags += ["--strategy", strategy]
                return await cloud_tools.run_intel(
                    agent_id, effective_tab, flags,
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "intel_stores":
                return await cloud_tools.run_intel(
                    agent_id, effective_tab, ["--stores"],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "intel_find_paths":
                global_name = input_data["global_name"]
                key = input_data["key"]
                return await cloud_tools.run_intel(
                    agent_id, effective_tab,
                    ["--find-paths", global_name, key],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "navigate":
                return await cloud_tools.navigate(
                    agent_id, effective_tab, input_data["url"],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "click":
                return await cloud_tools.click(
                    agent_id, effective_tab,
                    input_data["x"], input_data["y"],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "type_text":
                return await cloud_tools.type_text(
                    agent_id, effective_tab, input_data["text"],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "js_eval":
                return await cloud_tools.run_js(
                    agent_id, effective_tab, input_data["expression"],
                    RELAY_HOST, RELAY_PORT,
                )

            elif name == "screenshot":
                data = await cloud_tools.screenshot(
                    agent_id, effective_tab,
                    RELAY_HOST, RELAY_PORT,
                )
                return data  # base64 PNG

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            return f"Tool error ({name}): {e}"


def _safe_truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "..."


def main():
    parser = argparse.ArgumentParser(description="Unchained Chat Agent (SDK)")
    parser.add_argument("--key", default=os.environ.get("UNCHAINED_API_KEY", ""),
                        help="Unchained API key (default: UNCHAINED_API_KEY)")
    parser.add_argument("--agent", required=True, help="Agent ID (a-12345678)")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"Server URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--model", default=MODEL, help=f"Claude model (default: {MODEL})")
    args = parser.parse_args()

    if not args.key:
        parser.error("--key or UNCHAINED_API_KEY is required.")

    agent = ChatAgent(args.key, args.agent, args.server)
    agent.model = args.model

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: loop.stop())

    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        print("\nChat agent stopped.")


if __name__ == "__main__":
    main()
