"""scheduler_tool.py — manage scheduler jobs for an armed /schedule chat turn.

This CLI is intended for the local chat agent. It talks to dedicated scheduler
API endpoints and requires a turn-scoped scheduler grant minted by /web/chat.

Usage:
    uv run python scheduler_tool.py list
    uv run python scheduler_tool.py preview --id daily-brief --prompt "..." --daily-at 09:00
    uv run python scheduler_tool.py upsert --id daily-brief --prompt "..." --daily-at 09:00
    uv run python scheduler_tool.py delete --id daily-brief
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


API_KEY = os.environ.get("UNCHAINED_API_KEY", "").strip()
API_URL = (os.environ.get("UNCHAINED_API_URL", "https://api.unchainedsky.com").strip() or "https://api.unchainedsky.com").rstrip("/")
CHAT_SESSION_ID = os.environ.get("UNCHAINED_CHAT_SESSION_ID", "").strip()
SCHEDULER_GRANT_ID = os.environ.get("UNCHAINED_SCHEDULER_GRANT_ID", "").strip()


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _require_turn_grant() -> None:
    if not CHAT_SESSION_ID or not SCHEDULER_GRANT_ID:
        _die("Scheduler tool unavailable for this turn. Ask the user to start the message with /schedule.", code=2)


def _post(path: str, payload: dict) -> dict:
    body = {
        "session_id": CHAT_SESSION_ID,
        "scheduler_grant_id": SCHEDULER_GRANT_ID,
        **payload,
    }
    req = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("error", raw)
        except Exception:
            msg = raw or str(exc)
        _die(str(msg))
    except (urllib.error.URLError, TimeoutError):
        _die("Cannot reach the server. Check your internet connection.")


def _add_job_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="job_id", required=True, help="Stable task id")
    parser.add_argument("--prompt", required=True, help="Prompt that the scheduler will send to the agent")
    schedule = parser.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--every-seconds", type=int, help="Run every N seconds")
    schedule.add_argument("--every-minutes", type=int, help="Run every N minutes")
    schedule.add_argument("--daily-at", help="Run daily at HH:MM in UTC")
    schedule.add_argument("--at", help="Run once at ISO-8601 UTC timestamp")
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument("--enabled", dest="enabled", action="store_true", help="Store the job enabled")
    enabled.add_argument("--disabled", dest="enabled", action="store_false", help="Store the job disabled")
    parser.set_defaults(enabled=None)
    parser.add_argument("--model", default="", help="Optional model override")
    parser.add_argument("--scheduled-session-id", default="", help="Optional fixed scheduler session id")
    parser.add_argument("--use-stable-session", action="store_true", help="Reuse a stable scheduler session for this job")
    parser.add_argument("--headless", action="store_true", help="Run on the headless agent if configured")
    parser.add_argument("--profile-path", default="", help="Optional profile path for non-headless jobs")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="Run timeout in seconds")
    parser.add_argument("--retry-seconds", type=int, default=None, help="Delay before retry after failure")


def _job_from_args(args: argparse.Namespace) -> dict:
    schedule: dict[str, object]
    if args.every_seconds is not None:
        schedule = {"every_seconds": args.every_seconds}
    elif args.every_minutes is not None:
        schedule = {"every_minutes": args.every_minutes}
    elif args.daily_at:
        schedule = {"daily_at": args.daily_at}
    else:
        schedule = {"at": args.at}

    job = {
        "id": args.job_id,
        "prompt": args.prompt,
        "schedule": schedule,
    }
    if args.enabled is not None:
        job["enabled"] = bool(args.enabled)
    if args.model:
        job["model"] = args.model
    if args.scheduled_session_id:
        job["session_id"] = args.scheduled_session_id
    if args.use_stable_session:
        job["use_stable_session"] = True
    if args.headless:
        job["headless"] = True
    if args.profile_path:
        job["profile_path"] = args.profile_path
    if args.timeout_seconds is not None:
        job["timeout_seconds"] = args.timeout_seconds
    if args.retry_seconds is not None:
        job["retry_seconds"] = args.retry_seconds
    return job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage scheduler jobs for the current /schedule turn")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List scheduler jobs")

    preview = sub.add_parser("preview", help="Preview a candidate job without saving it")
    _add_job_args(preview)

    upsert = sub.add_parser("upsert", help="Create or replace one scheduler job")
    _add_job_args(upsert)

    delete = sub.add_parser("delete", help="Delete one scheduler job")
    delete.add_argument("--id", dest="job_id", required=True, help="Task id to delete")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _require_turn_grant()

    if args.command == "list":
        result = _post("/web/scheduler/agent/list", {})
    elif args.command == "preview":
        result = _post("/web/scheduler/agent/preview", {"job": _job_from_args(args)})
    elif args.command == "upsert":
        result = _post("/web/scheduler/agent/upsert", {"job": _job_from_args(args)})
    elif args.command == "delete":
        result = _post("/web/scheduler/agent/delete", {"job_id": args.job_id})
    else:
        _die(f"Unknown command: {args.command}")
        return

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
