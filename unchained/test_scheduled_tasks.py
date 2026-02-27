"""Unit tests for scheduled_tasks.py."""

import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
import unittest

# Ensure unchained/ is on the path
sys.path.insert(0, os.path.dirname(__file__))

from scheduled_tasks import (  # noqa: E402
    ChatTriggerClient,
    JobState,
    SchedulerEngine,
    TriggerResult,
    jobs_to_payload,
    load_jobs,
    load_state,
    parse_jobs_payload,
    preview_jobs,
    run_due_jobs_once,
    save_state,
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


if __name__ == "__main__":
    unittest.main()
