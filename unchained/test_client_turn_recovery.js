// Regression test for the client-side SSE interruption recovery.
// Mirrors _recoverInterruptedTurn's dispatch so a dropped stream self-heals
// without duplicating already-rendered content.
//
// Run: node test_client_turn_recovery.js

// --- minimal DOM stub (same shape as the browser) ---
function makeEl() {
  const el = {
    _cls: new Set(), _children: [], _html: '', _text: '', dataset: {},
    classList: {
      add(c){ el._cls.add(c); }, remove(c){ el._cls.delete(c); },
      contains(c){ return el._cls.has(c); },
    },
    appendChild(c){ el._children.push(c); return c; },
    querySelector(){ return null; },
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

// Real handlers (verbatim from templates.py)
function addToolCall(bubble, name, input) {
  const meta = TOOL_META[name] || {emoji:'⚙️',label:name};
  const desc = toolFriendlyDesc(name, input);
  _turnCount++;
  document.getElementById('agent-bar').classList.add('active');
  if (BROWSER_TOOLS.has(name)) {
    if (!_currentGroup) { const g=makeEl(); g.className='action-group'; bubble.appendChild(g); _currentGroup=g; }
    const step=makeEl(); step.className='action-step'; _currentGroup.appendChild(step); scrollToBottom(); return step;
  }
  const sa=makeEl(); sa.className='action-standalone'; bubble.appendChild(sa); scrollToBottom(); return sa;
}
function parseIntelBars(text){
  const m=text.match(/strategy:\s*(\S+)\s*\((\d+)%\)/); if(!m) return null;
  return [{label:m[1],pct:parseInt(m[2])}];
}
function setToolResult(el, result, isScreenshot, visible) {
  const isStep = el.classList.contains('action-step');
  const dotCls = isStep ? 'as-dot' : 'standalone-dot';
  const dot = el.querySelector('.'+dotCls);
  if (dot){ dot.className=dotCls+' done'; dot.textContent='✓'; }
  if (!isScreenshot) { parseIntelBars(result); scrollToBottom(); }
}
function appendText(bubble, text) {
  if (!bubble._rawText) bubble._rawText='';
  bubble._rawText += text;
  let span=bubble.querySelector('.text');
  if (!span){ span=makeEl(); span.className='text'; bubble.appendChild(span); }
  ensureMarkedConfigured();
  span.textContent = bubble._rawText;
  scrollToBottom();
}

// The recovery dispatch (mirrors _recoverInterruptedTurn)
function replayJournal(bubble, journalText) {
  bubble.querySelectorAll('.action-group,.action-standalone,.turn-recovery').forEach(el=>el.remove());
  bubble._rawText='';
  _currentGroup=null; _currentGroupSteps=0;
  let currentTool=null;
  const dispatch = (evt) => {
    if (evt.type==='tool_start') currentTool=addToolCall(bubble,evt.name,evt.input);
    else if (evt.type==='tool_result'){ if(currentTool){ setToolResult(currentTool,evt.data,evt.is_screenshot,evt.visible); currentTool=null; } }
    else if (evt.type==='text') appendText(bubble,evt.data);
    else if (evt.type==='cancelled') appendText(bubble,'[Cancelled by user]');
    else if (evt.type==='error') appendText(bubble,'Error: '+evt.data);
    else if (evt.type==='done'){ _finalizeGroup(); document.getElementById('agent-bar').classList.remove('active'); _turnCount=0; _navTrail=[]; renderNavTrail(); }
  };
  let buf=journalText, nl;
  while ((nl=buf.indexOf('\n\n'))!==-1){
    const chunk=buf.slice(0,nl); buf=buf.slice(nl+2);
    for (const line of chunk.split('\n')){
      if (!line.startsWith('data: ')) continue;
      let evt; try { evt=JSON.parse(line.slice(6)); } catch { continue; }
      if (evt.type) dispatch(evt);
    }
  }
}

// --- test fixtures: a WebSearch turn journal as the server would emit it ---
const websearchJournal = [
  {type:'tool_start',name:'websearch',input:'spatial inference model',req_id:'r1'},
  {type:'tool_result',name:'result',data:'completed',is_screenshot:false,visible:false,req_id:'r1'},
  {type:'text',data:'The widely used model is ...',req_id:'r1'},
  {type:'done',req_id:'r1'},
].map(e=>`data: ${JSON.stringify(e)}\n\n`).join('');

let passed=0, failed=0;
function assert(cond, msg){ if(cond){passed++;} else {failed++; console.error('FAIL:',msg);} }

// Case A: interrupted stream already rendered partial text -> recovery must not duplicate
const bubble = makeEl();
// simulate partial live render: tool_start + hidden completed + partial text
addToolCall(bubble,'websearch','spatial inference model');
appendText(bubble,'The widely used');  // partial
const beforeText = bubble._rawText;
replayJournal(bubble, websearchJournal);
// After replay, full text should be present and NOT contain the partial twice
assert(bubble._rawText.includes('The widely used model is ...'), 'replay restores full text');
assert(!bubble._rawText.includes('The widely usedThe widely used'), 'no duplicate text after replay');
assert(bubble.querySelectorAll('.action-standalone,.action-group').length===0, 'stale action elements cleared');
console.log('Case A (no duplication):', bubble._rawText);

// Case B: fresh interrupted turn with NO prior render -> recovery builds it
const bubble2 = makeEl();
replayJournal(bubble2, websearchJournal);
assert(bubble2._rawText.includes('The widely used model is ...'), 'replay builds text from scratch');
assert(bubble2.querySelectorAll('.action-standalone,.action-group').length===0, 'replay builds clean (action el reset)');

// Case C: journal with only done (already finished) -> no crash
const bubble3 = makeEl();
replayJournal(bubble3, 'data: '+JSON.stringify({type:'done',req_id:'r1'})+'\n\n');
assert(true, 'done-only journal does not throw');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed?1:0);
