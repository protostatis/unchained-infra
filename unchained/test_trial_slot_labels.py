"""Behavior checks for the trial page's three chat-session labels."""

from __future__ import annotations

import shutil
import subprocess
import unittest

from web_app import templates


class TestTrialSlotLabels(unittest.TestCase):
    def test_trial_uses_chat_labels_and_accessible_empty_state(self):
        html = templates.TRIAL_CHAT_HTML
        self.assertNotIn("Lane A", html)
        self.assertNotIn("Lane B", html)
        self.assertNotIn("Lane C", html)
        self.assertIn('role="group" aria-label="Chat sessions"', html)
        self.assertIn('<span class="slot-name">Chat 1</span>', html)
        self.assertIn('<span class="slot-preview">No task yet</span>', html)
        self.assertIn("text-overflow:ellipsis", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the JavaScript behavior check")
    def test_initial_populated_switched_and_cleared_labels(self):
        html = templates.TRIAL_CHAT_HTML
        start = html.index("function _sessionStoreKey()")
        end = html.index("function onModelChange(model)", start)
        slot_runtime = html[start:end]
        script = slot_runtime + r"""
class ClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  toggle(value, force) {
    if (force) this.values.add(value); else this.values.delete(value);
  }
  contains(value) { return this.values.has(value); }
}
function makeButton() {
  const children = {
    '.slot-name': {textContent: ''},
    '.slot-preview': {textContent: ''},
  };
  return {
    className: '',
    classList: new ClassList(),
    title: '',
    attrs: {},
    querySelector(selector) { return children[selector]; },
    setAttribute(name, value) { this.attrs[name] = value; },
    children,
  };
}
const buttons = {slot1: makeButton(), slot2: makeButton(), slot3: makeButton()};
const chat = {innerHTML: ''};
globalThis.document = {getElementById(id) { return buttons[id] || (id === 'chat' ? chat : null); }};
const values = new Map();
globalThis.localStorage = {
  getItem(key) { return values.has(key) ? values.get(key) : null; },
  setItem(key, value) { values.set(key, String(value)); },
};
agentId = 'test-agent';
sessionId = '';
sending = false;
globalThis.loadHistory = async function() {
  _setSlotPreview(activeSlot, activeSlot === 2 ? 'Compare accessible hotels' : '');
};
function check(condition, message) { if (!condition) throw new Error(message); }

_ensureSlotState();
_syncSlotButtons();
check(buttons.slot1.children['.slot-name'].textContent === 'Chat 1', 'initial chat name');
check(buttons.slot1.children['.slot-preview'].textContent === 'No task yet', 'initial empty label');
check(buttons.slot1.classList.contains('empty'), 'initial empty class');
check(buttons.slot1.attrs['aria-pressed'] === 'true', 'initial active state');

_setSlotPreview(1, _firstUserPreview([
  {role: 'assistant', content: 'How can I help?'},
  {role: 'user', content: '  Research   flights\n to Tokyo  '},
]));
check(buttons.slot1.children['.slot-preview'].textContent === 'Research flights to Tokyo', 'populated task label');
check(!buttons.slot1.classList.contains('empty'), 'populated class');
check(buttons.slot1.attrs['aria-label'].includes('Research flights to Tokyo'), 'accessible full preview');

(async function() {
  await switchSlot(2);
  check(buttons.slot1.children['.slot-preview'].textContent === 'Research flights to Tokyo', 'first preview survives switch');
  check(buttons.slot2.children['.slot-preview'].textContent === 'Compare accessible hotels', 'switched preview');
  check(buttons.slot2.attrs['aria-pressed'] === 'true', 'switched active state');
  check(buttons.slot1.attrs['aria-pressed'] === 'false', 'previous inactive state');

  _setSlotPreview(2, '');
  check(buttons.slot2.children['.slot-preview'].textContent === 'No task yet', 'cleared empty label');
  check(buttons.slot2.classList.contains('empty'), 'cleared empty class');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_send_and_clear_update_the_preview_state(self):
        html = templates.TRIAL_CHAT_HTML
        self.assertIn("_setSlotPreview(activeSlot, _firstUserPreview(data.messages));", html)
        self.assertIn("_setSlotPreviewIfEmpty(activeSlot, msg);", html)
        self.assertIn("_setSlotPreview(activeSlot, '');", html)


if __name__ == "__main__":
    unittest.main()
