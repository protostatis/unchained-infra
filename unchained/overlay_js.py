"""Overlay JavaScript for the floating copilot panel.

The JS is injected into the agent-controlled browser tab via CDP.
It uses a **closed** Shadow DOM so the agent's DDM walker cannot see
inside it (closed shadows return null for .shadowRoot).

The host element carries ``data-unchained-overlay="1"`` so DDM and
click code can skip it entirely.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Main overlay IIFE
# ---------------------------------------------------------------------------

OVERLAY_JS_TEMPLATE = r"""
(function() {
  'use strict';
  var HOST_ID = '__uc_overlay_host';

  // Remove previous instance if re-injected
  var old = document.getElementById(HOST_ID);
  if (old) old.remove();

  // --- Host element (visible to page DOM but opaque to DDM) ---
  var host = document.createElement('div');
  host.id = HOST_ID;
  host.setAttribute('data-unchained-overlay', '1');
  host.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;pointer-events:none;';
  document.documentElement.appendChild(host);

  // Closed shadow — DDM's _collectAll checks el.shadowRoot which returns null
  var shadow = host.attachShadow({mode: 'closed'});

  // --- Config (injected at build time via JSON.parse) ---
  var CFG = JSON.parse('%%CFG_JSON%%');

  // --- Styles ---
  var style = document.createElement('style');
  style.textContent = [
    ':host { all: initial; }',
    '.uc-panel {',
    '  pointer-events: auto;',
    '  width: 360px;',
    '  max-height: min(500px, calc(100vh - 32px));',
    '  background: rgba(13,17,23,0.92);',
    '  backdrop-filter: blur(8px);',
    '  -webkit-backdrop-filter: blur(8px);',
    '  border: 1px solid #30363D;',
    '  border-radius: 12px;',
    '  box-shadow: 0 8px 32px rgba(0,0,0,0.4);',
    '  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;',
    '  font-size: 13px;',
    '  color: #E6EDF3;',
    '  display: flex;',
    '  flex-direction: column;',
    '  overflow: hidden;',
    '  transition: max-height 0.25s ease, opacity 0.25s ease;',
    '}',
    '.uc-panel.minimized {',
    '  max-height: 40px;',
    '}',
    '.uc-header {',
    '  display: flex;',
    '  align-items: center;',
    '  justify-content: space-between;',
    '  padding: 8px 12px;',
    '  background: rgba(22,27,34,0.95);',
    '  border-bottom: 1px solid #30363D;',
    '  cursor: grab;',
    '  user-select: none;',
    '  -webkit-user-select: none;',
    '  flex-shrink: 0;',
    '}',
    '.uc-header:active { cursor: grabbing; }',
    '.uc-brand {',
    '  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;',
    '  font-size: 11px;',
    '  font-weight: 700;',
    '  color: #F57A4B;',
    '  letter-spacing: 0.5px;',
    '}',
    '.uc-header-right { display: flex; align-items: center; gap: 8px; }',
    '.uc-dot {',
    '  width: 7px; height: 7px; border-radius: 50%;',
    '  background: #484F58;',
    '  transition: background 0.3s;',
    '}',
    '.uc-dot.connected { background: #3FB950; }',
    '.uc-dot.error { background: #F85149; }',
    '.uc-minimize {',
    '  background: none; border: none; color: #8B949E;',
    '  font-size: 16px; cursor: pointer; padding: 0 2px;',
    '  line-height: 1;',
    '}',
    '.uc-minimize:hover { color: #E6EDF3; }',
    '.uc-body { display: flex; flex-direction: column; min-height: 0; flex: 1; }',
    '.uc-prompt {',
    '  padding: 10px 12px;',
    '  border-bottom: 1px solid #21262D;',
    '  flex-shrink: 0;',
    '}',
    '.uc-prompt-label {',
    '  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;',
    '  font-size: 10px;',
    '  color: #8B949E;',
    '  text-transform: uppercase;',
    '  letter-spacing: 0.5px;',
    '  margin-bottom: 4px;',
    '}',
    '.uc-prompt-text {',
    '  font-size: 12px;',
    '  color: #C9D1D9;',
    '  line-height: 1.4;',
    '  max-height: 54px;',
    '  overflow: hidden;',
    '  text-overflow: ellipsis;',
    '  display: -webkit-box;',
    '  -webkit-line-clamp: 3;',
    '  -webkit-box-orient: vertical;',
    '}',
    '.uc-log {',
    '  flex: 1;',
    '  overflow-y: auto;',
    '  padding: 8px 12px;',
    '  min-height: 80px;',
    '  max-height: 300px;',
    '}',
    '.uc-log::-webkit-scrollbar { width: 4px; }',
    '.uc-log::-webkit-scrollbar-thumb { background: #30363D; border-radius: 2px; }',
    '.uc-msg {',
    '  margin-bottom: 8px;',
    '  line-height: 1.4;',
    '  word-wrap: break-word;',
    '}',
    '.uc-msg.assistant { color: #C9D1D9; }',
    '.uc-msg.tool {',
    '  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;',
    '  font-size: 11px;',
    '  color: #8B949E;',
    '  padding: 3px 0;',
    '}',
    '.uc-msg.status {',
    '  font-size: 11px;',
    '  color: #F57A4B;',
    '  font-style: italic;',
    '}',
    '.uc-input-area {',
    '  padding: 8px 12px;',
    '  border-top: 1px solid #21262D;',
    '  display: flex;',
    '  gap: 6px;',
    '  flex-shrink: 0;',
    '}',
    '.uc-input {',
    '  flex: 1;',
    '  background: #161B22;',
    '  border: 1px solid #30363D;',
    '  border-radius: 6px;',
    '  color: #8B949E;',
    '  font-size: 12px;',
    '  padding: 6px 8px;',
    '  outline: none;',
    '  resize: none;',
    '  font-family: inherit;',
    '}',
    '.uc-send {',
    '  background: #21262D;',
    '  border: 1px solid #30363D;',
    '  border-radius: 6px;',
    '  color: #E6EDF3;',
    '  font-size: 12px;',
    '  padding: 6px 10px;',
    '  cursor: pointer;',
    '}',
    '.uc-send:hover { background: #30363D; }',
    '.uc-input:focus {',
    '  border-color: #F57A4B;',
    '  color: #E6EDF3;',
    '}',
  ].join('\n');
  shadow.appendChild(style);

  // --- Panel DOM ---
  var panel = document.createElement('div');
  panel.className = 'uc-panel';

  // Header
  var header = document.createElement('div');
  header.className = 'uc-header';
  var brand = document.createElement('span');
  brand.className = 'uc-brand';
  brand.textContent = 'UNCHAINED';
  var headerRight = document.createElement('div');
  headerRight.className = 'uc-header-right';
  var dot = document.createElement('span');
  dot.className = 'uc-dot';
  var minBtn = document.createElement('button');
  minBtn.className = 'uc-minimize';
  minBtn.textContent = '\u2013';
  minBtn.title = 'Minimize';
  headerRight.appendChild(dot);
  headerRight.appendChild(minBtn);
  header.appendChild(brand);
  header.appendChild(headerRight);
  panel.appendChild(header);

  // Body
  var body = document.createElement('div');
  body.className = 'uc-body';

  // Prompt
  var promptSection = document.createElement('div');
  promptSection.className = 'uc-prompt';
  var promptLabel = document.createElement('div');
  promptLabel.className = 'uc-prompt-label';
  promptLabel.textContent = 'PROMPT';
  var promptText = document.createElement('div');
  promptText.className = 'uc-prompt-text';
  promptText.textContent = CFG.prompt || '...';
  promptSection.appendChild(promptLabel);
  promptSection.appendChild(promptText);
  body.appendChild(promptSection);

  // Message log
  var log = document.createElement('div');
  log.className = 'uc-log';
  body.appendChild(log);

  // Input area
  var inputArea = document.createElement('div');
  inputArea.className = 'uc-input-area';
  var input = document.createElement('textarea');
  input.className = 'uc-input';
  input.rows = 1;
  input.placeholder = 'Send a follow-up...';
  var sendBtn = document.createElement('button');
  sendBtn.className = 'uc-send';
  sendBtn.textContent = 'Send';
  function sendFollowup() {
    var text = input.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({type: 'user_followup', message: text}));
    addMsg('status', 'You: ' + text);
    input.value = '';
  }
  sendBtn.addEventListener('click', function(e) { e.stopPropagation(); sendFollowup(); });
  // Stop ALL key events from leaking to the page (prevents typing in page inputs)
  ['keydown', 'keyup', 'keypress'].forEach(function(evt) {
    input.addEventListener(evt, function(e) { e.stopPropagation(); });
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendFollowup();
    }
  });
  // Prevent click-through to page when interacting with input
  inputArea.addEventListener('mousedown', function(e) { e.stopPropagation(); });
  inputArea.appendChild(input);
  inputArea.appendChild(sendBtn);
  body.appendChild(inputArea);

  panel.appendChild(body);
  shadow.appendChild(panel);

  // --- Minimize toggle ---
  var minimized = false;
  minBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    minimized = !minimized;
    panel.classList.toggle('minimized', minimized);
    minBtn.textContent = minimized ? '+' : '\u2013';
    minBtn.title = minimized ? 'Expand' : 'Minimize';
  });

  // --- Drag ---
  var dragX = 0, dragY = 0, startRight = 16, startBottom = 16;
  header.addEventListener('mousedown', function(e) {
    if (e.target === minBtn) return;
    e.preventDefault();
    dragX = e.clientX;
    dragY = e.clientY;
    var cs = getComputedStyle(host);
    startRight = parseInt(cs.right) || 16;
    startBottom = parseInt(cs.bottom) || 16;
    document.addEventListener('mousemove', onDrag);
    document.addEventListener('mouseup', offDrag);
  });
  function onDrag(e) {
    host.style.right = Math.max(0, startRight - (e.clientX - dragX)) + 'px';
    host.style.bottom = Math.max(0, startBottom - (e.clientY - dragY)) + 'px';
  }
  function offDrag() {
    document.removeEventListener('mousemove', onDrag);
    document.removeEventListener('mouseup', offDrag);
  }

  // --- Message log helpers ---
  function addMsg(cls, text) {
    var el = document.createElement('div');
    el.className = 'uc-msg ' + cls;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  // --- WebSocket ---
  var ws = null;
  var reconnectTimer = null;
  var reconnectDelay = 1000;
  var noReconnect = false;
  // Always use wss when not localhost to avoid sending token in plaintext
  var isLocal = CFG.host === 'localhost' || CFG.host.indexOf('127.0.0.1') === 0;
  var wsProto = (location.protocol === 'https:' || !isLocal) ? 'wss' : 'ws';

  function connect() {
    try {
      ws = new WebSocket(wsProto + '://' + CFG.host + '/overlay/ws');
    } catch (e) {
      dot.className = 'uc-dot error';
      return;
    }
    ws.onopen = function() {
      ws.send(JSON.stringify({type: 'auth', token: CFG.token}));
    };
    ws.onmessage = function(e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      var t = msg.type || '';
      if (t === 'auth_ok') {
        dot.className = 'uc-dot connected';
        reconnectDelay = 1000; // reset backoff on success
        addMsg('status', 'Connected to session');
        return;
      }
      if (t === 'text') {
        addMsg('assistant', msg.data || '');
      } else if (t === 'tool_use') {
        var tool = msg.tool || msg.name || 'tool';
        addMsg('tool', '\u25B6 ' + tool);
      } else if (t === 'tool_result') {
        var preview = String(msg.data || msg.result || '').substring(0, 120);
        if (preview) addMsg('tool', '  \u2192 ' + preview);
      } else if (t === 'done') {
        addMsg('status', 'Session ended');
        dot.className = 'uc-dot';
      } else if (t === 'error') {
        addMsg('status', 'Error: ' + (msg.data || msg.message || 'unknown'));
        dot.className = 'uc-dot error';
      }
    };
    ws.onclose = function() {
      dot.className = 'uc-dot';
      if (!noReconnect && !reconnectTimer) {
        reconnectTimer = setTimeout(function() {
          reconnectTimer = null;
          connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
      }
    };
    ws.onerror = function() {
      dot.className = 'uc-dot error';
    };
  }
  connect();

  // Cleanup on unload
  window.addEventListener('beforeunload', function() {
    noReconnect = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (ws) { try { ws.close(); } catch(_){} }
  });
})();
""".strip()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_overlay_js(
    *,
    token: str,
    relay_host: str,
    session_id: str,
    prompt_text: str,
) -> str:
    """Return injection-ready JS with runtime values substituted.

    Config is embedded as a single JSON.parse() call to avoid any
    placeholder-collision or double-substitution risks.

    Note: the HMAC token is visible in the injected JS source (and
    therefore to DevTools, extensions, or page scripts). This is an
    accepted limitation of client-side token injection; the token is
    short-lived (1 hour) and session-scoped.
    """
    cfg = json.dumps({
        "token": token,
        "host": relay_host,
        "sessionId": session_id,
        "prompt": prompt_text,
    }, separators=(",", ":"))
    # Escape for embedding inside a JS single-quoted string
    cfg_escaped = cfg.replace("\\", "\\\\").replace("'", "\\'")
    return OVERLAY_JS_TEMPLATE.replace("%%CFG_JSON%%", cfg_escaped)


# ---------------------------------------------------------------------------
# Bootstrap JS for Page.addScriptToEvaluateOnNewDocument
# ---------------------------------------------------------------------------

OVERLAY_BOOTSTRAP_TEMPLATE = r"""
(function() {
  'use strict';
  // Re-inject the full overlay after a short delay (let page DOM settle)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      setTimeout(function() { %%FULL_OVERLAY_JS%% }, 50);
    });
  } else {
    setTimeout(function() { %%FULL_OVERLAY_JS%% }, 50);
  }
})();
""".strip()


def build_overlay_bootstrap_js(
    *,
    token: str,
    relay_host: str,
    session_id: str,
    prompt_text: str,
) -> str:
    """Return bootstrap JS that re-injects the overlay on navigation."""
    full_js = build_overlay_js(
        token=token,
        relay_host=relay_host,
        session_id=session_id,
        prompt_text=prompt_text,
    )
    return OVERLAY_BOOTSTRAP_TEMPLATE.replace("%%FULL_OVERLAY_JS%%", full_js)
