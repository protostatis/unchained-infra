"""chat_agent_openrouter.py — Trial chat agent using OpenRouter (free model).

Runs on EC2 with the OpenRouter API key — users get free browser automation
without needing their own LLM API key. Uses the same WebSocket relay protocol
as chat_agent_sdk.py but drives an OpenRouter model instead of Claude.

Architecture:
    Phone → EC2 web server (POST /web/chat, SSE response)
         → WebSocket bridge
         → This script (runs on EC2, inside Docker)
         → OpenRouter API (free model, API key from EC2 env)
            → cloud_tools → relay:8765 → Chrome on user's machine

Usage (local dev):
    export OPENROUTER_API_KEY=sk-or-...
    uv run chat_agent_openrouter.py --key uc_live_... --agent a-12345678
    uv run chat_agent_openrouter.py --local-cli --agent a-12345678

Usage (EC2 / Docker):
    Set OPENROUTER_API_KEY, RELAY_HOST, RELAY_PORT, UNCHAINED_SERVER in env.
    docker compose up -d trial-agent

Model options (free tier):
    nvidia/nemotron-3-super-120b-a12b:free
    nvidia/nemotron-3-nano-30b-a3b:free
    poolside/laguna-xs-2.1:free
    meta-llama/llama-3.3-70b-instruct:free
    google/gemma-3-27b-it:free
    deepseek/deepseek-chat-v3-0324:free
"""

import argparse
import asyncio
from contextvars import ContextVar
import json
import os
import re
import signal
import sys
import time
import uuid as _uuid_module
from dataclasses import dataclass, field

import httpx
import websockets

import cloud_tools
from chat_event_transport import CHAT_WS_MAX_MESSAGE_BYTES, send_agent_event
from context_compact import compact_messages, emergency_trim
from scheduler_agent import (
    OPENAI_SCHEDULER_TOOLS,
    SCHEDULER_TOOL_NAMES,
    SCHEDULER_TOOL_PROMPT,
    _api_url_from_server,
    build_system_prompt as _build_scheduler_system_prompt,
    execute_scheduler_tool,
)
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
from reflex import ReflexState, REFLEX_ENABLED


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-3.1-flash-lite"
DEFAULT_SERVER = "wss://api.unchainedsky.com"
_REASONING_REQUIRED_MODELS = frozenset({"google/gemini-3.5-flash-lite"})
_DEFINITIVE_UNBILLED_HTTP_STATUSES = frozenset({400, 404})

# When running inside Docker, override these via env vars:
#   RELAY_HOST=relay  RELAY_PORT=8765
RELAY_HOST = os.environ.get("RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "443"))

MAX_TURNS = 50                # Base cap per user message
EXTENSION_BLOCK = 25          # Extra turns granted per healthy extension
MAX_ABSOLUTE_TURNS = 200      # Hard ceiling (never exceed)

# Session persistence: store messages in relay_data volume so history
# survives container restarts.
SESSION_DIR = os.environ.get(
    "SESSION_DIR",
    os.environ.get(
        "UNCHAINED_SESSIONS_DIR",
        os.path.join(
            os.environ.get("UNCHAINED_DATA_DIR", os.path.expanduser("~/.unchained")),
            "sessions",
        ),
    ),
)
MAX_SESSION_MESSAGES = 30  # keep last 30 messages (excluding system prompt)
TRIM_ON_ERROR = 10         # messages to keep on context-too-large retry
HOSTED_MAX_INPUT_CHARS = max(
    10_000, int(os.environ.get("HOSTED_MAX_INPUT_CHARS", "200000"))
)
TOOL_EXEC_TIMEOUT = int(os.environ.get("TOOL_EXEC_TIMEOUT", "45"))
FORCE_FINAL_TIMEOUT = int(os.environ.get("FORCE_FINAL_TIMEOUT", "35"))
LIVE_PREVIEW_TIMEOUT = int(os.environ.get("LIVE_PREVIEW_TIMEOUT", "20"))
AUTO_LIVE_PREVIEW = os.environ.get("AUTO_LIVE_PREVIEW", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_task_req_id: ContextVar[str] = ContextVar("chat_agent_openrouter_task_req_id", default="")
# Nudge constants are imported from nudge.py

# Local CLI defaults
LOCAL_SESSION_ID = "local-openrouter"
LOCAL_TOOL_PREVIEW_CHARS = 240
LOCAL_CONTEXT_KEEP_TAIL = 10


def _uuid_hex() -> str:
    """Return a random hex UUID string."""
    return _uuid_module.uuid4().hex


def _openrouter_error_message(response: httpx.Response | None) -> str:
    """Extract a bounded provider message without leaking a raw request URL."""
    if response is None:
        return "OpenRouter rejected the request."
    try:
        payload = response.json()
    except Exception:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message", "") or "").strip()
    else:
        message = str(error or "").strip()
    if not message:
        message = str(getattr(response, "text", "") or "").strip()
    return message[:400] or "OpenRouter rejected the request."


def _openrouter_user_error(exc: httpx.HTTPStatusError, model: str) -> str:
    """Return a useful user-facing provider rejection without httpx boilerplate."""
    status = int(getattr(exc.response, "status_code", 0) or 0)
    message = _openrouter_error_message(exc.response)
    if status == 404:
        return f"OpenRouter model {model} is currently unavailable: {message}"
    if status == 400:
        return f"OpenRouter rejected model {model}: {message}"
    status_label = str(status) if status else "provider error"
    return f"OpenRouter request failed ({status_label}): {message}"


# ---------------------------------------------------------------------------
# Per-task scheduler state — ContextVar avoids shared-instance races when
# concurrent sessions or same-session replacement tasks run on one agent.
# ---------------------------------------------------------------------------


@dataclass
class SchedulerTurnState:
    """Immutable snapshot of the current task's scheduler armament."""
    armed: bool = False
    grant_id: str = ""
    session_id: str = ""


_scheduler_turn: ContextVar[SchedulerTurnState] = ContextVar(
    "chat_agent_openrouter_scheduler_turn",
    default=SchedulerTurnState(),
)


# ---------------------------------------------------------------------------
# System prompt — built from CLAUDE.md + function-call tool reference
# ---------------------------------------------------------------------------

_CLAUDE_MD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CLAUDE.md")

# Function-call tool reference replaces the CLI cdp_tool.py section in CLAUDE.md
_TOOL_REF = """## Tool Reference (Function Calls)

IMPORTANT: You have browser tools available as **function calls**, not CLI commands.
Do NOT output shell commands like `cdp_tool.py` or `uv run`. Call tools directly.

## Browser Availability

If a browser tool returns a result starting with `BROWSER_UNAVAILABLE`, the user's
Chrome connector is not running. When this happens:
- Do NOT retry any browser tools
- Answer the user's question directly from your own knowledge
- You may mention that live browsing is unavailable if it's relevant to the answer
- You can still hold conversations, answer general questions, and help with tasks
  that don't require a browser

| Call | What it does |
|------|-------------|
| `ddm(flags="--llm-2pass --cols 60")` | Map page layout + interactive elements (~500 tok) |
| `ddm(flags="--text")` | Extract page text |
| `ddm(flags="--text --find keyword")` | Search text on page |
| `ddm(flags="--text --max 5000")` | More text (custom limit) |
| `ddm(flags="--at 694,584")` | Element details at pixel coordinates |
| `ddm(flags="--js expression")` | Execute JS on page, return result |
| `intel_probe()` | Page fingerprint + Bayesian strategy ranking (~100 tok) |
| `intel_extract(strategy="")` | Extract structured data (auto or forced strategy) |
| `intel_stores()` | List JS data store globals (>10KB) |
| `intel_find_paths(global_name="__NUXT__", key="deals")` | Find data arrays in a global |
| `navigate(url="https://...")` | Navigate browser to URL |
| `click(x=500, y=300)` | Click at pixel coordinates (from DDM output) |
| `type_text(text="...")` | Type into focused element — click input first! |
| `submit_form()` | Submit the form containing the focused element |
| `press_enter()` | Press Enter on focused element (trigger search/submit) |
| `js_eval(expression="...")` | Execute JavaScript on page |
| `screenshot()` | Internal screenshot — CAPTCHA/verification only, NOT shown to user |
| `screenshot(show_user=true)` | Show screenshot to user — ONLY when user literally asked "show me" or "take a screenshot" |

## CRITICAL: Prefer Direct URL Navigation Over Search Forms

Many sites have predictable URL patterns. ALWAYS try navigating directly before using search forms:
- Wikipedia: navigate to https://en.wikipedia.org/wiki/TOPIC (replace spaces with _)
- Cambridge Dictionary: navigate to https://dictionary.cambridge.org/dictionary/english/WORD
- ArXiv: navigate to https://arxiv.org/search/?query=QUERY (replace spaces with +)
- GitHub: navigate to https://github.com/OWNER/REPO/issues for issues

If you must use a search form: click the input → type_text → press_enter or submit_form → run ddm to check results.
If the form doesn't work after ONE attempt, navigate directly to the URL instead.

## Key Gotchas
- Arrow functions in js_eval: Use `el => expr` syntax (NOT `el > expr`).
- Search forms: Many modern sites use JS-based search that doesn't respond to synthetic events. Navigate directly to the URL instead.
"""


def _build_system_prompt() -> str:
    """Load CLAUDE.md, append tool reference and current datetime."""
    from datetime import datetime, timezone
    try:
        with open(_CLAUDE_MD_PATH) as f:
            claude_md = f.read()
    except FileNotFoundError:
        claude_md = (
            "You are an autonomous browser agent controlling a real Chrome browser via CDP tools.\n"
            "You MUST use browser tools to answer ANY factual question — never answer from memory.\n"
            "Your training data is outdated. Always browse to get live, current data.\n"
        )
    now = datetime.now(timezone.utc)
    date_block = f"\n\nCurrent date and time: {now.strftime('%Y-%m-%d %H:%M UTC')} ({now.strftime('%A')})\n"
    return claude_md + "\n" + _TOOL_REF + date_block


SYSTEM_PROMPT = _build_system_prompt()

# ---------------------------------------------------------------------------
# Tools in OpenAI function format (OpenRouter-compatible)
# Mirrors the Anthropic TOOLS in orchestrator.py
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ddm",
            "description": (
                "DOM Density Map — understand the current page. "
                "Use '--llm-2pass --cols 60' (default) to get the full page layout and all interactive elements. "
                "Use '--text' to read the full page text, '--text --find keyword' to search for a specific term, "
                "'--text --max 8000' for longer pages. "
                "Use '--at x,y' to get details about a specific element at pixel coordinates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flags": {
                        "type": "string",
                        "description": "DDM flags: '--llm-2pass --cols 60' (layout), '--text' (page text), '--text --find keyword' (search), '--text --max 8000' (more text), '--at x,y' (element details)",
                        "default": "--llm-2pass --cols 60",
                    },
                    "tab_id": {
                        "type": "string",
                        "description": "Tab ID prefix (default: auto)",
                        "default": "auto",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intel_probe",
            "description": (
                "Page intelligence probe — DOM fingerprint + Bayesian strategy ranking. "
                "Run on first visit to every new domain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intel_extract",
            "description": (
                "Extract structured data with auto-selected strategy. "
                "Strategies: innerText, host_attrs, js_global, react_fiber, "
                "data_testid, heading_hier, img_alt, shadow_pierce."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {"type": "string", "default": ""},
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intel_stores",
            "description": "List JS data store globals (>10KB). Use on Nuxt/Next/YouTube sites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intel_find_paths",
            "description": "Find data arrays in a JS global by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "global_name": {"type": "string"},
                    "key": {"type": "string"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["global_name", "key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL. Returns page layout with interactive elements — no separate ddm call needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click at pixel coordinates. Returns what was clicked, a '--- changed ---' diff, plus page layout with interactive elements — no separate ddm call needed. '--- no change ---' means the click had no effect — try a different coordinate or use js_eval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the focused input. Auto-focuses the single visible input if none is focused. Returns what received the text plus a '--- changed ---' diff with new elements and clickable coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_form",
            "description": (
                "Submit the form containing the focused element. Returns "
                "'--- changed ---' diff showing URL/title changes if navigation occurred."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_enter",
            "description": (
                "Press Enter on the focused element. Returns '--- changed ---' diff "
                "showing URL/title changes if form submitted, or new elements that appeared."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "js_eval",
            "description": "Execute JavaScript on the page. Returns JSON for objects/arrays.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "tab_id": {"type": "string", "default": "auto"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Capture screenshot. Use ONLY for CAPTCHAs or visual verification. IMPORTANT: show_user defaults to false — do NOT set it to true unless the user's message literally asks to see the page (e.g. 'show me', 'take a screenshot', 'what does it look like').",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "default": "auto"},
                    "show_user": {
                        "type": "boolean",
                        "default": False,
                        "description": "MUST be false (default) unless the user's message literally contains words like 'screenshot', 'show me the page', or 'what does it look like'. Navigation, verification, CAPTCHA checks, and debugging are NEVER show_user=true.",
                    },
                },
            },
        },
    },
]


def _build_scheduler_openai_tools(scheduler_armed: bool, scheduler_grant_id: str) -> list[dict]:
    """Return the OpenAI-format tool list with scheduler tools included when armed."""
    if not scheduler_armed or not scheduler_grant_id:
        return list(TOOLS)
    return list(TOOLS) + list(OPENAI_SCHEDULER_TOOLS)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "..."


def _decode_tool_arguments(raw_args) -> dict:
    """Decode OpenRouter tool arguments into a dict."""
    if raw_args is None:
        return {}
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        raw = raw_args.strip()
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        decoded = raw_args
    if isinstance(decoded, dict):
        return decoded
    return {}


def _ui_tool_name(tool_name: str) -> str:
    """Map function-call tool names to the same UI categories as chat_agent_cli.py."""
    if tool_name.startswith("intel_"):
        return "intel"
    if tool_name.startswith("scheduler_"):
        return "scheduler"
    if tool_name == "type_text":
        return "type"
    if tool_name == "js_eval":
        return "js"
    if tool_name == "submit_form":
        return "js"
    if tool_name == "press_enter":
        return "type"
    return tool_name or "tool"


def _ui_tool_input(tool_name: str, args: dict) -> str:
    """Render concise, CLI-like tool input text for the frontend action log."""
    if tool_name == "ddm":
        return str(args.get("flags", "--llm-2pass --cols 60"))
    if tool_name == "navigate":
        return str(args.get("url", ""))
    if tool_name == "click":
        x = args.get("x", "")
        y = args.get("y", "")
        return f"{x} {y}".strip()
    if tool_name == "type_text":
        return str(args.get("text", ""))
    if tool_name == "js_eval":
        return str(args.get("expression", ""))
    if tool_name == "intel_probe":
        return "--probe"
    if tool_name == "intel_extract":
        strategy = str(args.get("strategy", "")).strip()
        return f"--extract --strategy {strategy}" if strategy else "--extract"
    if tool_name == "intel_stores":
        return "--stores"
    if tool_name == "intel_find_paths":
        g = str(args.get("global_name", "")).strip()
        k = str(args.get("key", "")).strip()
        if g and k:
            return f"--find-paths {g} {k}"
        return "--find-paths"
    if tool_name == "submit_form":
        return "active form"
    if tool_name == "press_enter":
        return "Enter"
    if tool_name == "scheduler_list_jobs":
        return "list"
    if tool_name in {"scheduler_preview_job", "scheduler_save_job", "scheduler_delete_job"}:
        job_id = str(args.get("job_id", "")).strip()
        return job_id or "job"
    if tool_name == "screenshot":
        return ""
    return json.dumps(args, sort_keys=True)


def _message_content_as_text(message: dict) -> str:
    """Render any message content field to plain text for logs/token estimates."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _estimate_tokens(messages: list[dict]) -> int:
    """Very rough token estimate used for local context visibility."""
    total_chars = 0
    for msg in messages:
        total_chars += len(_message_content_as_text(msg))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            total_chars += len(str(fn.get("name", "")))
            total_chars += len(str(fn.get("arguments", "")))
    # Approximation: ~4 chars/token for English-heavy text
    return max(1, total_chars // 4)


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _extract_openrouter_usage(payload: dict) -> dict:
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    if not isinstance(usage, dict):
        usage = {}

    prompt_tokens = _coerce_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        0,
    )
    completion_tokens = _coerce_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0)),
        0,
    )
    total_tokens = _coerce_int(
        usage.get("total_tokens", prompt_tokens + completion_tokens),
        prompt_tokens + completion_tokens,
    )

    cost_usd = 0.0
    cost_present = False
    for candidate in (
        usage.get("cost"),
        usage.get("cost_usd"),
        usage.get("total_cost"),
        usage.get("total_cost_usd"),
        payload.get("cost"),
        payload.get("cost_usd"),
        payload.get("total_cost"),
        payload.get("total_cost_usd"),
    ):
        if candidate is not None:
            value = _coerce_float(candidate, 0.0)
            cost_present = True
            if value > 0:
                cost_usd = value
                break
            # Even zero-cost presence marks the flag

    est_cost_per_1k = max(
        0.0,
        _coerce_float(os.environ.get("OPENROUTER_EST_COST_PER_1K_TOKENS", "0"), 0.0),
    )
    estimated_cost_usd = 0.0
    if cost_usd <= 0 and est_cost_per_1k > 0 and total_tokens > 0:
        estimated_cost_usd = (total_tokens / 1000.0) * est_cost_per_1k

    return {
        "openrouter_id": str(payload.get("id", "")),
        "prompt_tokens": max(0, prompt_tokens),
        "completion_tokens": max(0, completion_tokens),
        "total_tokens": max(0, total_tokens),
        "cost_usd": round(max(0.0, cost_usd), 9),
        "estimated_cost_usd": round(max(0.0, estimated_cost_usd), 9),
        "cost_present": cost_present,
    }


def _looks_like_internal_tool_payload(text: str) -> bool:
    """Detect accidental user-visible tool-call payloads."""
    raw = (text or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if "<tool_call" in lowered or "</tool_call>" in lowered:
        return True
    if re.search(
        r'^\s*\{\s*"?name"?\s*:\s*[^,\n]+,\s*"?arguments"?\s*:\s*\{',
        raw,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(r'(?s)```(?:json)?\s*\{.*"name"\s*:.*"arguments"\s*:.*\}\s*```', raw):
        return True
    if raw.count('"name"') >= 1 and raw.count('"arguments"') >= 1 and len(raw) > 120:
        return True
    return False


def _strip_internal_tool_payload(text: str) -> str:
    """Best-effort local cleanup of leaked tool-call payload text."""
    cleaned = text or ""
    # Remove explicit tool-call wrappers and JSON fenced blocks first.
    cleaned = re.sub(r"(?is)<tool_call\b.*?</tool_call>", " ", cleaned)
    cleaned = re.sub(r"(?is)```(?:json)?\s*.*?```", " ", cleaned)
    # Remove inline function-call style JSON objects.
    cleaned = re.sub(
        r'(?is)\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
        " ",
        cleaned,
    )
    cleaned = re.sub(
        r'(?is)\{\s*name\s*:\s*[^,\n]+,\s*arguments\s*:\s*\{.*?\}\s*\}',
        " ",
        cleaned,
    )
    # Drop residual JSON-ish lines often emitted around tool payloads.
    kept_lines: list[str] = []
    for line in cleaned.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if "<tool_call" in low or "</tool_call>" in low:
            continue
        if s in {"{", "}", "[", "]", ",", "```", "```json"}:
            continue
        if (
            low.startswith('"name"')
            or low.startswith('"arguments"')
            or low.startswith('"tool_call_id"')
            or low.startswith("name:")
            or low.startswith("arguments:")
            or low.startswith("tool_call_id:")
        ):
            continue
        kept_lines.append(s)
    cleaned = " ".join(kept_lines)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Trial Agent
# ---------------------------------------------------------------------------

class TrialAgent:
    """OpenRouter-powered trial agent connecting to the unchained relay."""

    def __init__(self, api_key: str, agent_id: str, server: str, model: str):
        self.api_key = api_key
        self.agent_id = agent_id
        self.server = server
        self.model = model
        self.openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.ws = None
        # session_id → list of messages (in-memory cache; backed by disk)
        self.sessions: dict[str, list] = {}
        # session_id → active asyncio Task (for cancel support)
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.active_req_ids: dict[str, str] = {}
        # Per-session billing run IDs (set from ws_msg["billing_run_id"])
        self._session_billing_runs: dict[str, str] = {}

    @property
    def _credit_base_url(self) -> str:
        """Convert the WebSocket server URL to an HTTPS URL for credit API calls.

        When UNCHAINED_SERVER is ``ws://web:8080`` (Docker internal network),
        this becomes ``http://web:8080`` — the internal web service is always
        reachable without TLS inside the Docker network.
        """
        base = self.server
        if base.startswith("wss://"):
            base = "https://" + base[6:]
        elif base.startswith("ws://"):
            base = "http://" + base[5:]
        return base.rstrip("/")

    def _hosted_service_token(self) -> str:
        """Return the narrowly-scoped hosted-worker callback token.

        This credential is mandatory and intentionally does not fall back to
        the worker's WebSocket key or any other service credential.
        """
        return os.environ.get("HOSTED_AGENT_SERVICE_TOKEN", "").strip()

    def _credit_service_token(self) -> str:
        return self._hosted_service_token()

    async def _credit_reserve(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        model: str,
        idempotency_key: str,
    ) -> dict | None:
        """Call the credit reserve endpoint. Returns reservation dict or None.

        A duplicate (already_reserved) response is treated as a success:
        the existing ``call_id`` is still valid and usable.
        """
        token = self._credit_service_token()
        if not token or not run_id:
            return None
        url = f"{self._credit_base_url}/internal/credit/reserve"
        body = {
            "run_id": run_id,
            "model": model,
            "idempotency_key": idempotency_key,
        }
        try:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(5.0),
            )
            if resp.is_success:
                data = resp.json()
                # A duplicate reservation is still a valid call_id holder
                if isinstance(data, dict) and data.get("call_id"):
                    return data
                return None
            if resp.status_code == 402:
                # Specific credit-insufficient signal
                print(f"[credit] Reserve 402: insufficient balance")
            return None
        except Exception:
            return None

    async def _credit_mark_submitted(
        self,
        client: httpx.AsyncClient,
        call_id: str,
    ) -> dict | None:
        """Persist submission before crossing the OpenRouter boundary."""
        token = self._credit_service_token()
        if not token or not call_id:
            return None
        try:
            resp = await client.post(
                f"{self._credit_base_url}/internal/credit/submitted",
                json={"call_id": call_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(5.0),
            )
            if resp.is_success:
                data = resp.json()
                return data if isinstance(data, dict) else None
            return None
        except Exception:
            return None

    async def _credit_settle(
        self,
        client: httpx.AsyncClient,
        call_id: str,
        actual_cost_micro_usd: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_absent: bool = False,
        provider_cost_micro_usd: int = 0,
    ) -> dict | None:
        """Call the credit settle endpoint. Returns settlement dict or None."""
        token = self._credit_service_token()
        if not token or not call_id:
            return None
        url = f"{self._credit_base_url}/internal/credit/settle"
        body = {
            "call_id": call_id,
            "actual_cost_micro_usd": actual_cost_micro_usd,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_absent": cost_absent,
            "provider_cost_micro_usd": provider_cost_micro_usd,
        }
        try:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(5.0),
            )
            if resp.is_success:
                return resp.json()
            return None
        except Exception:
            return None

    async def _credit_release(
        self,
        client: httpx.AsyncClient,
        call_id: str,
    ) -> dict | None:
        """Call the credit release endpoint. Returns release dict or None."""
        token = self._credit_service_token()
        if not token or not call_id:
            return None
        url = f"{self._credit_base_url}/internal/credit/release"
        body = {"call_id": call_id}
        try:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(5.0),
            )
            if resp.is_success:
                return resp.json()
            return None
        except Exception:
            return None

    def _session_path(self, session_id: str) -> str:
        os.makedirs(SESSION_DIR, exist_ok=True)
        safe_id = session_id.replace("/", "_").replace("..", "").replace(" ", "_")
        return os.path.join(SESSION_DIR, f"{safe_id}.json")

    def _load_session(self, session_id: str) -> list:
        """Load session from disk, prepend current system prompt. Returns messages list."""
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
        return [{"role": "system", "content": _build_system_prompt()}] + msgs

    def _save_session(self, session_id: str, messages: list, max_messages: int | None = None):
        """Persist session messages to disk (system prompt excluded, capped)."""
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

    async def _sanitize_user_output(
        self,
        client: httpx.AsyncClient,
        model: str,
        draft_text: str,
        session_id: str = "",
    ) -> str:
        """Ensure assistant output is plain user-facing text (no raw tool payloads)."""
        text = (draft_text or "").strip()
        if not text:
            return ""
        if not _looks_like_internal_tool_payload(text):
            return text

        if session_id:
            print(f"[{session_id}] Sanitizing internal tool payload from final response")
        # Keep sanitization local-only so forced-final paths cannot hang on
        # an extra model round-trip when providers are degraded.
        cleaned = _strip_internal_tool_payload(text)
        if cleaned and not _looks_like_internal_tool_payload(cleaned):
            return cleaned
        if session_id:
            print(f"[{session_id}] Output sanitization fell back to safe placeholder")

        return (
            "I hit an internal formatting issue while preparing the response. "
            "I can continue and provide a clean summary if you want me to proceed."
        )

    async def _emit_intervention_event(
        self,
        session_id: str,
        agent_id: str,
        severity: str,
        prompt: str,
        messages: list | None = None,
        tab_id: str | None = None,
    ):
        """Emit intervention event and optional screenshot context for nudge severity."""
        await self._send(
            session_id,
            {
                "type": "tool_start",
                "name": "intervention",
                "input": severity,
            },
        )
        await self._send(
            session_id,
            {
                "type": "tool_result",
                "name": "intervention",
                "data": (prompt or "")[:1500],
                "is_screenshot": False,
            },
        )

        if severity != "nudge" or not INTERVENTION_SCREENSHOT_ON_NUDGE:
            return

        await self._send(
            session_id,
            {
                "type": "tool_start",
                "name": "intervention_screenshot",
                "input": "current page",
            },
        )
        try:
            screenshot = await asyncio.wait_for(
                self._execute_tool(agent_id, "screenshot", {}, tab_id=tab_id),
                timeout=INTERVENTION_SCREENSHOT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            screenshot = (
                "Intervention screenshot timed out after "
                f"{INTERVENTION_SCREENSHOT_TIMEOUT}s."
            )
        except Exception as e:
            screenshot = f"Intervention screenshot failed: {e}"

        if not isinstance(screenshot, str):
            screenshot = str(screenshot)
        is_screenshot = _is_base64_png_blob(screenshot)
        await self._send(
            session_id,
            {
                "type": "tool_result",
                "name": "intervention_screenshot",
                "data": screenshot if is_screenshot else screenshot[:3000],
                "is_screenshot": is_screenshot,
                "visible": False,
            },
        )

        if is_screenshot:
            print(f"[{session_id}] Intervention screenshot captured")
            if messages is not None:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Intervention context: a fresh screenshot was captured. "
                            "Re-orient on the current page state before choosing the next action."
                        ),
                    }
                )
        else:
            print(f"[{session_id}] Intervention screenshot unavailable: {_truncate(screenshot, 120)}")

    async def _emit_live_preview(
        self,
        session_id: str,
        agent_id: str,
        *,
        tab_id: str | None = None,
        note: str = "Page loaded",
    ):
        """Capture a small JPEG screenshot to refresh the First Look live panel.

        Uses JPEG at quality 50 clipped to 1280x720 to match the screencast
        viewport.  This keeps the image small (~30-50KB) and visually
        consistent with the live stream frames.
        """
        if not AUTO_LIVE_PREVIEW:
            return
        effective_tab = tab_id or "auto"
        try:
            screenshot = await asyncio.wait_for(
                cloud_tools.screenshot(
                    agent_id, effective_tab, RELAY_HOST, RELAY_PORT,
                    format="jpeg", quality=50,
                    max_width=1280, max_height=720,
                ),
                timeout=LIVE_PREVIEW_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[{session_id}] Live preview timed out after {LIVE_PREVIEW_TIMEOUT}s")
            return
        except Exception as e:
            print(f"[{session_id}] Live preview capture failed: {e}")
            return

        if not isinstance(screenshot, str):
            screenshot = str(screenshot)
        if not _is_base64_png_blob(screenshot):
            print(f"[{session_id}] Live preview skipped (non-image payload)")
            return

        print(f"[{session_id}] Live preview: sending ({len(screenshot)} bytes)")
        await self._send(
            session_id,
            {
                "type": "live_preview",
                "data": screenshot,
                "note": note,
                "mime": "image/jpeg",
            },
        )

    async def connect(self):
        url = f"{self.server}/chat/ws"
        print(f"Connecting to {url} ...")
        self.ws = await websockets.connect(
            url, ping_interval=20, ping_timeout=30, max_size=CHAT_WS_MAX_MESSAGE_BYTES
        )
        await self.ws.send(json.dumps({"key": self.api_key}))
        resp = json.loads(await self.ws.recv())
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {resp}")
        print(f"Authenticated. Model: {self.model}. Waiting for messages...")
        if intervention_runtime_available():
            print(
                "Intervention runtime: enabled "
                f"(min_severity={INTERVENTION_MIN_SEVERITY}, "
                f"min_steps={INTERVENTION_MIN_TOOL_STEPS}, "
                f"cooldown={INTERVENTION_COOLDOWN_TURNS}, "
                f"max_events={INTERVENTION_MAX_EVENTS})"
            )
        else:
            reason = _INTERVENTION_IMPORT_ERROR or "disabled by configuration"
            print(f"Intervention runtime: disabled ({reason})")

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
                        # Store billing run ID for credit accounting
                        billing_rid = str(msg.get("billing_run_id", "")).strip()
                        if billing_rid:
                            self._session_billing_runs[sid] = billing_rid
                        req_id = ""
                        # Cancel any existing task for this session before starting a new one
                        if sid:
                            old_task = self.active_tasks.get(sid)
                            if old_task and not old_task.done():
                                old_task.cancel()
                                print(f"[{sid}] Auto-cancelled previous task (new message arrived)")
                            req_id = str(msg.get("req_id", "") or "")
                            self.active_req_ids[sid] = req_id
                        token = _task_req_id.set(req_id)
                        try:
                            task = asyncio.create_task(self._handle_message(msg))
                        finally:
                            _task_req_id.reset(token)
                        if sid:
                            self.active_tasks[sid] = task
                            task.add_done_callback(
                                lambda t, s=sid, r=req_id: self._finish_task(s, r, t)
                            )
                    elif msg.get("type") == "cancel":
                        sid = msg.get("session_id", "")
                        task = self.active_tasks.get(sid)
                        if task and not task.done():
                            req_id = self.active_req_ids.get(sid, "")
                            task.cancel()
                            print(f"[{sid}] Cancelled")
                            await self._send(sid, {"type": "cancelled", "req_id": req_id})
                            await self._send(sid, {"type": "done", "req_id": req_id})
            except websockets.ConnectionClosed:
                print("Connection lost. Reconnecting in 3s...")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"Error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def _finish_task(self, session_id: str, req_id: str, task: asyncio.Task):
        """Clear correlation state only when this task still owns the session."""
        if self.active_tasks.get(session_id) is task:
            self.active_tasks.pop(session_id, None)
            getattr(self, "_session_billing_runs", {}).pop(session_id, None)
            if self.active_req_ids.get(session_id) == req_id:
                self.active_req_ids.pop(session_id, None)

    async def _send(self, session_id: str, event: dict):
        event["session_id"] = session_id
        req_id = _task_req_id.get() or getattr(self, "active_req_ids", {}).get(session_id, "")
        event.setdefault("req_id", req_id)
        try:
            await send_agent_event(self.ws, event)
        except Exception as e:
            print(f"Send error: {e}")

    async def _emit_openrouter_usage_event(
        self,
        session_id: str,
        user_id: str,
        model: str,
        payload: dict,
    ):
        if not session_id or not user_id:
            return
        usage = _extract_openrouter_usage(payload)
        await self._send(
            session_id,
            {
                "type": "openrouter_usage",
                "user_id": user_id,
                "model": model,
                **usage,
            },
        )

    async def _handle_message(self, msg: dict):
        session_id = msg["session_id"]
        # agent_id from the message routes to the right user's Chrome
        agent_id = msg.get("agent_id", self.agent_id)
        session_tab_id = msg.get("tab_id")  # per-session tab isolation
        user_id = str(msg.get("user_id", "")).strip()
        user_text = msg["message"]

        # Set per-task scheduler state via ContextVar so concurrent sessions
        # and same-session replacement tasks cannot cross-contaminate grants.
        scheduler_armed = bool(msg.get("scheduler_armed", False))
        scheduler_grant_id = str(msg.get("scheduler_grant_id", "") or "").strip()
        turn_state = SchedulerTurnState(
            armed=scheduler_armed,
            grant_id=scheduler_grant_id,
            session_id=session_id,
        )
        token = _scheduler_turn.set(turn_state)

        # Use model from message if provided (allows front-end model selector)
        model = msg.get("model") or self.model

        print(f"[{session_id}] User ({agent_id}): {user_text[:80]} (model={model})")

        # Start or continue conversation (load from disk if not in memory cache)
        if session_id not in self.sessions:
            self.sessions[session_id] = self._load_session(session_id)
        messages = self.sessions[session_id]
        # Rebuild system prompt with scheduler instructions when armed
        base_system = _build_system_prompt()
        if turn_state.armed and turn_state.grant_id:
            base_system = _build_scheduler_system_prompt(
                base_system, scheduler_armed=True, scheduler_grant_id=turn_state.grant_id,
            )
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": base_system}
        elif not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": base_system})
        # Sanitize malformed messages (e.g. content as dict/None instead of string)
        for msg in messages:
            c = msg.get("content")
            if c is None:
                # Qwen rejects content:null — use reasoning if available, else empty
                msg["content"] = msg.get("reasoning") or ""
            elif not isinstance(c, (str, list)):
                msg["content"] = str(c)
            # Remove non-standard fields that some providers reject
            for key in ("refusal", "reasoning"):
                msg.pop(key, None)
        messages.append({"role": "user", "content": user_text})

        ns = NudgeState()
        reflex = ReflexState()
        reflex.set_user_goal(user_text)

        js_eval_cache: dict[tuple[str, str], dict] = {}

        def _invalidate_js_eval_cache(tab_id: str):
            """Clear cached js_eval outputs after actions that may change page state."""
            if tab_id == "auto":
                js_eval_cache.clear()
                return
            for key in list(js_eval_cache):
                if key[0] in (tab_id, "auto"):
                    js_eval_cache.pop(key, None)

        # web.py passes a dedicated per-session tab_id for all sessions.

        try:
            async with httpx.AsyncClient() as client:
                async def _force_final_response(reason_log: str, fallback: str):
                    print(f"[{session_id}] {reason_log}")
                    try:
                        final_resp = await asyncio.wait_for(
                            self._call_openrouter(
                                client,
                                messages,
                                model,
                                tool_choice="none",
                                session_id=session_id,
                                user_id=user_id,
                            ),
                            timeout=FORCE_FINAL_TIMEOUT,
                        )
                        final_msg = final_resp["choices"][0]["message"]
                        messages.append(final_msg)
                        text = final_msg.get("content") or final_msg.get("reasoning") or ""
                        text = await self._sanitize_user_output(
                            client,
                            model,
                            text,
                            session_id=session_id,
                        )
                    except asyncio.TimeoutError:
                        print(
                            f"[{session_id}] Forced-final model timeout after "
                            f"{FORCE_FINAL_TIMEOUT}s"
                        )
                        text = ""
                    except Exception as e:
                        print(f"[{session_id}] Forced-final model error: {e}")
                        text = ""
                    if not text:
                        text = fallback
                    await self._send(session_id, {"type": "text", "data": text})
                    self._save_session(session_id, messages)
                    await self._send(session_id, {"type": "done"})

                turn_cap = MAX_TURNS  # start at 50

                for turn in range(MAX_ABSOLUTE_TURNS):
                    # --- Dynamic extension check ---
                    if turn >= turn_cap:
                        if ns.should_extend_turns():
                            turn_cap = min(turn + EXTENSION_BLOCK, MAX_ABSOLUTE_TURNS)
                            # Replenish intervention budget for the new window
                            ns.intervention_events = max(0, ns.intervention_events - 1)
                            print(f"[{session_id}] Dynamic extension: cap now {turn_cap} (turn {turn})")
                        else:
                            print(f"[{session_id}] Extension denied at turn {turn} — forcing final")
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
                            print(
                                f"[{session_id}] Hard-stop guard: forcing final response after one recovery turn"
                            )
                        # Skip reasoning on first turn for instant action
                        # (user sees the agent react immediately).
                        first_turn_fast = turn == 0
                        response = await self._call_openrouter(
                            client,
                            messages,
                            model,
                            tool_choice=next_tool_choice,
                            session_id=session_id,
                            user_id=user_id,
                            reasoning=not first_turn_fast,
                        )
                    except httpx.ReadTimeout:
                        print(f"[{session_id}] OpenRouter read timeout on turn {turn+1} — retrying once")
                        response = await self._call_openrouter(
                            client,
                            messages,
                            model,
                            tool_choice=next_tool_choice,
                            session_id=session_id,
                            user_id=user_id,
                        )
                    except httpx.HTTPStatusError as e:
                        provider_message = _openrouter_error_message(e.response)
                        if (
                            e.response.status_code == 400
                            and first_turn_fast
                            and "reasoning is mandatory" in provider_message.lower()
                        ):
                            print(
                                f"[{session_id}] Provider requires reasoning; "
                                "retrying with model defaults"
                            )
                            try:
                                response = await self._call_openrouter(
                                    client,
                                    messages,
                                    model,
                                    tool_choice=next_tool_choice,
                                    session_id=session_id,
                                    user_id=user_id,
                                    reasoning=True,
                                )
                            except httpx.HTTPStatusError as retry_error:
                                raise RuntimeError(
                                    _openrouter_user_error(retry_error, model)
                                ) from retry_error
                        elif e.response.status_code == 400 and len(messages) > TRIM_ON_ERROR + 1:
                            messages = emergency_trim(messages, fmt="openai", keep_tail=TRIM_ON_ERROR)
                            self.sessions[session_id] = messages
                            print(f"[{session_id}] 400 on turn {turn} — emergency trim to {len(messages)} msgs, retrying")
                            try:
                                response = await self._call_openrouter(
                                    client,
                                    messages,
                                    model,
                                    session_id=session_id,
                                    user_id=user_id,
                                )
                            except httpx.HTTPStatusError as retry_error:
                                raise RuntimeError(
                                    _openrouter_user_error(retry_error, model)
                                ) from retry_error
                        else:
                            raise RuntimeError(_openrouter_user_error(e, model)) from e
                    choice = response["choices"][0]
                    message = choice["message"]
                    finish_reason = choice.get("finish_reason", "")
                    tool_calls = message.get("tool_calls") or []

                    messages.append(message)

                    # Loop detection via NudgeState
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
                                print(
                                    f"[{session_id}] Intervention {feedback.severity} "
                                    f"(reasons={','.join(feedback.reason_codes[:3])}) [loop-short-circuit]"
                                )
                                await self._emit_intervention_event(
                                    session_id=session_id,
                                    agent_id=agent_id,
                                    severity=getattr(feedback, "severity", "hard_stop"),
                                    prompt=nudge_text,
                                    messages=messages,
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

                    if loop_detected:
                        continue

                    # No tool calls → final answer
                    if not tool_calls:
                        text = message.get("content") or ""
                        # Some chain-of-thought models (e.g. Solar Pro) put their
                        # thinking in a 'reasoning' field and return content=None.
                        # Fall back to reasoning if content is empty.
                        if not text:
                            text = message.get("reasoning") or ""
                        text = await self._sanitize_user_output(
                            client,
                            model,
                            text,
                            session_id=session_id,
                        )
                        print(f"[{session_id}] Final turn {turn+1}: "
                              f"finish={finish_reason!r} "
                              f"content={repr((message.get('content') or '')[:80])} "
                              f"reasoning_len={len(message.get('reasoning') or '')}")
                        if text:
                            await self._send(session_id, {"type": "text", "data": text})
                        else:
                            # Model returned nothing — warn in logs, show placeholder
                            print(f"[{session_id}] WARNING: empty final content (finish={finish_reason!r})")
                            await self._send(session_id, {"type": "text",
                                "data": "[Agent completed the task but returned no text response. "
                                        "Try asking it to summarize what it found.]"})
                        self._save_session(session_id, messages)
                        await self._send(session_id, {"type": "done"})
                        print(f"[{session_id}] Done ({turn + 1} turns)")
                        return

                    # Execute each tool call
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
                        # Keep arguments canonical when echoed back to OpenRouter.
                        fn["arguments"] = json.dumps(args, separators=(",", ":"))
                        tab_id = str(args.get("tab_id", "auto"))

                        if name in {"navigate", "click", "type_text", "press_enter", "submit_form"}:
                            _invalidate_js_eval_cache(tab_id)

                        if name == "navigate":
                            nav_url = str(args.get("url", "")).strip()
                            # Only count as real navigation if URL actually changed
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

                        # Resolve the effective tab the tool will actually
                        # run on, so the preview can follow tab switches.
                        # Mirrors _execute_tool() resolution logic.
                        args_tab = args.get("tab_id", "")
                        if args_tab and args_tab != "auto" and "://" not in args_tab and "/" not in args_tab:
                            preview_tab = args_tab
                        elif session_tab_id:
                            preview_tab = session_tab_id
                        else:
                            preview_tab = ""
                        flags_str = args.get("flags", "")
                        if name == "ddm" and any(f in flags_str for f in ("--new", "--tabs", "--close")):
                            preview_tab = ""

                        tool_start_evt = {
                            "type": "tool_start",
                            "name": ui_name,
                            "input": ui_input,
                        }
                        if preview_tab:
                            tool_start_evt["tab_id"] = preview_tab
                        await self._send(session_id, tool_start_evt)

                        print(
                            f"[{session_id}] Tool {turn + 1}.{idx + 1} start: "
                            f"{name} args={_truncate(json.dumps(args, sort_keys=True), 200)}"
                        )
                        tool_ms = 0.0
                        cache_hit = False
                        expr = ""
                        cache_key = None
                        if name == "js_eval":
                            expr = str(args.get("expression", "")).strip()
                            if expr:
                                cache_key = (tab_id, expr)
                                cached = js_eval_cache.get(cache_key)
                                if cached:
                                    cache_hit = True
                                    cached["reuse_count"] = int(cached.get("reuse_count", 0)) + 1
                                    cached_output = str(cached.get("result", ""))
                                    if cached["reuse_count"] == 1:
                                        result = (
                                            f"{cached_output}\n\n"
                                            "[cache] Reused identical js_eval result on the same tab. "
                                            "If this isn't enough, switch strategy."
                                        )
                                    else:
                                        result = (
                                            "JS_EVAL_REPEAT_BLOCKED: Same js_eval expression repeated on "
                                            "the same tab. Switch strategy (different selector, "
                                            "ddm --text --find, ddm --at, or direct navigate)."
                                        )

                        if not cache_hit:
                            tool_t0 = time.monotonic()
                            try:
                                result = await asyncio.wait_for(
                                    self._execute_tool(agent_id, name, args, tab_id=session_tab_id),
                                    timeout=TOOL_EXEC_TIMEOUT,
                                )
                            except asyncio.TimeoutError:
                                result = (
                                    "BROWSER_UNAVAILABLE: Tool execution timed out — "
                                    "Chrome connector may be offline or unresponsive."
                                )
                                print(
                                    f"[{session_id}] Tool {turn + 1}.{idx + 1} timeout: "
                                    f"{name} after {TOOL_EXEC_TIMEOUT}s"
                                )
                            tool_ms = (time.monotonic() - tool_t0) * 1000.0

                            if name == "js_eval" and cache_key and isinstance(result, str):
                                if not result.startswith("BROWSER_UNAVAILABLE"):
                                    js_eval_cache[cache_key] = {
                                        "result": result,
                                        "reuse_count": 0,
                                    }
                        if not isinstance(result, str):
                            result = str(result)
                        print(
                            f"[{session_id}] Tool {turn + 1}.{idx + 1} done: "
                            f"{name}{' [cache-hit]' if cache_hit else ''} ({tool_ms:.1f}ms) -> "
                            f"{_truncate(result.replace(chr(10), ' '), 180)}"
                        )
                        turn_step_sigs.append(_tool_progress_sig(name, args, result))
                        if name == "ddm":
                            flags = str(args.get("flags", "")).strip()
                            if flags.startswith("--text --find"):
                                query = flags[len("--text --find"):].strip().lower()
                                if query:
                                    turn_find_queries.append(query)

                        is_screenshot = name == "screenshot" and _is_base64_png_blob(result)
                        show_user = args.get("show_user", False)
                        tool_result_evt = {
                            "type": "tool_result",
                            "name": ui_name,
                            "data": result if is_screenshot else result[:3000],
                            "is_screenshot": is_screenshot,
                            "visible": is_screenshot and bool(show_user),
                        }
                        # Emit structured new_tab_id for ddm --new so clients
                        # don't need to regex-parse the result text.
                        if name == "ddm" and "--new" in str(args.get("flags", "")):
                            _tab_m = re.search(r"^Tab:\s*([A-Fa-f0-9]{8,64})", result, re.MULTILINE)
                            if _tab_m:
                                tool_result_evt["new_tab_id"] = _tab_m.group(1)
                        await self._send(session_id, tool_result_evt)

                        tool_failed = result.startswith("BROWSER_UNAVAILABLE") or result.startswith("Tool error (")
                        # Headless sessions have a live screencast — screenshot
                        # backup opens a competing CDP connection that captures
                        # blank/stale state.  Only use screenshot backup for
                        # non-headless (local Chrome) sessions.
                        is_headless = agent_id.startswith("headless-")
                        if name == "navigate" and not tool_failed and not is_headless:
                            await self._emit_live_preview(
                                session_id,
                                agent_id,
                                tab_id=session_tab_id,
                                note="Page loaded",
                            )
                        elif is_screenshot and bool(show_user):
                            await self._send(
                                session_id,
                                {
                                    "type": "live_preview",
                                    "data": result,
                                    "note": "Screenshot captured",
                                },
                            )

                        ns.live_tool_log.append(
                            {
                                "turn": turn + 1,
                                "tool": name,
                                "args": args,
                                "duration_ms": round(tool_ms, 1),
                                "cache_hit": cache_hit,
                                "output_preview": result[:3000],
                            }
                        )

                        # Truncate for history to keep context manageable
                        tool_call_id = tc.get("id") or f"tc-{turn + 1}-{idx + 1}"
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result[:8000],
                        })

                        # Collect reflex hints per tool call
                        _rh = reflex.on_tool_result(name, args, result)
                        if _rh:
                            reflex_hints.append(_rh)
                            print(f"[{session_id}] {_rh}")

                    messages.extend(tool_results)

                    # Inject reflex hints after all tool results
                    for _rh in reflex_hints:
                        messages.append({"role": "system", "content": _rh})

                    # Update stagnation via NudgeState
                    ns.update_stagnation(
                        turn_step_sigs, turn_find_queries,
                        turn_had_navigation, turn_domain_switch,
                        turn_had_interaction,
                    )

                    # Run progress-based intervention
                    _should_emit, feedback = ns.run_intervention(turn + 1)
                    if _should_emit and feedback:
                        prompt = (feedback.feedback_prompt or "").strip()
                        if prompt:
                            messages.append({"role": "system", "content": prompt})
                            ns.intervention_events += 1
                            ns.last_intervention_model_turn = turn + 1
                            print(
                                f"[{session_id}] Intervention {feedback.severity} "
                                f"(reasons={','.join(feedback.reason_codes[:3])})"
                            )
                            await self._emit_intervention_event(
                                session_id=session_id,
                                agent_id=agent_id,
                                severity=feedback.severity,
                                prompt=prompt,
                                messages=messages,
                                tab_id=session_tab_id,
                            )
                            if feedback.severity == "nudge":
                                prev_stagnation = ns.stagnation_score
                                ns.apply_nudge_decay()
                                print(
                                    f"[{session_id}] Nudge decay: "
                                    f"stall={prev_stagnation}->{ns.stagnation_score}"
                                )
                                ns.apply_nudge_reset()
                                if INTERVENTION_NUDGE_RESET_PROGRESS:
                                    print(
                                        f"[{session_id}] Nudge reset: "
                                        f"live_steps={len(ns.live_tool_log)} (kept), loops={ns.loop_events}"
                                    )
                            if feedback.severity == "hard_stop":
                                ns.hard_stop_guard = True
                                ns.hard_stop_recovery_used = 0

                    # Check stall threshold
                    action, guidance = ns.check_stall_threshold()
                    if action == "guidance":
                        print(
                            f"[{session_id}] Stall strike "
                            f"{ns.stall_force_strikes} "
                            f"(score={ns.stagnation_score}) — continuing with guidance"
                        )
                        messages.append({"role": "system", "content": guidance})
                    elif action == "force":
                        await _force_final_response(
                            f"Progress stalled (score={ns.stagnation_score}) — forcing final response",
                            guidance,
                        )
                        return

                    if ns.hard_stop_guard and tool_calls:
                        ns.hard_stop_recovery_used += 1

            # Absolute ceiling reached
            print(f"[{session_id}] Reached absolute max turns ({MAX_ABSOLUTE_TURNS})")
            await _force_final_response(
                f"Absolute max turns ({MAX_ABSOLUTE_TURNS}) reached",
                "I've reached the maximum number of research turns. Here's what I found.",
            )

        except asyncio.CancelledError:
            print(f"[{session_id}] Task cancelled")
            self._save_session(session_id, messages)
            # Don't send done — the cancel handler or new message handler does that
        except Exception as e:
            import traceback
            print(f"[{session_id}] Error ({type(e).__name__}): {e}")
            traceback.print_exc()
            await self._send(session_id, {"type": "error", "data": str(e) or type(e).__name__})
            self._save_session(session_id, messages)
            await self._send(session_id, {"type": "done"})
        finally:
            # Reset the per-task scheduler context so the next turn on this
            # event-loop task slot starts from a clean slate.  ContextVar
            # isolation means concurrent sessions never see each other's state.
            _scheduler_turn.reset(token)

    async def _call_openrouter(
        self,
        client: httpx.AsyncClient,
        messages: list,
        model: str = "",
        tool_choice: str = "auto",
        session_id: str = "",
        user_id: str = "",
        reasoning: bool = True,
    ) -> dict:
        effective_model = model or self.model
        body: dict = {
            "model": effective_model,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.2,
        }
        if not reasoning and effective_model not in _REASONING_REQUIRED_MODELS:
            body["reasoning"] = {"enabled": False}
        if tool_choice == "none":
            body["tool_choice"] = "none"
        else:
            tools_list = list(TOOLS)
            turn_state = _scheduler_turn.get()
            if turn_state.armed and turn_state.grant_id:
                tools_list = _build_scheduler_openai_tools(
                    turn_state.armed, turn_state.grant_id,
                )
            body["tools"] = tools_list
            body["tool_choice"] = "auto"

        # --- Credit reserve before API call ---
        billing_run_id = self._session_billing_runs.get(session_id, "")
        token = self._credit_service_token()
        credit_client: httpx.AsyncClient | None = None
        reserved_call_id: str | None = None
        credit_reservation: dict | None = None
        provider_submitted = False
        if billing_run_id:
            if not token:
                raise RuntimeError(
                    "Hosted credit authorization is unavailable. Please try again later."
                )
            input_chars = len(json.dumps(messages, ensure_ascii=False, default=str))
            if input_chars > HOSTED_MAX_INPUT_CHARS:
                raise RuntimeError(
                    "Hosted conversation context is too large. Start a new chat or archive this thread."
                )
            credit_client = httpx.AsyncClient()
            idem_key = f"or-call-{_uuid_hex()[:16]}-{int(time.time())}"
            # Retry transient reserve failures up to two times. A 402 also
            # remains fail-closed; the provider request is never sent.
            for reserve_attempt in range(3):
                credit_reservation = await self._credit_reserve(
                    credit_client, billing_run_id,
                    effective_model, idem_key,
                )
                if credit_reservation:
                    reserved_call_id = credit_reservation.get("call_id", "")
                    if reserved_call_id:
                        break
                if reserve_attempt < 2:
                    print(f"[{session_id}] Credit reserve failed, "
                          f"retry {reserve_attempt + 1}/2")
                    await asyncio.sleep(1.0)

            if not credit_reservation or not reserved_call_id:
                # Fail closed: authenticated billing run exists but
                # reserve failed/is insufficient — do NOT proceed.
                if credit_client:
                    try:
                        await credit_client.aclose()
                    except Exception:
                        pass
                raise RuntimeError(
                    "Hosted credit authorization failed. Add credit or try a free model."
                )

        try:
            if reserved_call_id and credit_client:
                submitted = None
                for submit_attempt in range(3):
                    submitted = await self._credit_mark_submitted(
                        credit_client, reserved_call_id
                    )
                    if submitted:
                        break
                    if submit_attempt < 2:
                        await asyncio.sleep(0.5)
                if not submitted:
                    # No provider request has been sent. The shared exception
                    # path below attempts one release; if the submission
                    # callback committed but its response was lost, release is
                    # rejected and the ambiguous hold is captured later.
                    raise RuntimeError(
                        "Hosted credit submission authorization failed. Please try again."
                    )
                provider_submitted = True

            try:
                data = await self._do_openrouter_call(
                    client,
                    body,
                    effective_model,
                    session_id,
                    allow_unmetered_retries=not bool(billing_run_id),
                )
            except httpx.HTTPStatusError as exc:
                status_code = int(getattr(exc.response, "status_code", 0) or 0)
                if (
                    status_code in _DEFINITIVE_UNBILLED_HTTP_STATUSES
                    and reserved_call_id
                    and credit_client
                ):
                    # OpenRouter definitively rejected the request. Preserve the
                    # submitted-call audit trail, but settle at a reported zero
                    # instead of capturing the full safety hold.
                    rejection_settlement = None
                    for settle_attempt in range(3):
                        rejection_settlement = await self._credit_settle(
                            credit_client,
                            reserved_call_id,
                            actual_cost_micro_usd=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                            cost_absent=False,
                            provider_cost_micro_usd=0,
                        )
                        if rejection_settlement:
                            break
                        if settle_attempt < 2:
                            await asyncio.sleep(0.5)
                    if not rejection_settlement:
                        print(
                            f"[{session_id}] Definitive provider rejection "
                            "could not be zero-settled; hold retained"
                        )
                raise
            # --- Credit settle after success ---
            if reserved_call_id and credit_client:
                usage_data = _extract_openrouter_usage(data)
                cost_usd = usage_data.get("cost_usd", 0)
                cost_present = bool(usage_data.get("cost_present", False))
                est_cost_usd = usage_data.get("estimated_cost_usd", 0)

                # Determine actual settlement cost
                if cost_present:
                    actual_cost_micro = max(0, round(cost_usd * 1_000_000))
                    cost_absent = False
                elif est_cost_usd > 0:
                    actual_cost_micro = max(1, round(est_cost_usd * 1_000_000))
                    cost_absent = False
                else:
                    # No cost info at all — let settle_call handle the
                    # conservative fallback (full reservation)
                    actual_cost_micro = 0
                    cost_absent = True

                # Provider-reported cost for reconciliation (may exceed reserve)
                provider_cost_micro = max(0, round(cost_usd * 1_000_000)) if cost_present else 0

                # Retry transient settle failures
                settlement = None
                for settle_attempt in range(3):
                    settlement = await self._credit_settle(
                        credit_client,
                        reserved_call_id,
                        actual_cost_micro_usd=actual_cost_micro,
                        prompt_tokens=usage_data.get("prompt_tokens", 0),
                        completion_tokens=usage_data.get(
                            "completion_tokens", 0,
                        ),
                        total_tokens=usage_data.get("total_tokens", 0),
                        cost_absent=cost_absent,
                        provider_cost_micro_usd=provider_cost_micro,
                    )
                    if settlement:
                        break
                    if settle_attempt < 2:
                        print(f"[{session_id}] Credit settle failed, "
                              f"retry {settle_attempt + 1}/2")
                        await asyncio.sleep(1.0)
                if not settlement:
                    # Leave the hold intact. The control plane captures any
                    # unresolved completed-call hold conservatively at turn end.
                    print(f"[{session_id}] Credit settlement unavailable; hold retained")
        except Exception:
            # A submitted request is never released: OpenRouter may have
            # accepted/billed it before a timeout or cancellation. Only a
            # definitely pre-submit reservation may be returned.
            if reserved_call_id and credit_client and not provider_submitted:
                try:
                    await self._credit_release(credit_client, reserved_call_id)
                except Exception:
                    pass
            raise
        finally:
            if credit_client:
                try:
                    await credit_client.aclose()
                except Exception:
                    pass

        usage = _extract_openrouter_usage(data)
        usage_log = (
            f"model={body.get('model', self.model)} "
            f"prompt_tokens={usage.get('prompt_tokens', 0)} "
            f"completion_tokens={usage.get('completion_tokens', 0)} "
            f"total_tokens={usage.get('total_tokens', 0)} "
            f"cost_usd={float(usage.get('cost_usd', 0.0)):.9f} "
            f"estimated_cost_usd={float(usage.get('estimated_cost_usd', 0.0)):.9f}"
        )
        if session_id:
            print(f"[{session_id}] OpenRouter usage: {usage_log}")
        else:
            print(f"[openrouter] usage: {usage_log}")
        try:
            await self._emit_openrouter_usage_event(
                session_id=session_id,
                user_id=user_id,
                model=body.get("model", self.model),
                payload=data,
            )
        except Exception as e:
            if session_id:
                print(f"[{session_id}] Failed to emit OpenRouter usage event: {e}")
            else:
                print(f"[openrouter] Failed to emit usage event: {e}")
        return data

    async def _do_openrouter_call(
        self,
        client: httpx.AsyncClient,
        body: dict,
        effective_model: str,
        session_id: str = "",
        *,
        allow_unmetered_retries: bool = False,
    ) -> dict:
        _FALLBACK_MODEL = os.environ.get(
            "OPENROUTER_RATE_RAMP_FALLBACK_MODEL",
            self.model,
        ).strip() or self.model
        _RETRY_CODES = (500, 502, 503)

        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://unchainedsky.com",
            "X-Title": "Unchained Trial Agent",
        }
        resp = await client.post(
            OPENROUTER_URL, json=body, headers=headers, timeout=httpx.Timeout(10.0, read=300.0),
        )
        # Local/unbilled CLI requests retain historical retry behavior. Hosted
        # requests never reuse one reservation for multiple provider attempts.
        if (
            allow_unmetered_retries
            and resp.status_code == 429
            and _FALLBACK_MODEL != body.get("model")
        ):
            print(f"[openrouter] 429 → switching {body['model']} → {_FALLBACK_MODEL}")
            body["model"] = _FALLBACK_MODEL
            resp = await client.post(
                OPENROUTER_URL, json=body, headers=headers,
                timeout=httpx.Timeout(10.0, read=300.0),
            )
        # Retry on 500/502/503 (transient server errors)
        if allow_unmetered_retries and resp.status_code in _RETRY_CODES:
            for attempt in range(1, 4):
                delay = 2 * attempt
                print(f"[openrouter] {resp.status_code} — retry {attempt}/3 after {delay}s")
                await asyncio.sleep(delay)
                resp = await client.post(
                    OPENROUTER_URL, json=body, headers=headers, timeout=httpx.Timeout(10.0, read=300.0),
                )
                if resp.status_code not in _RETRY_CODES:
                    break
        if not resp.is_success:
            print(f"[openrouter] {resp.status_code} error: {resp.text[:400]}")
            resp.raise_for_status()
        data = resp.json()
        # Provider error (200 but no "choices") — switch model immediately
        if "choices" not in data:
            err_msg = data.get("error", {})
            if isinstance(err_msg, dict):
                err_msg = err_msg.get("message", str(data)[:200])
            if allow_unmetered_retries and _FALLBACK_MODEL != body.get("model"):
                print(f"[openrouter] Provider error → switching {body['model']} → {_FALLBACK_MODEL}: {str(err_msg)[:120]}")
                body["model"] = _FALLBACK_MODEL
                resp = await client.post(
                    OPENROUTER_URL, json=body, headers=headers, timeout=httpx.Timeout(10.0, read=300.0),
                )
                if resp.is_success:
                    data = resp.json()
            elif allow_unmetered_retries:
                # Already on fallback, retry once after short delay
                print(f"[openrouter] Provider error on fallback: {str(err_msg)[:120]} — retrying in 3s")
                await asyncio.sleep(3)
                resp = await client.post(
                    OPENROUTER_URL, json=body, headers=headers, timeout=httpx.Timeout(10.0, read=300.0),
                )
                if resp.is_success:
                    data = resp.json()
            if "choices" not in data:
                raise RuntimeError(f"OpenRouter provider error: {err_msg}")
        return data

    async def _execute_tool(self, agent_id: str, name: str, args: dict, tab_id: str | None = None) -> str:
        # Model can override session tab by explicitly providing a non-default tab_id
        # Reject URLs/paths that models sometimes hallucinate as tab IDs
        args_tab = args.get("tab_id", "")
        if args_tab and args_tab != "auto" and "://" not in args_tab and "/" not in args_tab:
            effective_tab = args_tab       # Model's explicit choice (real tab ID)
        elif tab_id:
            effective_tab = tab_id         # Session tab from web.py
        else:
            effective_tab = "auto"         # Fallback when no session tab

        # Browser-level DDM ops (--new, --tabs, --close) work on any alive tab
        flags_str = args.get("flags", "")
        if name == "ddm" and any(f in flags_str for f in ("--new", "--tabs", "--close")):
            effective_tab = "auto"

        result = await self._dispatch_tool(agent_id, effective_tab, name, args)

        # If session tab appears dead, retry on 'auto' (first alive tab)
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

            elif name in SCHEDULER_TOOL_NAMES:
                turn_state = _scheduler_turn.get()
                return await execute_scheduler_tool(
                    server_url=self.server,
                    api_key=self._hosted_service_token(),
                    session_id=turn_state.session_id,
                    scheduler_grant_id=turn_state.grant_id,
                    tool_name=name,
                    args=args,
                )

            else:
                return f"Unknown tool: {name}"

        except (asyncio.TimeoutError, TimeoutError):
            return "BROWSER_UNAVAILABLE: Chrome connector timed out — it may not be running."
        except Exception as e:
            err = str(e)
            # Detect relay rejection when Chrome connector isn't connected
            if any(k in err.lower() for k in
                   ("404", "403", "invalid status", "connection refused",
                    "connection closed", "no agent", "not found", "websocket",
                    "timed out", "timeout")):
                return ("BROWSER_UNAVAILABLE: Chrome connector is not running. "
                        "Answer from knowledge — do not retry browser tools.")
            return f"Tool error ({name}): {err}"


# ---------------------------------------------------------------------------
# Local OpenRouter CLI mode (interactive terminal)
# ---------------------------------------------------------------------------


@dataclass
class ToolPermissionPolicy:
    """Simple allow/deny policy for OpenRouter tool calls."""
    allow: set[str] = field(default_factory=set)
    deny: set[str] = field(default_factory=set)

    def decision(self, tool_name: str) -> str:
        """Return allow|deny|ask for a tool."""
        if "*" in self.deny or tool_name in self.deny:
            return "deny"
        if "*" in self.allow or tool_name in self.allow:
            return "allow"
        return "ask"


class LocalOpenRouterCLI:
    """Minimal interactive OpenRouter CLI with actions + context controls."""

    def __init__(
        self,
        agent: TrialAgent,
        model: str,
        session_id: str = LOCAL_SESSION_ID,
        enable_tools: bool = True,
        max_turns: int = MAX_TURNS,
        max_history_messages: int = MAX_SESSION_MESSAGES,
    ):
        self.agent = agent
        self.model = model
        self.session_id = session_id
        self.enable_tools = enable_tools
        self.max_turns = max_turns
        self.max_history_messages = max_history_messages
        self.policy = ToolPermissionPolicy()
        self.messages = self.agent._load_session(session_id)

    def _save_local_session(self):
        """Persist local CLI session using the configured history cap."""
        self.agent._save_session(
            self.session_id,
            self.messages,
            max_messages=self.max_history_messages,
        )

    def _tool_names(self) -> list[str]:
        names: list[str] = []
        for t in TOOLS:
            fn = t.get("function", {})
            name = fn.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return sorted(names)

    async def run(self):
        print(f"OpenRouter Local CLI")
        print(f"Model: {self.model}")
        print(f"Session: {self.session_id}")
        print("Type /help for commands. Type /exit to quit.")
        print()

        async with httpx.AsyncClient() as client:
            while True:
                try:
                    user_text = await asyncio.to_thread(input, "you> ")
                except (EOFError, KeyboardInterrupt):
                    print("\nExiting.")
                    break

                user_text = user_text.strip()
                if not user_text:
                    continue

                if user_text.startswith("/"):
                    should_continue = await self._handle_command(client, user_text)
                    if not should_continue:
                        break
                    continue

                await self._run_turn(client, user_text)

    async def _handle_command(self, client: httpx.AsyncClient, command_line: str) -> bool:
        parts = command_line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/exit", "/quit"):
            return False

        if cmd == "/help":
            print("Commands:")
            print("  /help                 Show this help")
            print("  /exit                 Exit local CLI mode")
            print("  /model [id]           Show or set model")
            print("  /usage                Show message count + rough token estimate")
            print("  /context [n]          Show last n messages (default 8)")
            print("  /compact              Force context compaction")
            print("  /clear                Clear current session history")
            print("  /tools                Show tool names + permission policy")
            print("  /allow <tool|*>       Always allow tool")
            print("  /deny <tool|*>        Always deny tool")
            print("  /unallow <tool|*>     Remove allow rule")
            print("  /undeny <tool|*>      Remove deny rule")
            print("  /tools on|off         Enable or disable tool use")
            return True

        if cmd == "/model":
            if not arg:
                print(f"Current model: {self.model}")
            else:
                self.model = arg
                print(f"Model set to: {self.model}")
            return True

        if cmd == "/usage":
            msg_count = len([m for m in self.messages if m.get("role") != "system"])
            token_est = _estimate_tokens(self.messages)
            print(f"Messages (non-system): {msg_count}")
            print(f"Estimated tokens: ~{token_est}")
            print(f"Compaction threshold: {self.max_history_messages} messages")
            return True

        if cmd == "/context":
            n = 8
            if arg:
                try:
                    n = max(1, int(arg))
                except ValueError:
                    print("Usage: /context [n]")
                    return True
            relevant = self.messages[-n:]
            print(f"Last {len(relevant)} messages:")
            for idx, msg in enumerate(relevant, 1):
                role = msg.get("role", "unknown")
                text = _truncate(_message_content_as_text(msg).replace("\n", " "), 180)
                print(f"  {idx:>2}. {role}: {text}")
            return True

        if cmd == "/compact":
            compacted = await self._compact_history(client, force=True)
            print("Context compacted." if compacted else "Nothing to compact.")
            return True

        if cmd == "/clear":
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            self._save_local_session()
            print("Session history cleared.")
            return True

        if cmd == "/tools":
            lowered = arg.lower()
            if lowered in ("on", "off"):
                self.enable_tools = lowered == "on"
                print(f"Tools {'enabled' if self.enable_tools else 'disabled'}.")
                return True
            print(f"Tools enabled: {self.enable_tools}")
            print(f"Available tools: {', '.join(self._tool_names())}")
            print(f"Allow list: {sorted(self.policy.allow) if self.policy.allow else '[]'}")
            print(f"Deny list: {sorted(self.policy.deny) if self.policy.deny else '[]'}")
            return True

        if cmd == "/allow":
            if not arg:
                print("Usage: /allow <tool_name|*>")
                return True
            self.policy.allow.add(arg)
            self.policy.deny.discard(arg)
            print(f"Always allow: {arg}")
            return True

        if cmd == "/deny":
            if not arg:
                print("Usage: /deny <tool_name|*>")
                return True
            self.policy.deny.add(arg)
            self.policy.allow.discard(arg)
            print(f"Always deny: {arg}")
            return True

        if cmd == "/unallow":
            if not arg:
                print("Usage: /unallow <tool_name|*>")
                return True
            self.policy.allow.discard(arg)
            print(f"Removed allow rule: {arg}")
            return True

        if cmd == "/undeny":
            if not arg:
                print("Usage: /undeny <tool_name|*>")
                return True
            self.policy.deny.discard(arg)
            print(f"Removed deny rule: {arg}")
            return True

        print(f"Unknown command: {cmd}. Use /help.")
        return True

    async def _compact_history(self, client: httpx.AsyncClient, force: bool = False) -> bool:
        non_system = [m for m in self.messages if m.get("role") != "system"]
        if not force and len(non_system) <= self.max_history_messages:
            return False
        if len(non_system) <= LOCAL_CONTEXT_KEEP_TAIL + 2:
            return False

        # Tier 1: deterministic tool-result compaction (free, instant)
        self.messages, cstats = compact_messages(self.messages, fmt="openai")
        if cstats["compacted"]:
            print(f"[context] Tier 1: compacted {cstats['compacted']} stale tool results "
                  f"({cstats['tokens_before']}→{cstats['tokens_after']} est tokens)")

        # Re-check after tier 1 — if token estimate dropped enough, skip Tier 2.
        # Tier 1 only shrinks content (never removes messages), so we check tokens.
        if not force and cstats["compacted"] and cstats["tokens_after"] < cstats["tokens_before"] * 0.6:
            self._save_local_session()
            print("[context] Tier 1 sufficient — skipping Tier 2 model summary")
            return True

        # Tier 2: model-generated summary of older messages
        older = non_system[:-LOCAL_CONTEXT_KEEP_TAIL]
        tail = non_system[-LOCAL_CONTEXT_KEEP_TAIL:]

        transcript_lines = []
        for m in older[-80:]:
            role = m.get("role", "unknown")
            text = _truncate(_message_content_as_text(m).replace("\n", " "), 500)
            transcript_lines.append(f"{role}: {text}")
        transcript = "\n".join(transcript_lines)

        summary_prompt = [
            {
                "role": "system",
                "content": (
                    "Summarize prior conversation for continuation in a coding/browser agent. "
                    "Output concise bullets with: user goals, pages visited, facts discovered, "
                    "open TODOs, and constraints. Keep under 180 words."
                ),
            },
            {"role": "user", "content": transcript or "No prior messages."},
        ]

        summary = ""
        try:
            summary_resp = await self.agent._call_openrouter(
                client, summary_prompt, self.model, tool_choice="none"
            )
            summary_msg = summary_resp["choices"][0]["message"]
            summary = (summary_msg.get("content") or summary_msg.get("reasoning") or "").strip()
        except Exception as e:
            print(f"[context] Tier 2 skipped: summary request failed: {e}")
            return bool(cstats["compacted"])

        if not summary:
            print("[context] Tier 2 skipped: empty summary from model.")
            return bool(cstats["compacted"])

        # Keep summary as assistant content so it persists in existing session format.
        summary_entry = {
            "role": "assistant",
            "content": f"[Context summary]\n{summary}",
        }

        base_system = self.messages[0] if self.messages else {"role": "system", "content": SYSTEM_PROMPT}
        self.messages = [base_system, summary_entry] + tail
        self._save_local_session()
        return True

    async def _confirm_tool_call(self, tool_name: str, args: dict) -> bool:
        preview = _truncate(json.dumps(args, ensure_ascii=False), LOCAL_TOOL_PREVIEW_CHARS)
        prompt = (
            f"[permission] Allow tool '{tool_name}' args={preview}? "
            "[y]es/[n]o/[a]lways allow/[d]eny always: "
        )
        choice = (await asyncio.to_thread(input, prompt)).strip().lower()
        if choice == "a":
            self.policy.allow.add(tool_name)
            self.policy.deny.discard(tool_name)
            return True
        if choice == "d":
            self.policy.deny.add(tool_name)
            self.policy.allow.discard(tool_name)
            return False
        if choice in ("y", "yes"):
            return True
        return False

    async def _execute_tool_with_policy(self, tool_name: str, args: dict) -> str:
        if not self.enable_tools:
            return f"Tool blocked: tools are disabled (/tools on to enable). Requested: {tool_name}"

        decision = self.policy.decision(tool_name)
        if decision == "deny":
            return f"Tool blocked by deny policy: {tool_name}"
        if decision == "ask":
            allowed = await self._confirm_tool_call(tool_name, args)
            if not allowed:
                return f"Tool call denied by user: {tool_name}"

        return await self.agent._execute_tool(self.agent.agent_id, tool_name, args)

    async def _run_turn(self, client: httpx.AsyncClient, user_text: str):
        self.messages.append({"role": "user", "content": user_text})

        ns = NudgeState()
        reflex = ReflexState()
        reflex.set_user_goal(user_text)

        turn_cap = self.max_turns  # default 50, or --max-turns override
        absolute_cap = max(turn_cap, MAX_ABSOLUTE_TURNS)  # respect explicit override

        for turn in range(absolute_cap):
            # --- Dynamic extension check ---
            if turn >= turn_cap:
                if ns.should_extend_turns():
                    turn_cap = min(turn + EXTENSION_BLOCK, absolute_cap)
                    # Replenish intervention budget for the new window
                    ns.intervention_events = max(0, ns.intervention_events - 1)
                    print(f"[dynamic-cap] Extended to {turn_cap} (turn {turn})")
                else:
                    print(f"[dynamic-cap] Extension denied at turn {turn} — forcing final response")
                    break

            try:
                if await self._compact_history(client):
                    print("[context] Auto-compacted old history.")

                tool_choice = "auto" if self.enable_tools else "none"
                if ns.hard_stop_guard and ns.hard_stop_recovery_used >= 1:
                    tool_choice = "none"
                    print("[intervention] Hard-stop guard: forcing final response.")
                response = await self.agent._call_openrouter(
                    client, self.messages, self.model, tool_choice=tool_choice
                )
            except httpx.HTTPStatusError as e:
                print(f"[openrouter] HTTP {e.response.status_code}: {_truncate(e.response.text, 300)}")
                self._save_local_session()
                return
            except Exception as e:
                print(f"[openrouter] Request failed: {e}")
                self._save_local_session()
                return

            choice = response["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "")
            tool_calls = message.get("tool_calls") or []
            self.messages.append(message)

            if not tool_calls:
                text = message.get("content") or message.get("reasoning") or ""
                text = await self.agent._sanitize_user_output(
                    client,
                    self.model,
                    text,
                    session_id=self.session_id,
                )
                if not text:
                    text = f"[empty response, finish_reason={finish_reason}]"
                print(f"\nassistant> {text}\n")
                self._save_local_session()
                return

            sig = json.dumps(
                [
                    {
                        "name": tc.get("function", {}).get("name"),
                        "args": tc.get("function", {}).get("arguments"),
                    }
                    for tc in tool_calls
                ],
                sort_keys=True,
            )
            loop_detected, nudge_text, feedback = ns.check_loop(sig)

            if loop_detected:
                print("[loop] Repeated identical tool call. Forcing final text response.")
                if feedback and getattr(feedback, "should_intervene", False):
                    print(
                        f"[intervention] {feedback.severity} "
                        f"reasons={','.join(feedback.reason_codes[:3])} [loop-short-circuit]"
                    )
                    print(f"[intervention-prompt]\n{nudge_text}\n")
                for tc in tool_calls:
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", "loop"),
                            "content": nudge_text,
                        }
                    )
                final_text = ""
                try:
                    final_resp = await asyncio.wait_for(
                        self.agent._call_openrouter(
                            client, self.messages, self.model, tool_choice="none"
                        ),
                        timeout=FORCE_FINAL_TIMEOUT,
                    )
                    final_msg = final_resp["choices"][0]["message"]
                    final_text = final_msg.get("content") or final_msg.get("reasoning") or ""
                    final_text = await self.agent._sanitize_user_output(
                        client,
                        self.model,
                        final_text,
                        session_id=self.session_id,
                    )
                    self.messages.append(final_msg)
                except asyncio.TimeoutError:
                    print(f"[loop] Final response request timed out after {FORCE_FINAL_TIMEOUT}s")
                except Exception as e:
                    print(f"[loop] Final response request failed: {e}")
                if not final_text:
                    final_text = "I got stuck and could not complete the task."
                print(f"\nassistant> {final_text}\n")
                self._save_local_session()
                return

            tool_results = []
            reflex_hints: list[str] = []
            for idx, tc in enumerate(tool_calls):
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = _decode_tool_arguments(fn.get("arguments"))
                fn["arguments"] = json.dumps(tool_args, separators=(",", ":"))

                print(f"[tool] {tool_name}({_truncate(json.dumps(tool_args), 140)})")
                result = await self._execute_tool_with_policy(tool_name, tool_args)
                if not isinstance(result, str):
                    result = str(result)
                print(f"[tool-result] {_truncate(result.replace(chr(10), ' '), 220)}")

                ns.live_tool_log.append(
                    {
                        "turn": turn + 1,
                        "tool": tool_name,
                        "args": tool_args,
                        "output_preview": result[:3000],
                    }
                )

                tool_call_id = tc.get("id") or f"tc-{turn + 1}-{idx + 1}"
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result[:8000],
                    }
                )

                # Collect reflex hints per tool call
                _rh = reflex.on_tool_result(tool_name, tool_args, result)
                if _rh:
                    reflex_hints.append(_rh)
                    print(f"[reflex] {_rh}")

            self.messages.extend(tool_results)

            # Inject reflex hints after all tool results
            for _rh in reflex_hints:
                self.messages.append({"role": "system", "content": _rh})

            # Run progress-based intervention via NudgeState
            _should_emit, feedback = ns.run_intervention(turn + 1)
            if _should_emit and feedback:
                prompt = (feedback.feedback_prompt or "").strip()
                if prompt:
                    self.messages.append({"role": "system", "content": prompt})
                    ns.intervention_events += 1
                    ns.last_intervention_model_turn = turn + 1
                    print(
                        f"[intervention] {feedback.severity} "
                        f"reasons={','.join(feedback.reason_codes[:3])}"
                    )
                    print(f"[intervention-prompt]\n{prompt}\n")
                    if feedback.severity == "nudge":
                        ns.apply_nudge_decay()
                        ns.apply_nudge_reset()
                        if INTERVENTION_NUDGE_RESET_PROGRESS:
                            print(
                                f"[intervention] Nudge reset: "
                                f"live_steps={len(ns.live_tool_log)} (kept), loops={ns.loop_events}"
                            )
                    if feedback.severity == "hard_stop":
                        ns.hard_stop_guard = True
                        ns.hard_stop_recovery_used = 0

            if ns.hard_stop_guard and tool_calls:
                ns.hard_stop_recovery_used += 1

        print("[agent] Reached max turns for this user message.")
        self._save_local_session()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Unchained Trial Agent (OpenRouter)")
    parser.add_argument("--key", default=os.environ.get("UNCHAINED_API_KEY", ""),
                        help="Unchained API key (default: UNCHAINED_API_KEY)")
    parser.add_argument("--agent", help="Default agent ID (a-12345678)")
    parser.add_argument("--server",
                        default=os.environ.get("UNCHAINED_SERVER", DEFAULT_SERVER),
                        help=f"WebSocket server URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--model",
                        default=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
                        help=f"OpenRouter model ID (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--local-cli",
        action="store_true",
        help="Run local interactive OpenRouter CLI mode (no websocket bridge).",
    )
    parser.add_argument(
        "--session-id",
        default=LOCAL_SESSION_ID,
        help=f"Session ID for local CLI history (default: {LOCAL_SESSION_ID})",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="Disable tool execution in local CLI mode.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=MAX_TURNS,
        help=f"Max model/tool turns per user message (default: {MAX_TURNS})",
    )
    parser.add_argument(
        "--max-history-messages",
        type=int,
        default=MAX_SESSION_MESSAGES,
        help=f"Trigger local compaction after this many non-system messages (default: {MAX_SESSION_MESSAGES})",
    )
    args = parser.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY env var not set.", file=sys.stderr)
        sys.exit(1)

    if args.local_cli:
        local_agent = TrialAgent(
            api_key=args.key or "",
            agent_id=args.agent or os.environ.get("CDP_AGENT_ID", "trial-local"),
            server=args.server,
            model=args.model,
        )
        local_cli = LocalOpenRouterCLI(
            agent=local_agent,
            model=args.model,
            session_id=args.session_id,
            enable_tools=not args.no_tools,
            max_turns=max(1, args.max_turns),
            max_history_messages=max(8, args.max_history_messages),
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.stop())
        try:
            loop.run_until_complete(local_cli.run())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()
        return

    if not args.key or not args.agent:
        parser.error("--agent and either --key or UNCHAINED_API_KEY are required unless --local-cli is used.")

    agent = TrialAgent(
        api_key=args.key,
        agent_id=args.agent,
        server=args.server,
        model=args.model,
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
        print("\nTrial agent stopped.")


if __name__ == "__main__":
    main()
