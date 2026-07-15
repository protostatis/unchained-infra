(function() {
  'use strict';

  const reconnect = {
    active: false,
    bubble: null,
    eventsController: null,
    historyLoads: 0,
    lastSeq: 0,
    normalMonitor: false,
    openTools: [],
    pendingStepIds: [],
    draftMessage: '',
    renderedStepId: '',
    reqId: '',
    retry: 0,
    retryTimer: null,
    restoring: false,
    seenSeq: new Set(),
    sessionId: '',
    steps: new Map(),
    unboundTools: [],
    userBubble: null,
  };

  const reconnectStyle = document.createElement('style');
  reconnectStyle.textContent =
    'body.unchained-turn-active #sendbtn{display:none!important}' +
    'body.unchained-turn-active #cancelbtn{display:block!important}' +
    'body.unchained-turn-active #agent-bar{display:flex!important}' +
    'body.unchained-turn-active #slotbar button{pointer-events:none!important;opacity:.4!important}' +
    '.bubble.user.unchained-message-not-sent{opacity:.72}' +
    '.bubble.user.unchained-message-not-sent::after{content:"Not sent";display:block;margin-top:6px;font-size:11px;font-weight:600;opacity:.8}';
  document.head.appendChild(reconnectStyle);

  function randomRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function(value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function currentSessionId() {
    return typeof sessionId === 'string' ? sessionId : '';
  }

  function isCurrentTurn() {
    return reconnect.active && reconnect.sessionId && reconnect.sessionId === currentSessionId();
  }

  function resetRenderState() {
    reconnect.bubble = null;
    reconnect.openTools = [];
    reconnect.pendingStepIds = [];
    reconnect.renderedStepId = '';
    reconnect.steps.clear();
    reconnect.unboundTools = [];
  }

  function preserveRejectedDraft() {
    const message = String(reconnect.draftMessage || '');
    const input = document.getElementById('msginput');
    if (message && input && !String(input.value || '').trim()) {
      input.value = message;
      if (typeof autoGrow === 'function') autoGrow(input);
    }
    if (reconnect.userBubble && reconnect.userBubble.isConnected) {
      reconnect.userBubble.classList.add('unchained-message-not-sent');
    }
  }

  function rememberEvent(evt) {
    const seq = Number(evt && evt.seq);
    if (!Number.isFinite(seq) || seq <= 0) return true;
    if (reconnect.seenSeq.has(seq) || seq <= reconnect.lastSeq) return false;
    reconnect.seenSeq.add(seq);
    reconnect.lastSeq = seq;
    if (reconnect.seenSeq.size > 512) {
      reconnect.seenSeq.delete(reconnect.seenSeq.values().next().value);
    }
    return true;
  }

  function setTurnUi(active, detail) {
    try { sending = !!active; } catch(e) {}
    document.body.classList.toggle('unchained-turn-active', !!active);
    const send = document.getElementById('sendbtn');
    const cancel = document.getElementById('cancelbtn');
    const slots = document.getElementById('slotbar');
    const agentBar = document.getElementById('agent-bar');
    const action = document.getElementById('agent-action');
    if (send) send.style.display = active ? 'none' : 'block';
    if (cancel) cancel.style.display = active ? 'block' : 'none';
    if (slots) slots.classList.toggle('locked', !!active);
    if (agentBar) agentBar.classList.toggle('active', !!active);
    if (active && action && detail) action.textContent = detail;
    if (active && typeof setAgentViewState === 'function') setAgentViewState(detail || 'Reconnecting to turn', true);
    if (active && typeof setAgentShellPhase === 'function') setAgentShellPhase('planning', detail || 'Reconnecting to turn');
  }

  function updateActivity(value) {
    if (!value || typeof value !== 'object') return;
    const current = value.current_action || value.action;
    const detail = current && typeof current === 'object'
      ? [current.name || current.type || '', current.input || ''].filter(Boolean).join(' ')
      : String(current || value.phase || value.status || '').trim();
    if (!detail) return;
    const action = document.getElementById('agent-action');
    if (action) action.textContent = detail;
    if (typeof setAgentViewState === 'function') setAgentViewState(detail, true);
    if (typeof setAgentShellPhase === 'function' && value.phase) setAgentShellPhase(value.phase, detail);
  }

  function ensureBubble() {
    if (reconnect.bubble && reconnect.bubble.isConnected) return reconnect.bubble;
    const existing = Array.from(document.querySelectorAll('.bubble.asst')).find(function(el) {
      return el.dataset.unchainedActiveTurn === reconnect.reqId;
    });
    if (existing) {
      reconnect.bubble = existing;
      return existing;
    }
    if (typeof addAsstBubble !== 'function') return null;
    reconnect.bubble = addAsstBubble();
    reconnect.bubble.dataset.unchainedActiveTurn = reconnect.reqId;
    return reconnect.bubble;
  }

  function registerTool(el, stepId) {
    if (!el) return;
    const id = String(stepId || '');
    if (id) {
      el.dataset.unchainedStepId = id;
      reconnect.steps.set(id, el);
    }
    reconnect.openTools.push(el);
  }

  function bindPendingStep(el) {
    if (!el) return;
    const stepId = reconnect.pendingStepIds.shift();
    if (stepId) {
      el.dataset.unchainedStepId = stepId;
      reconnect.steps.set(stepId, el);
    } else {
      reconnect.unboundTools.push(el);
    }
    reconnect.openTools.push(el);
  }

  function noteNormalToolStart(evt) {
    const stepId = String(evt && evt.step_id || '');
    if (!stepId) return;
    const el = reconnect.unboundTools.shift();
    if (el) {
      el.dataset.unchainedStepId = stepId;
      reconnect.steps.set(stepId, el);
    } else {
      reconnect.pendingStepIds.push(stepId);
    }
  }

  function takeTool(evt) {
    const stepId = String(evt && evt.step_id || '');
    let el = stepId ? reconnect.steps.get(stepId) : null;
    if (!el) el = reconnect.openTools.find(function(candidate) {
      return candidate && candidate.isConnected;
    }) || null;
    if (!el) return null;
    reconnect.openTools = reconnect.openTools.filter(function(candidate) { return candidate !== el; });
    reconnect.unboundTools = reconnect.unboundTools.filter(function(candidate) { return candidate !== el; });
    if (stepId) reconnect.steps.delete(stepId);
    else if (el.dataset.unchainedStepId) reconnect.steps.delete(el.dataset.unchainedStepId);
    return el;
  }

  function finishTurn(outcome, terminal) {
    if (!reconnect.active && !terminal) return;
    reconnect.active = false;
    reconnect.normalMonitor = false;
    if (reconnect.eventsController) reconnect.eventsController.abort();
    reconnect.eventsController = null;
    if (reconnect.retryTimer) clearTimeout(reconnect.retryTimer);
    reconnect.retryTimer = null;
    setTurnUi(false);
    if (typeof _finalizeGroup === 'function') _finalizeGroup();
    if (typeof _turnCount !== 'undefined') _turnCount = 0;
    if (typeof _navTrail !== 'undefined') _navTrail = [];
    if (typeof renderNavTrail === 'function') renderNavTrail();
    if (outcome === 'done' && typeof showClaudeUpgradeCard === 'function') showClaudeUpgradeCard();
    if (typeof maybeShowUpgrade === 'function') maybeShowUpgrade();
    if (typeof completeAgentShellTurn === 'function') completeAgentShellTurn(outcome);
    if (typeof maybeRevealAgentResponse === 'function') maybeRevealAgentResponse();
  }

  function renderModelForced(evt) {
    if (!Array.isArray(evt.allowed_models) || !evt.allowed_models.length) return;
    if (typeof _POST_CAP_ALLOWED_MODELS !== 'undefined') {
      _POST_CAP_ALLOWED_MODELS = evt.allowed_models.map(function(value) {
        return String(value || '').trim();
      }).filter(Boolean);
    }
    if (evt.budget && typeof evt.budget === 'object' && typeof _openrouterUsage !== 'undefined') {
      _openrouterUsage = evt.budget;
    }
    const model = document.getElementById('modelsel');
    if (evt.model && model && typeof _modelOptionExists === 'function' && _modelOptionExists(evt.model)) {
      model.value = evt.model;
      if (typeof _persistTrialModel === 'function') _persistTrialModel(evt.model);
    }
    if (typeof _applyOpenRouterCapUi === 'function') _applyOpenRouterCapUi();
    if (typeof _syncCustomModelUi === 'function') _syncCustomModelUi();
  }

  function renderEvent(evt) {
    if (typeof evt === 'string') {
      try { evt = JSON.parse(evt); } catch(e) { return; }
    }
    if (!evt || typeof evt !== 'object') return;
    if (evt.seq == null && evt.id != null) evt = Object.assign({}, evt, {seq: evt.id});
    if (evt.req_id && reconnect.reqId && evt.req_id !== reconnect.reqId) return;
    if (!rememberEvent(evt)) return;
    reconnect.retry = 0;
    updateActivity(evt);
    const bubble = ensureBubble();
    if (!bubble) return;
    if (evt.type === 'tool_start') {
      reconnect.renderedStepId = String(evt.step_id || '');
      try {
        addToolCall(bubble, evt.name, evt.input);
      } finally {
        reconnect.renderedStepId = '';
      }
      return;
    }
    if (evt.type === 'tool_result') {
      const tool = takeTool(evt);
      if (tool) setToolResult(tool, evt.data, evt.is_screenshot, evt.visible);
      return;
    }
    if (evt.type === 'text') {
      appendText(bubble, String(evt.data || ''));
      return;
    }
    if (evt.type === 'model_forced') {
      renderModelForced(evt);
      return;
    }
    if (evt.type === 'cancelled') {
      appendText(bubble, '[Cancelled by user]');
      finishTurn('cancelled', true);
      return;
    }
    if (evt.type === 'error') {
      appendText(bubble, 'Error: ' + String(evt.data || 'Request failed'));
      finishTurn('error', true);
      return;
    }
    if (evt.type === 'done') finishTurn('done', true);
  }

  function parseSseBlock(block) {
    let id = '';
    const data = [];
    for (const line of block.split(/\r?\n/)) {
      const separator = line.indexOf(':');
      if (separator < 0) continue;
      const field = line.slice(0, separator);
      const value = line.slice(separator + 1).replace(/^ /, '');
      if (field === 'id') id = value;
      if (field === 'data') data.push(value);
    }
    if (!data.length) return null;
    try {
      const evt = JSON.parse(data.join('\n'));
      if (evt && evt.seq == null && id) evt.seq = Number(id) || id;
      return evt;
    } catch(e) {
      return null;
    }
  }

  async function readSse(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, {stream: true});
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const evt = parseSseBlock(block);
        if (evt) onEvent(evt);
      }
    }
    buffer += decoder.decode();
    const evt = parseSseBlock(buffer);
    if (evt) onEvent(evt);
  }

  const nativeFetch = window.fetch.bind(window);

  async function activeTurn(session) {
    const query = new URLSearchParams({session_id: session});
    const response = await nativeFetch('/web/chat/active?' + query.toString());
    if (response.status === 404) {
      const data = await response.json().catch(function() { return null; });
      if (data && data.active === false) return data;
    }
    if (!response.ok) throw new Error('active turn unavailable');
    return response.json();
  }

  function scheduleReconnect() {
    if (!isCurrentTurn() || reconnect.retryTimer) return;
    const delay = Math.min(8000, 300 * Math.pow(2, Math.min(reconnect.retry++, 5)));
    reconnect.retryTimer = setTimeout(function() {
      reconnect.retryTimer = null;
      reconcileTurn();
    }, delay);
  }

  async function subscribeEvents() {
    if (!isCurrentTurn() || reconnect.eventsController) return;
    const controller = new AbortController();
    reconnect.eventsController = controller;
    const currentReqId = reconnect.reqId;
    try {
      const query = new URLSearchParams({
        session_id: reconnect.sessionId,
        req_id: currentReqId,
        after: String(reconnect.lastSeq),
      });
      const response = await nativeFetch('/web/chat/events?' + query.toString(), {signal: controller.signal});
      if (!response.ok || !response.body) throw new Error('events unavailable');
      await readSse(response.body, renderEvent);
      if (isCurrentTurn() && reconnect.reqId === currentReqId) scheduleReconnect();
    } catch(e) {
      if (e && e.name !== 'AbortError' && isCurrentTurn() && reconnect.reqId === currentReqId) scheduleReconnect();
    } finally {
      if (reconnect.eventsController === controller) reconnect.eventsController = null;
    }
  }

  async function restoreActiveTurn() {
    const requestedSession = currentSessionId();
    if (!requestedSession || reconnect.restoring) return;
    reconnect.restoring = true;
    try {
      const data = await activeTurn(requestedSession);
      if (requestedSession !== currentSessionId()) return;
      if (!data || !data.active) {
        if (reconnect.sessionId === requestedSession) finishTurn('error', false);
        return;
      }
      reconnect.active = true;
      reconnect.sessionId = requestedSession;
      reconnect.reqId = String(data.req_id || '');
      reconnect.lastSeq = Math.max(0, Number(data.first_seq || 1) - 1);
      reconnect.retry = 0;
      reconnect.seenSeq.clear();
      resetRenderState();
      setTurnUi(true, String(data.current_action || data.phase || data.status || 'Reconnecting to turn'));
      if (typeof beginAgentViewResponseTurn === 'function') beginAgentViewResponseTurn();
      updateActivity(data);
      const events = Array.isArray(data.events) ? data.events : [];
      for (const evt of events) renderEvent(evt);
      if (!events.length) reconnect.lastSeq = Math.max(reconnect.lastSeq, Number(data.last_seq) || 0);
      if (isCurrentTurn()) subscribeEvents();
    } catch(e) {
      // Keep the local UI locked until an authoritative response says no turn is active.
      if (isCurrentTurn()) scheduleReconnect();
    } finally {
      reconnect.restoring = false;
    }
  }

  async function reconcileTurn() {
    if (!isCurrentTurn()) return;
    const requestedSession = reconnect.sessionId;
    try {
      const data = await activeTurn(requestedSession);
      if (!isCurrentTurn() || reconnect.sessionId !== requestedSession) return;
      if (!data || !data.active) {
        // The turn may be terminal in the registry, but its journal may still
        // hold a completed answer that can be replayed via /events.  Try
        // events before falling back to a generic error finish.
        await tryTerminalEventsFallback();
        return;
      }
      if (data.req_id && data.req_id !== reconnect.reqId) {
        // A locally started request may resume only its own authoritative
        // journal. Adopting another request here replays an older turn into a
        // newly submitted prompt that the server never accepted.
        finishTurn('error', false);
        return;
      }
      updateActivity(data);
      setTurnUi(true, String(data.current_action || data.phase || data.status || 'Reconnecting to turn'));
      subscribeEvents();
    } catch(e) {
      scheduleReconnect();
    }
  }

  async function tryTerminalEventsFallback() {
    if (!isCurrentTurn()) return;
    var requestedSession = reconnect.sessionId;
    var requestedReqId = reconnect.reqId;
    var hadEvents = false;
    var sawTerminal = false;
    try {
      var query = new URLSearchParams({
        session_id: requestedSession,
        req_id: requestedReqId,
        after: String(0),
      });
      var response = await nativeFetch('/web/chat/events?' + query.toString());
      if (!response.ok || !response.body) throw new Error('events unavailable');
      await readSse(response.body, function(evt) {
        if (!isCurrentTurn() || reconnect.reqId !== requestedReqId) return;
        hadEvents = true;
        if (evt.type === 'done' || evt.type === 'error' || evt.type === 'cancelled') {
          sawTerminal = true;
        }
        renderEvent(evt);
      });
    } catch(e) {
      // Fall through to error finish below.
    }
    if (isCurrentTurn() && reconnect.reqId === requestedReqId) {
      if (!hadEvents) {
        finishTurn('error', false);
      } else if (!sawTerminal) {
        // Received events but no terminal marker — the stream may have been
        // interrupted.  Retry from the last seen event position.
        scheduleReconnect();
      }
      // If we had events AND saw a terminal event, renderEvent already
      // called finishTurn for the terminal outcome.
    }
  }

  function beginTurn(body) {
    const requestedSession = String(body && body.session_id || currentSessionId());
    if (!requestedSession) return '';
    if (reconnect.eventsController) reconnect.eventsController.abort();
    if (reconnect.retryTimer) clearTimeout(reconnect.retryTimer);
    reconnect.active = true;
    reconnect.bubble = Array.from(document.querySelectorAll('.bubble.asst')).pop() || null;
    reconnect.userBubble = Array.from(document.querySelectorAll('.bubble.user')).pop() || null;
    reconnect.draftMessage = String(body && body.message || '');
    reconnect.eventsController = null;
    reconnect.lastSeq = 0;
    reconnect.normalMonitor = true;
    reconnect.reqId = randomRequestId();
    reconnect.retry = 0;
    reconnect.seenSeq.clear();
    reconnect.sessionId = requestedSession;
    reconnect.steps.clear();
    reconnect.openTools = [];
    reconnect.pendingStepIds = [];
    reconnect.unboundTools = [];
    if (reconnect.bubble) reconnect.bubble.dataset.unchainedActiveTurn = reconnect.reqId;
    return reconnect.reqId;
  }

  function monitorNormalStream(response, reqId) {
    readSse(response.body, function(evt) {
      if (!isCurrentTurn() || reconnect.reqId !== reqId) return;
      if (evt.req_id && evt.req_id !== reqId) return;
      if (!rememberEvent(evt)) return;
      if (evt.type === 'tool_start') noteNormalToolStart(evt);
      if (evt.type === 'done') finishTurn('done', true);
      if (evt.type === 'cancelled') finishTurn('cancelled', true);
      if (evt.type === 'error') finishTurn('error', true);
    }).catch(function() {}).finally(function() {
      if (isCurrentTurn() && reconnect.reqId === reqId) {
        reconnect.normalMonitor = false;
        reconcileTurn();
      }
    });
  }

  const originalAddToolCall = window.addToolCall;
  if (typeof originalAddToolCall === 'function') {
    window.addToolCall = function() {
      const tool = originalAddToolCall.apply(this, arguments);
      if (reconnect.renderedStepId) registerTool(tool, reconnect.renderedStepId);
      else if (isCurrentTurn()) bindPendingStep(tool);
      return tool;
    };
  }

  const originalLoadHistory = window.loadHistory;
  if (typeof originalLoadHistory === 'function') {
    window.loadHistory = async function() {
      resetRenderState();
      reconnect.historyLoads++;
      try {
        return await originalLoadHistory.apply(this, arguments);
      } finally {
        reconnect.historyLoads--;
        // The initial POST stream already has a clone monitoring its journal.
        // Do not attach a second stream if a background history refresh races it.
        if (!reconnect.normalMonitor) restoreActiveTurn();
      }
    };
  }

  const originalShowMain = window.showMain;
  if (typeof originalShowMain === 'function') {
    window.showMain = function() {
      const result = originalShowMain.apply(this, arguments);
      setTimeout(function() {
        if (!reconnect.active && !reconnect.historyLoads) restoreActiveTurn();
      }, 0);
      return result;
    };
  }

  const originalDoCancel = window.doCancel;
  if (typeof originalDoCancel === 'function') {
    window.doCancel = function() {
      if (isCurrentTurn()) setTurnUi(true, 'Cancelling turn');
      return originalDoCancel.apply(this, arguments);
    };
  }

  window.chatReconnectFetch = function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const parsed = new URL(url, window.location.href);
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (parsed.pathname === '/web/chat' && method === 'POST') {
      let body = {};
      try { body = JSON.parse((init && init.body) || '{}'); } catch(e) {}
      const reqId = beginTurn(body);
      const headers = new Headers((init && init.headers) || {});
      if (reqId) headers.set('X-Request-ID', reqId);
      const next = Object.assign({}, init || {}, {headers: headers});
      return nativeFetch(input, next).then(function(response) {
        if (response.ok && response.body && reqId) monitorNormalStream(response.clone(), reqId);
        else {
          // HTTP errors are definitive rejections, not ambiguous transport
          // failures. In particular, a 409 means this prompt was not sent and
          // must not attach to an older active request for the same session.
          if (isCurrentTurn() && reconnect.reqId === reqId) {
            preserveRejectedDraft();
            finishTurn('error', false);
          }
        }
        return response;
      }, function(error) {
        setTimeout(reconcileTurn, 0);
        throw error;
      });
    }
    if (parsed.pathname === '/web/chat/cancel' && method === 'POST' && isCurrentTurn()) {
      let body = {};
      try { body = JSON.parse((init && init.body) || '{}'); } catch(e) {}
      body.req_id = reconnect.reqId;
      const headers = new Headers((init && init.headers) || {});
      headers.set('X-Request-ID', reconnect.reqId);
      init = Object.assign({}, init || {}, {headers: headers, body: JSON.stringify(body)});
    }
    return nativeFetch(input, init);
  };
})();
