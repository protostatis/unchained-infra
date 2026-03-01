"""Unit tests for scheduled_tasks.py."""

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

# Ensure unchained/ is on the path
sys.path.insert(0, os.path.dirname(__file__))

from scheduled_tasks import (  # noqa: E402
    append_run_record,
    ChatTriggerClient,
    JobState,
    SchedulerEngine,
    TriggerResult,
    jobs_to_payload,
    latest_success_output,
    load_jobs,
    load_run_history,
    load_state,
    parse_jobs_payload,
    preview_jobs,
    run_due_jobs_once,
    run_due_jobs_multi_once,
    save_state,
    _scheduler_slug,
)


def _utc(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


class _FakeTriggerClient(ChatTriggerClient):
    def __init__(self, results: list[TriggerResult]):
        self._results = list(results)
        self.calls: list[dict] = []

    def trigger(self, **kwargs):
        self.calls.append(kwargs)
        if not self._results:
            return TriggerResult(ok=True, text="ok")
        return self._results.pop(0)


class TestScheduledTasks(unittest.TestCase):
    def test_interval_and_daily_schedule_initialization(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.json"
            jobs_path.write_text(
                """{
  "jobs": [
    {"id": "j1", "prompt": "p1", "schedule": {"every_minutes": 15}},
    {"id": "j2", "prompt": "p2", "schedule": {"daily_at": "09:30"}}
  ]
}
"""
            )
            jobs = load_jobs(jobs_path)
            state = {}
            now = _utc(2026, 2, 25, 9, 0, 0)
            engine = SchedulerEngine(jobs, state)
            engine.initialize_missing(now)

            self.assertEqual(state["j1"].next_run_at, _utc(2026, 2, 25, 9, 15, 0))
            self.assertEqual(state["j2"].next_run_at, _utc(2026, 2, 25, 9, 30, 0))

            due_at_915 = [j.id for j in engine.due_jobs(_utc(2026, 2, 25, 9, 15, 0))]
            self.assertIn("j1", due_at_915)
            self.assertNotIn("j2", due_at_915)

    def test_once_job_runs_once_and_disables_future_runs(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.json"
            state_path = Path(td) / "state.json"
            jobs_path.write_text(
                """{
  "jobs": [
    {"id": "once", "prompt": "p", "schedule": {"at": "2026-02-25T09:00:00Z"}}
  ]
}
"""
            )
            fake = _FakeTriggerClient([TriggerResult(ok=True, text="done")])
            outcomes = run_due_jobs_once(
                jobs_path=jobs_path,
                state_path=state_path,
                trigger_client=fake,
                now=_utc(2026, 2, 25, 9, 0, 1),
            )
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(outcomes[0].ok)

            state = load_state(state_path)
            self.assertIsNotNone(state.get("once"))
            self.assertIsNone(state["once"].next_run_at)
            self.assertEqual(state["once"].run_count, 1)

            # Second run has no due jobs.
            outcomes2 = run_due_jobs_once(
                jobs_path=jobs_path,
                state_path=state_path,
                trigger_client=_FakeTriggerClient([]),
                now=_utc(2026, 2, 25, 9, 5, 0),
            )
            self.assertEqual(outcomes2, [])

    def test_failure_uses_retry_seconds(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.json"
            state_path = Path(td) / "state.json"
            jobs_path.write_text(
                """{
  "jobs": [
    {
      "id": "retrying",
      "prompt": "p",
      "schedule": {"every_seconds": 60},
      "retry_seconds": 15
    }
  ]
}
"""
            )
            save_state(
                state_path,
                {
                    "retrying": JobState(
                        next_run_at=_utc(2026, 2, 25, 10, 0, 0),
                    )
                },
            )
            fake = _FakeTriggerClient([TriggerResult(ok=False, error="agent offline")])
            now = _utc(2026, 2, 25, 10, 0, 2)
            outcomes = run_due_jobs_once(
                jobs_path=jobs_path,
                state_path=state_path,
                trigger_client=fake,
                now=now,
            )
            self.assertEqual(len(outcomes), 1)
            self.assertFalse(outcomes[0].ok)

            state = load_state(state_path)
            self.assertEqual(state["retrying"].last_status, "error")
            self.assertEqual(state["retrying"].next_run_at, _utc(2026, 2, 25, 10, 0, 17))

    def test_stable_session_id_generation(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.json"
            state_path = Path(td) / "state.json"
            jobs_path.write_text(
                """{
  "jobs": [
    {
      "id": "stable",
      "prompt": "p",
      "schedule": {"at": "2026-02-25T11:00:00Z"},
      "use_stable_session": true
    }
  ]
}
"""
            )
            fake = _FakeTriggerClient([TriggerResult(ok=True, text="ok")])
            run_due_jobs_once(
                jobs_path=jobs_path,
                state_path=state_path,
                trigger_client=fake,
                now=_utc(2026, 2, 25, 11, 0, 1),
                api_key_for_sessions="uc_live_abc123",
            )
            self.assertEqual(len(fake.calls), 1)
            session_id = fake.calls[0].get("session_id", "")
            self.assertTrue(session_id.startswith("s-claude-"))
            self.assertEqual(len(session_id.split("-")), 4)

    def test_parse_payload_and_preview_due(self):
        payload = {
            "jobs": [
                {
                    "id": "a",
                    "prompt": "p",
                    "schedule": {"every_seconds": 60},
                    "enabled": True,
                }
            ]
        }
        jobs = parse_jobs_payload(payload)
        canonical = jobs_to_payload(jobs)
        self.assertEqual(canonical["jobs"][0]["id"], "a")
        self.assertEqual(canonical["jobs"][0]["schedule"], {"every_seconds": 60})

        state = {"a": JobState(next_run_at=_utc(2026, 2, 25, 12, 0, 0))}
        rows = preview_jobs(jobs, state=state, now=_utc(2026, 2, 25, 12, 0, 1))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_due"])
        self.assertEqual(rows[0]["id"], "a")

    def test_history_append_and_tail_read(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "sched.state.json"
            append_run_record(state_path, "job-a", True, "first", _utc(2026, 2, 25, 12, 0, 0), max_records=3)
            append_run_record(state_path, "job-a", False, "second", _utc(2026, 2, 25, 12, 1, 0), max_records=3)
            append_run_record(state_path, "job-a", True, "third", _utc(2026, 2, 25, 12, 2, 0), max_records=3)
            append_run_record(state_path, "job-a", True, "fourth", _utc(2026, 2, 25, 12, 3, 0), max_records=3)

            rows = load_run_history(state_path, "job-a", limit=10)
            self.assertEqual([row["detail"] for row in rows], ["fourth", "third", "second"])
            self.assertEqual(latest_success_output(state_path, "job-a"), "fourth")

    def test_run_due_jobs_multi_once_uses_per_user_api_key(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_dir = Path(td) / "scheduler_jobs"
            jobs_dir.mkdir()
            db_path = Path(td) / "auth.db"

            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, api_key TEXT)")
                conn.execute(
                    "INSERT INTO users (user_id, api_key) VALUES (?, ?)",
                    ("u-user1", "uc_live_user1"),
                )

            slug = _scheduler_slug("u-user1")
            jobs_path = jobs_dir / f"{slug}.jobs.json"
            jobs_path.write_text(
                """{
  "jobs": [
    {"id": "job1", "prompt": "p1", "schedule": {"at": "2026-02-25T09:00:00Z"}}
  ]
}
"""
            )

            calls: list[tuple[str, str]] = []

            class FakeClient:
                def __init__(self, api_url: str, api_key: str):
                    self.api_url = api_url
                    self.api_key = api_key

                def trigger(self, **kwargs):
                    calls.append((self.api_key, kwargs.get("prompt", "")))
                    return TriggerResult(ok=True, text="ok")

            with patch("scheduled_tasks.ChatTriggerClient", FakeClient):
                batches = run_due_jobs_multi_once(
                    jobs_dir=jobs_dir,
                    db_path=db_path,
                    api_url="http://web:8080",
                    now=_utc(2026, 2, 25, 9, 0, 1),
                    sleep_between_jobs=0.0,
                )

            self.assertEqual(len(batches), 1)
            self.assertEqual(calls, [("uc_live_user1", "p1")])
            state = load_state(jobs_dir / f"{slug}.state.json")
            self.assertEqual(state["job1"].last_status, "success")
            history = load_run_history(jobs_dir / f"{slug}.state.json", "job1", limit=5)
            self.assertEqual(history[0]["detail"], "ok")


if __name__ == "__main__":
    unittest.main()
