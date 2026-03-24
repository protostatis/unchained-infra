"""Tests: provisioned Chrome isolation between agents.

Validates that:
1. _write_prov_state persists agent_id
2. _reconcile_prov_chromes skips slots belonging to other agents
3. _cleanup_all_prov_chromes only kills own agent's slots
4. _handle_provision_cleanup (no slot) only cleans own agent's slots

Run:
    python3 test_provision_agent_isolation.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from chrome_bridge import (
    Agent,
    _write_prov_state,
    _read_prov_state,
    _remove_prov_state,
    _list_prov_state_slots,
    PROVISION_STATE_DIR,
)


class _FakeProcess:
    def terminate(self): pass
    def wait(self, timeout=None): pass
    def kill(self): pass


def _make_agent(agent_id: str = "agent-a") -> Agent:
    a = Agent(relay_url="ws://localhost:8765/tunnel")
    a.agent_id = agent_id
    a.running = True
    return a


class TestWriteProvStateIncludesAgentId(unittest.TestCase):
    """_write_prov_state should persist the agent_id field."""

    def test_agent_id_written(self):
        slot = "aa01"
        try:
            _write_prov_state(slot, {
                "pid": 12345,
                "port": 19222,
                "temp_dir": "/tmp/fake",
                "agent_id": "agent-a",
            })
            state = _read_prov_state(slot)
            self.assertIsNotNone(state)
            self.assertEqual(state["agent_id"], "agent-a")
        finally:
            _remove_prov_state(slot)

    def test_missing_agent_id_defaults_empty(self):
        slot = "aa02"
        try:
            _write_prov_state(slot, {
                "pid": 12345,
                "port": 19222,
                "temp_dir": "/tmp/fake",
            })
            state = _read_prov_state(slot)
            self.assertEqual(state["agent_id"], "")
        finally:
            _remove_prov_state(slot)


class TestReconcileSkipsOtherAgents(unittest.TestCase):
    """_reconcile_prov_chromes should not adopt slots from other agents."""

    def test_skips_foreign_slot(self):
        """Agent A's reconcile should not pick up Agent B's persisted slot."""
        slot_b = "bb01"
        try:
            _write_prov_state(slot_b, {
                "pid": 99999,  # non-existent PID — will be classified as dead
                "port": 19333,
                "temp_dir": "/tmp/nonexistent",
                "agent_id": "agent-b",
            })
            agent_a = _make_agent("agent-a")
            agent_a._reconcile_prov_chromes(force=True)
            # Agent A should NOT have adopted agent-b's slot
            self.assertNotIn(slot_b, agent_a._prov_chromes)
        finally:
            _remove_prov_state(slot_b)

    @patch("chrome_bridge._classify_prov_pid", return_value="alive")
    def test_adopts_own_slot(self, _mock_classify):
        """Agent A's reconcile should adopt its own persisted slot (if PID alive)."""
        slot_a = "aa03"
        tmp = tempfile.mkdtemp(prefix="prov_tmp_")
        try:
            _write_prov_state(slot_a, {
                "pid": 12345,
                "port": 19444,
                "temp_dir": tmp,
                "agent_id": "agent-a",
                "ready": True,
            })
            agent_a = _make_agent("agent-a")
            agent_a._reconcile_prov_chromes(force=True)
            self.assertIn(slot_a, agent_a._prov_chromes)
        finally:
            _remove_prov_state(slot_a)
            shutil.rmtree(tmp, ignore_errors=True)

    @patch("chrome_bridge._classify_prov_pid", return_value="alive")
    def test_adopts_legacy_slot_without_agent_id(self, _mock_classify):
        """Slots written before agent_id tracking should be adopted by any agent."""
        slot_legacy = "cc01"
        tmp = tempfile.mkdtemp(prefix="prov_tmp_")
        try:
            # Simulate a legacy slot file with no agent_id
            _write_prov_state(slot_legacy, {
                "pid": 12345,
                "port": 19555,
                "temp_dir": tmp,
                "ready": True,
            })
            # Manually strip agent_id to simulate pre-fix state file
            import json as _json
            path = os.path.join(PROVISION_STATE_DIR, f"{slot_legacy}.json")
            with open(path) as f:
                data = _json.load(f)
            data.pop("agent_id", None)
            with open(path, "w") as f:
                _json.dump(data, f)

            agent_a = _make_agent("agent-a")
            agent_a._reconcile_prov_chromes(force=True)
            self.assertIn(slot_legacy, agent_a._prov_chromes)
        finally:
            _remove_prov_state(slot_legacy)
            shutil.rmtree(tmp, ignore_errors=True)


class TestCleanupAllOnlyOwn(unittest.TestCase):
    """_cleanup_all_prov_chromes should only kill this agent's provisioned Chromes."""

    @patch("chrome_bridge._classify_prov_pid", return_value="alive")
    def test_does_not_kill_foreign_persisted_slots(self, _mock_classify):
        """Agent A's cleanup-all should not touch Agent B's live persisted slots."""
        slot_b = "dd01"
        try:
            _write_prov_state(slot_b, {
                "pid": 12345,
                "port": 19666,
                "temp_dir": "/tmp/nonexistent",
                "agent_id": "agent-b",
            })
            agent_a = _make_agent("agent-a")
            # cleanup_all with include_persisted reconciles first
            agent_a._cleanup_all_prov_chromes(include_persisted=True)
            # Agent B's slot file should still exist (live PID, foreign agent)
            state = _read_prov_state(slot_b)
            self.assertIsNotNone(state, "Agent B's slot was deleted by Agent A's cleanup")
            self.assertNotIn(slot_b, agent_a._prov_chromes)
        finally:
            _remove_prov_state(slot_b)

    def test_prunes_dead_foreign_slot_state_file(self):
        """Dead foreign slots should have their state files pruned during reconcile."""
        slot_b = "dd02"
        try:
            _write_prov_state(slot_b, {
                "pid": 99999,
                "port": 19666,
                "temp_dir": "/tmp/nonexistent",
                "agent_id": "agent-b",
            })
            agent_a = _make_agent("agent-a")
            agent_a._cleanup_all_prov_chromes(include_persisted=True)
            # Dead foreign slot's state file should be pruned
            state = _read_prov_state(slot_b)
            self.assertIsNone(state, "Dead foreign slot state file should have been pruned")
        finally:
            _remove_prov_state(slot_b)


class TestHandleProvisionCleanupIsolation(unittest.IsolatedAsyncioTestCase):
    """_handle_provision_cleanup (no slot) should only clean this agent's Chromes."""

    @patch("chrome_bridge._classify_prov_pid", return_value="alive")
    async def test_cleanup_no_slot_only_cleans_own(self, _mock_classify):
        """When called with no slot, should clean in-memory slots but not adopt foreign ones."""
        slot_b = "ee01"
        try:
            _write_prov_state(slot_b, {
                "pid": 12345,
                "port": 19777,
                "temp_dir": "/tmp/nonexistent",
                "agent_id": "agent-b",
            })

            agent_a = _make_agent("agent-a")
            agent_a.ws = AsyncMock()
            tmp_a = tempfile.mkdtemp(prefix="prov_own_")
            agent_a._prov_chromes["ff01"] = {
                "port": 19888,
                "process": _FakeProcess(),
                "temp_dir": tmp_a,
                "profile_dir_name": "Profile 1",
            }

            await agent_a._handle_provision_cleanup(req_id="iso-test")

            # Own slot cleaned
            self.assertNotIn("ff01", agent_a._prov_chromes)
            # Foreign slot's persisted state should be untouched (live PID)
            state = _read_prov_state(slot_b)
            self.assertIsNotNone(state, "Agent B's persisted slot was deleted by Agent A's cleanup")

            sent = json.loads(agent_a.ws.send.call_args[0][0])
            self.assertEqual(sent["body"]["status"], "cleaned_up")
        finally:
            _remove_prov_state(slot_b)


if __name__ == "__main__":
    unittest.main()
