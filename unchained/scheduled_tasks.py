"""scheduled_tasks.py — local scheduler for pre-scheduled agent prompts.

This module is intentionally self-contained:
- Job definitions live in a local JSON file.
- Run state (next run, last status) lives in a local JSON file.
- Due jobs trigger prompts via POST /web/chat using the user's API key.

Schedule formats:
  {"every_seconds": 3600}
  {"every_minutes": 30}
  {"daily_at": "09:15"}            # UTC
  {"at": "2026-03-01T18:00:00Z"}   # one-time (UTC if no offset)
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request


DEFAULT_JOBS_PATH = Path("scheduled_jobs.json")
DEFAULT_STATE_PATH = Path("scheduled_jobs.state.json")
DEFAULT_API_URL = os.environ.get("UNCHAINED_API_URL", "https://api.unchainedsky.com")
DEFAULT_API_KEY = os.environ.get("UNCHAINED_API_KEY", "")
DEFAULT_DB_PATH = Path(os.environ.get("UNCHAINED_DB_PATH", os.path.expanduser("~/.unchained/auth.db")))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"daily_at must be HH:MM, got: {value!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"daily_at out of range: {value!r}")
    return hour, minute


def _agent_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:8]


def _scheduler_slug(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:24]


def _default_session_id(api_key: str, job_id: str) -> str:
    """Generate a stable per-job session id that passes server ownership checks."""
    key_hash = _agent_key_hash(api_key)
    suffix = hashlib.sha256(job_id.encode()).hexdigest()[:8]
    return f"s-claude-{key_hash}-{suffix}"


@dataclass(frozen=True)
class JobSchedule:
    kind: str  # interval | daily | once
    every_seconds: int | None = None
    hour: int | None = None
    minute: int | None = None
    at: datetime | None = None


@dataclass(frozen=True)
class ScheduledJob:
    id: str
    prompt: str
    schedule: JobSchedule
    enabled: bool = True
    model: str = ""
    session_id: str = ""
    use_stable_session: bool = False
    headless: bool = False
    profile_path: str = ""
    timeout_seconds: int = 180
    retry_seconds: int = 0


@dataclass
class JobState:
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str = ""
    last_error: str = ""
    run_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        return cls(
            next_run_at=_parse_datetime(data["next_run_at"]) if data.get("next_run_at") else None,
            last_run_at=_parse_datetime(data["last_run_at"]) if data.get("last_run_at") else None,
            last_status=str(data.get("last_status", "")),
            last_error=str(data.get("last_error", "")),
            run_count=int(data.get("run_count", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_run_at": _isoformat_utc(self.next_run_at) if self.next_run_at else None,
            "last_run_at": _isoformat_utc(self.last_run_at) if self.last_run_at else None,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }


@dataclass(frozen=True)
class TriggerResult:
    ok: bool
    text: str = ""
    error: str = ""


@dataclass(frozen=True)
class RunOutcome:
    job_id: str
    ok: bool
    detail: str


def _parse_schedule(raw: dict[str, Any]) -> JobSchedule:
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")

    keys = [k for k in ("every_seconds", "every_minutes", "daily_at", "at") if k in raw]
    if len(keys) != 1:
        raise ValueError("schedule must define exactly one of every_seconds/every_minutes/daily_at/at")
    key = keys[0]

    if key == "every_seconds":
        every = int(raw["every_seconds"])
        if every <= 0:
            raise ValueError("every_seconds must be > 0")
        return JobSchedule(kind="interval", every_seconds=every)

    if key == "every_minutes":
        every_minutes = int(raw["every_minutes"])
        if every_minutes <= 0:
            raise ValueError("every_minutes must be > 0")
        return JobSchedule(kind="interval", every_seconds=every_minutes * 60)

    if key == "daily_at":
        hour, minute = _parse_hhmm(str(raw["daily_at"]))
        return JobSchedule(kind="daily", hour=hour, minute=minute)

    at = _parse_datetime(str(raw["at"]))
    return JobSchedule(kind="once", at=at)


def parse_jobs_payload(payload: dict[str, Any]) -> list[ScheduledJob]:
    """Parse and validate scheduler payload {jobs:[...]} into ScheduledJob list."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    items = payload.get("jobs")
    if not isinstance(items, list):
        raise ValueError("jobs file must contain a top-level 'jobs' array")

    jobs: list[ScheduledJob] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each job must be an object")
        job_id = str(item.get("id", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        if not job_id:
            raise ValueError("job.id is required")
        if not prompt:
            raise ValueError(f"job {job_id!r} missing prompt")
        if job_id in seen:
            raise ValueError(f"duplicate job id: {job_id}")
        seen.add(job_id)

        schedule = _parse_schedule(item.get("schedule", {}))
        timeout_seconds = int(item.get("timeout_seconds", 180))
        if timeout_seconds <= 0:
            raise ValueError(f"job {job_id!r} timeout_seconds must be > 0")
        retry_seconds = int(item.get("retry_seconds", item.get("retry_after_seconds", 0)))
        if retry_seconds < 0:
            raise ValueError(f"job {job_id!r} retry_seconds must be >= 0")
        headless = bool(item.get("headless", False))
        profile_path = str(item.get("profile_path", "")).strip()
        if headless and profile_path:
            raise ValueError(f"job {job_id!r} profile_path is not supported in headless mode")

        jobs.append(
            ScheduledJob(
                id=job_id,
                prompt=prompt,
                schedule=schedule,
                enabled=bool(item.get("enabled", True)),
                model=str(item.get("model", "")),
                session_id=str(item.get("session_id", "")),
                use_stable_session=bool(item.get("use_stable_session", item.get("keep_session", False))),
                headless=headless,
                profile_path=profile_path,
                timeout_seconds=timeout_seconds,
                retry_seconds=retry_seconds,
            )
        )
    return jobs


def jobs_to_payload(jobs: list[ScheduledJob]) -> dict[str, Any]:
    """Serialize ScheduledJob list to canonical JSON payload."""
    out: list[dict[str, Any]] = []
    for job in jobs:
        schedule: dict[str, Any]
        if job.schedule.kind == "interval":
            schedule = {"every_seconds": int(job.schedule.every_seconds or 0)}
        elif job.schedule.kind == "daily":
            schedule = {"daily_at": f"{int(job.schedule.hour):02d}:{int(job.schedule.minute):02d}"}
        else:
            schedule = {"at": _isoformat_utc(job.schedule.at)}

        item: dict[str, Any] = {
            "id": job.id,
            "prompt": job.prompt,
            "schedule": schedule,
            "enabled": job.enabled,
            "timeout_seconds": job.timeout_seconds,
            "retry_seconds": job.retry_seconds,
        }
        if job.model:
            item["model"] = job.model
        if job.session_id:
            item["session_id"] = job.session_id
        if job.use_stable_session:
            item["use_stable_session"] = True
        if job.headless:
            item["headless"] = True
        if job.profile_path:
            item["profile_path"] = job.profile_path
        out.append(item)
    return {"jobs": out}


def preview_jobs(
    jobs: list[ScheduledJob],
    state: dict[str, JobState] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Compute preview rows with next run and last run metadata."""
    state = state or {}
    now = now or _utcnow()
    engine = SchedulerEngine(jobs, state)
    engine.initialize_missing(now)
    due_ids = {j.id for j in engine.due_jobs(now)}
    rows: list[dict[str, Any]] = []
    for job in jobs:
        js = engine.state.get(job.id, JobState())
        rows.append(
            {
                "id": job.id,
                "enabled": job.enabled,
                "schedule_kind": job.schedule.kind,
                "next_run_at": _isoformat_utc(js.next_run_at) if js.next_run_at else None,
                "last_run_at": _isoformat_utc(js.last_run_at) if js.last_run_at else None,
                "last_status": js.last_status,
                "run_count": js.run_count,
                "is_due": job.id in due_ids,
            }
        )
    return rows


def load_jobs(path: Path) -> list[ScheduledJob]:
    if not path.exists():
        raise FileNotFoundError(f"jobs file not found: {path}")
    raw = json.loads(path.read_text())
    return parse_jobs_payload(raw)


def load_state(path: Path) -> dict[str, JobState]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    jobs_raw = raw.get("jobs", {})
    if not isinstance(jobs_raw, dict):
        return {}
    return {job_id: JobState.from_dict(data) for job_id, data in jobs_raw.items() if isinstance(data, dict)}


def save_state(path: Path, state: dict[str, JobState]) -> None:
    payload = {"jobs": {job_id: s.to_dict() for job_id, s in state.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent)) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def _history_path(state_path: Path, job_id: str) -> Path:
    stem = state_path.name
    if stem.endswith(".state.json"):
        stem = stem[: -len(".state.json")]
    else:
        stem = state_path.stem
    job_token = hashlib.sha256(job_id.encode()).hexdigest()[:16]
    return state_path.parent / f"{stem}.history.{job_token}.jsonl"


def append_run_record(
    state_path: Path,
    job_id: str,
    ok: bool,
    detail: str,
    ran_at: datetime,
    *,
    max_records: int = 50,
) -> None:
    history_path = _history_path(state_path, job_id)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _isoformat_utc(ran_at),
        "ok": bool(ok),
        "detail": str(detail or ""),
        "len": len(str(detail or "")),
    }
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    if max_records <= 0:
        return

    lines = deque(maxlen=max_records)
    with history_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                lines.append(line)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(history_path.parent), encoding="utf-8") as tmp:
        for line in lines:
            tmp.write(line)
        tmp_name = tmp.name
    os.replace(tmp_name, history_path)


def load_run_history(state_path: Path, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
    history_path = _history_path(state_path, job_id)
    if limit <= 0 or not history_path.exists():
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    with history_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                rows.append(record)
    return list(reversed(list(rows)))


def latest_success_output(state_path: Path, job_id: str, limit: int = 50) -> str:
    for record in load_run_history(state_path, job_id, limit=limit):
        if record.get("ok"):
            detail = str(record.get("detail", "") or "").strip()
            if detail:
                return detail
    return ""


def _state_path_for_jobs(jobs_path: Path) -> Path:
    name = jobs_path.name
    if name.endswith(".jobs.json"):
        return jobs_path.with_name(f"{name[:-len('.jobs.json')]}.state.json")
    return jobs_path.with_name(f"{jobs_path.stem}.state.json")


def _load_api_keys_by_scheduler_slug(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    keys: dict[str, str] = {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT user_id, api_key FROM users WHERE api_key IS NOT NULL AND api_key != ''"
        ).fetchall()
    for user_id, api_key in rows:
        if not user_id or not api_key:
            continue
        keys[_scheduler_slug(str(user_id))] = str(api_key)
    return keys


def run_due_jobs_multi_once(
    *,
    jobs_dir: Path,
    db_path: Path,
    api_url: str,
    now: datetime | None = None,
    sleep_between_jobs: float = 1.0,
) -> list[tuple[Path, list[RunOutcome]]]:
    now = now or _utcnow()
    api_keys = _load_api_keys_by_scheduler_slug(db_path)
    all_outcomes: list[tuple[Path, list[RunOutcome]]] = []
    for jobs_path in sorted(jobs_dir.glob("*.jobs.json")):
        slug = jobs_path.name[: -len(".jobs.json")]
        api_key = api_keys.get(slug, "")
        if not api_key:
            print(f"[{_isoformat_utc(_utcnow())}] scheduler skip {jobs_path.name}: no API key for slug")
            continue
        state_path = _state_path_for_jobs(jobs_path)
        try:
            client = ChatTriggerClient(api_url, api_key)
            outcomes = run_due_jobs_once(
                jobs_path=jobs_path,
                state_path=state_path,
                trigger_client=client,
                now=now,
                sleep_between_jobs=sleep_between_jobs,
                api_key_for_sessions=api_key,
            )
        except Exception as exc:
            print(f"[{_isoformat_utc(_utcnow())}] scheduler skip {jobs_path.name}: {exc}")
            continue
        if outcomes:
            all_outcomes.append((jobs_path, outcomes))
    return all_outcomes


class SchedulerEngine:
    def __init__(self, jobs: list[ScheduledJob], state: dict[str, JobState]):
        self.jobs = jobs
        self.state = state

    def initialize_missing(self, now: datetime) -> None:
        for job in self.jobs:
            js = self.state.setdefault(job.id, JobState())
            if js.next_run_at is None and job.enabled:
                # One-time jobs should not be reinitialized after they already ran.
                if job.schedule.kind == "once" and js.run_count > 0:
                    continue
                js.next_run_at = self._initial_next_run(job, now)

    def due_jobs(self, now: datetime) -> list[ScheduledJob]:
        due: list[ScheduledJob] = []
        for job in self.jobs:
            if not job.enabled:
                continue
            js = self.state.get(job.id)
            if js and js.next_run_at and js.next_run_at <= now:
                due.append(job)
        return due

    def mark_run(self, job: ScheduledJob, now: datetime, ok: bool, error: str = "") -> None:
        js = self.state.setdefault(job.id, JobState())
        js.last_run_at = now
        js.last_status = "success" if ok else "error"
        js.last_error = "" if ok else error
        js.run_count += 1

        if not ok and job.retry_seconds > 0:
            js.next_run_at = now + timedelta(seconds=job.retry_seconds)
            return

        js.next_run_at = self._next_after(job, now)

    @staticmethod
    def _initial_next_run(job: ScheduledJob, now: datetime) -> datetime | None:
        # One-time schedules should preserve their configured timestamp so
        # overdue jobs run immediately on next scheduler tick.
        if job.schedule.kind == "once":
            return job.schedule.at
        return SchedulerEngine._next_after(job, now)

    @staticmethod
    def _next_after(job: ScheduledJob, now: datetime) -> datetime | None:
        s = job.schedule
        if s.kind == "interval":
            return now + timedelta(seconds=int(s.every_seconds or 0))
        if s.kind == "daily":
            candidate = now.replace(hour=int(s.hour), minute=int(s.minute), second=0, microsecond=0)
            if candidate <= now:
                candidate = candidate + timedelta(days=1)
            return candidate
        # One-time job: no future run after execution.
        return None


def recalculate_next_run(
    jobs: list[ScheduledJob],
    state: dict[str, JobState],
    now: datetime,
) -> None:
    """Detect schedule changes and reset next_run_at for affected jobs."""
    for job in jobs:
        js = state.get(job.id)
        if js is None or js.next_run_at is None:
            continue  # initialize_missing handles these
        if not job.enabled:
            continue

        if job.schedule.kind == "daily":
            if (js.next_run_at.hour != job.schedule.hour
                    or js.next_run_at.minute != job.schedule.minute):
                js.next_run_at = SchedulerEngine._initial_next_run(job, now)

        elif job.schedule.kind == "interval":
            # Use last_run_at to detect whether every_seconds actually changed.
            # If the job never ran we can't tell, so skip (harmless — the
            # existing next_run_at from initialize_missing is fine).
            if js.last_run_at is not None:
                elapsed = (js.next_run_at - js.last_run_at).total_seconds()
                interval = job.schedule.every_seconds or 0
                if abs(elapsed - interval) < 1:
                    continue  # matches current interval
                if job.retry_seconds > 0 and abs(elapsed - job.retry_seconds) < 1:
                    continue  # pending retry
                js.next_run_at = SchedulerEngine._initial_next_run(job, now)

        elif job.schedule.kind == "once" and js.run_count == 0:
            if js.next_run_at != job.schedule.at:
                js.next_run_at = job.schedule.at


class ChatTriggerClient:
    """Minimal /web/chat client using SSE response parsing."""

    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def trigger(
        self,
        *,
        prompt: str,
        model: str = "",
        session_id: str = "",
        headless: bool = False,
        profile_path: str = "",
        timeout_seconds: int = 180,
    ) -> TriggerResult:
        body: dict[str, Any] = {"message": prompt, "allow_scheduler_trigger": False}
        if model:
            body["model"] = model
        if session_id:
            body["session_id"] = session_id
        if headless:
            body["headless"] = True
        if profile_path:
            body["profile_path"] = profile_path

        req = urllib.request.Request(
            f"{self.api_url}/web/chat",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                chunks: list[str] = []
                done = False
                while True:
                    raw_line = resp.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload_raw = line[5:].strip()
                    try:
                        evt = json.loads(payload_raw)
                    except json.JSONDecodeError:
                        continue
                    evt_type = evt.get("type", "")
                    if evt_type == "text":
                        text = str(evt.get("data", ""))
                        if text:
                            chunks.append(text)
                    elif evt_type == "error":
                        return TriggerResult(ok=False, error=str(evt.get("data", "unknown error")))
                    elif evt_type == "done":
                        done = True
                        break

                if not done and not chunks:
                    return TriggerResult(ok=False, error="scheduler received no stream output from /web/chat")
                return TriggerResult(ok=True, text="\n".join(chunks).strip())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
                msg = str(data.get("error", raw))
            except Exception:
                msg = raw or str(e)
            return TriggerResult(ok=False, error=f"HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            return TriggerResult(ok=False, error=f"request failed: {e.reason}")


def run_due_jobs_once(
    *,
    jobs_path: Path,
    state_path: Path,
    trigger_client: ChatTriggerClient,
    now: datetime | None = None,
    sleep_between_jobs: float = 1.0,
    api_key_for_sessions: str = "",
) -> list[RunOutcome]:
    now = now or _utcnow()
    jobs = load_jobs(jobs_path)
    state = load_state(state_path)
    engine = SchedulerEngine(jobs, state)
    engine.initialize_missing(now)
    due = engine.due_jobs(now)

    outcomes: list[RunOutcome] = []
    for idx, job in enumerate(due):
        run_now = now if now is not None else _utcnow()
        session_id = job.session_id
        if not session_id and job.use_stable_session and api_key_for_sessions:
            session_id = _default_session_id(api_key_for_sessions, job.id)
        result = trigger_client.trigger(
            prompt=job.prompt,
            model=job.model,
            session_id=session_id,
            headless=job.headless,
            profile_path=job.profile_path,
            timeout_seconds=job.timeout_seconds,
        )
        engine.mark_run(job, run_now, result.ok, result.error)
        detail = result.text if result.ok else result.error
        try:
            append_run_record(state_path, job.id, result.ok, detail, run_now)
        except Exception as history_error:
            detail = f"{detail}\n[history write failed: {history_error}]".strip()
        outcomes.append(RunOutcome(job_id=job.id, ok=result.ok, detail=detail))

        # Serial execution reduces browser contention and anti-bot risk.
        if idx < len(due) - 1 and sleep_between_jobs > 0:
            time.sleep(sleep_between_jobs)

    save_state(state_path, engine.state)
    return outcomes


def _write_example_jobs(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    example = {
        "jobs": [
            {
                "id": "daily-summary",
                "prompt": "Open my dashboard and summarize anything important since yesterday.",
                "schedule": {"daily_at": "14:00"},
                "use_stable_session": True,
                "timeout_seconds": 240,
            },
            {
                "id": "quick-health-check",
                "prompt": "Check if the status page shows incidents and report back.",
                "schedule": {"every_minutes": 30},
                "retry_seconds": 120,
            },
            {
                "id": "one-time-export",
                "prompt": "Export this week's report as CSV and confirm where it was downloaded.",
                "schedule": {"at": "2026-03-01T18:00:00Z"},
                "enabled": False,
            },
        ]
    }
    path.write_text(json.dumps(example, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local scheduler for pre-scheduled Unchained prompts")
    parser.add_argument("--jobs", default=str(DEFAULT_JOBS_PATH), help="Path to scheduled jobs JSON")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Path to scheduler state JSON")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Server base URL (default from UNCHAINED_API_URL)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key (default from UNCHAINED_API_KEY)")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-example", help="Write an example jobs file")
    sub.add_parser("list", help="Print jobs and next run times")

    run_due = sub.add_parser("run-due", help="Run all jobs currently due")
    run_due.add_argument("--sleep-between-jobs", type=float, default=1.0)

    daemon = sub.add_parser("daemon", help="Poll and run due jobs continuously")
    daemon.add_argument("--poll-seconds", type=float, default=30.0)
    daemon.add_argument("--sleep-between-jobs", type=float, default=1.0)

    daemon_multi = sub.add_parser("daemon-multi", help="Multi-user daemon for server deployment")
    daemon_multi.add_argument("--jobs-dir", required=True, help="Directory with *.jobs.json files")
    daemon_multi.add_argument("--poll-seconds", type=float, default=30.0)
    daemon_multi.add_argument("--sleep-between-jobs", type=float, default=1.0)
    daemon_multi.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to auth.db for per-user API keys")

    args = parser.parse_args()
    jobs_path = Path(args.jobs)
    state_path = Path(args.state)

    if args.command == "init-example":
        _write_example_jobs(jobs_path)
        print(f"Wrote example jobs file: {jobs_path}")
        return

    if args.command == "list":
        jobs = load_jobs(jobs_path)
        state = load_state(state_path)
        now = _utcnow()
        engine = SchedulerEngine(jobs, state)
        engine.initialize_missing(now)
        save_state(state_path, engine.state)
        print(f"{'job_id':24s} {'enabled':7s} {'next_run_utc':24s} {'last_status':10s} {'runs':5s}")
        for job in jobs:
            js = engine.state.get(job.id, JobState())
            next_run = _isoformat_utc(js.next_run_at) if js.next_run_at else "-"
            status = js.last_status or "-"
            print(f"{job.id:24s} {str(job.enabled):7s} {next_run:24s} {status:10s} {js.run_count:5d}")
        return

    if args.command != "daemon-multi" and not args.api_key:
        raise SystemExit("API key missing. Set UNCHAINED_API_KEY or pass --api-key.")

    client = ChatTriggerClient(args.api_url, args.api_key) if args.command != "daemon-multi" else None

    if args.command == "run-due":
        outcomes = run_due_jobs_once(
            jobs_path=jobs_path,
            state_path=state_path,
            trigger_client=client,
            sleep_between_jobs=float(args.sleep_between_jobs),
            api_key_for_sessions=args.api_key,
        )
        if not outcomes:
            print("No jobs due.")
            return
        for out in outcomes:
            status = "OK" if out.ok else "ERR"
            print(f"[{status}] {out.job_id}: {out.detail[:240]}")
        return

    if args.command == "daemon-multi":
        poll_seconds = float(args.poll_seconds)
        if poll_seconds <= 0:
            raise SystemExit("--poll-seconds must be > 0")
        jobs_dir = Path(args.jobs_dir)
        db_path = Path(args.db_path)
        if not jobs_dir.exists():
            raise SystemExit(f"jobs dir not found: {jobs_dir}")
        if not db_path.exists():
            raise SystemExit(f"auth db not found: {db_path}")
        print(
            "Scheduler multi-user daemon started. "
            f"jobs_dir={jobs_dir} db={db_path} poll={poll_seconds}s"
        )
        while True:
            try:
                batches = run_due_jobs_multi_once(
                    jobs_dir=jobs_dir,
                    db_path=db_path,
                    api_url=args.api_url,
                    sleep_between_jobs=float(args.sleep_between_jobs),
                )
                for jobs_path, outcomes in batches:
                    for out in outcomes:
                        status = "OK" if out.ok else "ERR"
                        print(
                            f"[{_isoformat_utc(_utcnow())}] "
                            f"[{jobs_path.name}] [{status}] {out.job_id}: {out.detail[:240]}"
                        )
            except Exception as e:
                print(f"[{_isoformat_utc(_utcnow())}] scheduler tick error: {e}")
            time.sleep(poll_seconds)

    # daemon mode
    poll_seconds = float(args.poll_seconds)
    if poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be > 0")
    print(f"Scheduler daemon started. jobs={jobs_path} state={state_path} poll={poll_seconds}s")
    while True:
        try:
            outcomes = run_due_jobs_once(
                jobs_path=jobs_path,
                state_path=state_path,
                trigger_client=client,
                sleep_between_jobs=float(args.sleep_between_jobs),
                api_key_for_sessions=args.api_key,
            )
            for out in outcomes:
                status = "OK" if out.ok else "ERR"
                print(f"[{_isoformat_utc(_utcnow())}] [{status}] {out.job_id}: {out.detail[:240]}")
        except Exception as e:
            print(f"[{_isoformat_utc(_utcnow())}] scheduler tick error: {e}")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
