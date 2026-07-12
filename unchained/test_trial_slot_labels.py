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
        self.assertIn("_setSlotPreview(requestedSlot, _firstUserPreview(data.messages));", html)
        self.assertIn("_setSlotPreviewIfEmpty(activeSlot, msg);", html)
        self.assertIn("_setSlotPreview(activeSlot, '');", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the JavaScript race check")
    def test_out_of_order_history_response_cannot_replace_active_chat(self):
        html = templates.TRIAL_CHAT_HTML
        start = html.index("function _sessionStoreKey()")
        end = html.index("function showHintsIfEmpty()", start)
        runtime = html[start:end]
        script = runtime + r"""
class ClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  toggle(value, force) {
    if (force) this.values.add(value); else this.values.delete(value);
  }
}
function makeButton() {
  const children = {
    '.slot-name': {textContent: ''},
    '.slot-preview': {textContent: ''},
  };
  return {
    className: '', classList: new ClassList(), title: '', attrs: {}, children,
    querySelector(selector) { return children[selector]; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}
const buttons = {slot1: makeButton(), slot2: makeButton(), slot3: makeButton()};
const chat = {innerHTML: 'initial', rendered: []};
globalThis.document = {getElementById(id) { return buttons[id] || (id === 'chat' ? chat : null); }};
const values = new Map();
globalThis.localStorage = {
  getItem(key) { return values.has(key) ? values.get(key) : null; },
  setItem(key, value) { values.set(key, String(value)); },
};
globalThis.currentModel = () => 'test-model';
globalThis.hideHints = () => {};
globalThis.showHintsIfEmpty = () => { chat.rendered.push('empty'); };
globalThis.addUserBubble = text => { chat.rendered.push(text); };
globalThis.addAsstBubble = () => ({
  querySelector() { return null; },
});
globalThis.appendText = (bubble, text) => { chat.rendered.push(text); };
globalThis.showClaudeUpgradeCard = () => {};
const pending = {};
globalThis.fetch = url => {
  const slot = new URL(url, 'https://example.test').searchParams.get('slot');
  return new Promise(resolve => { pending[slot] = resolve; });
};
function response(sessionId, task) {
  return {
    ok: true,
    async json() {
      return {session_id: sessionId, messages: [{role: 'user', content: task}]};
    },
  };
}
function check(condition, message) { if (!condition) throw new Error(message); }

agentId = 'test-agent';
sessionId = '';
sending = false;
const initial = _ensureSlotState();

(async function() {
  initial.active_slot = 2;
  _saveSlotState(initial);
  activeSlot = 2;
  sessionId = initial.slots['2'];
  const slot2Session = sessionId;
  const slot2Load = loadHistory();

  const switched = _loadSlotState();
  switched.active_slot = 3;
  _saveSlotState(switched);
  activeSlot = 3;
  sessionId = switched.slots['3'];
  const slot3Session = sessionId;
  const slot3Load = loadHistory();

  pending['3'](response(slot3Session, 'Current Chat 3 task'));
  await slot3Load;
  check(activeSlot === 3 && sessionId === slot3Session, 'current response changed active session');
  check(_loadSlotState().previews['3'] === 'Current Chat 3 task', 'current preview missing');
  check(chat.rendered.join('|') === 'Current Chat 3 task', 'current history was not rendered');

  pending['2'](response('stale-replacement-session', 'Stale Chat 2 task'));
  await slot2Load;
  const finalState = _loadSlotState();
  check(activeSlot === 3 && sessionId === slot3Session, 'stale response changed active session');
  check(finalState.active_slot === 3, 'stale response changed persisted active slot');
  check(finalState.slots['2'] === slot2Session, 'stale response replaced Chat 2 session');
  check(finalState.previews['2'] === '', 'stale response labeled Chat 2');
  check(finalState.previews['3'] === 'Current Chat 3 task', 'stale response changed Chat 3 preview');
  check(chat.rendered.join('|') === 'Current Chat 3 task', 'stale response altered active chat');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
