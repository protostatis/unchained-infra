// Regression test: a journal tool_result with omitted/undefined `data` must
// not throw inside the recovery renderer. This is the real bug in
// signed-chat-reconnect.js renderEvent (setToolResult(tool, evt.data, ...)
// where evt.data can be undefined when the journal body was omitted).
//
// We load the ACTUAL setToolResult / parseIntelBars / addToolCall bodies from
// web_app/templates.py (not a copy) so the test tracks production behavior.
//
// Run: node test_sse_recovery_render.js

const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(
  path.join(__dirname, 'web_app', 'templates.py'), 'utf8');

function extractFn(name) {
  const re = new RegExp('function ' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(tpl);
  if (!m) throw new Error('function not found: ' + name);
  let i = m.index + m[0].length - 1; // at first '{'
  let depth = 0;
  for (; i < tpl.length; i++) {
    if (tpl[i] === '{') depth++;
    else if (tpl[i] === '}') { depth--; if (depth === 0) break; }
  }
  return tpl.slice(m.index, i + 1);
}

// --- minimal DOM stub ---
function makeEl() {
  const el = {
    _cls: new Set(), _children: [], _html: '', _text: '', dataset: {},
    classList: { add(c){ el._cls.add(c); }, remove(c){ el._cls.delete(c); }, contains(c){ return el._cls.has(c); } },
    appendChild(c){ el._children.push(c); return c; },
    querySelector(){ return makeEl(); },
    querySelectorAll(){ return []; },
    remove(){},
    after(){},
    set innerHTML(v){ el._html = v; }, get innerHTML(){ return el._html; },
    set textContent(v){ el._text = v; }, get textContent(){ return el._text; },
    set className(v){ el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className(){ return [...el._cls].join(' '); },
  };
  return el;
}

const TOOL_META = { websearch:{emoji:'🔍',label:'Search'}, navigate:{emoji:'🌐',label:'Navigate'} };
const BROWSER_TOOLS = new Set(['navigate']);
let _navTrail=[], _turnCount=0, _currentGroup=null, _currentGroupSteps=0;
function esc(s){ return String(s); }
function renderNavTrail(){}
function scrollToBottom(){}
function toolFriendlyDesc(n,i){ return String(i||'').slice(0,60); }
function _finalizeGroup(){}
function ensureMarkedConfigured(){}
function renderSafeMarkdown(t){ return t; }
const document = {
  getElementById: id => (id==='agent-bar'||id==='agent-action'||id==='turn-ctr') ? makeEl() : null,
  createElement: () => makeEl(), querySelectorAll: () => [],
};

// Load the real functions from templates.py.
eval(extractFn('parseIntelBars'));
eval(extractFn('addToolCall'));
eval(extractFn('setToolResult'));

let passed=0, failed=0;
function assert(cond, msg){ if(cond){passed++;} else {failed++; console.error('FAIL:',msg);} }

// Case 1: tool_result with undefined data (omitted journal body) must not throw
const bubble = makeEl();
const tool = addToolCall(bubble, 'websearch', 'spatial inference model');
let threw = false;
try {
  setToolResult(tool, undefined, false, false);
} catch (e) { threw = true; console.error('threw:', e.message); }
assert(!threw, 'setToolResult(undefined data) does not throw');

// Case 2: tool_result with null data also safe
let threw2 = false;
try {
  const tool2 = addToolCall(bubble, 'websearch', 'q2');
  setToolResult(tool2, null, false, false);
} catch (e) { threw2 = true; console.error('threw2:', e.message); }
assert(!threw2, 'setToolResult(null data) does not throw');

// Case 3: normal data still works
let threw3 = false;
try {
  const tool3 = addToolCall(bubble, 'websearch', 'q3');
  setToolResult(tool3, 'Found 3 results', false, true);
} catch (e) { threw3 = true; console.error('threw3:', e.message); }
assert(!threw3, 'setToolResult(normal data) does not throw');

// Case 4: intel-bars-shaped data (the path that calls text.match) safe
let threw4 = false;
try {
  const tool4 = addToolCall(bubble, 'navigate', 'https://x.com');
  setToolResult(tool4, 'strategy: dom (82%) runner-up: ddm (11%)', false, true);
} catch (e) { threw4 = true; console.error('threw4:', e.message); }
assert(!threw4, 'setToolResult(intel-bars data) does not throw');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed?1:0);
