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
    recalculate_next_run,
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
      "use_stable_session": true,
      "profile_path": "/tmp/chrome/Profile 2"
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
            self.assertEqual(fake.calls[0].get("profile_path"), "/tmp/chrome/Profile 2")

    def test_parse_payload_and_preview_due(self):
        payload = {
            "jobs": [
                {
                    "id": "a",
                    "prompt": "p",
                    "schedule": {"every_seconds": 60},
                    "enabled": True,
                    "profile_path": "/tmp/chrome/Profile 1",
                }
            ]
        }
        jobs = parse_jobs_payload(payload)
        canonical = jobs_to_payload(jobs)
        self.assertEqual(canonical["jobs"][0]["id"], "a")
        self.assertEqual(canonical["jobs"][0]["schedule"], {"every_seconds": 60})
        self.assertEqual(canonical["jobs"][0]["profile_path"], "/tmp/chrome/Profile 1")

        state = {"a": JobState(next_run_at=_utc(2026, 2, 25, 12, 0, 0))}
        rows = preview_jobs(jobs, state=state, now=_utc(2026, 2, 25, 12, 0, 1))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_due"])
        self.assertEqual(rows[0]["id"], "a")

    def test_parse_rejects_headless_profile_path_combo(self):
        payload = {
            "jobs": [
                {
                    "id": "bad",
                    "prompt": "p",
                    "schedule": {"every_seconds": 60},
                    "headless": True,
                    "profile_path": "/tmp/chrome/Profile 9",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "profile_path is not supported in headless mode"):
            parse_jobs_payload(payload)

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


    def test_recalculate_next_run_daily_schedule_change(self):
        """When daily_at changes, next_run_at should be reset to the new time."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "d1", "prompt": "p", "schedule": {"daily_at": "12:00"}},
            ]
        })
        state = {"d1": JobState(next_run_at=_utc(2026, 3, 14, 14, 0, 0))}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["d1"].next_run_at.hour, 12)
        self.assertEqual(state["d1"].next_run_at.minute, 0)

    def test_recalculate_next_run_no_change_when_schedule_matches(self):
        """If the schedule matches the existing next_run_at, no change."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "d1", "prompt": "p", "schedule": {"daily_at": "14:00"}},
            ]
        })
        original = _utc(2026, 3, 14, 14, 0, 0)
        state = {"d1": JobState(next_run_at=original)}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["d1"].next_run_at, original)

    def test_recalculate_next_run_skips_disabled_jobs(self):
        """Disabled jobs should not be recalculated."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "d1", "prompt": "p", "schedule": {"daily_at": "12:00"}, "enabled": False},
            ]
        })
        original = _utc(2026, 3, 14, 14, 0, 0)
        state = {"d1": JobState(next_run_at=original)}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["d1"].next_run_at, original)

    def test_recalculate_next_run_once_job_timestamp_change(self):
        """When a once-job's 'at' changes and it hasn't run yet, reset."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "o1", "prompt": "p", "schedule": {"at": "2026-03-20T18:00:00Z"}},
            ]
        })
        state = {"o1": JobState(next_run_at=_utc(2026, 3, 15, 12, 0, 0), run_count=0)}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["o1"].next_run_at, _utc(2026, 3, 20, 18, 0, 0))

    def test_recalculate_next_run_once_job_already_ran(self):
        """Once-job that already ran should not be reset."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "o1", "prompt": "p", "schedule": {"at": "2026-03-20T18:00:00Z"}},
            ]
        })
        original = _utc(2026, 3, 15, 12, 0, 0)
        state = {"o1": JobState(next_run_at=original, run_count=1)}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["o1"].next_run_at, original)

    def test_recalculate_interval_no_reset_on_prompt_edit(self):
        """Editing prompt on an interval job should not reset the countdown."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "i1", "prompt": "new prompt", "schedule": {"every_seconds": 3600}},
            ]
        })
        # Ran 50 minutes ago, next_run in 10 minutes (3600s interval)
        last = _utc(2026, 3, 13, 9, 10, 0)
        nxt = _utc(2026, 3, 13, 10, 10, 0)  # last + 3600s
        state = {"i1": JobState(next_run_at=nxt, last_run_at=last, run_count=5)}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        # Should NOT have been reset
        self.assertEqual(state["i1"].next_run_at, nxt)

    def test_recalculate_interval_resets_when_interval_changes(self):
        """Changing every_seconds should reset next_run_at."""
        # Old interval was 3600s, new interval is 1800s
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "i1", "prompt": "p", "schedule": {"every_seconds": 1800}},
            ]
        })
        last = _utc(2026, 3, 13, 9, 10, 0)
        nxt = _utc(2026, 3, 13, 10, 10, 0)  # last + 3600s (old interval)
        state = {"i1": JobState(next_run_at=nxt, last_run_at=last, run_count=5)}
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        # Should be reset to now + 1800s
        self.assertEqual(state["i1"].next_run_at, _utc(2026, 3, 13, 10, 30, 0))

    def test_recalculate_interval_skips_never_ran_job(self):
        """Interval job that never ran should not be touched."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "i1", "prompt": "p", "schedule": {"every_seconds": 3600}},
            ]
        })
        original = _utc(2026, 3, 13, 11, 0, 0)
        state = {"i1": JobState(next_run_at=original)}  # last_run_at=None
        now = _utc(2026, 3, 13, 10, 0, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["i1"].next_run_at, original)

    def test_recalculate_interval_preserves_retry_countdown(self):
        """Saving while a retry is pending should not reset next_run_at."""
        jobs = parse_jobs_payload({
            "jobs": [
                {"id": "i1", "prompt": "p", "schedule": {"every_seconds": 3600},
                 "retry_seconds": 120},
            ]
        })
        last = _utc(2026, 3, 13, 9, 58, 0)
        nxt = _utc(2026, 3, 13, 10, 0, 0)  # last + 120s (retry)
        state = {"i1": JobState(
            next_run_at=nxt, last_run_at=last, last_status="error", run_count=3,
        )}
        now = _utc(2026, 3, 13, 9, 59, 0)

        recalculate_next_run(jobs, state, now)

        self.assertEqual(state["i1"].next_run_at, nxt)


if __name__ == "__main__":
    unittest.main()
