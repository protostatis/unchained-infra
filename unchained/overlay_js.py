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

  // Prevent duplicate overlays from concurrent injections
  if (window.__uc_overlay_creating) return;
  window.__uc_overlay_creating = true;
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
    '  background: rgba(240,240,245,0.4);',
    '  backdrop-filter: blur(12px);',
    '  -webkit-backdrop-filter: blur(12px);',
    '  border: 1px solid rgba(200,200,210,0.5);',
    '  border-radius: 12px;',
    '  box-shadow: 0 4px 24px rgba(0,0,0,0.12);',
    '  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;',
    '  font-size: 13px;',
    '  color: #1a1a2e;',
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
    '  background: rgba(230,230,238,0.6);',
    '  border-bottom: 1px solid rgba(180,180,195,0.4);',
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
    '  color: #E85D3A;',
    '  letter-spacing: 0.5px;',
    '}',
    '.uc-header-right { display: flex; align-items: center; gap: 8px; }',
    '.uc-dot {',
    '  width: 7px; height: 7px; border-radius: 50%;',
    '  background: #aaa;',
    '  transition: background 0.3s;',
    '}',
    '.uc-dot.connected { background: #3FB950; }',
    '.uc-dot.error { background: #F85149; }',
    '.uc-minimize {',
    '  background: none; border: none; color: #666;',
    '  font-size: 16px; cursor: pointer; padding: 0 2px;',
    '  line-height: 1;',
    '}',
    '.uc-minimize:hover { color: #222; }',
    '.uc-body { display: flex; flex-direction: column; min-height: 0; flex: 1; }',
    '.uc-prompt {',
    '  padding: 10px 12px;',
    '  border-bottom: 1px solid rgba(180,180,195,0.3);',
    '  flex-shrink: 0;',
    '}',
    '.uc-prompt-label {',
    '  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;',
    '  font-size: 10px;',
    '  color: #777;',
    '  text-transform: uppercase;',
    '  letter-spacing: 0.5px;',
    '  margin-bottom: 4px;',
    '}',
    '.uc-prompt-text {',
    '  font-size: 12px;',
    '  color: #2a2a3e;',
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
    '.uc-log::-webkit-scrollbar-thumb { background: rgba(150,150,165,0.4); border-radius: 2px; }',
    '.uc-msg {',
    '  margin-bottom: 8px;',
    '  line-height: 1.4;',
    '  word-wrap: break-word;',
    '}',
    '.uc-msg.assistant { color: #2a2a3e; }',
    '.uc-msg.tool {',
    '  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;',
    '  font-size: 11px;',
    '  color: #666;',
    '  padding: 3px 0;',
    '}',
    '.uc-msg.status {',
    '  font-size: 11px;',
    '  color: #E85D3A;',
    '  font-style: italic;',
    '}',
    '.uc-input-area {',
    '  padding: 8px 12px;',
    '  border-top: 1px solid rgba(180,180,195,0.3);',
    '  display: flex;',
    '  gap: 6px;',
    '  flex-shrink: 0;',
    '}',
    '.uc-input {',
    '  flex: 1;',
    '  background: rgba(255,255,255,0.5);',
    '  border: 1px solid rgba(180,180,195,0.4);',
    '  border-radius: 6px;',
    '  color: #333;',
    '  font-size: 12px;',
    '  padding: 6px 8px;',
    '  outline: none;',
    '  resize: none;',
    '  font-family: inherit;',
    '}',
    '.uc-send {',
    '  background: rgba(220,220,230,0.5);',
    '  border: 1px solid rgba(180,180,195,0.4);',
    '  border-radius: 6px;',
    '  color: #333;',
    '  font-size: 12px;',
    '  padding: 6px 10px;',
    '  cursor: pointer;',
    '}',
    '.uc-send:hover { background: rgba(200,200,215,0.6); }',
    '.uc-input:focus {',
    '  border-color: #E85D3A;',
    '  color: #1a1a2e;',
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
    if (!text) return;
    // Push to outbox with nonce — server verifies nonce on poll
    window.__uc_overlay_outbox.push({type: 'user_followup', message: text, nonce: CFG.nonce});
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

  // --- Event receiver via CDP Runtime.evaluate (no network needed) ---
  // The server pushes events by calling window.__uc_overlay_push(evt)
  // via CDP. This bypasses all CSP restrictions.
  window.__uc_overlay_push = function(evt) {
    dot.className = 'uc-dot connected';  // green on first event
    if (!evt || !evt.type) return;
    var t = evt.type;
    if (t === 'text') {
      var working = log.querySelector('.uc-working');
      if (working) working.remove();
      addMsg('assistant', evt.data || '');
    } else if (t === 'tool_use' || t === 'tool_result') {
      if (!log.querySelector('.uc-working')) {
        var w = document.createElement('div');
        w.className = 'uc-msg status uc-working';
        w.textContent = 'Working\u2026';
        log.appendChild(w);
        log.scrollTop = log.scrollHeight;
      }
    } else if (t === 'done') {
      var w2 = log.querySelector('.uc-working');
      if (w2) w2.remove();
      addMsg('status', 'Ready for follow-up');
    } else if (t === 'error') {
      addMsg('status', 'Error: ' + (evt.data || evt.message || 'unknown'));
    }
  };
  // Follow-up sender via CDP — server polls window.__uc_overlay_outbox
  window.__uc_overlay_outbox = [];
  window.__uc_overlay_creating = false;
})();
""".strip()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_overlay_js(
    *,
    session_id: str,
    prompt_text: str,
    nonce: str,
    token: str = "",    # unused in v3 (kept for API compat)
    relay_host: str = "",  # unused in v3
) -> str:
    """Return injection-ready JS with runtime values substituted.

    v3: no network transport needed — events pushed via CDP.
    Config only needs session_id, prompt, and nonce.
    """
    cfg = json.dumps({
        "sessionId": session_id,
        "prompt": prompt_text,
        "nonce": nonce,
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
    session_id: str,
    prompt_text: str,
    nonce: str,
    token: str = "",
    relay_host: str = "",
) -> str:
    """Return bootstrap JS that re-injects the overlay on navigation."""
    full_js = build_overlay_js(
        session_id=session_id,
        prompt_text=prompt_text,
        nonce=nonce,
    )
    return OVERLAY_BOOTSTRAP_TEMPLATE.replace("%%FULL_OVERLAY_JS%%", full_js)
