"""Tests the deploy-time preservation of hosted archive metadata."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "preserve_hosted_conversations.py"
_SPEC = importlib.util.spec_from_file_location("preserve_hosted_conversations", _SCRIPT)
assert _SPEC and _SPEC.loader
preserver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preserver)


class TestPreserveHostedConversations(unittest.TestCase):
    def test_copies_json_data_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "home" / "hosted-conversations"
            destination = root / "data" / "hosted-conversations"
            archive = source / "user-key" / "archives" / "archive-12345678.json"
            slot_state = source / "user-key" / "slot-state.json"
            archive.parent.mkdir(parents=True)
            archive.write_text(json.dumps({"preview": "private transcript omitted"}))
            slot_state.write_text(json.dumps({"active_slot": 2}))

            self.assertEqual(preserver.preserve_json_files(source, destination), (2, 0))
            self.assertEqual(
                json.loads((destination / "user-key" / "slot-state.json").read_text()),
                {"active_slot": 2},
            )
            self.assertEqual(preserver.preserve_json_files(source, destination), (0, 2))

    def test_conflicting_destination_fails_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source_file = source / "user" / "slot-state.json"
            destination_file = destination / "user" / "slot-state.json"
            safe_source_file = source / "user" / "safe.json"
            safe_destination_file = destination / "user" / "safe.json"
            source_file.parent.mkdir(parents=True)
            destination_file.parent.mkdir(parents=True)
            source_file.write_text('{"active_slot":1}')
            destination_file.write_text('{"active_slot":2}')
            safe_source_file.write_text('{"preserve":true}')

            with self.assertRaisesRegex(
                RuntimeError, "conflicting destination file: user/slot-state.json"
            ):
                preserver.preserve_json_files(source, destination)
            self.assertEqual(destination_file.read_text(), '{"active_slot":2}')
            self.assertEqual(json.loads(safe_destination_file.read_text()), {"preserve": True})

    def test_missing_source_is_a_safe_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                preserver.preserve_json_files(root / "missing", root / "persistent"),
                (0, 0),
            )

    def test_timestamp_preservation_is_best_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "slot-state.json").write_text('{"active_slot":1}')
            with patch.object(preserver.os, "utime", side_effect=OSError("unsupported")):
                self.assertEqual(preserver.preserve_json_files(source, destination), (1, 0))
            self.assertEqual(
                json.loads((destination / "slot-state.json").read_text()),
                {"active_slot": 1},
            )

    def test_scan_errors_are_reported_for_operator_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            destination = root / "destination"

            def erroring_walk(path, *, followlinks, onerror):
                error = PermissionError("permission denied")
                error.filename = str(source / "user")
                onerror(error)
                return iter(())

            with patch.object(preserver.os, "walk", side_effect=erroring_walk):
                with self.assertRaisesRegex(RuntimeError, "cannot scan user"):
                    preserver.preserve_json_files(source, destination)


if __name__ == "__main__":
    unittest.main()
