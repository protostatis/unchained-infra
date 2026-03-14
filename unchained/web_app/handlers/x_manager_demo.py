"""Slogan demo for X.com: "Unchained drives. You navigate."

This is intentionally a single-file prototype:
- page HTML
- controller loop
- MCP-style tool wrappers over existing Unchained cloud tools
- heuristic reward critic

The demo is read-only. It never posts to X. It opens a fresh tab, gathers
evidence with `cdp_navigate`, `js_eval`, and optional `ddm` fallback, then
shows the controller trajectory and a lightweight playbook.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict, dataclass, field
import json
import math
import os
import re
from typing import Any
from urllib.parse import quote_plus

from aiohttp import web

import cloud_tools
from web_app.cmd_dispatch import is_chrome_unavailable_error
from web_app.core import get_core as _core
from web_app.handlers.auth_admin import is_admin as _is_admin

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Unchained drives. You navigate. | Unchained</title>
  <style>
    :root{
      --bg:#07131a;
      --panel:#0f2028;
      --panel-2:#122832;
      --line:rgba(192,220,230,0.18);
      --text:#e8f2f4;
      --muted:#9eb6bd;
      --accent:#7be0b8;
      --accent-2:#f4c55c;
      --danger:#f28482;
      --mono:"SFMono-Regular",Menlo,Consolas,monospace;
      --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",serif;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      min-height:100vh;
      font-family:var(--serif);
      background:
        radial-gradient(circle at top left, rgba(123,224,184,0.12), transparent 34%),
        radial-gradient(circle at top right, rgba(244,197,92,0.10), transparent 28%),
        linear-gradient(180deg, #08141b 0%, #07131a 54%, #060e13 100%);
      color:var(--text);
    }
    .shell{
      max-width:1200px;
      margin:0 auto;
      padding:32px 18px 56px;
    }
    .hero{
      display:grid;
      grid-template-columns:1.2fr 0.8fr;
      gap:18px;
      align-items:start;
      margin-bottom:18px;
    }
    .hero-card,.panel{
      background:linear-gradient(180deg, rgba(18,40,50,0.92), rgba(11,24,31,0.92));
      border:1px solid var(--line);
      border-radius:22px;
      padding:22px;
      box-shadow:0 24px 70px rgba(0,0,0,0.28);
      backdrop-filter:blur(8px);
    }
    .eyebrow{
      display:inline-flex;
      align-items:center;
      gap:8px;
      font-family:var(--mono);
      font-size:12px;
      letter-spacing:0.12em;
      text-transform:uppercase;
      color:var(--accent);
      margin-bottom:10px;
    }
    .eyebrow::before{
      content:"";
      width:10px;
      height:10px;
      border-radius:999px;
      background:var(--accent);
      box-shadow:0 0 18px rgba(123,224,184,0.55);
    }
    h1{
      margin:0 0 10px;
      font-size:clamp(30px,5vw,56px);
      line-height:0.98;
      letter-spacing:-0.04em;
    }
    h1 span{color:var(--accent)}
    .sub{
      margin:0;
      color:var(--muted);
      font-size:16px;
      line-height:1.55;
      max-width:62ch;
    }
    .tag-grid{
      display:grid;
      grid-template-columns:repeat(2,minmax(0,1fr));
      gap:10px;
      margin-top:18px;
    }
    .tag{
      border:1px solid var(--line);
      border-radius:16px;
      padding:12px 14px;
      background:rgba(255,255,255,0.02);
    }
    .tag strong{
      display:block;
      margin-bottom:4px;
      font-size:13px;
      font-family:var(--mono);
      color:var(--accent-2);
      text-transform:uppercase;
      letter-spacing:0.08em;
    }
    .tag div{
      font-size:14px;
      color:var(--text);
      line-height:1.4;
    }
    .hero-side{
      display:grid;
      gap:12px;
      font-size:14px;
      color:var(--muted);
    }
    .hero-side .callout{
      border:1px solid var(--line);
      border-radius:18px;
      padding:16px;
      background:rgba(255,255,255,0.03);
    }
    .hero-side code{
      font-family:var(--mono);
      color:var(--accent);
      font-size:12px;
    }
    .layout{
      display:grid;
      grid-template-columns:360px 1fr;
      gap:18px;
    }
    form{
      display:grid;
      gap:14px;
    }
    label{
      display:grid;
      gap:8px;
      font-size:14px;
      color:var(--muted);
    }
    .label-row{
      display:flex;
      justify-content:space-between;
      gap:12px;
      align-items:center;
    }
    .label-row span:last-child{
      font-family:var(--mono);
      font-size:11px;
      letter-spacing:0.08em;
      text-transform:uppercase;
      color:var(--accent-2);
    }
    input,textarea,select,button{
      font:inherit;
    }
    input,textarea,select{
      width:100%;
      border:1px solid rgba(192,220,230,0.18);
      border-radius:16px;
      background:rgba(4,12,16,0.72);
      color:var(--text);
      padding:13px 14px;
      outline:none;
      transition:border-color 0.15s, transform 0.15s;
    }
    textarea{
      min-height:132px;
      resize:vertical;
      line-height:1.45;
    }
    input:focus,textarea:focus,select:focus{
      border-color:var(--accent);
      transform:translateY(-1px);
    }
    .actions{
      display:flex;
      gap:10px;
      align-items:center;
    }
    .btn{
      appearance:none;
      border:none;
      border-radius:999px;
      padding:13px 18px;
      cursor:pointer;
      font-family:var(--mono);
      font-size:12px;
      font-weight:700;
      letter-spacing:0.08em;
      text-transform:uppercase;
      transition:transform 0.15s, box-shadow 0.15s, opacity 0.15s;
    }
    .btn:hover{transform:translateY(-1px)}
    .btn:disabled{opacity:0.6;cursor:wait;transform:none}
    .btn-primary{
      background:linear-gradient(90deg, var(--accent), #a8f7d9);
      color:#062018;
      box-shadow:0 18px 40px rgba(123,224,184,0.22);
    }
    .btn-secondary{
      background:rgba(255,255,255,0.06);
      color:var(--text);
      border:1px solid var(--line);
    }
    .note{
      margin-top:4px;
      color:var(--muted);
      font-size:13px;
      line-height:1.5;
    }
    .status{
      display:none;
      border-radius:16px;
      padding:13px 14px;
      margin-top:14px;
      font-size:14px;
      line-height:1.45;
    }
    .status.show{display:block}
    .status.error{
      background:rgba(242,132,130,0.10);
      border:1px solid rgba(242,132,130,0.25);
      color:#ffd4d3;
    }
    .status.info{
      background:rgba(123,224,184,0.08);
      border:1px solid rgba(123,224,184,0.22);
      color:#d8fff0;
    }
    .results{
      display:grid;
      gap:16px;
    }
    .empty{
      border:1px dashed var(--line);
      border-radius:22px;
      min-height:420px;
      display:grid;
      place-items:center;
      text-align:center;
      padding:24px;
      color:var(--muted);
      background:rgba(255,255,255,0.02);
    }
    .empty strong{
      display:block;
      margin-bottom:8px;
      color:var(--text);
      font-size:19px;
    }
    .summary{
      display:grid;
      gap:12px;
      grid-template-columns:repeat(3,minmax(0,1fr));
    }
    .metric{
      border:1px solid var(--line);
      border-radius:18px;
      padding:16px;
      background:rgba(255,255,255,0.025);
    }
    .metric .k{
      display:block;
      font-family:var(--mono);
      text-transform:uppercase;
      letter-spacing:0.08em;
      font-size:11px;
      color:var(--accent-2);
      margin-bottom:8px;
    }
    .metric .v{
      font-size:22px;
      color:var(--text);
    }
    .belief,.playbook,.step-card{
      border:1px solid var(--line);
      border-radius:22px;
      padding:18px;
      background:rgba(255,255,255,0.025);
    }
    .belief h2,.playbook h2,.steps h2{
      margin:0 0 12px;
      font-size:20px;
      letter-spacing:-0.03em;
    }
    .belief p,.playbook p,.playbook li,.step-card p{
      margin:0;
      line-height:1.55;
      color:#d7e6e9;
    }
    .playbook ul{
      margin:0;
      padding-left:18px;
      display:grid;
      gap:8px;
    }
    .steps{
      display:grid;
      gap:12px;
    }
    .step-head{
      display:flex;
      justify-content:space-between;
      gap:12px;
      align-items:flex-start;
      margin-bottom:10px;
    }
    .step-head strong{
      display:block;
      font-size:17px;
      margin-bottom:4px;
    }
    .badge{
      display:inline-flex;
      align-items:center;
      border-radius:999px;
      padding:6px 10px;
      font-family:var(--mono);
      font-size:11px;
      letter-spacing:0.08em;
      text-transform:uppercase;
      background:rgba(123,224,184,0.12);
      color:var(--accent);
      white-space:nowrap;
    }
    .reason{
      margin-top:10px;
      color:var(--muted);
      font-size:14px;
    }
    .grid-2{
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:10px;
      margin-top:12px;
    }
    .mini{
      border:1px solid rgba(192,220,230,0.14);
      border-radius:16px;
      padding:12px;
      background:rgba(4,12,16,0.45);
    }
    .mini h3{
      margin:0 0 8px;
      font-size:13px;
      font-family:var(--mono);
      color:var(--accent-2);
      letter-spacing:0.08em;
      text-transform:uppercase;
    }
    .mini pre,.mini code{
      margin:0;
      font-family:var(--mono);
      white-space:pre-wrap;
      word-break:break-word;
      font-size:12px;
      line-height:1.5;
      color:#d8e6e9;
    }
    .reward-list{
      display:grid;
      gap:6px;
      margin-top:10px;
    }
    .reward-row{
      display:flex;
      justify-content:space-between;
      gap:10px;
      font-family:var(--mono);
      font-size:12px;
      color:var(--muted);
    }
    .reward-row strong{color:var(--text)}
    @media (max-width: 960px){
      .hero,.layout,.summary,.grid-2{grid-template-columns:1fr}
      .shell{padding:18px 14px 40px}
      .hero-card,.panel,.belief,.playbook,.step-card{border-radius:18px}
      h1{font-size:36px}
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-card">
        <div class="eyebrow">Labs / You Navigate</div>
        <h1>Unchained drives. <span>You navigate.</span></h1>
        <p class="sub">
          This X.com demo wraps the existing Unchained browser stack in a tiny controller loop.
          It chooses the next <code>X.com</code> research mode, gathers evidence through
          MCP-style tools, and scores each move with a toy reward critic.
        </p>
        <div class="tag-grid">
          <div class="tag">
            <strong>Base Agent</strong>
            <div>Read-only <code>cdp_navigate</code>, <code>js_eval</code>, and fallback <code>ddm</code>.</div>
          </div>
          <div class="tag">
            <strong>Controller</strong>
            <div>Small bandit-style selector over profile, mentions, topic, competitor, and finish modes.</div>
          </div>
          <div class="tag">
            <strong>Ephemeral Critic</strong>
            <div>Scores intent alignment, information gain, growth value, efficiency, and risk for a single step.</div>
          </div>
          <div class="tag">
            <strong>Scope</strong>
            <div>Research only. The demo never posts, follows, likes, or sends anything.</div>
          </div>
        </div>
      </div>
      <div class="hero-side">
        <div class="callout">
          <strong>How to use it</strong><br>
          Connect your local browser agent first, then give the demo a handle and a growth brief.
          It will open a fresh tab in your browser and run a short read-only loop.
        </div>
        <div class="callout">
          <strong>What success looks like</strong><br>
          The loop should converge toward a clearer growth hypothesis like:
          <code>reply-led growth around active topics</code> or
          <code>tighten positioning before posting more</code>.
        </div>
      </div>
    </section>

    <section class="layout">
      <div class="panel">
        <form id="demo-form">
          <label>
            <div class="label-row">
              <span>X handle</span>
              <span>required</span>
            </div>
            <input id="handle" name="handle" value="@unchained_sky" placeholder="@youraccount" autocomplete="off">
          </label>

          <label>
            <div class="label-row">
              <span>Browser profile</span>
              <span>optional</span>
            </div>
            <input id="profile" name="profile" value="Profile 5" placeholder="guest" autocomplete="off">
          </label>

          <label>
            <div class="label-row">
              <span>Growth brief</span>
              <span>required</span>
            </div>
            <textarea id="brief" name="brief" placeholder="Example: I want to grow with founders and product engineers. My posts get impressions but weak replies. I want sharper engagement, not generic growth hacks.">I want to grow with founders and product engineers. My posts get impressions but weak replies. I want sharper engagement around product strategy and shipping lessons, not generic growth advice.</textarea>
          </label>

          <label>
            <div class="label-row">
              <span>Peer or competitor handles</span>
              <span>optional</span>
            </div>
            <input id="targets" name="targets" placeholder="@lennysan, @shreyas">
          </label>

          <label>
            <div class="label-row">
              <span>Max controller steps</span>
              <span>2-5</span>
            </div>
            <select id="max-steps" name="max_steps">
              <option value="2">2</option>
              <option value="3" selected>3</option>
              <option value="4">4</option>
              <option value="5">5</option>
            </select>
          </label>

          <div class="actions">
            <button class="btn btn-primary" id="run-btn" type="submit">Run Toy Loop</button>
            <button class="btn btn-secondary" id="fill-btn" type="button">Load Example</button>
          </div>
          <div class="note">
            The demo uses your connected browser session and opens a new tab. If you leave browser profile blank it uses your default profile. It reads the visible page and returns a playbook. It does not post.
          </div>
          <div id="status" class="status"></div>
        </form>
      </div>

      <div class="results" id="results">
        <div class="empty" id="empty-state">
          <div>
            <strong>No trajectory yet</strong>
            Run the loop to see the controller scores, tool calls, reward breakdown, and final playbook.
          </div>
        </div>
      </div>
    </section>
  </div>

  <script>
    const form = document.getElementById('demo-form');
    const statusEl = document.getElementById('status');
    const runBtn = document.getElementById('run-btn');
    const fillBtn = document.getElementById('fill-btn');
    const resultsEl = document.getElementById('results');
    const emptyEl = document.getElementById('empty-state');

    function esc(value){
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function showStatus(kind, text){
      statusEl.className = 'status show ' + kind;
      statusEl.textContent = text;
    }

    function clearStatus(){
      statusEl.className = 'status';
      statusEl.textContent = '';
    }

    function renderMetric(label, value){
      return '<div class="metric"><span class="k">' + esc(label) + '</span><div class="v">' + esc(value) + '</div></div>';
    }

    function renderList(items){
      if(!items || !items.length) return '<p>None.</p>';
      return '<ul>' + items.map(item => '<li>' + esc(item) + '</li>').join('') + '</ul>';
    }

    function renderStep(step){
      const rewardRows = Object.entries(step.reward || {}).map(([key, value]) => {
        return '<div class="reward-row"><span>' + esc(key.replaceAll('_', ' ')) + '</span><strong>' + esc(Number(value).toFixed(2)) + '</strong></div>';
      }).join('');
      const toolRows = (step.tool_calls || []).map(call => {
        const payload = typeof call.input === 'string' ? call.input : JSON.stringify(call.input || {});
        return '<div class="mini"><h3>' + esc(call.name) + '</h3><pre>' + esc(payload) + '\n\n' + esc(call.output_excerpt || '') + '</pre></div>';
      }).join('');
      const evidenceRows = (step.snapshot && step.snapshot.highlights || []).map(item => '<li>' + esc(item) + '</li>').join('');
      return [
        '<div class="step-card">',
          '<div class="step-head">',
            '<div>',
              '<strong>Step ' + esc(step.step) + ': ' + esc(step.action) + '</strong>',
              '<p>' + esc(step.summary || '') + '</p>',
            '</div>',
            '<div class="badge">reward ' + esc(Number(step.total_reward || 0).toFixed(2)) + '</div>',
          '</div>',
          '<div class="reason">' + esc(step.reason || '') + '</div>',
          '<div class="grid-2">',
            '<div class="mini"><h3>Belief Before</h3><pre>' + esc(step.belief_before || '') + '</pre></div>',
            '<div class="mini"><h3>Belief After</h3><pre>' + esc(step.belief_after || '') + '</pre></div>',
          '</div>',
          '<div class="grid-2">',
            '<div class="mini"><h3>Action Score</h3><pre>' + esc(JSON.stringify(step.action_score || {}, null, 2)) + '</pre></div>',
            '<div class="mini"><h3>Evidence</h3>' + (evidenceRows ? '<ul>' + evidenceRows + '</ul>' : '<p>No highlights captured.</p>') + '</div>',
          '</div>',
          '<div class="reward-list">' + rewardRows + '</div>',
          '<div class="grid-2">' + toolRows + '</div>',
        '</div>'
      ].join('');
    }

    function renderResult(data){
      if(emptyEl) emptyEl.remove();
      const playbook = data.playbook || {};
      const trajectory = data.trajectory || [];
      const metrics = [
        renderMetric('Steps', trajectory.length),
        renderMetric('Best Reward', Number(data.best_reward || 0).toFixed(2)),
        renderMetric('Focus Terms', (data.focus_terms || []).slice(0, 4).join(', ') || 'none')
      ].join('');

      const html = [
        '<div class="summary">' + metrics + '</div>',
        '<div class="belief"><h2>Current Belief</h2><p>' + esc(data.final_belief || '') + '</p></div>',
        '<div class="playbook">',
          '<h2>Playbook</h2>',
          '<p>' + esc(playbook.north_star || '') + '</p>',
          '<div class="grid-2" style="margin-top:12px">',
            '<div class="mini"><h3>Next Moves</h3>' + renderList(playbook.next_moves || []) + '</div>',
            '<div class="mini"><h3>Evidence Digest</h3>' + renderList(playbook.evidence_digest || []) + '</div>',
          '</div>',
          '<div class="grid-2">',
            '<div class="mini"><h3>Draft Reply</h3><pre>' + esc(playbook.draft_reply || '') + '</pre></div>',
            '<div class="mini"><h3>Draft Post</h3><pre>' + esc(playbook.draft_post || '') + '</pre></div>',
          '</div>',
        '</div>',
        '<div class="steps"><h2>Controller Trajectory</h2>' + trajectory.map(renderStep).join('') + '</div>'
      ].join('');
      resultsEl.innerHTML = html;
    }

    fillBtn.addEventListener('click', () => {
      document.getElementById('handle').value = '@unchained_sky';
      document.getElementById('profile').value = 'Profile 5';
      document.getElementById('brief').value = 'I want to grow with founders and product engineers. My posts get impressions but weak replies. I want sharper engagement around product strategy and shipping lessons, not generic growth advice.';
      document.getElementById('targets').value = '@lennysan, @shreyas';
      document.getElementById('max-steps').value = '3';
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearStatus();
      runBtn.disabled = true;
      runBtn.textContent = 'Running...';
      showStatus('info', 'Opening a fresh X.com tab and running the toy controller...');
      const payload = {
        handle: document.getElementById('handle').value,
        profile: document.getElementById('profile').value,
        brief: document.getElementById('brief').value,
        targets: document.getElementById('targets').value,
        max_steps: document.getElementById('max-steps').value
      };
      try{
        const resp = await fetch('/web/labs/you-navigate/run', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if(!resp.ok){
          throw new Error(data.error || data.message || 'Request failed');
        }
        clearStatus();
        renderResult(data);
      }catch(err){
        showStatus('error', err.message || String(err));
      }finally{
        runBtn.disabled = false;
        runBtn.textContent = 'Run Toy Loop';
      }
    });
  </script>
</body>
</html>
"""

TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{2,}", re.I)
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
HANDLE_PATH_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})(?:/status/\d+)?$")
STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been",
    "being", "brief", "build", "but", "click", "com", "does", "dont", "each",
    "for", "from", "generic", "gets", "give", "grow", "growth", "have", "help",
    "here", "into", "just", "less", "like", "look", "make", "more", "need",
    "not", "only", "ours", "over", "posts", "read", "reply", "same", "scan",
    "should", "that", "them", "then", "they", "this", "through", "too",
    "want", "weak", "with", "your", "xcom",
}
ACTION_ORDER = ("profile_scan", "mention_scan", "topic_scan", "competitor_scan", "finish")
BASE_GROWTH_VALUE = {
    "profile_scan": 0.56,
    "mention_scan": 0.66,
    "topic_scan": 0.75,
    "competitor_scan": 0.64,
    "finish": 0.52,
}
X_SNAPSHOT_JS = r"""
(() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const uniq = (arr) => Array.from(new Set(arr.filter(Boolean)));
  const read = (el) => norm(el ? (el.innerText || el.textContent || '') : '');
  const lines = (el, maxLines) => uniq(read(el).split('\n').map(norm).filter(Boolean)).slice(0, maxLines);
  const collect = (sel, limit) => uniq(Array.from(document.querySelectorAll(sel)).map(read).filter(Boolean)).slice(0, limit);
  const articles = Array.from(document.querySelectorAll('article')).slice(0, 6).map((article, idx) => {
    const articleLines = lines(article, 18);
    const links = uniq(Array.from(article.querySelectorAll('a[href]')).map(a => a.getAttribute('href') || '')).slice(0, 8);
    return {
      index: idx,
      text: articleLines.join(' | ').slice(0, 1000),
      links
    };
  }).filter(item => item.text);
  const headingText = collect('h1, h2, [role="heading"]', 8);
  const profileBio = collect('[data-testid="UserDescription"], [data-testid="UserProfileHeader_Items"] span, aside span', 12).join(' | ').slice(0, 520);
  const statText = collect('a[href$="/followers"], a[href*="/verified_followers"], a[href*="/following"]', 8).join(' | ').slice(0, 260);
  const mainText = read(document.querySelector('main')).slice(0, 2200);
  const firstBody = read(document.body).slice(0, 2000);
  const pathname = location.pathname + location.search;
  let pageType = 'feed';
  if (location.pathname.startsWith('/search')) pageType = 'search';
  else if (/^\/[^\/\?]+$/.test(location.pathname) && location.pathname !== '/home' && location.pathname !== '/explore') pageType = 'profile';
  const loginGate = /sign in|log in|create account|join x/i.test(firstBody);
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    pathname,
    page_type: pageType,
    login_gate: loginGate,
    headings: headingText,
    bio: profileBio,
    stats: statText,
    article_count: articles.length,
    articles,
    text_excerpt: mainText
  });
})()
"""


@dataclass
class ActionStat:
    count: int = 0
    total_reward: float = 0.0

    @property
    def avg_reward(self) -> float:
        return self.total_reward / self.count if self.count else 0.0


@dataclass
class StepRecord:
    step: int
    action: str
    summary: str
    reason: str
    belief_before: str
    belief_after: str
    action_score: dict[str, float]
    reward: dict[str, float]
    total_reward: float
    tool_calls: list[dict[str, Any]]
    snapshot: dict[str, Any] = field(default_factory=dict)


def _normalize_handle(value: str) -> str:
    handle = (value or "").strip().lstrip("@")
    if not HANDLE_RE.fullmatch(handle):
        return ""
    return handle


def _split_handles(value: str) -> list[str]:
    items: list[str] = []
    for part in (value or "").split(","):
        handle = _normalize_handle(part)
        if handle and handle not in items:
            items.append(handle)
    return items


def _resolve_profile_auth(auth_info: dict[str, Any], profile: str) -> dict[str, Any]:
    name = (profile or "").strip()
    resolved = dict(auth_info)
    if not name or name == "default":
        resolved["profile"] = "default"
        resolved["agent_id"] = f"claude-{auth_info['key_hash']}"
        return resolved
    if not PROFILE_RE.fullmatch(name):
        raise ValueError("Profile name must be 1-32 chars of letters, numbers, underscores, or dashes.")
    resolved["profile"] = name
    resolved["agent_id"] = f"claude-{auth_info['key_hash']}-{name}"
    return resolved


def _resolve_local_profile_path(profile: str) -> str:
    raw = (profile or "").strip()
    if not raw:
        return ""
    try:
        import signup_agent
    except Exception:
        return ""

    target = raw.lower()
    for profile_info in signup_agent.list_chrome_profiles():
        candidates = {
            str(profile_info.get("dir_name", "") or "").strip(),
            str(profile_info.get("name", "") or "").strip(),
            os.path.basename(str(profile_info.get("path", "") or "").strip()),
        }
        if any(candidate and candidate.lower() == target for candidate in candidates):
            return str(profile_info.get("path", "") or "").strip()
    return ""


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    for token in TOKEN_RE.findall((text or "").lower()):
        if token in STOPWORDS or token.startswith("http"):
            continue
        out.append(token)
    return out


def _extract_focus_terms(brief: str, handle: str, competitors: list[str]) -> list[str]:
    banned = {handle.lower(), *(c.lower() for c in competitors)}
    counts = Counter(t for t in _tokenize(brief) if t not in banned)
    return [token for token, _ in counts.most_common(6)]


def _extract_handles_from_links(snapshot: dict[str, Any]) -> list[str]:
    hits: list[str] = []
    for article in snapshot.get("articles", []):
        for link in article.get("links", []):
            match = HANDLE_PATH_RE.match(link or "")
            if not match:
                continue
            handle = match.group(1)
            if handle.lower() not in {"home", "explore", "search", "messages"}:
                hits.append(handle)
    seen: list[str] = []
    for handle in hits:
        if handle not in seen:
            seen.append(handle)
    return seen[:6]


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    parts = [
        snapshot.get("title", ""),
        snapshot.get("bio", ""),
        snapshot.get("stats", ""),
        snapshot.get("text_excerpt", ""),
    ]
    for article in snapshot.get("articles", []):
        parts.append(article.get("text", ""))
    return "\n".join(p for p in parts if p)


def _parse_js_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"raw": text, "text_excerpt": text[:1800], "articles": []}


def _snapshot_highlights(snapshot: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if snapshot.get("bio"):
        out.append(snapshot["bio"][:220])
    for article in snapshot.get("articles", [])[:3]:
        text = (article.get("text") or "").strip()
        if text:
            out.append(text[:220])
    if not out and snapshot.get("text_excerpt"):
        out.append(snapshot["text_excerpt"][:220])
    return out[:4]


def _initial_belief(handle: str, brief: str, focus_terms: list[str]) -> str:
    if focus_terms:
        return (
            f"The user likely wants @{handle} to grow by finding a sharper angle around "
            f"{', '.join(focus_terms[:3])}. The first job is to figure out whether the "
            "best move is profile positioning, replies/mentions, or live topic conversations."
        )
    return (
        f"The user likely wants @{handle} to grow with more qualified engagement, but the "
        "best surface is still unclear. Start by grounding on the profile, then test where "
        "the active conversations are."
    )


def _candidate_actions(step_index: int, competitors: list[str]) -> list[str]:
    actions = ["profile_scan", "mention_scan", "topic_scan"]
    if competitors:
        actions.append("competitor_scan")
    if step_index >= 1:
        actions.append("finish")
    return actions


def _action_prior(
    action: str,
    *,
    step_index: int,
    focus_terms: list[str],
    competitors: list[str],
    last_reward: float,
    have_evidence: bool,
) -> float:
    if action == "profile_scan":
        return 0.95 if step_index == 0 else 0.18
    if action == "mention_scan":
        return 0.60 + (0.08 if step_index >= 1 else 0.0)
    if action == "topic_scan":
        return 0.76 if focus_terms else 0.40
    if action == "competitor_scan":
        return 0.62 if competitors else -1.0
    if action == "finish":
        base = 0.14 if step_index < 2 else 0.68
        if have_evidence:
            base += 0.08
        if last_reward > 0.64:
            base += 0.06
        return base
    return 0.0


def _choose_action(
    *,
    step_index: int,
    stats: dict[str, ActionStat],
    focus_terms: list[str],
    competitors: list[str],
    last_action: str,
    last_reward: float,
    have_evidence: bool,
) -> tuple[str, dict[str, dict[str, float]]]:
    candidates = _candidate_actions(step_index, competitors)
    total_rounds = sum(stat.count for stat in stats.values())
    debug: dict[str, dict[str, float]] = {}
    best_name = candidates[0]
    best_score = -999.0
    for name in candidates:
        stat = stats[name]
        prior = _action_prior(
            name,
            step_index=step_index,
            focus_terms=focus_terms,
            competitors=competitors,
            last_reward=last_reward,
            have_evidence=have_evidence,
        )
        explore = 0.34 * math.sqrt(math.log(total_rounds + 2.0) / (stat.count + 1.0))
        repeat_penalty = 0.18 if last_action == name else 0.0
        revisit_penalty = 0.20 if stat.count and name not in {"topic_scan", "finish"} else 0.0
        score = prior + stat.avg_reward + explore - repeat_penalty - revisit_penalty
        debug[name] = {
            "score": round(score, 4),
            "prior": round(prior, 4),
            "avg_reward": round(stat.avg_reward, 4),
            "explore": round(explore, 4),
            "repeat_penalty": round(repeat_penalty + revisit_penalty, 4),
        }
        if score > best_score:
            best_name = name
            best_score = score
    return best_name, debug


async def _tool_call(
    auth_info: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    name: str,
    **kwargs,
) -> str:
    agent_id = auth_info["agent_id"]
    tab_id = kwargs.get("tab_id", "auto")
    if name == "cdp_navigate":
        output = await cloud_tools.navigate(agent_id, tab_id, kwargs["url"])
    elif name == "js_eval":
        output = await cloud_tools.run_js(agent_id, tab_id, kwargs["expression"])
    elif name == "ddm":
        output = await cloud_tools.run_ddm(agent_id, tab_id, kwargs["flags"])
    else:
        raise ValueError(f"Unsupported tool: {name}")
    tool_calls.append(
        {
            "name": name,
            "input": kwargs,
            "output_excerpt": (output or "")[:420],
        }
    )
    return output


async def _load_snapshot(
    auth_info: dict[str, Any],
    *,
    tab_id: str,
    url: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    await _tool_call(auth_info, tool_calls, "cdp_navigate", url=url, tab_id=tab_id)
    await asyncio.sleep(1.4)
    raw = await _tool_call(auth_info, tool_calls, "js_eval", expression=X_SNAPSHOT_JS, tab_id=tab_id)
    snapshot = _parse_js_json(raw)
    if snapshot.get("article_count") or snapshot.get("login_gate"):
        snapshot["highlights"] = _snapshot_highlights(snapshot)
        return snapshot

    ddm_text = await _tool_call(
        auth_info,
        tool_calls,
        "ddm",
        flags=["--text", "--max", "1600"],
        tab_id=tab_id,
    )
    if ddm_text and not snapshot.get("text_excerpt"):
        snapshot["text_excerpt"] = ddm_text[:1600]
    snapshot["highlights"] = _snapshot_highlights(snapshot)
    return snapshot


async def _run_action(
    action: str,
    *,
    auth_info: dict[str, Any],
    tab_id: str,
    handle: str,
    competitors: list[str],
    focus_terms: list[str],
    step_index: int,
    competitor_cursor: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    tool_calls: list[dict[str, Any]] = []
    if action == "profile_scan":
        url = f"https://x.com/{handle}"
        snapshot = await _load_snapshot(auth_info, tab_id=tab_id, url=url, tool_calls=tool_calls)
        summary = f"Scanned @{handle}'s profile to ground the account voice and recent visible posts."
        return snapshot, tool_calls, summary

    if action == "mention_scan":
        query = quote_plus(f"@{handle}")
        url = f"https://x.com/search?q={query}&src=typed_query&f=live"
        snapshot = await _load_snapshot(auth_info, tab_id=tab_id, url=url, tool_calls=tool_calls)
        summary = f"Searched live mentions for @{handle} to see where the account is already getting attention."
        return snapshot, tool_calls, summary

    if action == "topic_scan":
        topic = " ".join(focus_terms[:2]) if focus_terms else handle
        query = quote_plus(topic)
        url = f"https://x.com/search?q={query}&src=typed_query&f=live"
        snapshot = await _load_snapshot(auth_info, tab_id=tab_id, url=url, tool_calls=tool_calls)
        summary = f"Searched live conversation around '{topic}' to locate current engagement surfaces."
        return snapshot, tool_calls, summary

    if action == "competitor_scan":
        target = competitors[competitor_cursor % len(competitors)]
        url = f"https://x.com/{target}"
        snapshot = await _load_snapshot(auth_info, tab_id=tab_id, url=url, tool_calls=tool_calls)
        summary = f"Reviewed @{target}'s profile for adjacent conversation patterns and positioning cues."
        snapshot["competitor_target"] = target
        return snapshot, tool_calls, summary

    return {"page_type": "finish", "highlights": []}, tool_calls, "Stopped browsing and consolidated the current belief into a playbook."


def _score_snapshot(
    *,
    action: str,
    snapshot: dict[str, Any],
    focus_terms: list[str],
    seen_tokens: set[str],
) -> tuple[dict[str, float], str]:
    text = _snapshot_text(snapshot)
    tokens = set(_tokenize(text))
    focus_hits = [term for term in focus_terms if term in tokens]
    overlap_target = max(1, min(4, len(focus_terms))) if focus_terms else 1
    intent_alignment = (
        min(1.0, len(focus_hits) / overlap_target)
        if focus_terms
        else min(1.0, len(tokens) / 24.0)
    )

    new_tokens = tokens - seen_tokens
    article_count = int(snapshot.get("article_count") or len(snapshot.get("articles", [])) or 0)
    information_gain = min(1.0, len(new_tokens) / 18.0)
    if article_count:
        information_gain = min(1.0, information_gain + 0.18)

    growth_value = BASE_GROWTH_VALUE.get(action, 0.55)
    if action == "topic_scan" and focus_hits:
        growth_value += 0.12
    if action == "mention_scan" and article_count >= 2:
        growth_value += 0.10
    if action == "competitor_scan" and article_count >= 2:
        growth_value += 0.06
    growth_value = min(1.0, growth_value)

    if snapshot.get("login_gate"):
        efficiency = 0.08
    elif article_count >= 3:
        efficiency = 0.95
    elif article_count >= 1 or snapshot.get("text_excerpt"):
        efficiency = 0.62
    else:
        efficiency = 0.24

    risk = 0.06
    if snapshot.get("login_gate"):
        risk = 0.76
    elif "something went wrong" in text.lower() or "try reloading" in text.lower():
        risk = 0.38
    elif article_count == 0 and not snapshot.get("text_excerpt"):
        risk = 0.30

    total = (
        0.34 * intent_alignment
        + 0.24 * information_gain
        + 0.26 * growth_value
        + 0.16 * efficiency
        - 0.22 * risk
    )
    total = round(max(-1.0, min(1.0, total)), 4)
    reward = {
        "intent_alignment": round(intent_alignment, 4),
        "information_gain": round(information_gain, 4),
        "growth_value": round(growth_value, 4),
        "efficiency": round(efficiency, 4),
        "risk": round(risk, 4),
    }
    if snapshot.get("login_gate"):
        reason = "X appears to be behind a login gate in this browser tab, so the step had low efficiency and high risk."
    elif focus_hits:
        reason = f"The step surfaced direct overlap with the growth brief around {', '.join(focus_hits[:3])}."
    elif article_count >= 3:
        reason = "The step surfaced enough live conversation to improve the growth hypothesis, even without strong keyword overlap."
    else:
        reason = "The step gathered only light evidence, so the controller should keep exploring."
    return reward | {"total": total}, reason


def _belief_from_state(
    *,
    handle: str,
    best_action: str,
    top_terms: list[str],
    best_reward: float,
    handles_seen: list[str],
) -> str:
    if best_action == "topic_scan" and top_terms:
        return (
            f"Current belief: @{handle} should grow by joining live conversations around "
            f"{', '.join(top_terms[:3])}, then echoing that angle in its own post."
        )
    if best_action == "mention_scan":
        return (
            f"Current belief: the best near-term growth surface for @{handle} is replies and mentions. "
            "Lean into active conversations instead of posting into empty air."
        )
    if best_action == "competitor_scan" and handles_seen:
        return (
            f"Current belief: adjacent accounts like @{handles_seen[0]} reveal stronger conversation entry points "
            f"than @{handle}'s current posting pattern."
        )
    if best_reward < 0.30:
        return (
            f"Current belief: the browser session needs better X visibility before the system can infer a strong "
            f"growth direction for @{handle}."
        )
    return (
        f"Current belief: @{handle} needs tighter positioning and clearer hooks before posting more often."
    )


def _build_playbook(
    *,
    handle: str,
    brief: str,
    focus_terms: list[str],
    best_action: str,
    evidence_digest: list[str],
    visible_handles: list[str],
    final_belief: str,
    best_reward: float,
) -> dict[str, Any]:
    topic = focus_terms[0] if focus_terms else "your sharpest operating insight"
    if best_action == "topic_scan":
        north_star = (
            f"Use live {topic} conversations as the main acquisition surface. Reply where the discussion is already hot, "
            "then post a follow-up that reframes the same angle in your own voice."
        )
        next_moves = [
            f"Reply to 3 live threads about {topic} with a concrete, non-generic point.",
            f"Post one follow-up that turns your strongest reply about {topic} into a standalone stance.",
            "Track which thread format earns the most qualified replies, not just impressions.",
        ]
    elif best_action == "mention_scan":
        north_star = (
            "Treat mentions and replies as the primary growth loop. The fastest lift will come from engaging where the account "
            "already has context instead of publishing more generic top-level posts."
        )
        next_moves = [
            "Answer mentions with sharper opinions and one concrete example.",
            "Convert the strongest mention thread into a follow-up post within the same day.",
            "Flag recurring question patterns and turn them into a weekly content spine.",
        ]
    elif best_action == "competitor_scan" and visible_handles:
        north_star = (
            f"Borrow the conversation surfaces already working for adjacent accounts like @{visible_handles[0]}, but translate "
            f"them into @{handle}'s own voice and domain expertise."
        )
        next_moves = [
            f"Watch how @{visible_handles[0]} enters active threads, then use the same entry points with a more specific take.",
            "Focus on where peers earn replies, not on copying their exact post format.",
            "Turn one observed competitor angle into an original thread or note with stronger specificity.",
        ]
    elif best_reward < 0.30:
        north_star = (
            "Get the browser into a healthy logged-in X session first. Without that, the controller cannot collect enough evidence "
            "to steer growth well."
        )
        next_moves = [
            "Confirm the local browser agent is online and X is logged in.",
            "Re-run the demo from a visible X profile or search page.",
            "Then compare profile, mentions, and topic surfaces again.",
        ]
    else:
        north_star = (
            f"Tighten @{handle}'s positioning, then use {topic} as the recurring hook. The account likely needs a clearer point of view "
            "before more posting volume helps."
        )
        next_moves = [
            "Sharpen the bio and opening profile framing so visitors understand the niche immediately.",
            f"Pick one repeatable theme around {topic} and reuse it across replies and posts for a week.",
            "Favor sharper opinions and specific lessons over generic growth language.",
        ]

    visible_handles = [h for h in visible_handles if h.lower() != handle.lower()]
    draft_reply = (
        f"My take on {topic}: the generic advice misses the actual tradeoff. "
        f"The useful move is being more specific about what changed, what failed, and what you'd do differently next time."
    )
    draft_post = (
        f"If you want to grow on X in {topic}, stop trying to sound universally right. "
        f"Pick one concrete belief, back it with something you've seen firsthand, and let the replies sharpen the rest."
    )
    return {
        "north_star": north_star,
        "next_moves": next_moves,
        "evidence_digest": evidence_digest[:4] or [final_belief],
        "draft_reply": draft_reply,
        "draft_post": draft_post,
        "watch_handles": visible_handles[:3],
        "brief": brief[:240],
    }


async def _run_demo(
    *,
    auth_info: dict[str, Any],
    handle: str,
    brief: str,
    competitors: list[str],
    max_steps: int,
    profile_path: str = "",
) -> dict[str, Any]:
    focus_terms = _extract_focus_terms(brief, handle, competitors)
    if profile_path:
        launch = await cloud_tools.provision_launch(auth_info["agent_id"], profile_path)
        tab_id = str((launch or {}).get("tab_id", "")).strip()
        if not tab_id:
            raise RuntimeError(f"Could not launch a provisioned browser tab for {os.path.basename(profile_path)}.")
    else:
        tab_id = await cloud_tools.create_tab(auth_info["agent_id"], "about:blank")
        if not tab_id:
            raise RuntimeError("Could not create a fresh browser tab for the demo.")
    seen_tokens: set[str] = set()
    stats = {name: ActionStat() for name in ACTION_ORDER}
    best_action = "profile_scan"
    best_reward = -1.0
    evidence_digest: list[str] = []
    visible_handles: list[str] = []
    belief = _initial_belief(handle, brief, focus_terms)
    competitor_cursor = 0
    last_action = ""
    last_reward = 0.0
    trajectory: list[StepRecord] = []

    for step_index in range(max_steps):
        action, debug_scores = _choose_action(
            step_index=step_index,
            stats=stats,
            focus_terms=focus_terms,
            competitors=competitors,
            last_action=last_action,
            last_reward=last_reward,
            have_evidence=bool(evidence_digest),
        )
        belief_before = belief
        snapshot, tool_calls, summary = await _run_action(
            action,
            auth_info=auth_info,
            tab_id=tab_id,
            handle=handle,
            competitors=competitors,
            focus_terms=focus_terms,
            step_index=step_index,
            competitor_cursor=competitor_cursor,
        )
        if action == "competitor_scan" and competitors:
            competitor_cursor += 1

        reward, reason = _score_snapshot(
            action=action,
            snapshot=snapshot,
            focus_terms=focus_terms,
            seen_tokens=seen_tokens,
        )
        stats[action].count += 1
        stats[action].total_reward += reward["total"]
        last_action = action
        last_reward = reward["total"]

        snapshot_tokens = set(_tokenize(_snapshot_text(snapshot)))
        seen_tokens |= snapshot_tokens
        handles_seen = _extract_handles_from_links(snapshot)
        for seen in handles_seen:
            if seen not in visible_handles:
                visible_handles.append(seen)
        if snapshot.get("highlights"):
            evidence_digest.extend(snapshot["highlights"])

        keyword_counts = Counter(t for t in seen_tokens if t not in STOPWORDS)
        merged_terms = [term for term, _ in keyword_counts.most_common(6)]
        if reward["total"] > best_reward:
            best_reward = reward["total"]
            best_action = action

        belief = _belief_from_state(
            handle=handle,
            best_action=best_action,
            top_terms=merged_terms or focus_terms,
            best_reward=best_reward,
            handles_seen=visible_handles,
        )
        trajectory.append(
            StepRecord(
                step=step_index + 1,
                action=action,
                summary=summary,
                reason=reason,
                belief_before=belief_before,
                belief_after=belief,
                action_score=debug_scores.get(action, {}),
                reward={k: v for k, v in reward.items() if k != "total"},
                total_reward=reward["total"],
                tool_calls=tool_calls,
                snapshot={
                    "url": snapshot.get("url", ""),
                    "page_type": snapshot.get("page_type", ""),
                    "highlights": snapshot.get("highlights", []),
                },
            )
        )
        if action == "finish":
            break
        if step_index >= 1 and reward["total"] > 0.70:
            break

    final_terms = _extract_focus_terms(" ".join(evidence_digest) + " " + brief, handle, competitors)
    if final_terms:
        focus_terms = final_terms
    final_belief = _belief_from_state(
        handle=handle,
        best_action=best_action,
        top_terms=focus_terms,
        best_reward=best_reward,
        handles_seen=visible_handles,
    )
    playbook = _build_playbook(
        handle=handle,
        brief=brief,
        focus_terms=focus_terms,
        best_action=best_action,
        evidence_digest=evidence_digest,
        visible_handles=visible_handles,
        final_belief=final_belief,
        best_reward=best_reward,
    )
    return {
        "ok": True,
        "tab_id": tab_id,
        "focus_terms": focus_terms,
        "best_action": best_action,
        "best_reward": round(best_reward, 4),
        "final_belief": final_belief,
        "trajectory": [asdict(step) for step in trajectory],
        "playbook": playbook,
    }


async def handle_x_manager_demo_page(request: web.Request) -> web.Response:
    """Serve the "Unchained drives. You navigate." X.com demo page."""
    core = _core()
    auth_info = _is_admin(request)
    if auth_info is None:
        raise web.HTTPNotFound()
    core._track_page_view(request, auth_info=auth_info)
    return web.Response(text=PAGE_HTML, content_type="text/html")


async def handle_x_manager_demo_run(request: web.Request) -> web.Response:
    """Run the "Unchained drives. You navigate." X.com controller loop."""
    core = _core()
    auth_info = _is_admin(request)
    if auth_info is None:
        return web.json_response({"error": "Not found."}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    handle = _normalize_handle(str(body.get("handle", "")))
    profile = str(body.get("profile", "")).strip()
    brief = str(body.get("brief", "")).strip()
    competitors = _split_handles(str(body.get("targets", "")))
    profile_path = _resolve_local_profile_path(profile)
    try:
        max_steps = int(body.get("max_steps", 3))
    except Exception:
        max_steps = 3
    max_steps = max(2, min(5, max_steps))

    if not handle:
        return web.json_response({"error": "A valid X handle is required."}, status=400)
    if not brief:
        return web.json_response({"error": "A growth brief is required."}, status=400)

    try:
        scoped_auth_info = _resolve_profile_auth(auth_info, "" if profile_path else profile)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    try:
        result = await _run_demo(
            auth_info=scoped_auth_info,
            handle=handle,
            brief=brief[:900],
            competitors=competitors,
            max_steps=max_steps,
            profile_path=profile_path,
        )
    except Exception as exc:
        if is_chrome_unavailable_error(exc):
            return web.json_response(
                {
                    "error": (
                        "Your local browser agent appears offline. Start the Unchained local agent, "
                        "confirm the browser bridge is connected, then rerun the demo."
                    )
                },
                status=409,
            )
        return web.json_response({"error": f"Demo failed: {exc}"}, status=500)

    return web.json_response(result)
