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
    def setUp(self):
        from web_app.handlers import chat_flow

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()

    def _core(self):
        return SimpleNamespace(
            _authenticate=lambda request: {"agent_id": "trial-agent"},
            _is_pending_user=lambda auth_info: False,
            _is_openrouter_model=lambda model: True,
            _resolve_chat_agent_id=lambda auth_info, model: "trial-agent",
            _resolve_trial_session_id=lambda agent_id, requested: requested,
            _session_tabs={},
            _session_agent_map={},
            _session_allowed_tabs={},
            _session_profile_paths={},
            _session_last_active={},
            _chat_preview_generations={},
            _delete_trial_session=Mock(),
            time=SimpleNamespace(time=lambda: 1234.5),
            _OPENROUTER_TRIAL_DEFAULT_MODEL="google/gemini-3.1-flash-lite",
        )

    def _new_request(self, request_id: str, session_id: str = "s-trial-agent-current"):
        return SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "session_id": session_id,
                    "slot": 2,
                }
            )
        )

    def _ack_request(self, request_id: str, old_session: str, new_session: str):
        return SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "model": "google/gemini-3.1-flash-lite",
                    "request_id": request_id,
                    "previous_session_id": old_session,
                    "session_id": new_session,
                    "slot": 2,
                }
            )
        )

    @patch("web_app.handlers.chat_flow._core")
    async def test_trial_reservation_preserves_old_session_until_ack(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new

        old_session = "s-trial-agent-current"
        core = self._core()
        core._session_tabs[old_session] = "active-tab"
        core._session_agent_map[old_session] = "active-agent"
        mock_core.return_value = core

        response = await handle_chat_new(
            self._new_request("request-0000000000000001", old_session)
        )
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertFalse(data["replayed"])
        self.assertEqual(data["request_id"], "request-0000000000000001")
        self.assertEqual(data["commit_request_id"], data["request_id"])
        self.assertEqual(data["previous_session_id"], old_session)
        self.assertEqual(data["active_slot"], 2)
        self.assertNotEqual(data["session_id"], old_session)
        self.assertEqual(core._session_tabs, {old_session: "active-tab"})
        self.assertEqual(core._session_agent_map, {old_session: "active-agent"})
        core._delete_trial_session.assert_not_called()

    @patch("web_app.handlers.chat_flow._core")
    async def test_lost_response_retry_replays_commit_then_ack_is_idempotent(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new, handle_chat_new_ack

        request_id = "request-0000000000000002"
        old_session = "s-trial-agent-current"
        other_session = "s-trial-agent-other"
        core = self._core()
        core._session_tabs.update({old_session: "active-tab", other_session: "other-tab"})
        core._session_agent_map.update({old_session: "active-agent", other_session: "other-agent"})
        core._session_allowed_tabs[old_session] = {"active-tab"}
        core._session_profile_paths[old_session] = "/profiles/trial"
        core._session_last_active[old_session] = 100.0
        mock_core.return_value = core

        first = await handle_chat_new(self._new_request(request_id, old_session))
        first_data = json.loads(first.body.decode())
        # The first response is intentionally ignored to model transport loss.
        retry = await handle_chat_new(self._new_request(request_id, old_session))
        retry_data = json.loads(retry.body.decode())

        self.assertTrue(retry_data["replayed"])
        self.assertEqual(retry_data["session_id"], first_data["session_id"])
        self.assertIn(old_session, core._session_tabs)
        core._delete_trial_session.assert_not_called()

        from web_app.handlers import chat_flow

        chat_flow._trial_new_chat_requests.clear()
        chat_flow._trial_new_chat_sources.clear()

        ack = await handle_chat_new_ack(
            self._ack_request(request_id, old_session, retry_data["session_id"])
        )
        ack_data = json.loads(ack.body.decode())

        self.assertTrue(ack_data["acknowledged"])
        self.assertNotIn(old_session, core._session_tabs)
        self.assertEqual(core._session_tabs[retry_data["session_id"]], "active-tab")
        self.assertEqual(core._session_tabs[other_session], "other-tab")
        self.assertEqual(core._session_agent_map[other_session], "other-agent")
        self.assertEqual(core._session_allowed_tabs[retry_data["session_id"]], {"active-tab"})
        self.assertEqual(core._session_profile_paths[retry_data["session_id"]], "/profiles/trial")
        core._delete_trial_session.assert_called_once_with(old_session)

        repeated_ack = await handle_chat_new_ack(
            self._ack_request(request_id, old_session, retry_data["session_id"])
        )
        self.assertEqual(repeated_ack.status, 200)
        core._delete_trial_session.assert_called_once_with(old_session)

    @patch("web_app.handlers.chat_flow._core")
    async def test_competing_requests_for_same_source_replay_one_transition(self, mock_core):
        from web_app.handlers.chat_flow import handle_chat_new

        old_session = "s-trial-agent-current"
        core = self._core()
        mock_core.return_value = core

        first = await handle_chat_new(
            self._new_request("request-0000000000000003", old_session)
        )
        later = await handle_chat_new(
            self._new_request("request-0000000000000004", old_session)
        )
        first_data = json.loads(first.body.decode())
        later_data = json.loads(later.body.decode())

        self.assertEqual(later_data["request_id"], "request-0000000000000004")
        self.assertEqual(later_data["commit_request_id"], "request-0000000000000003")
        self.assertTrue(later_data["replayed"])
        self.assertEqual(later_data["session_id"], first_data["session_id"])
        core._delete_trial_session.assert_not_called()

        reused = await handle_chat_new(
            self._new_request(
                "request-0000000000000003", "s-trial-agent-different"
            )
        )
        self.assertEqual(reused.status, 409)


class TestTrialNewChatTemplate(unittest.TestCase):
    def test_trial_template_has_accessible_transactional_feedback(self):
        html = templates.TRIAL_CHAT_HTML

        self.assertIn('id="new-chat-feedback" role="status" aria-live="polite"', html)
        self.assertIn("if (sending || newChatPending) return;", html)
        self.assertIn("const previousSessionId = sessionId;", html)
        self.assertIn("const data = await r.json().catch(() => ({}));", html)
        self.assertIn("nextSessionId === previousSessionId", html)
        self.assertIn("request_id: pending.request_id", html)
        self.assertIn("/web/chat/new/ack", html)
        self.assertIn("data.request_id !== pending.request_id", html)
        self.assertIn("data.commit_request_id", html)
        self.assertIn("recoverPendingNewChat();", html)
        self.assertIn("resetNewChatUi();", html)
        self.assertIn("Your current chat is unchanged.", html)


class TestTrialNewChatRuntime(unittest.TestCase):
    def test_lost_response_retry_ack_recovery_and_stale_response_ordering(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the browser-runtime harness")

        html = templates.TRIAL_CHAT_HTML
        runtime = "\n".join(
            [
                _js_function(html, "_newChatStateKey"),
                _js_function(html, "_newChatRequestId"),
                _js_function(html, "_loadPendingNewChat"),
                _js_function(html, "_savePendingNewChat"),
                _js_function(html, "_clearPendingNewChat"),
                _js_function(html, "acknowledgeNewChatTransition", async_function=True),
                _js_function(html, "recoverPendingNewChat", async_function=True),
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
const storage = new Map();
global.localStorage = {{
  getItem: key => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: key => storage.delete(key),
}};
let generatedRequest = 0;
Object.defineProperty(globalThis, 'crypto', {{
  configurable: true,
  value: {{randomUUID: () => 'request-000000000000000' + (++generatedRequest)}},
}});
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
function _sessionStoreKey() {{ return 'unchained_session_trial-agent_openrouter'; }}
function _slotLabel(slot) {{ return ['Lane A','Lane B','Lane C'][slot - 1]; }}
function _persistSessionId(value) {{ persisted = value; }}
function _setActiveSlotSession(value) {{ activePersisted = value; }}
function _syncSlotButtons() {{ syncCount += 1; }}
function loadSidebarHistory() {{}}
function showHintsIfEmpty() {{ hintsShown += 1; elements.chat.innerHTML = 'fresh hints'; }}
function _finalizeGroup() {{}}
function renderNavTrail() {{}}
{runtime}

function restoreCurrentChat(sid) {{
  sessionId = sid;
  elements.chat.innerHTML = 'existing history';
  elements.msginput.value = 'draft prompt';
  elements.msginput.style.height = '88px';
  elements['agent-action'].textContent = 'Browsing';
  elements['new-chat-feedback'].textContent = '';
  persisted = '';
  activePersisted = '';
}}

(async () => {{
  let mode = 'lost-first';
  let ackMode = 'success';
  let reservedSession = '';
  const reserveRequestIds = [];
  global.fetch = async (url, options) => {{
    const body = JSON.parse(options.body);
    if (url === '/web/chat/new/ack') {{
      if (ackMode === 'lost') throw new Error('ack response lost');
      return {{ok:true, json:async () => ({{
        ok:true, acknowledged:true, request_id:body.request_id,
        previous_session_id:body.previous_session_id, session_id:body.session_id,
      }})}};
    }}
    assert.strictEqual(url, '/web/chat/new');
    assert.strictEqual(elements.chat.innerHTML, 'existing history', 'history cleared before reservation response');
    reserveRequestIds.push(body.request_id);
    if (!reservedSession) reservedSession = 's-trial-agent-fresh-' + generatedRequest;
    if (mode === 'lost-first') {{
      mode = 'replay';
      throw new Error('reservation response lost');
    }}
    const responseRequestId = mode === 'stale' ? 'request-stale-00000000000' : body.request_id;
    return {{ok:true, json:async () => ({{
      ok:true, request_id:responseRequestId, commit_request_id:body.request_id,
      previous_session_id:body.session_id,
      session_id:reservedSession, active_slot:body.slot, replayed:mode === 'replay',
    }})}};
  }};

  // The server committed, but the first response was lost. UI and draft remain,
  // and the retry reuses the persisted request ID to recover that exact commit.
  restoreCurrentChat('s-trial-agent-old');
  await doNewChat();
  const ambiguous = _loadPendingNewChat();
  assert.ok(ambiguous);
  assert.strictEqual(ambiguous.session_id, '');
  assert.strictEqual(sessionId, 's-trial-agent-old');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.strictEqual(elements.msginput.value, 'draft prompt');
  assert.match(elements['new-chat-feedback'].textContent, /retry safely/);

  await doNewChat();
  assert.strictEqual(reserveRequestIds[0], reserveRequestIds[1]);
  assert.strictEqual(sessionId, reservedSession);
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
  assert.strictEqual(_loadPendingNewChat(), null, 'successful ack clears recovery state');

  // If the destructive ack commits but its response is lost, the client has
  // already adopted the new session and retains enough state to retry the ack.
  storage.clear();
  reservedSession = '';
  mode = 'normal';
  ackMode = 'lost';
  restoreCurrentChat('s-trial-agent-second');
  await doNewChat();
  const unacked = _loadPendingNewChat();
  assert.strictEqual(sessionId, reservedSession);
  assert.strictEqual(unacked.session_id, reservedSession);
  assert.strictEqual(elements['new-chat-feedback'].className, 'success');
  ackMode = 'success';
  await recoverPendingNewChat();
  assert.strictEqual(_loadPendingNewChat(), null, 'reload recovery retries lost ack');

  // A stale response from an older request cannot replace the active session.
  storage.clear();
  reservedSession = '';
  mode = 'stale';
  restoreCurrentChat('s-trial-agent-third');
  await doNewChat();
  const stalePending = _loadPendingNewChat();
  assert.strictEqual(sessionId, 's-trial-agent-third');
  assert.strictEqual(elements.chat.innerHTML, 'existing history');
  assert.strictEqual(elements.msginput.value, 'draft prompt');
  assert.match(elements['new-chat-feedback'].textContent, /stale or invalid/);
  mode = 'replay';
  await doNewChat();
  assert.strictEqual(reserveRequestIds.at(-1), stalePending.request_id);
  assert.strictEqual(sessionId, reservedSession);
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
