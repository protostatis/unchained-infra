"""Contract tests for the isolated local Agent View stack launcher."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent


class TestDevScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "dev.sh").read_text()

    def test_agent_view_mode_uses_one_local_identity(self):
        self.assertIn('DEV_API_KEY=$(UNCHAINED_DB_PATH="$DEV_DB_PATH"', self.script)
        self.assertIn('UNCHAINED_API_KEY="$DEV_API_KEY"', self.script)
        self.assertIn('printf \'%s\' "$DEV_API_KEY" > "$PIDDIR/api-key"', self.script)
        self.assertIn('CDP_PROFILE="$DEV_PROFILE"', self.script)

    def test_agent_view_mode_runs_private_core_without_overlay(self):
        self.assertIn('PRIVATE_CORE_DIR=${PRIVATE_CORE_DIR:-', self.script)
        self.assertIn('uv run python private_core_server.py', self.script)
        self.assertIn('PRIVATE_CORE_MODE_VALUE="http"', self.script)
        self.assertIn('PRIVATE_CORE_MODE="$PRIVATE_CORE_MODE_VALUE"', self.script)
        self.assertNotIn("install_private_core.sh", self.script)

    def test_stop_covers_full_stack(self):
        self.assertIn("for svc in chat-agent bridge web private-core relay", self.script)


if __name__ == "__main__":
    unittest.main()
