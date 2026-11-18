"""Guard: a reconnect handler that promises a delay must actually take it.

The OpenRouter trial worker printed "Reconnecting in 5s..." and then looped
immediately, so a stopped local stack made it reconnect ~3000 times/second and
grow /tmp/unchained-dev/trial-agent.log to 1 TB over 30 days.

Every lane is checked here because the sibling lanes (codex, gemini, sdk) all
had the sleep while this one silently diverged.
"""

import ast
import unittest
from pathlib import Path

LANES = (
    "chat_agent_cli.py",
    "chat_agent_codex.py",
    "chat_agent_gemini.py",
    "chat_agent_openrouter.py",
    "chat_agent_sdk.py",
)

HERE = Path(__file__).resolve().parent


def _awaited_sleeps(node):
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Await)
        and isinstance(child.value, ast.Call)
        and isinstance(child.value.func, ast.Attribute)
        and child.value.func.attr == "sleep"
    ]


def _reconnect_handlers(tree):
    """Yield (lineno, handler) for reconnect handlers inside a retry loop."""
    for loop in ast.walk(tree):
        if not isinstance(loop, ast.While):
            continue
        if not (isinstance(loop.test, ast.Constant) and loop.test.value is True):
            continue
        for statement in ast.walk(loop):
            if not isinstance(statement, ast.Try):
                continue
            for handler in statement.handlers:
                if "Reconnecting" in ast.unparse(handler):
                    yield handler.lineno, handler


class TestReconnectBackoff(unittest.TestCase):
    def test_every_reconnect_waits(self):
        checked = 0
        for lane in LANES:
            source = (HERE / lane).read_text()
            tree = ast.parse(source, filename=lane)
            handlers = list(_reconnect_handlers(tree))
            self.assertTrue(
                handlers, f"{lane} has no reconnect handler; the guard went stale"
            )
            for lineno, handler in handlers:
                with self.subTest(lane=lane, line=lineno):
                    self.assertTrue(
                        _awaited_sleeps(handler),
                        f"{lane}:{lineno} announces a reconnect delay without "
                        "awaiting a sleep; this spins a hot loop when the "
                        "server is unreachable",
                    )
                    checked += 1
        self.assertGreaterEqual(checked, len(LANES))


if __name__ == "__main__":
    unittest.main()
