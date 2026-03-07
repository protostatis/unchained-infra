"""Shared scheduler tool helpers for bridge-backed chat agents."""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_API_URL = "https://api.unchainedsky.com"

SCHEDULER_LIST_TOOL = "scheduler_list_jobs"
SCHEDULER_PREVIEW_TOOL = "scheduler_preview_job"
SCHEDULER_SAVE_TOOL = "scheduler_save_job"
SCHEDULER_DELETE_TOOL = "scheduler_delete_job"
SCHEDULER_TOOL_NAMES = frozenset(
    {
        SCHEDULER_LIST_TOOL,
        SCHEDULER_PREVIEW_TOOL,
        SCHEDULER_SAVE_TOOL,
        SCHEDULER_DELETE_TOOL,
    }
)

SCHEDULER_TOOL_PROMPT = """## Scheduler Access

The scheduler tools are available only because the user's current message started with `/schedule`.
They read and write the exact same scheduled task list shown in `/scheduler`.

Rules:
- Use `scheduler_list_jobs` before editing or deleting an existing job.
- Ask for missing schedule details before creating a new job.
- Confirm before saving or deleting a job.
- Use `scheduler_preview_job` before `scheduler_save_job` when the next run time is important.
- Save self-contained prompts. Never rely on chat context like "do this again".
- Default `use_stable_session` to false unless the user explicitly wants the task to reuse a stable session.
"""

_SCHEDULER_JOB_PROPERTIES = {
    "job_id": {
        "type": "string",
        "description": "Stable scheduler job id. Required for preview, save, and delete.",
    },
    "prompt": {
        "type": "string",
        "description": "Self-contained prompt the scheduled task will send to the agent.",
    },
    "every_seconds": {
        "type": "integer",
        "description": "Run every N seconds. Mutually exclusive with every_minutes, daily_at, and at.",
    },
    "every_minutes": {
        "type": "integer",
        "description": "Run every N minutes. Mutually exclusive with every_seconds, daily_at, and at.",
    },
    "daily_at": {
        "type": "string",
        "description": "Run daily at HH:MM in UTC. Mutually exclusive with the other schedule fields.",
    },
    "at": {
        "type": "string",
        "description": "Run once at an ISO-8601 UTC timestamp. Mutually exclusive with the other schedule fields.",
    },
    "enabled": {
        "type": "boolean",
        "description": "Optional enabled state.",
    },
    "model": {
        "type": "string",
        "description": "Optional model override.",
    },
    "scheduled_session_id": {
        "type": "string",
        "description": "Optional fixed scheduler session id.",
    },
    "use_stable_session": {
        "type": "boolean",
        "description": "Optional stable-session flag. Defaults to false unless the user asks for reuse.",
    },
    "headless": {
        "type": "boolean",
        "description": "Run on the headless agent if configured.",
    },
    "profile_path": {
        "type": "string",
        "description": "Optional Chrome profile path for non-headless runs.",
    },
    "timeout_seconds": {
        "type": "integer",
        "description": "Optional run timeout in seconds.",
    },
    "retry_seconds": {
        "type": "integer",
        "description": "Optional retry delay in seconds after failure.",
    },
}

OPENAI_SCHEDULER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": SCHEDULER_LIST_TOOL,
            "description": (
                "List the current scheduler jobs from the same task list shown in /scheduler."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SCHEDULER_PREVIEW_TOOL,
            "description": (
                "Preview one scheduler job without saving it. Use this to inspect the normalized job and next run time."
            ),
            "parameters": {
                "type": "object",
                "properties": _SCHEDULER_JOB_PROPERTIES,
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SCHEDULER_SAVE_TOOL,
            "description": (
                "Create or update one scheduler job in the shared /scheduler task list."
            ),
            "parameters": {
                "type": "object",
                "properties": _SCHEDULER_JOB_PROPERTIES,
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SCHEDULER_DELETE_TOOL,
            "description": "Delete one scheduler job by id from the shared /scheduler task list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": _SCHEDULER_JOB_PROPERTIES["job_id"],
                },
                "required": ["job_id"],
            },
        },
    },
]

ANTHROPIC_SCHEDULER_TOOLS = [
    {
        "name": SCHEDULER_LIST_TOOL,
        "description": "List the current scheduler jobs from the same task list shown in /scheduler.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": SCHEDULER_PREVIEW_TOOL,
        "description": "Preview one scheduler job without saving it.",
        "input_schema": {
            "type": "object",
            "properties": _SCHEDULER_JOB_PROPERTIES,
            "required": ["job_id"],
        },
    },
    {
        "name": SCHEDULER_SAVE_TOOL,
        "description": "Create or update one scheduler job in the shared /scheduler task list.",
        "input_schema": {
            "type": "object",
            "properties": _SCHEDULER_JOB_PROPERTIES,
            "required": ["job_id"],
        },
    },
    {
        "name": SCHEDULER_DELETE_TOOL,
        "description": "Delete one scheduler job by id from the shared /scheduler task list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": _SCHEDULER_JOB_PROPERTIES["job_id"],
            },
            "required": ["job_id"],
        },
    },
]


def scheduler_tools_enabled(*, scheduler_armed: bool, scheduler_grant_id: str = "") -> bool:
    return bool(scheduler_armed and str(scheduler_grant_id or "").strip())


def build_system_prompt(
    base_prompt: str,
    *,
    scheduler_armed: bool,
    scheduler_grant_id: str = "",
) -> str:
    if not scheduler_tools_enabled(
        scheduler_armed=scheduler_armed,
        scheduler_grant_id=scheduler_grant_id,
    ):
        return base_prompt
    return f"{base_prompt}\n\n{SCHEDULER_TOOL_PROMPT}"


def build_openai_tools(
    base_tools: list[dict],
    *,
    scheduler_armed: bool,
    scheduler_grant_id: str = "",
) -> list[dict]:
    tools = list(base_tools)
    if scheduler_tools_enabled(
        scheduler_armed=scheduler_armed,
        scheduler_grant_id=scheduler_grant_id,
    ):
        tools.extend(OPENAI_SCHEDULER_TOOLS)
    return tools


def build_anthropic_tools(
    base_tools: list[dict],
    *,
    scheduler_armed: bool,
    scheduler_grant_id: str = "",
) -> list[dict]:
    tools = list(base_tools)
    if scheduler_tools_enabled(
        scheduler_armed=scheduler_armed,
        scheduler_grant_id=scheduler_grant_id,
    ):
        tools.extend(ANTHROPIC_SCHEDULER_TOOLS)
    return tools


def _api_url_from_server(server_url: str) -> str:
    raw = str(server_url or "").strip()
    if not raw:
        return DEFAULT_API_URL
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    parts = urlsplit(raw)
    scheme = parts.scheme
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    elif not scheme:
        scheme = "https"
    path = parts.path
    if path.endswith("/chat/ws"):
        path = path[: -len("/chat/ws")]
    return urlunsplit((scheme, parts.netloc, path.rstrip("/"), "", ""))


def _scheduler_job_from_args(args: dict) -> dict:
    job_id = str(args.get("job_id", "") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")

    job: dict[str, object] = {"id": job_id}

    if "prompt" in args and args.get("prompt") is not None:
        job["prompt"] = str(args.get("prompt") or "")

    schedule_parts: list[tuple[str, object]] = []
    for key in ("every_seconds", "every_minutes"):
        if key in args and args.get(key) is not None:
            schedule_parts.append((key, int(args[key])))
    for key in ("daily_at", "at"):
        if key in args and args.get(key) is not None:
            value = str(args.get(key) or "").strip()
            if value:
                schedule_parts.append((key, value))
    if len(schedule_parts) > 1:
        raise ValueError(
            "Provide only one schedule field: every_seconds, every_minutes, daily_at, or at"
        )
    if schedule_parts:
        schedule_key, schedule_value = schedule_parts[0]
        job["schedule"] = {schedule_key: schedule_value}

    for key in ("enabled", "use_stable_session", "headless"):
        if key in args and args.get(key) is not None:
            job[key] = bool(args.get(key))

    for source_key, target_key in (
        ("model", "model"),
        ("profile_path", "profile_path"),
        ("scheduled_session_id", "session_id"),
        ("session_id", "session_id"),
    ):
        if source_key in args and args.get(source_key) is not None:
            job[target_key] = str(args.get(source_key) or "")

    for key in ("timeout_seconds", "retry_seconds"):
        if key in args and args.get(key) is not None:
            job[key] = int(args[key])

    return job


async def execute_scheduler_tool(
    *,
    server_url: str,
    api_key: str,
    session_id: str,
    scheduler_grant_id: str,
    tool_name: str,
    args: dict,
    client: httpx.AsyncClient | None = None,
) -> str:
    if tool_name not in SCHEDULER_TOOL_NAMES:
        return f"Unknown tool: {tool_name}"
    if not session_id or not scheduler_grant_id:
        return (
            "Scheduler tool unavailable for this turn. Ask the user to start the message with /schedule."
        )

    try:
        if tool_name == SCHEDULER_LIST_TOOL:
            path = "/web/scheduler/agent/list"
            payload = {}
        elif tool_name == SCHEDULER_PREVIEW_TOOL:
            path = "/web/scheduler/agent/preview"
            payload = {"job": _scheduler_job_from_args(args)}
        elif tool_name == SCHEDULER_SAVE_TOOL:
            path = "/web/scheduler/agent/upsert"
            payload = {"job": _scheduler_job_from_args(args)}
        else:
            job_id = str(args.get("job_id", "") or "").strip()
            if not job_id:
                raise ValueError("job_id is required")
            path = "/web/scheduler/agent/delete"
            payload = {"job_id": job_id}
    except ValueError as exc:
        return f"Scheduler error ({tool_name}): {exc}"

    body = {
        "session_id": session_id,
        "scheduler_grant_id": scheduler_grant_id,
        **payload,
    }
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    close_client = client is None
    http_client = client or httpx.AsyncClient()
    try:
        resp = await http_client.post(
            f"{_api_url_from_server(server_url)}{path}",
            json=body,
            headers=headers,
            timeout=120.0,
        )
        raw_text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            data = None

        if not resp.is_success:
            if isinstance(data, dict):
                err = str(data.get("error") or data.get("message") or raw_text or resp.reason_phrase)
            else:
                err = raw_text[:400] or resp.reason_phrase
            return f"Scheduler error ({tool_name}): {err}"

        if isinstance(data, (dict, list)):
            return json.dumps(data, indent=2, sort_keys=True)
        return raw_text or "{}"
    except httpx.RequestError:
        return "Scheduler error: Cannot reach the server. Check your internet connection."
    finally:
        if close_client:
            await http_client.aclose()
