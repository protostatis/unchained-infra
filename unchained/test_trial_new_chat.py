"""Focused backend, template, and browser-runtime tests for trial New Chat."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from web_app import templates


def _js_function(source: str, name: str, *, async_function: bool = False) -> str:
    marker = f"{'async ' if async_function else ''}function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class TestTrialNewChatBackend(unittest.IsolatedAsyncioTestCase):
    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_reset_replaces_only_requested_lane_session(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new

        old_session = "s-trial-agent-current"
        other_session = "s-trial-agent-other"
        delete_session = Mock()
        core = SimpleNamespace(
            _authenticate=lambda request: {"agent_id": "trial-agent"},
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested,
            _session_tabs={old_session: "active-tab", other_session: "other-tab"},
            _session_agent_map={old_session: "active-agent", other_session: "other-agent"},
            _delete_trial_session=delete_session,
            time=SimpleNamespace(time=lambda: 1234.5),
        )
        mock_core.return_value = core
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "session_id": old_session,
                    "slot": 2,
                }
            )
        )

        response = await handle_chat_new(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["active_slot"], 2)
        self.assertNotEqual(data["session_id"], old_session)
        self.assertTrue(data["session_id"].startswith("s-trial-agent-"))
        self.assertEqual(core._session_tabs[data["session_id"]], "active-tab")
        self.assertEqual(core._session_tabs[other_session], "other-tab")
        self.assertEqual(core._session_agent_map[other_session], "other-agent")
        delete_session.assert_called_once_with(old_session)


class TestTrialNewChatTemplate(unittest.TestCase):
    def test_trial_template_has_accessible_transactional_feedback(self):
        html = templates.TRIAL_CHAT_HTML

        self.assertIn('id="new-chat-feedback" role="status" aria-live="polite"', html)
        self.assertIn("if (sending || newChatPending) return;", html)
        self.assertIn("const previousSessionId = sessionId;", html)
        self.assertIn("const data = await r.json().catch(() => ({}));", html)
        self.assertIn("nextSessionId === previousSessionId", html)
        self.assertIn("resetNewChatUi();", html)
        self.assertIn("Your current chat is unchanged.", html)


class TestTrialNewChatRuntime(unittest.TestCase):
    def test_success_commits_reset_and_failures_preserve_current_chat(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the browser-runtime harness")

        html = templates.TRIAL_CHAT_HTML
        runtime = "\n".join(
            [
                _js_function(html, "setNewChatFeedback"),
                _js_function(html, "resetNewChatUi"),
                _js_function(html, "doNewChat", async_function=True),
            ]
        )
        harness = f"""
const assert = require('assert');
function classList() {{
  const values = new Set();
  return {{add: v => values.add(v), remove: v => values.delete(v), contains: v => values.has(v)}};
}}
const elements = {{
  'new-chat-feedback': {{textContent:'', className:'', attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}}},
  chat: {{innerHTML:'existing history'}},
  msginput: {{value:'draft prompt', style:{{height:'88px'}}}},
  'agent-action': {{textContent:'Browsing'}},
  'turn-ctr': {{textContent:'t4'}},
  'agent-bar': {{classList:classList()}},
  sendbtn: {{style:{{display:'none'}}, disabled:false, attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}}},
  cancelbtn: {{style:{{display:'block'}}}},
  slotbar: {{classList:classList()}},
}};
elements['agent-bar'].classList.add('active');
global.document = {{getElementById: id => elements[id]}};
let agentId = 'trial-agent';
let sessionId = 's-trial-agent-old';
let activeSlot = 2;
let sending = false;
let newChatPending = false;
let _cancelCtrl = {{stale:true}};
let _turnCount = 4;
let _navTrail = ['example.com'];
let persisted = '';
let activePersisted = '';
let hintsShown = 0;
let syncCount = 0;
function currentModel() {{ return 'google/gemini-3.1-flash-lite'; }}
function _slotLabel(slot) {{ return ['Lane A','Lane B','Lane C'][slot - 1]; }}
function _persistSessionId(value) {{ persisted = value; }}
function _setActiveSlotSession(value) {{ activePersisted = value; }}
function _syncSlotButtons() {{ syncCount += 1; }}
function loadSidebarHistory() {{}}
function showHintsIfEmpty() {{ hintsShown += 1; elements.chat.innerHTML = 'fresh hints'; }}
function _finalizeGroup() {{}}
function renderNavTrail() {{}}
{runtime}

function restoreCurrentChat() {{
  sessionId = 's-trial-agent-old';
  elements.chat.innerHTML = 'existing history';
  elements.msginput.value = 'draft prompt';
  elements['agent-action'].textContent = 'Browsing';
  elements['new-chat-feedback'].textContent = '';
  persisted = '';
  activePersisted = '';
}}

(async () => {{
  global.fetch = async (url, options) => {{
    assert.strictEqual(url, '/web/chat/new');
    assert.strictEqual(elements.chat.innerHTML, 'existing history', 'history cleared before response');
    const body = JSON.parse(options.body);
    assert.strictEqual(body.slot, 2);
    assert.strictEqual(body.session_id, 's-trial-agent-old');
    return {{ok:true, json:async () => ({{ok:true, session_id:'s-trial-agent-fresh', active_slot:2}})}};
  }};
  await doNewChat();
  assert.strictEqual(sessionId, 's-trial-agent-fresh');
  assert.strictEqual(persisted, sessionId);
  assert.strictEqual(activePersisted, sessionId);
  assert.strictEqual(activeSlot, 2);
  assert.strictEqual(elements.chat.innerHTML, 'fresh hints');
  assert.strictEqual(elements.msginput.value, '');
  assert.strictEqual(elements.msginput.style.height, 'auto');
  assert.strictEqual(elements['agent-action'].textContent, '');
  assert.strictEqual(elements['turn-ctr'].textContent, '');
  assert.strictEqual(elements['new-chat-feedback'].className, 'success');
  assert.match(elements['new-chat-feedback'].textContent, /Fresh Lane B ready/);
  assert.strictEqual(hintsShown, 1);
  assert.strictEqual(syncCount, 1);
  assert.strictEqual(newChatPending, false);
  assert.strictEqual(elements.slotbar.classList.contains('locked'), false);
  assert.strictEqual(elements.sendbtn.disabled, false);

  restoreCurrentChat();
  global.fetch = async () => ({{ok:false, json:async () => ({{error:'Agent not connected'}})}});
  await doNewChat();
  assert.strictEqual(sessionId, 's-trial-agent-old');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.strictEqual(elements.msginput.value, 'draft prompt');
  assert.strictEqual(elements['agent-action'].textContent, 'Browsing');
  assert.strictEqual(persisted, '');
  assert.strictEqual(activePersisted, '');
  assert.strictEqual(elements['new-chat-feedback'].className, 'error');
  assert.strictEqual(elements['new-chat-feedback'].attrs.role, 'alert');
  assert.match(elements['new-chat-feedback'].textContent, /Agent not connected/);
  assert.match(elements['new-chat-feedback'].textContent, /current chat is unchanged/);

  restoreCurrentChat();
  global.fetch = async () => {{ throw new Error('network offline'); }};
  await doNewChat();
  assert.strictEqual(sessionId, 's-trial-agent-old');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.strictEqual(elements.msginput.value, 'draft prompt');
  assert.match(elements['new-chat-feedback'].textContent, /network offline/);

  restoreCurrentChat();
  global.fetch = async () => ({{ok:true, json:async () => ({{ok:true, session_id:'s-trial-agent-old'}})}});
  await doNewChat();
  assert.strictEqual(sessionId, 's-trial-agent-old');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.match(elements['new-chat-feedback'].textContent, /did not return a fresh session/);
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
