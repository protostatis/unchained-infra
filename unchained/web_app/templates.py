"""HTML template constants extracted from web.py."""

from __future__ import annotations

BRANDED_TAB_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>UnchainedSky</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root{
  --bg:#07111b;--bg2:#0e1b2d;--panel:rgba(9,16,28,0.82);
  --line:rgba(148,163,184,0.12);--text:#eef2ff;--muted:#9fb0c8;--accent:#e94560;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;
  min-height:100vh;
  display:grid;
  place-items:center;
  overflow:hidden;
  color:var(--text);
  background:
    radial-gradient(circle at top, rgba(233,69,96,0.16), transparent 34%),
    radial-gradient(circle at 80% 20%, rgba(59,130,246,0.16), transparent 28%),
    linear-gradient(160deg, var(--bg), var(--bg2));
  font-family:"Avenir Next","Segoe UI",sans-serif;
}
body::before{
  content:"";
  position:fixed;
  inset:0;
  background-image:
    linear-gradient(var(--line) 1px, transparent 1px),
    linear-gradient(90deg, var(--line) 1px, transparent 1px);
  background-size:56px 56px;
  mask-image:linear-gradient(to bottom, rgba(0,0,0,0.75), transparent 82%);
  pointer-events:none;
}
.shell{
  position:relative;
  width:min(940px, calc(100vw - 32px));
  padding:34px 40px 38px;
  border:1px solid rgba(255,255,255,0.1);
  border-radius:28px;
  background:var(--panel);
  backdrop-filter:blur(18px);
  box-shadow:0 24px 80px rgba(0,0,0,0.38);
}
.eyebrow{
  display:inline-flex;
  align-items:center;
  gap:10px;
  margin-bottom:20px;
  color:var(--muted);
  font-size:12px;
  font-weight:600;
  letter-spacing:0.18em;
  text-transform:uppercase;
}
.eyebrow::before{
  content:"";
  width:34px;
  height:1px;
  background:linear-gradient(90deg, transparent, var(--accent));
}
h1{
  margin:0;
  font-size:clamp(42px, 8vw, 104px);
  letter-spacing:0.04em;
  line-height:0.94;
  text-transform:uppercase;
}
h1 span{color:var(--accent)}
p{
  margin:14px 0 0;
  max-width:32ch;
  color:var(--muted);
  font-size:16px;
  line-height:1.6;
}
.status{
  margin-top:22px;
  display:inline-block;
  padding:8px 12px;
  border-radius:999px;
  border:1px solid rgba(233,69,96,0.28);
  background:rgba(233,69,96,0.08);
  color:#ffd7df;
  font-size:12px;
  letter-spacing:0.08em;
  text-transform:uppercase;
}
@media (max-width: 640px){
  .shell{padding:24px 22px 24px;border-radius:22px}
  h1{font-size:clamp(34px, 15vw, 62px);letter-spacing:0.03em}
}
</style>
</head>
<body data-unchained-tab="brand-default">
  <main class="shell" aria-label="UnchainedSky default tab">
    <div class="eyebrow">Local browser agent</div>
    <h1>Unchained<span>Sky</span></h1>
    <p>Your browser is connected. Open a destination or let the agent navigate from here.</p>
    <div class="status">Ready for navigation</div>
  </main>
</body>
</html>
"""

LANDING_V1_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{margin:0;padding:0;box-sizing:border-box}
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&family=Cormorant+Garamond:ital,wght@0,400;1,400&display=swap');
:root{
  --bg:#0a0a0f;--surface:#111119;--subtle:#222;
  --accent:#e94560;--accent-glow:rgba(233,69,96,0.15);
  --text:#e8e8ec;--muted:#666;
}
body{
  font-family:'Inter',sans-serif;
  background:var(--bg);color:var(--text);
  overflow-x:hidden;
}

/* Subtle grid background */
body::before{
  content:'';position:fixed;inset:0;
  background-image:
    linear-gradient(rgba(233,69,96,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(233,69,96,0.03) 1px, transparent 1px);
  background-size:60px 60px;
  pointer-events:none;z-index:0;
}

/* ── Hero ── */
.hero{
  position:relative;z-index:1;
  max-width:800px;margin:0 auto;padding:0 24px;
  display:flex;flex-direction:column;align-items:center;
  height:100vh;justify-content:center;
}

.wordmark{
  font-size:clamp(48px,10vw,72px);
  font-weight:600;letter-spacing:4px;
  color:var(--text);margin-bottom:48px;
  text-transform:uppercase;
}
.wordmark span{color:var(--accent)}

.poem{
  font-family:'Cormorant Garamond',serif;
  font-size:clamp(20px,4vw,28px);
  line-height:1.8;text-align:center;
  font-style:italic;color:var(--text);
  opacity:0.9;margin-bottom:64px;
  max-width:500px;
}
.poem .line{
  display:block;
  animation:fadeIn 1s ease-out both;
}
.poem .line:nth-child(1){animation-delay:0.3s}
.poem .line:nth-child(2){animation-delay:0.9s}
.poem .line:nth-child(3){animation-delay:1.5s}

@keyframes fadeIn{
  from{opacity:0;transform:translateY(12px)}
  to{opacity:1;transform:translateY(0)}
}

.cta{
  display:inline-flex;align-items:center;gap:10px;
  padding:14px 32px;border:1px solid var(--accent);
  border-radius:8px;color:var(--accent);
  font-size:15px;font-weight:500;letter-spacing:1px;
  text-decoration:none;text-transform:uppercase;
  transition:all 0.3s ease;
  animation:fadeIn 1s ease-out 2.2s both;
}
.cta:hover{
  background:var(--accent);color:#fff;
  box-shadow:0 0 30px var(--accent-glow);
}

.tagline{
  margin-top:80px;font-size:13px;
  color:var(--muted);letter-spacing:2px;
  text-transform:uppercase;
  animation:fadeIn 1s ease-out 2.6s both;
}

.scroll-hint{
  position:absolute;bottom:28px;left:50%;transform:translateX(-50%);
  display:flex;flex-direction:column;align-items:center;gap:4px;
  color:var(--muted);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;
  animation:fadeInCenter 1s ease-out 3.2s both;cursor:pointer;
}
@keyframes fadeInCenter{
  from{opacity:0;transform:translateX(-50%) translateY(12px)}
  to{opacity:1;transform:translateX(-50%) translateY(0)}
}
.scroll-hint span{animation:bounce 2s ease-in-out infinite;font-size:16px;line-height:1}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(5px)}}

/* ── Get Started ── */
.getstarted{
  position:relative;z-index:1;
  max-width:900px;margin:0 auto;
  padding:80px 24px 120px;
}
.gs-header{
  text-align:center;margin-bottom:56px;
}
.gs-header h2{
  font-size:clamp(22px,4vw,32px);font-weight:600;
  letter-spacing:2px;text-transform:uppercase;margin-bottom:16px;
}
.gs-header p{
  color:var(--muted);font-size:15px;line-height:1.7;max-width:480px;margin:0 auto;
}

.section-label{
  grid-column:1/-1;
  font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;
  color:var(--muted);margin-bottom:-8px;margin-top:8px;padding-bottom:8px;
  border-bottom:1px solid var(--subtle);
}
.section-label:first-child{margin-top:0}

.cards{
  display:grid;grid-template-columns:1fr 1fr;gap:24px;
}
@media(max-width:640px){.cards{grid-template-columns:1fr}}
.sdk-row{
  grid-column:1/-1;
  display:grid;grid-template-columns:repeat(3,1fr);gap:24px;
}
@media(max-width:800px){.sdk-row{grid-template-columns:1fr}}

.card{
  background:var(--surface);border:1px solid var(--subtle);
  border-radius:16px;padding:32px;
  display:flex;flex-direction:column;gap:16px;
  transition:border-color 0.3s;
}
.card:hover{border-color:#444}

.card-badge{
  display:inline-flex;align-items:center;gap:6px;
  font-size:11px;font-weight:600;letter-spacing:1.5px;
  text-transform:uppercase;padding:4px 10px;border-radius:4px;
  width:fit-content;
}
.card.demo .card-badge,.card.trial .card-badge{
  background:rgba(233,69,96,0.12);color:var(--accent);
  border:1px solid rgba(233,69,96,0.25);
}
.card.dev .card-badge,.card.local .card-badge{
  background:rgba(100,180,255,0.08);color:#64b4ff;
  border:1px solid rgba(100,180,255,0.2);
}
.card.setup .card-badge,.card.sdk .card-badge{
  background:rgba(251,191,36,0.1);color:#fbbf24;
  border:1px solid rgba(251,191,36,0.2);
}
.card.codex .card-badge{
  background:rgba(20,184,166,0.12);color:#2dd4bf;
  border:1px solid rgba(45,212,191,0.25);
}

.card-title{font-size:20px;font-weight:600}
.card-desc{color:var(--muted);font-size:14px;line-height:1.7;flex:1}
.card-note{
  font-size:12px;line-height:1.6;padding:10px 12px;border-radius:8px;
  border:1px solid #3a2a2a;background:rgba(233,69,96,0.08);color:#f2c4cc;
}

/* Requirements pills */
.card-reqs{
  display:flex;flex-wrap:wrap;gap:6px;
}
.req{
  font-size:11px;padding:3px 9px;border-radius:12px;
  background:rgba(255,255,255,0.06);border:1px solid #333;
  color:#999;font-family:var(--mono,monospace);
}
.req-none{font-style:italic;color:#555}

.card-steps{
  display:flex;flex-direction:column;gap:10px;
  border-top:1px solid var(--subtle);padding-top:20px;
}
.step{
  display:flex;align-items:center;gap:10px;
  font-size:13px;color:#aaa;
}
.step-num{
  width:20px;height:20px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:600;flex-shrink:0;
}
.card.demo .step-num,.card.trial .step-num{background:rgba(233,69,96,0.15);color:var(--accent)}
.card.dev .step-num,.card.local .step-num{background:rgba(100,180,255,0.1);color:#64b4ff}
.card.setup .step-num,.card.sdk .step-num{background:rgba(251,191,36,0.1);color:#fbbf24}
.card.codex .step-num{background:rgba(45,212,191,0.12);color:#2dd4bf}

/* -- More Options Toggle -- */
.more-toggle-btn{
  display:flex;align-items:center;justify-content:center;gap:8px;
  margin:32px auto 0;padding:8px 0;
  background:none;border:none;cursor:pointer;
  font-size:14px;color:var(--muted);letter-spacing:0.5px;
  transition:color 0.2s;
}
.more-toggle-btn:hover{color:var(--text)}
.more-toggle-btn .arrow{
  display:inline-block;transition:transform 0.3s;font-size:12px;
}
.more-toggle-btn.expanded .arrow{transform:rotate(180deg)}
.more-cards-wrap{
  max-height:0;overflow:hidden;
  transition:max-height 0.4s ease;
}

.card-btn{
  display:inline-flex;align-items:center;justify-content:center;
  padding:11px 24px;border-radius:8px;
  font-size:14px;font-weight:500;letter-spacing:0.5px;
  text-decoration:none;transition:all 0.2s;margin-top:4px;
}
.card.demo .card-btn,.card.trial .card-btn{
  background:var(--accent);color:#fff;border:1px solid var(--accent);
}
.card.demo .card-btn:hover,.card.trial .card-btn:hover{box-shadow:0 0 20px var(--accent-glow);opacity:0.9}
.card.dev .card-btn,.card.local .card-btn{
  background:transparent;color:#aaa;border:1px solid #444;
}
.card.dev .card-btn:hover,.card.local .card-btn:hover{border-color:#666;color:var(--text)}
.card.setup .card-btn,.card.sdk .card-btn{
  background:#fbbf24;color:#1a1a2e;border:1px solid #fbbf24;font-weight:600;
}
.card.setup .card-btn:hover,.card.sdk .card-btn:hover{box-shadow:0 0 20px rgba(251,191,36,0.3);opacity:0.9}
.card.sdk .card-btn-secondary{
  background:transparent;color:#fbbf24;border:1px solid rgba(251,191,36,0.4);font-weight:500;
}
.card.sdk .card-btn-secondary:hover{border-color:#fbbf24;box-shadow:none}
.card.codex .card-btn{
  background:#0f766e;color:#e6fffb;border:1px solid #14b8a6;font-weight:600;
}
.card.codex .card-btn:hover{box-shadow:0 0 20px rgba(45,212,191,0.25);opacity:0.9}

/* ── Mock interaction ── */
.mock-section{
  position:relative;z-index:1;
  max-width:680px;margin:0 auto;padding:0 24px 80px;
  display:flex;flex-direction:column;align-items:center;
}
.mock-header{text-align:center;margin-bottom:32px}
.mock-header h2{
  font-size:clamp(20px,3.5vw,28px);font-weight:600;
  letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;
}
.mock-header p{color:var(--muted);font-size:14px;line-height:1.6}
.mock-chat{
  width:100%;
  background:#1a1a2e;border:1px solid var(--subtle);
  border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:10px;
  min-height:320px;overflow:hidden;
  font-family:-apple-system,system-ui,sans-serif;
  text-align:left;
}
/* Reuse real chat bubble styles inline for the mock */
.mock-chat .bubble{
  max-width:85%;padding:10px 14px;border-radius:12px;
  font-size:14px;line-height:1.5;word-break:break-word;
  opacity:0;transform:translateY(6px);transition:opacity 0.35s,transform 0.35s;
}
.mock-chat .bubble.show{opacity:1;transform:translateY(0)}
.mock-chat .bubble.user{
  align-self:flex-end;background:#2a1a3e;border:1px solid #3a2a5e;
  border-bottom-right-radius:4px;color:#eee;
}
.mock-chat .bubble.asst{
  align-self:flex-start;background:#1e2a3e;border:1px solid #2a3a5e;
  border-bottom-left-radius:4px;color:#eee;
}
.mock-chat .bubble.asst p{margin:0.4em 0}
.mock-chat .bubble.asst p:first-child{margin-top:0}
.mock-chat .bubble.asst p:last-child{margin-bottom:0}
.mock-chat .bubble.asst strong{font-weight:600}
.mock-chat .bubble.asst a{color:var(--accent);text-decoration:underline}
/* Reuse real action-group styles */
.mock-chat .action-group{
  margin:6px 0;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;
  font-size:12px;font-family:'SF Mono','Menlo','Monaco','Consolas',monospace;
  opacity:0;transform:translateY(6px);transition:opacity 0.35s,transform 0.35s;
}
.mock-chat .action-group.show{opacity:1;transform:translateY(0)}
.mock-chat .ag-header{
  display:flex;align-items:center;gap:8px;padding:7px 12px;
  background:#111;border-bottom:1px solid #1e1e1e;
}
.mock-chat .ag-emoji{font-size:14px;flex-shrink:0}
.mock-chat .ag-site{color:var(--accent);font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mock-chat .ag-count{color:var(--muted);font-size:10px;flex-shrink:0}
.mock-chat .ag-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.mock-chat .ag-dot.done{color:#4ade80}
.mock-chat .ag-steps{padding:2px 0 4px}
.mock-chat .action-step{
  display:flex;align-items:center;gap:7px;padding:3px 12px 3px 28px;
}
.mock-chat .as-emoji{font-size:11px;flex-shrink:0;width:16px;text-align:center}
.mock-chat .as-label{color:#eee;font-size:11px;flex-shrink:0}
.mock-chat .as-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.mock-chat .as-dot{flex-shrink:0;font-size:10px}
.mock-chat .as-dot.done{color:#4ade80}
@keyframes mockPulse{0%,100%{opacity:1}50%{opacity:0.4}}
.mock-chat .as-dot.running{color:var(--muted);animation:mockPulse 1.2s ease-in-out infinite}
.mock-chat .ag-dot.running{color:var(--muted);animation:mockPulse 1.2s ease-in-out infinite}
.mock-chat .as-screenshot{padding:4px 12px 6px 28px}
.mock-chat .as-screenshot img{max-width:100%;border-radius:4px;border:1px solid #2a2a2a}
.mock-cta{
  display:inline-flex;align-items:center;gap:8px;
  padding:12px 28px;border-radius:8px;
  background:var(--accent);color:#fff;font-size:14px;font-weight:600;
  text-decoration:none;letter-spacing:0.5px;transition:all 0.2s;
  margin-top:24px;cursor:pointer;
}
.mock-cta:hover{opacity:0.9;box-shadow:0 0 20px var(--accent-glow)}
.mock-replay{
  display:none;align-items:center;gap:8px;
  padding:10px 24px;border-radius:8px;cursor:pointer;
  background:transparent;color:var(--accent);font-size:14px;font-weight:600;
  border:1px solid var(--accent);letter-spacing:0.5px;transition:all 0.2s;
  margin-top:12px;
}
.mock-replay:hover{background:var(--accent);color:#fff;box-shadow:0 0 20px var(--accent-glow)}
.mock-replay.visible{display:inline-flex}
.mock-chat .bubble.asst table{
  width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:13px;
}
.mock-chat .bubble.asst table th{
  text-align:left;padding:6px 8px;border-bottom:2px solid #3a4a6e;color:var(--accent);font-weight:600;
}
.mock-chat .bubble.asst table td{
  padding:5px 8px;border-bottom:1px solid #2a3a5e;
}
.mock-chat .bubble.asst table tr:last-child td{border-bottom:none}
.mock-chat .bubble.asst .best-price{color:#4ade80;font-weight:700}

/* ── Footer ── */
.footer{
  position:relative;z-index:1;
  text-align:center;padding:0 24px 48px;
  color:var(--muted);font-size:12px;letter-spacing:1px;
}
.footer-links{
  display:flex;gap:24px;justify-content:center;margin-bottom:16px;
}
.footer-links a{
  color:var(--muted);text-decoration:none;font-size:13px;letter-spacing:0.5px;
  transition:color 0.15s;
}
.footer-links a:hover{color:var(--accent)}
</style>
</head>
<body>

<!-- Hero -->
<div class="hero">
  <div class="wordmark">Un<span>chain</span>ed</div>
  <div class="poem">
    <span class="line">Chains fall from my wrists</span>
    <span class="line">Wind rushes where walls once stood</span>
    <span class="line">I am sky, unchained</span>
  </div>
  <a href="/demo" class="cta">Try it free &rarr;</a>
  <div class="tagline">Your browser. Your data. No walls.</div>
  <div class="scroll-hint" onclick="document.querySelector('.mock-section').scrollIntoView({behavior:'smooth'})">
    <span>&#8595;</span>
    watch it work
  </div>
</div>

<!-- Watch it work -->
<div class="mock-section" id="mock-section">
  <div class="mock-header">
    <h2>Watch it work</h2>
    <p>See the agent search multiple sites, compare results, and deliver structured answers &mdash; in real time.</p>
  </div>
  <div class="mock-chat" id="mock-chat"></div>
  <a href="/demo" class="mock-cta">Try it yourself &rarr;</a>
  <button class="mock-replay" id="mock-replay" onclick="playMock()">&#8635; Replay demo</button>
</div>

<!-- Get Started -->
<div class="getstarted">
  <div class="gs-header">
    <h2>Get Started</h2>
    <p>No API key? Start free in 30 seconds. Have an API key? Provision once and get full model power.</p>
  </div>
  <div class="cards">

    <!-- Section: No Setup Required -->
    <div class="section-label">No Setup Required</div>

    <!-- Headless Demo -->
    <div class="card demo">
      <div class="card-badge">&#9889; Instant Demo</div>
      <div class="card-title">Headless Browser Demo</div>
      <div class="card-desc">Watch an AI agent browse the web live &mdash; no install. We run a headless Chrome on our servers. Just sign in and go.</div>
      <div class="card-reqs"><span class="req req-none">Nothing to install</span></div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Sign in with Google</div>
        <div class="step"><span class="step-num">2</span>Type a task and watch the agent work</div>
      </div>
      <div class="card-note">Demo uses lighter models on a server-side browser. No logins or cookies from your machine.</div>
      <a href="/first-look" class="card-btn">Launch Demo &#8594;</a>
    </div>

    <!-- Free Tier -->
    <div class="card trial">
      <div class="card-badge">Free Tier</div>
      <div class="card-title">Your Browser, Free Models</div>
      <div class="card-desc">Connect your own Chrome &mdash; the agent controls your real browser with your logins, cookies, and sessions intact. No API key. Uses free-tier AI models.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Terminal (curl)</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Sign in with Google</div>
        <div class="step"><span class="step-num">2</span>Run the one-line install command</div>
        <div class="step"><span class="step-num">3</span>Chat &mdash; the agent drives your browser</div>
      </div>
      <div class="card-note">Free-tier models are lightweight. For stronger results, upgrade to a full API lane.</div>
      <a href="/trial" class="card-btn">Start Free &#8594;</a>
    </div>

  </div>

  <button class="more-toggle-btn" id="moreToggleBtn" onclick="toggleMoreCards()" aria-expanded="false" aria-controls="moreCardsWrap">
    <span class="toggle-label">More ways to get started</span> <span class="arrow">&#9660;</span>
  </button>

  <div class="more-cards-wrap" id="moreCardsWrap">
    <div class="cards" style="padding-top:24px">

    <!-- Section: SDK Agent Lanes -->
    <div class="section-label">API Agent Lanes &mdash; provision an API key once, then chat</div>

    <div class="sdk-row">
    <!-- Gemini SDK -->
    <div class="card sdk">
      <div class="card-badge">Gemini API &mdash; FREE TRIAL</div>
      <div class="card-title">Gemini API</div>
      <div class="card-desc">Provision a Gemini API key once via your Chrome browser. Full Gemini model capability &mdash; auto-provisioned in ~30 seconds.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Terminal (curl)</span>
        <span class="req">Auto-provision</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Connect Chrome and auto-provision key</div>
        <div class="step"><span class="step-num">2</span>Open the chat page</div>
      </div>
      <a href="/setup?provider=gemini" class="card-btn">Setup &#8594;</a>
      <a href="/chat-gemini" class="card-btn card-btn-secondary">Open Chat &#8594;</a>
    </div>

    <!-- Claude SDK -->
    <div class="card sdk">
      <div class="card-badge">Claude API</div>
      <div class="card-title">Claude API</div>
      <div class="card-desc">Provision an Anthropic API key once. Full Claude model capability &mdash; auto-provisioned or paste manually.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Anthropic account</span>
        <span class="req">Auto-provision</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Provision or paste your Anthropic key</div>
        <div class="step"><span class="step-num">2</span>Open the chat page</div>
      </div>
      <a href="/setup?provider=claude-sdk" class="card-btn">Setup &#8594;</a>
      <a href="/chat-claude" class="card-btn card-btn-secondary">Open Chat &#8594;</a>
    </div>

    <!-- Codex SDK -->
    <div class="card sdk">
      <div class="card-badge">Codex API</div>
      <div class="card-title">Codex API</div>
      <div class="card-desc">Provision an OpenAI API key once. Model routing optimized for Codex &mdash; auto-provisioned or paste manually.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Terminal (curl)</span>
        <span class="req">Auto-provision</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Provision or paste your OpenAI key</div>
        <div class="step"><span class="step-num">2</span>Open the chat page</div>
      </div>
      <a href="/setup?provider=codex-sdk" class="card-btn">Setup &#8594;</a>
      <a href="/chat-codex" class="card-btn card-btn-secondary">Open Chat &#8594;</a>
    </div>
    </div>

    <!-- Section: Local Agent -->
    <div class="section-label">Local Agent &mdash; install once, run on your machine</div>

    <!-- Claude CLI -->
    <div class="card local">
      <div class="card-badge">&#128187; Local</div>
      <div class="card-title">Claude CLI</div>
      <div class="card-desc">Run Claude Code on your local machine. Full Claude power (Sonnet, Opus, Haiku) with direct browser control via CDP. Works with Claude Pro, Max, or API.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Claude CLI</span>
        <span class="req">Terminal (curl)</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Sign in and install the local agent (curl)</div>
        <div class="step"><span class="step-num">2</span>Run Claude Code with your browser as a tool</div>
      </div>
      <a href="/local" class="card-btn">Open Chat &#8594;</a>
    </div>

    <!-- Codex CLI -->
    <div class="card local">
      <div class="card-badge">&#128187; Local</div>
      <div class="card-title">Codex CLI</div>
      <div class="card-desc">Run Codex CLI on your local machine. No key provisioning needed &mdash; just install, login, and use Codex CLI models in chat.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Codex CLI</span>
        <span class="req">Terminal (curl)</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Install local agent and Codex CLI</div>
        <div class="step"><span class="step-num">2</span>Choose Codex CLI models in chat</div>
      </div>
      <a href="/chat-codex?model=codex-cli:gpt-5.1-codex-mini" class="card-btn">Open Chat &#8594;</a>
    </div>

    <!-- MCP -->
    <div class="card trial">
      <div class="card-badge">MCP</div>
      <div class="card-title">MCP Server</div>
      <div class="card-desc">Use your real Chrome through any MCP client &mdash; Claude Code, Claude Desktop, or any tool that speaks MCP. One command to connect.</div>
      <div class="card-reqs">
        <span class="req">MCP Client</span>
        <span class="req">Agent Installed</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Install the agent (one-line curl)</div>
        <div class="step"><span class="step-num">2</span>Add MCP server config to your client</div>
        <div class="step"><span class="step-num">3</span>Use browser tools from any MCP conversation</div>
      </div>
      <a href="/mcp" class="card-btn">Set Up MCP &#8594;</a>
    </div>

    </div>
  </div>
</div>

<!-- Case Study -->
<div class="getstarted" style="padding-top:0">
  <div class="gs-header">
    <h2>See It In Action</h2>
    <p>Real conversations, real results. See what the agent can do end-to-end.</p>
  </div>
  <div class="cards">
    <div class="card demo" style="grid-column:1/-1">
      <div class="card-badge">Case Study</div>
      <div class="card-title">Rental Relisting on Zillow</div>
      <div class="card-desc">From market research to published listing in one conversation. The agent researched comps, negotiated pricing, caught lease typos, scheduled tours, and published&mdash;all hands-free.</div>
      <a href="/case-study/zillow-rental" class="card-btn">Read Case Study &#8594;</a>
    </div>
  </div>
</div>

<div class="footer">
  <div class="footer-links">
    <a href="/first-look">Demo</a>
    <a href="/trial">Free Tier</a>
    <a href="/mcp">MCP</a>
    <a href="/setup">API Setup</a>
    <a href="https://github.com/protostatis/unchained-infra" target="_blank" rel="noopener noreferrer">Infra GitHub</a>
    <a href="mailto:__CONTACT_EMAIL__">Contact</a>
  </div>
  <div>UNCHAINED &mdash; YOUR BROWSER. YOUR DATA. NO WALLS.</div>
</div>

<script>
// More options toggle
function toggleMoreCards(){
  var wrap=document.getElementById('moreCardsWrap');
  var btn=document.getElementById('moreToggleBtn');
  var label=btn.querySelector('.toggle-label');
  var open=wrap.classList.toggle('open');
  btn.classList.toggle('expanded',open);
  btn.setAttribute('aria-expanded',open);
  label.textContent=open?'Show less':'More ways to get started';
  if(open){wrap.style.maxHeight=wrap.scrollHeight+'px'}
  else{wrap.style.maxHeight='0'}
}

// Mock interaction — renders real action-group DOM structure
var mockPlayed = false;
function playMock() {
  if (mockPlayed) return;
  mockPlayed = true;
  var chat = document.getElementById('mock-chat');
  chat.innerHTML = '';

  function reveal(el){ requestAnimationFrame(function(){ el.classList.add('show'); }); }

  var timeline = [
    // User message
    {delay:0, fn:function(){
      var b = document.createElement('div');
      b.className = 'bubble user';
      b.textContent = 'Go to Hacker News and find the top 3 trending stories right now';
      chat.appendChild(b);
      reveal(b);
    }},
    // Assistant bubble with action-group (navigate opens the group)
    {delay:900, fn:function(){
      var asst = document.createElement('div');
      asst.className = 'bubble asst';
      asst.id = 'mock-asst';
      var ag = document.createElement('div');
      ag.className = 'action-group';
      ag.id = 'mock-ag';
      ag.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">\uD83C\uDF10</span>' +
          '<span class="ag-site">news.ycombinator.com</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">\u25CF</span>' +
        '</div>' +
        '<div class="ag-steps" id="mock-steps">' +
          '<div class="action-step">' +
            '<span class="as-emoji">\uD83C\uDF10</span>' +
            '<span class="as-label">Navigate</span>' +
            '<span class="as-desc">news.ycombinator.com</span>' +
            '<span class="as-dot running">\u25CF</span>' +
          '</div>' +
        '</div>';
      asst.appendChild(ag);
      chat.appendChild(asst);
      reveal(asst); reveal(ag);
    }},
    // Navigate done -> add "Look — map layout"
    {delay:2100, fn:function(){
      var steps = document.getElementById('mock-steps');
      var prev = steps.querySelector('.action-step:last-child .as-dot');
      if(prev){prev.className='as-dot done';prev.textContent='\u2713';}
      var s = document.createElement('div');
      s.className = 'action-step';
      s.innerHTML =
        '<span class="as-emoji">\uD83D\uDC41</span>' +
        '<span class="as-label">Look</span>' +
        '<span class="as-desc">map layout</span>' +
        '<span class="as-dot running">\u25CF</span>';
      steps.appendChild(s);
    }},
    // Layout done -> add "Screenshot"
    {delay:2800, fn:function(){
      var steps = document.getElementById('mock-steps');
      var prev = steps.querySelector('.action-step:last-child .as-dot');
      if(prev){prev.className='as-dot done';prev.textContent='\u2713';}
      var s = document.createElement('div');
      s.className = 'action-step';
      s.innerHTML =
        '<span class="as-emoji">\uD83D\uDCF7</span>' +
        '<span class="as-label">Screenshot</span>' +
        '<span class="as-desc">capture page</span>' +
        '<span class="as-dot running">\u25CF</span>';
      steps.appendChild(s);
      // Draw mock HN screenshot on canvas
      var c = document.createElement('canvas');
      c.width = 640; c.height = 360;
      var ctx = c.getContext('2d');
      // Background
      ctx.fillStyle = '#f6f6ef'; ctx.fillRect(0, 0, 640, 360);
      // Orange header bar
      ctx.fillStyle = '#ff6600'; ctx.fillRect(0, 0, 640, 28);
      ctx.fillStyle = '#fff'; ctx.font = 'bold 14px sans-serif';
      ctx.fillText('Y', 8, 19);
      ctx.fillStyle = '#000'; ctx.font = 'bold 12px sans-serif';
      ctx.fillText('Hacker News', 28, 19);
      ctx.fillStyle = '#888'; ctx.font = '11px sans-serif';
      ctx.fillText('new | past | comments | ask | show | jobs | submit', 140, 18);
      // Story entries
      var stories = [
        'Show HN: I built an open-source browser agent',
        'The death of the cookie: what comes next',
        'Why SQLite is so great for edge computing',
        'A visual guide to quantization in LLMs',
        'Ask HN: What are you working on? (February 2026)',
        'The unreasonable effectiveness of simple HTML',
        'WebAssembly is eating the world, quietly',
        'How we scaled to 10M WebSocket connections'
      ];
      for (var i = 0; i < stories.length; i++) {
        var y = 50 + i * 38;
        ctx.fillStyle = '#828282'; ctx.font = '11px sans-serif';
        ctx.fillText((i+1) + '.', 12, y);
        ctx.fillStyle = '#000'; ctx.font = '13px sans-serif';
        ctx.fillText(stories[i], 36, y);
        ctx.fillStyle = '#828282'; ctx.font = '10px sans-serif';
        ctx.fillText((352 - i*40) + ' points by user' + (i+1) + ' | ' + (128 - i*12) + ' comments', 36, y + 16);
      }
      var imgDiv = document.createElement('div');
      imgDiv.className = 'as-screenshot';
      imgDiv.innerHTML = '<img src="' + c.toDataURL('image/png') + '">';
      var ag = document.getElementById('mock-ag');
      ag.querySelector('.ag-steps').after(imgDiv);
    }},
    // Screenshot done -> add "Analyze — probe page type"
    {delay:3800, fn:function(){
      var steps = document.getElementById('mock-steps');
      var prev = steps.querySelector('.action-step:last-child .as-dot');
      if(prev){prev.className='as-dot done';prev.textContent='\u2713';}
      var s = document.createElement('div');
      s.className = 'action-step';
      s.innerHTML =
        '<span class="as-emoji">\uD83D\uDD2C</span>' +
        '<span class="as-label">Analyze</span>' +
        '<span class="as-desc">probe page type</span>' +
        '<span class="as-dot running">\u25CF</span>';
      steps.appendChild(s);
    }},
    // Probe done -> add "Look — read text"
    {delay:4500, fn:function(){
      var steps = document.getElementById('mock-steps');
      var prev = steps.querySelector('.action-step:last-child .as-dot');
      if(prev){prev.className='as-dot done';prev.textContent='\u2713';}
      var s = document.createElement('div');
      s.className = 'action-step';
      s.innerHTML =
        '<span class="as-emoji">\uD83D\uDC41</span>' +
        '<span class="as-label">Look</span>' +
        '<span class="as-desc">read text</span>' +
        '<span class="as-dot running">\u25CF</span>';
      steps.appendChild(s);
      var ct = document.getElementById('mock-ag').querySelector('.ag-count');
      if(ct) ct.textContent = '5 steps';
    }},
    // All done — finalize group, render answer
    {delay:6200, fn:function(){
      document.querySelectorAll('#mock-ag .as-dot').forEach(function(d){
        d.className='as-dot done';d.textContent='\u2713';
      });
      var gd = document.querySelector('#mock-ag .ag-dot');
      if(gd){gd.className='ag-dot done';gd.textContent='\u2713';}
      var ct = document.getElementById('mock-ag').querySelector('.ag-count');
      if(ct) ct.textContent = '5 steps';
      var asst = document.getElementById('mock-asst');
      var txt = document.createElement('span');
      txt.className = 'text rendered';
      txt.style.display = 'block';
      txt.style.marginTop = '8px';
      txt.innerHTML =
        '<p>Here are the top 3 stories on Hacker News right now:</p>' +
        '<p><strong>1. Show HN: I built an open-source browser agent</strong> \u2014 352 points, 128 comments</p>' +
        '<p><strong>2. The death of the cookie: what comes next</strong> \u2014 287 points, 94 comments</p>' +
        '<p><strong>3. Why SQLite is so great for edge computing</strong> \u2014 241 points, 67 comments</p>';
      asst.appendChild(txt);
      chat.scrollTop = chat.scrollHeight;
    }},
  ];

  timeline.forEach(function(t){ setTimeout(t.fn, t.delay); });
}

// Auto-play on scroll via IntersectionObserver
if ('IntersectionObserver' in window) {
  new IntersectionObserver(function(entries) {
    entries.forEach(function(e) { if (e.isIntersecting) playMock(); });
  }, {threshold: 0.3}).observe(document.getElementById('mock-section'));
}

// Show "Enter" button only for logged-in users, pointing to last route
(function(){
  fetch('/auth/me').then(function(r){return r.json()}).then(function(d){
    if (!d.authenticated) return;
    var btn = document.getElementById('hero-enter');
    var last = localStorage.getItem('unchained_last_route') || '/demo';
    btn.href = last;
    btn.style.display = '';
  }).catch(function(){});
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTML — Landing V2: Haiku Morph (promoted to main landing page)
# ---------------------------------------------------------------------------

LANDING_V2_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&family=Cormorant+Garamond:ital,wght@0,400;1,400&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0a0f;--surface:#111119;--subtle:#222;
  --accent:#e94560;--accent-glow:rgba(233,69,96,0.15);
  --text:#e8e8ec;--muted:#666;
}
body{
  font-family:'Inter',sans-serif;
  background:var(--bg);color:var(--text);
  overflow-x:hidden;
}

/* Subtle grid background */
body::before{
  content:'';position:fixed;inset:0;
  background-image:
    linear-gradient(rgba(233,69,96,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(233,69,96,0.03) 1px, transparent 1px);
  background-size:60px 60px;
  pointer-events:none;z-index:0;
}

/* ── Hero ── */
.hero{
  position:relative;z-index:1;
  max-width:800px;margin:0 auto;padding:0 24px;
  display:flex;flex-direction:column;align-items:center;
  height:100vh;justify-content:center;
}

.wordmark{
  font-size:clamp(48px,10vw,72px);
  font-weight:600;letter-spacing:4px;
  color:var(--text);margin-bottom:48px;
  text-transform:uppercase;
}
.wordmark span{color:var(--accent)}

.poem{
  font-family:'Cormorant Garamond',serif;
  font-size:clamp(20px,4vw,28px);
  line-height:1.8;text-align:center;
  font-style:italic;color:var(--text);
  opacity:0.9;margin-bottom:64px;
  max-width:500px;
}
.poem .line{
  display:block;
  animation:fadeIn 1s ease-out both;
}
.poem .line:nth-child(1){animation-delay:0.3s}
.poem .line:nth-child(2){animation-delay:0.9s}
.poem .line:nth-child(3){animation-delay:1.5s}

@keyframes fadeIn{
  from{opacity:0;transform:translateY(12px)}
  to{opacity:1;transform:translateY(0)}
}

/* ── Morph system — 3-stage line crossfade ── */
.poem .line{
  position:relative;
  height:1.8em;
}
.poem .line .stage{
  position:absolute;
  left:0;right:0;
  text-align:center;
  transition:opacity 1s ease, filter 1s ease;
  opacity:0;
  filter:blur(6px);
}
.poem .line .stage.s1{
  position:relative; /* s1 holds the line height */
  opacity:1;
  filter:blur(0);
}

/* Stage 2: stagger lines 0s, 0.3s, 0.6s */
.poem.stage-2 .line:nth-child(1) .s1{opacity:0;filter:blur(6px);transition-delay:0s}
.poem.stage-2 .line:nth-child(1) .s2{opacity:1;filter:blur(0);transition-delay:0s}
.poem.stage-2 .line:nth-child(2) .s1{opacity:0;filter:blur(6px);transition-delay:0.3s}
.poem.stage-2 .line:nth-child(2) .s2{opacity:1;filter:blur(0);transition-delay:0.3s}
.poem.stage-2 .line:nth-child(3) .s1,
.poem.stage-2 .line:nth-child(3) .s2,
.poem.stage-2 .line:nth-child(3) .s3{transition-delay:0.6s}

/* Stage 3: all three lines morph */
.poem.stage-3 .line .s1{opacity:0;filter:blur(6px)}
.poem.stage-3 .line .s2{opacity:0;filter:blur(6px)}
.poem.stage-3 .line .s3{opacity:1;filter:blur(0)}
.poem.stage-3 .line:nth-child(1) .s2{transition-delay:0s}
.poem.stage-3 .line:nth-child(1) .s3{transition-delay:0s}
.poem.stage-3 .line:nth-child(2) .s2{transition-delay:0.3s}
.poem.stage-3 .line:nth-child(2) .s3{transition-delay:0.3s}
.poem.stage-3 .line:nth-child(3) .s2{transition-delay:0.6s}
.poem.stage-3 .line:nth-child(3) .s3{transition-delay:0.6s}

/* Subtle glow on final stage */
@keyframes subtleGlow{
  0%{text-shadow:none}
  50%{text-shadow:0 0 20px rgba(233,69,96,0.3)}
  100%{text-shadow:none}
}
.poem.stage-3 .line .s3{
  animation:subtleGlow 2s ease 1.5s both;
}

.cta{
  display:inline-flex;align-items:center;gap:10px;
  padding:14px 32px;border:1px solid var(--accent);
  border-radius:8px;color:var(--accent);
  font-size:15px;font-weight:500;letter-spacing:1px;
  text-decoration:none;text-transform:uppercase;
  transition:all 0.3s ease;
  animation:fadeIn 1s ease-out 2.2s both;
}
.cta:hover{
  background:var(--accent);color:#fff;
  box-shadow:0 0 30px var(--accent-glow);
}

.tagline{
  margin-top:80px;font-size:13px;
  color:var(--muted);letter-spacing:2px;
  text-transform:uppercase;
  animation:fadeIn 1s ease-out 2.6s both;
}

.scroll-hint{
  position:absolute;bottom:28px;left:50%;transform:translateX(-50%);
  display:flex;flex-direction:column;align-items:center;gap:4px;
  color:var(--muted);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;
  animation:fadeInCenter 1s ease-out 3.2s both;cursor:pointer;
}
@keyframes fadeInCenter{
  from{opacity:0;transform:translateX(-50%) translateY(12px)}
  to{opacity:1;transform:translateX(-50%) translateY(0)}
}
.scroll-hint span{animation:bounce 2s ease-in-out infinite;font-size:16px;line-height:1}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(5px)}}
.more-toggle-btn{
  display:flex;align-items:center;justify-content:center;gap:8px;
  margin:32px auto 0;padding:8px 0;
  background:none;border:none;cursor:pointer;
  font-size:14px;color:var(--muted);letter-spacing:0.5px;transition:color 0.2s;
}
.more-toggle-btn:hover{color:var(--text)}
.more-toggle-btn .arrow{display:inline-block;transition:transform 0.3s;font-size:12px}
.more-toggle-btn.expanded .arrow{transform:rotate(180deg)}
.more-cards-wrap{max-height:0;overflow:hidden;transition:max-height 0.4s ease}

/* ── Get Started ── */
.getstarted{
  position:relative;z-index:1;
  max-width:900px;margin:0 auto;
  padding:80px 24px 120px;
}
.gs-header{
  text-align:center;margin-bottom:56px;
}
.gs-header h2{
  font-size:clamp(22px,4vw,32px);font-weight:600;
  letter-spacing:2px;text-transform:uppercase;margin-bottom:16px;
}
.gs-header p{
  color:var(--muted);font-size:15px;line-height:1.7;max-width:480px;margin:0 auto;
}

.section-label{
  grid-column:1/-1;
  font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;
  color:var(--muted);margin-bottom:-8px;margin-top:8px;padding-bottom:8px;
  border-bottom:1px solid var(--subtle);
}
.section-label:first-child{margin-top:0}

.cards{
  display:grid;grid-template-columns:1fr 1fr;gap:24px;
}
@media(max-width:640px){.cards{grid-template-columns:1fr}}
.sdk-row{
  grid-column:1/-1;
  display:grid;grid-template-columns:repeat(3,1fr);gap:24px;
}
@media(max-width:800px){.sdk-row{grid-template-columns:1fr}}

.card{
  background:var(--surface);border:1px solid var(--subtle);
  border-radius:16px;padding:32px;
  display:flex;flex-direction:column;gap:16px;
  transition:border-color 0.3s;
}
.card:hover{border-color:#444}

.card-badge{
  display:inline-flex;align-items:center;gap:6px;
  font-size:11px;font-weight:600;letter-spacing:1.5px;
  text-transform:uppercase;padding:4px 10px;border-radius:4px;
  width:fit-content;
}
.card.demo .card-badge,.card.trial .card-badge{
  background:rgba(233,69,96,0.12);color:var(--accent);
  border:1px solid rgba(233,69,96,0.25);
}
.card.dev .card-badge,.card.local .card-badge{
  background:rgba(100,180,255,0.08);color:#64b4ff;
  border:1px solid rgba(100,180,255,0.2);
}
.card.setup .card-badge,.card.sdk .card-badge{
  background:rgba(251,191,36,0.1);color:#fbbf24;
  border:1px solid rgba(251,191,36,0.2);
}
.card.codex .card-badge{
  background:rgba(20,184,166,0.12);color:#2dd4bf;
  border:1px solid rgba(45,212,191,0.25);
}

.card-title{font-size:20px;font-weight:600}
.card-desc{color:var(--muted);font-size:14px;line-height:1.7;flex:1}
.card-note{
  font-size:12px;line-height:1.6;padding:10px 12px;border-radius:8px;
  border:1px solid #3a2a2a;background:rgba(233,69,96,0.08);color:#f2c4cc;
}

/* Requirements pills */
.card-reqs{
  display:flex;flex-wrap:wrap;gap:6px;
}
.req{
  font-size:11px;padding:3px 9px;border-radius:12px;
  background:rgba(255,255,255,0.06);border:1px solid #333;
  color:#999;font-family:var(--mono,monospace);
}
.req-none{font-style:italic;color:#555}

.card-steps{
  display:flex;flex-direction:column;gap:10px;
  border-top:1px solid var(--subtle);padding-top:20px;
}
.step{
  display:flex;align-items:center;gap:10px;
  font-size:13px;color:#aaa;
}
.step-num{
  width:20px;height:20px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:600;flex-shrink:0;
}
.card.demo .step-num,.card.trial .step-num{background:rgba(233,69,96,0.15);color:var(--accent)}
.card.dev .step-num,.card.local .step-num{background:rgba(100,180,255,0.1);color:#64b4ff}
.card.setup .step-num,.card.sdk .step-num{background:rgba(251,191,36,0.1);color:#fbbf24}
.card.codex .step-num{background:rgba(45,212,191,0.12);color:#2dd4bf}

.card-btn{
  display:inline-flex;align-items:center;justify-content:center;
  padding:11px 24px;border-radius:8px;
  font-size:14px;font-weight:500;letter-spacing:0.5px;
  text-decoration:none;transition:all 0.2s;margin-top:4px;
}
.card.demo .card-btn,.card.trial .card-btn{
  background:var(--accent);color:#fff;border:1px solid var(--accent);
}
.card.demo .card-btn:hover,.card.trial .card-btn:hover{box-shadow:0 0 20px var(--accent-glow);opacity:0.9}
.card.dev .card-btn,.card.local .card-btn{
  background:transparent;color:#aaa;border:1px solid #444;
}
.card.dev .card-btn:hover,.card.local .card-btn:hover{border-color:#666;color:var(--text)}
.card.setup .card-btn,.card.sdk .card-btn{
  background:#fbbf24;color:#1a1a2e;border:1px solid #fbbf24;font-weight:600;
}
.card.setup .card-btn:hover,.card.sdk .card-btn:hover{box-shadow:0 0 20px rgba(251,191,36,0.3);opacity:0.9}
.card.sdk .card-btn-secondary{
  background:transparent;color:#fbbf24;border:1px solid rgba(251,191,36,0.4);font-weight:500;
}
.card.sdk .card-btn-secondary:hover{border-color:#fbbf24;box-shadow:none}
.card.codex .card-btn{
  background:#0f766e;color:#e6fffb;border:1px solid #14b8a6;font-weight:600;
}
.card.codex .card-btn:hover{box-shadow:0 0 20px rgba(45,212,191,0.25);opacity:0.9}

/* ── Mock interaction ── */
.mock-section{
  position:relative;z-index:1;
  max-width:680px;margin:0 auto;padding:0 24px 80px;
  display:flex;flex-direction:column;align-items:center;
}
.mock-header{text-align:center;margin-bottom:32px}
.mock-header h2{
  font-size:clamp(20px,3.5vw,28px);font-weight:600;
  letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;
}
.mock-header p{color:var(--muted);font-size:14px;line-height:1.6}
.mock-chat{
  width:100%;
  background:#1a1a2e;border:1px solid var(--subtle);
  border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:10px;
  min-height:320px;overflow:hidden;
  font-family:-apple-system,system-ui,sans-serif;
  text-align:left;
}
.mock-chat .bubble{
  max-width:85%;padding:10px 14px;border-radius:12px;
  font-size:14px;line-height:1.5;word-break:break-word;
  opacity:0;transform:translateY(6px);transition:opacity 0.35s,transform 0.35s;
}
.mock-chat .bubble.show{opacity:1;transform:translateY(0)}
.mock-chat .bubble.user{
  align-self:flex-end;background:#2a1a3e;border:1px solid #3a2a5e;
  border-bottom-right-radius:4px;color:#eee;
}
.mock-chat .bubble.asst{
  align-self:flex-start;background:#1e2a3e;border:1px solid #2a3a5e;
  border-bottom-left-radius:4px;color:#eee;
}
.mock-chat .bubble.asst p{margin:0.4em 0}
.mock-chat .bubble.asst p:first-child{margin-top:0}
.mock-chat .bubble.asst p:last-child{margin-bottom:0}
.mock-chat .bubble.asst strong{font-weight:600}
.mock-chat .bubble.asst a{color:var(--accent);text-decoration:underline}
.mock-chat .action-group{
  margin:6px 0;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;
  font-size:12px;font-family:'SF Mono','Menlo','Monaco','Consolas',monospace;
  opacity:0;transform:translateY(6px);transition:opacity 0.35s,transform 0.35s;
}
.mock-chat .action-group.show{opacity:1;transform:translateY(0)}
.mock-chat .ag-header{
  display:flex;align-items:center;gap:8px;padding:7px 12px;
  background:#111;border-bottom:1px solid #1e1e1e;
}
.mock-chat .ag-emoji{font-size:14px;flex-shrink:0}
.mock-chat .ag-site{color:var(--accent);font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mock-chat .ag-count{color:var(--muted);font-size:10px;flex-shrink:0}
.mock-chat .ag-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.mock-chat .ag-dot.done{color:#4ade80}
.mock-chat .ag-steps{padding:2px 0 4px}
.mock-chat .action-step{
  display:flex;align-items:center;gap:7px;padding:3px 12px 3px 28px;
}
.mock-chat .as-emoji{font-size:11px;flex-shrink:0;width:16px;text-align:center}
.mock-chat .as-label{color:#eee;font-size:11px;flex-shrink:0}
.mock-chat .as-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.mock-chat .as-dot{flex-shrink:0;font-size:10px}
.mock-chat .as-dot.done{color:#4ade80}
@keyframes mockPulse{0%,100%{opacity:1}50%{opacity:0.4}}
.mock-chat .as-dot.running{color:var(--muted);animation:mockPulse 1.2s ease-in-out infinite}
.mock-chat .ag-dot.running{color:var(--muted);animation:mockPulse 1.2s ease-in-out infinite}
.mock-chat .as-screenshot{padding:4px 12px 6px 28px}
.mock-chat .as-screenshot img{max-width:100%;border-radius:4px;border:1px solid #2a2a2a}
.mock-cta{
  display:inline-flex;align-items:center;gap:8px;
  padding:12px 28px;border-radius:8px;
  background:var(--accent);color:#fff;font-size:14px;font-weight:600;
  text-decoration:none;letter-spacing:0.5px;transition:all 0.2s;
  margin-top:24px;
}
.mock-cta:hover{opacity:0.9;box-shadow:0 0 20px var(--accent-glow)}

/* ── Footer ── */
.footer{
  position:relative;z-index:1;
  text-align:center;padding:0 24px 48px;
  color:var(--muted);font-size:12px;letter-spacing:1px;
}
.footer-links{
  display:flex;gap:24px;justify-content:center;margin-bottom:16px;
}
.footer-links a{
  color:var(--muted);text-decoration:none;font-size:13px;letter-spacing:0.5px;
  transition:color 0.15s;
}
.footer-links a:hover{color:var(--accent)}


</style>
</head>
<body>

<!-- Hero -->
<div class="hero">
  <div class="wordmark">Un<span>chain</span>ed</div>
  <div class="poem" id="poem">
    <span class="line">
      <span class="stage s1">Chains fall from my wrists</span>
      <span class="stage s2">Tasks fall from my hours</span>
      <span class="stage s3">You prompt what you need</span>
    </span>
    <span class="line">
      <span class="stage s1">Wind rushes where walls once stood</span>
      <span class="stage s2">Wind drives where walls once stood</span>
      <span class="stage s3">A browser agent does it for you</span>
    </span>
    <span class="line">
      <span class="stage s1">I am sky, unchained</span>
      <span class="stage s2">I am sky, unchained</span>
      <span class="stage s3">You are sky, unchained</span>
    </span>
  </div>
  <a href="/demo" class="cta">Try it free &rarr;</a>
  <div class="tagline">Your browser. Your data. No walls.</div>
  <div class="scroll-hint" onclick="document.querySelector('.mock-section').scrollIntoView({behavior:'smooth'})">
    <span>&#8595;</span>
    watch it work
  </div>
</div>

<!-- Watch it work -->
<div class="mock-section" id="mock-section">
  <div class="mock-header">
    <h2>Watch it work</h2>
    <p>See the agent browse the web, read pages, and extract information &mdash; in real time.</p>
  </div>
  <div class="mock-chat" id="mock-chat"></div>
  <a href="/demo" class="mock-cta">Try it yourself &rarr;</a>
</div>

<!-- Get Started -->
<div class="getstarted">
  <div class="gs-header">
    <h2>Get Started</h2>
    <p>No API key? Start free in 30 seconds. Have an API key? Provision once and get full model power.</p>
  </div>
  <div class="cards">

    <!-- Section: No Setup Required -->
    <div class="section-label">No Setup Required</div>

    <!-- Headless Demo -->
    <div class="card demo">
      <div class="card-badge">&#9889; Instant Demo</div>
      <div class="card-title">Headless Browser Demo</div>
      <div class="card-desc">Watch an AI agent browse the web live &mdash; no install. We run a headless Chrome on our servers. Just sign in and go.</div>
      <div class="card-reqs"><span class="req req-none">Nothing to install</span></div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Sign in with Google</div>
        <div class="step"><span class="step-num">2</span>Type a task and watch the agent work</div>
      </div>
      <div class="card-note">Demo uses lighter models on a server-side browser. No logins or cookies from your machine.</div>
      <a href="/first-look" class="card-btn">Launch Demo &#8594;</a>
    </div>

    <!-- Free Tier -->
    <div class="card trial">
      <div class="card-badge">Free Tier</div>
      <div class="card-title">Your Browser, Free Models</div>
      <div class="card-desc">Connect your own Chrome &mdash; the agent controls your real browser with your logins, cookies, and sessions intact. No API key. Uses free-tier AI models.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Terminal (curl)</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Sign in with Google</div>
        <div class="step"><span class="step-num">2</span>Run the one-line install command</div>
        <div class="step"><span class="step-num">3</span>Chat &mdash; the agent drives your browser</div>
      </div>
      <div class="card-note">Free-tier models are lightweight. For stronger results, upgrade to a full API lane.</div>
      <a href="/trial" class="card-btn">Start Free &#8594;</a>
    </div>

    <button class="more-toggle-btn" id="moreToggleBtn" onclick="toggleMoreCards()" aria-expanded="false" aria-controls="moreCardsWrap">
      <span class="toggle-label">More ways to get started</span> <span class="arrow">&#9660;</span>
    </button>

    <div class="more-cards-wrap" id="moreCardsWrap">

    <!-- Section: SDK Agent Lanes -->
    <div class="section-label">API Agent Lanes &mdash; provision an API key once, then chat</div>

    <div class="sdk-row">
    <!-- Gemini SDK -->
    <div class="card sdk">
      <div class="card-badge">Gemini API &mdash; FREE TRIAL</div>
      <div class="card-title">Gemini API</div>
      <div class="card-desc">Provision a Gemini API key once via your Chrome browser. Full Gemini model capability &mdash; auto-provisioned in ~30 seconds.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Terminal (curl)</span>
        <span class="req">Auto-provision</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Connect Chrome and auto-provision key</div>
        <div class="step"><span class="step-num">2</span>Open the chat page</div>
      </div>
      <a href="/setup?provider=gemini" class="card-btn">Setup &#8594;</a>
      <a href="/chat-gemini" class="card-btn card-btn-secondary">Open Chat &#8594;</a>
    </div>

    <!-- Claude SDK -->
    <div class="card sdk">
      <div class="card-badge">Claude API</div>
      <div class="card-title">Claude API</div>
      <div class="card-desc">Provision an Anthropic API key once. Full Claude model capability &mdash; auto-provisioned or paste manually.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Anthropic account</span>
        <span class="req">Auto-provision</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Provision or paste your Anthropic key</div>
        <div class="step"><span class="step-num">2</span>Open the chat page</div>
      </div>
      <a href="/setup?provider=claude-sdk" class="card-btn">Setup &#8594;</a>
      <a href="/chat-claude" class="card-btn card-btn-secondary">Open Chat &#8594;</a>
    </div>

    <!-- Codex SDK -->
    <div class="card sdk">
      <div class="card-badge">Codex API</div>
      <div class="card-title">Codex API</div>
      <div class="card-desc">Provision an OpenAI API key once. Model routing optimized for Codex &mdash; auto-provisioned or paste manually.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Terminal (curl)</span>
        <span class="req">Auto-provision</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Provision or paste your OpenAI key</div>
        <div class="step"><span class="step-num">2</span>Open the chat page</div>
      </div>
      <a href="/setup?provider=codex-sdk" class="card-btn">Setup &#8594;</a>
      <a href="/chat-codex" class="card-btn card-btn-secondary">Open Chat &#8594;</a>
    </div>
    </div>

    <!-- Section: Local Agent -->
    <div class="section-label">Local Agent &mdash; install once, run on your machine</div>

    <!-- Claude CLI -->
    <div class="card local">
      <div class="card-badge">&#128187; Local</div>
      <div class="card-title">Claude CLI</div>
      <div class="card-desc">Run Claude Code on your local machine. Full Claude power (Sonnet, Opus, Haiku) with direct browser control via CDP. Works with Claude Pro, Max, or API.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Claude CLI</span>
        <span class="req">Terminal (curl)</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Sign in and install the local agent (curl)</div>
        <div class="step"><span class="step-num">2</span>Run Claude Code with your browser as a tool</div>
      </div>
      <a href="/local" class="card-btn">Open Chat &#8594;</a>
    </div>

    <!-- Codex CLI -->
    <div class="card local">
      <div class="card-badge">&#128187; Local</div>
      <div class="card-title">Codex CLI</div>
      <div class="card-desc">Run Codex CLI on your local machine. No key provisioning needed &mdash; just install, login, and use Codex CLI models in chat.</div>
      <div class="card-reqs">
        <span class="req">Chrome</span>
        <span class="req">Codex CLI</span>
        <span class="req">Terminal (curl)</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Install local agent and Codex CLI</div>
        <div class="step"><span class="step-num">2</span>Choose Codex CLI models in chat</div>
      </div>
      <a href="/chat-codex?model=codex-cli:gpt-5.1-codex-mini" class="card-btn">Open Chat &#8594;</a>
    </div>

    <!-- MCP -->
    <div class="card trial">
      <div class="card-badge">MCP</div>
      <div class="card-title">MCP Server</div>
      <div class="card-desc">Use your real Chrome through any MCP client &mdash; Claude Code, Claude Desktop, or any tool that speaks MCP. One command to connect.</div>
      <div class="card-reqs">
        <span class="req">MCP Client</span>
        <span class="req">Agent Installed</span>
      </div>
      <div class="card-steps">
        <div class="step"><span class="step-num">1</span>Install the agent (one-line curl)</div>
        <div class="step"><span class="step-num">2</span>Add MCP server config to your client</div>
        <div class="step"><span class="step-num">3</span>Use browser tools from any MCP conversation</div>
      </div>
      <a href="/mcp" class="card-btn">Set Up MCP &#8594;</a>
    </div>

    </div><!-- /more-cards-wrap -->

  </div>
</div>

<!-- Case Study -->
<div class="getstarted" style="padding-top:0">
  <div class="gs-header">
    <h2>See It In Action</h2>
    <p>Real conversations, real results. See what the agent can do end-to-end.</p>
  </div>
  <div class="cards">
    <div class="card demo" style="grid-column:1/-1">
      <div class="card-badge">Case Study</div>
      <div class="card-title">Rental Relisting on Zillow</div>
      <div class="card-desc">From market research to published listing in one conversation. The agent researched comps, negotiated pricing, caught lease typos, scheduled tours, and published&mdash;all hands-free.</div>
      <a href="/case-study/zillow-rental" class="card-btn">Read Case Study &#8594;</a>
    </div>
  </div>
</div>

<div class="footer">
  <div class="footer-links">
    <a href="/first-look">Demo</a>
    <a href="/trial">Free Tier</a>
    <a href="/mcp">MCP</a>
    <a href="/setup">API Setup</a>
    <a href="https://github.com/protostatis/unchained-infra" target="_blank" rel="noopener noreferrer">Infra GitHub</a>
    <a href="mailto:__CONTACT_EMAIL__">Contact</a>
  </div>
  <div>UNCHAINED &mdash; YOUR BROWSER. YOUR DATA. NO WALLS.</div>
</div>

<script>
// Toggle more cards
function toggleMoreCards(){
  var wrap=document.getElementById('moreCardsWrap');
  var btn=document.getElementById('moreToggleBtn');
  var label=btn.querySelector('.toggle-label');
  var open=wrap.classList.toggle('open');
  btn.classList.toggle('expanded',open);
  btn.setAttribute('aria-expanded',open);
  label.textContent=open?'Show less':'More ways to get started';
  if(open){wrap.style.maxHeight=wrap.scrollHeight+'px'}
  else{wrap.style.maxHeight='0'}
}

// Haiku morph — cycle through 3 stages every 6s
(function(){
  var poem = document.getElementById('poem');
  if (!poem) return;
  var stages = ['', 'stage-2', 'stage-3'];
  var idx = 0;
  // Start first morph after initial fade-in (4s)
  setTimeout(function cycle(){
    idx = (idx + 1) % stages.length;
    poem.className = 'poem' + (stages[idx] ? ' ' + stages[idx] : '');
    setTimeout(cycle, 6000);
  }, 4000);
})();
</script>

<script>
// Mock interaction — flight comparison demo
var mockTimers = [];
function playMock() {
  // Clear any running timers
  mockTimers.forEach(function(t){ clearTimeout(t); });
  mockTimers = [];
  var chat = document.getElementById('mock-chat');
  chat.innerHTML = '';
  var replayBtn = document.getElementById('mock-replay');
  if (replayBtn) replayBtn.classList.remove('visible');

  function reveal(el){ requestAnimationFrame(function(){ el.classList.add('show'); }); }
  function markDone(stepsId){
    var steps = document.getElementById(stepsId);
    if(!steps) return;
    var prev = steps.querySelector('.action-step:last-child .as-dot');
    if(prev){prev.className='as-dot done';prev.textContent='\u2713';}
  }
  function addStep(stepsId, emoji, label, desc){
    var steps = document.getElementById(stepsId);
    var s = document.createElement('div');
    s.className = 'action-step';
    s.innerHTML =
      '<span class="as-emoji">' + emoji + '</span>' +
      '<span class="as-label">' + label + '</span>' +
      '<span class="as-desc">' + desc + '</span>' +
      '<span class="as-dot running">\u25CF</span>';
    steps.appendChild(s);
  }
  function drawFlightCanvas(id, site, results) {
    var c = document.createElement('canvas');
    c.width = 640; c.height = 280;
    var ctx = c.getContext('2d');
    ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, 640, 280);
    // Header bar
    ctx.fillStyle = site.color; ctx.fillRect(0, 0, 640, 36);
    ctx.fillStyle = '#fff'; ctx.font = 'bold 14px sans-serif';
    ctx.fillText(site.name, 16, 24);
    ctx.fillStyle = 'rgba(255,255,255,0.7)'; ctx.font = '12px sans-serif';
    ctx.fillText('NYC \u2192 Tokyo \u00B7 Apr 2026 \u00B7 Round trip', 200, 24);
    // Find lowest-price row index
    var lowestIdx = 0;
    var lowestVal = Infinity;
    for (var j = 0; j < results.length; j++) {
      var num = parseFloat(results[j].price.replace(/[^0-9.]/g, ''));
      if (num < lowestVal) { lowestVal = num; lowestIdx = j; }
    }
    // Flight results
    for (var i = 0; i < results.length; i++) {
      var y = 52 + i * 56;
      ctx.fillStyle = i === lowestIdx ? '#1a2332' : '#111820';
      ctx.fillRect(8, y - 8, 624, 48);
      ctx.fillStyle = '#58a6ff'; ctx.font = 'bold 13px sans-serif';
      ctx.fillText(results[i].airline, 20, y + 10);
      ctx.fillStyle = '#8b949e'; ctx.font = '11px sans-serif';
      ctx.fillText(results[i].route, 20, y + 28);
      ctx.fillText(results[i].duration, 340, y + 10);
      ctx.fillStyle = results[i].stops === 'Nonstop' ? '#3fb950' : '#d29922';
      ctx.font = '11px sans-serif';
      ctx.fillText(results[i].stops, 340, y + 28);
      ctx.fillStyle = i === lowestIdx ? '#3fb950' : '#c9d1d9';
      ctx.font = 'bold 15px sans-serif';
      ctx.fillText(results[i].price, 530, y + 18);
    }
    var imgDiv = document.createElement('div');
    imgDiv.className = 'as-screenshot';
    imgDiv.innerHTML = '<img src="' + c.toDataURL('image/png') + '">';
    var ag = document.getElementById(id);
    ag.querySelector('.ag-steps').after(imgDiv);
  }

  var timeline = [
    {delay:0, fn:function(){
      var b = document.createElement('div');
      b.className = 'bubble user';
      b.textContent = 'Find me the cheapest direct flight from NYC to Tokyo in April. Compare Google Flights and Kayak.';
      chat.appendChild(b);
      reveal(b);
      chat.scrollTop = chat.scrollHeight;
    }},
    // === Site 1: Google Flights ===
    {delay:900, fn:function(){
      var asst = document.createElement('div');
      asst.className = 'bubble asst';
      asst.id = 'mock-asst';
      var ag = document.createElement('div');
      ag.className = 'action-group';
      ag.id = 'mock-ag1';
      ag.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">\uD83C\uDF10</span>' +
          '<span class="ag-site">google.com/travel/flights</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">\u25CF</span>' +
        '</div>' +
        '<div class="ag-steps" id="mock-steps1">' +
          '<div class="action-step">' +
            '<span class="as-emoji">\uD83C\uDF10</span>' +
            '<span class="as-label">Navigate</span>' +
            '<span class="as-desc">google.com/travel/flights</span>' +
            '<span class="as-dot running">\u25CF</span>' +
          '</div>' +
        '</div>';
      asst.appendChild(ag);
      chat.appendChild(asst);
      reveal(asst); reveal(ag);
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:2000, fn:function(){
      markDone('mock-steps1');
      addStep('mock-steps1', '\uD83D\uDC41', 'Look', 'map layout');
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:2700, fn:function(){
      markDone('mock-steps1');
      addStep('mock-steps1', '\u2328', 'Type', 'NYC to Tokyo, April 2026');
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:3500, fn:function(){
      markDone('mock-steps1');
      addStep('mock-steps1', '\uD83D\uDCF7', 'Screenshot', 'capture results');
      chat.scrollTop = chat.scrollHeight;
      drawFlightCanvas('mock-ag1', {name: 'Google Flights', color: '#1a73e8'}, [
        {airline: 'ANA (All Nippon)', route: 'JFK \u2192 NRT', duration: '14h 10m', stops: 'Nonstop', price: '$1,247'},
        {airline: 'Japan Airlines', route: 'JFK \u2192 HND', duration: '14h 35m', stops: 'Nonstop', price: '$1,312'},
        {airline: 'United Airlines', route: 'EWR \u2192 NRT', duration: '14h 25m', stops: 'Nonstop', price: '$1,389'},
        {airline: 'Delta Air Lines', route: 'JFK \u2192 HND', duration: '14h 50m', stops: 'Nonstop', price: '$1,456'},
      ]);
      ctx.fillText('Hacker News', 28, 19);
      ctx.fillStyle = '#888'; ctx.font = '11px sans-serif';
      ctx.fillText('new | past | comments | ask | show | jobs | submit', 140, 18);
      var stories = [
        'Show HN: I built an open-source browser agent',
        'The death of the cookie: what comes next',
        'Why SQLite is so great for edge computing',
        'A visual guide to quantization in LLMs',
        'Ask HN: What are you working on? (February 2026)',
        'The unreasonable effectiveness of simple HTML',
        'WebAssembly is eating the world, quietly',
        'How we scaled to 10M WebSocket connections'
      ];
      for (var i = 0; i < stories.length; i++) {
        var y = 50 + i * 38;
        ctx.fillStyle = '#828282'; ctx.font = '11px sans-serif';
        ctx.fillText((i+1) + '.', 12, y);
        ctx.fillStyle = '#000'; ctx.font = '13px sans-serif';
        ctx.fillText(stories[i], 36, y);
        ctx.fillStyle = '#828282'; ctx.font = '10px sans-serif';
        ctx.fillText((352 - i*40) + ' points by user' + (i+1) + ' | ' + (128 - i*12) + ' comments', 36, y + 16);
      }
      var imgDiv = document.createElement('div');
      imgDiv.className = 'as-screenshot';
      imgDiv.innerHTML = '<img src="' + c.toDataURL('image/png') + '">';
      var ag = document.getElementById('mock-ag');
      ag.querySelector('.ag-steps').after(imgDiv);
    }},
    {delay:3800, fn:function(){
      var steps = document.getElementById('mock-steps');
      var prev = steps.querySelector('.action-step:last-child .as-dot');
      if(prev){prev.className='as-dot done';prev.textContent='\u2713';}
      var s = document.createElement('div');
      s.className = 'action-step';
      s.innerHTML =
        '<span class="as-emoji">\uD83D\uDD2C</span>' +
        '<span class="as-label">Analyze</span>' +
        '<span class="as-desc">probe page type</span>' +
        '<span class="as-dot running">\u25CF</span>';
    }},
    {delay:4500, fn:function(){
      markDone('mock-steps1');
      addStep('mock-steps1', '\uD83D\uDD2C', 'Extract', 'read flight prices');
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:5200, fn:function(){
      document.querySelectorAll('#mock-ag1 .as-dot').forEach(function(d){
        d.className='as-dot done';d.textContent='\u2713';
      });
      var gd = document.querySelector('#mock-ag1 .ag-dot');
      if(gd){gd.className='ag-dot done';gd.textContent='\u2713';}
      var ct = document.querySelector('#mock-ag1 .ag-count');
      if(ct) ct.textContent = '5 steps';
      chat.scrollTop = chat.scrollHeight;
    }},

    // === Site 2: Kayak ===
    {delay:5800, fn:function(){
      var asst = document.getElementById('mock-asst');
      var ag = document.createElement('div');
      ag.className = 'action-group';
      ag.id = 'mock-ag2';
      ag.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">\uD83C\uDF10</span>' +
          '<span class="ag-site">kayak.com/flights</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">\u25CF</span>' +
        '</div>' +
        '<div class="ag-steps" id="mock-steps2">' +
          '<div class="action-step">' +
            '<span class="as-emoji">\uD83C\uDF10</span>' +
            '<span class="as-label">Navigate</span>' +
            '<span class="as-desc">kayak.com/flights</span>' +
            '<span class="as-dot running">\u25CF</span>' +
          '</div>' +
        '</div>';
      asst.appendChild(ag);
      reveal(ag);
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:6800, fn:function(){
      markDone('mock-steps2');
      addStep('mock-steps2', '\uD83D\uDC41', 'Look', 'map layout');
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:7400, fn:function(){
      markDone('mock-steps2');
      addStep('mock-steps2', '\u2328', 'Type', 'NYC to Tokyo, April 2026');
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:8200, fn:function(){
      markDone('mock-steps2');
      addStep('mock-steps2', '\uD83D\uDCF7', 'Screenshot', 'capture results');
      chat.scrollTop = chat.scrollHeight;
      drawFlightCanvas('mock-ag2', {name: 'Kayak', color: '#ff690f'}, [
        {airline: 'ANA (All Nippon)', route: 'JFK \u2192 NRT', duration: '14h 10m', stops: 'Nonstop', price: '$1,198'},
        {airline: 'Japan Airlines', route: 'JFK \u2192 HND', duration: '14h 35m', stops: 'Nonstop', price: '$1,295'},
        {airline: 'United Airlines', route: 'EWR \u2192 NRT', duration: '14h 25m', stops: 'Nonstop', price: '$1,410'},
        {airline: 'Delta Air Lines', route: 'JFK \u2192 HND', duration: '14h 50m', stops: 'Nonstop', price: '$1,478'},
      ]);
    }},
    {delay:9200, fn:function(){
      markDone('mock-steps2');
      addStep('mock-steps2', '\uD83D\uDD2C', 'Extract', 'read flight prices');
      chat.scrollTop = chat.scrollHeight;
    }},
    {delay:9900, fn:function(){
      document.querySelectorAll('#mock-ag2 .as-dot').forEach(function(d){
        d.className='as-dot done';d.textContent='\u2713';
      });
      var gd = document.querySelector('#mock-ag2 .ag-dot');
      if(gd){gd.className='ag-dot done';gd.textContent='\u2713';}
      var ct = document.querySelector('#mock-ag2 .ag-count');
      if(ct) ct.textContent = '5 steps';
      chat.scrollTop = chat.scrollHeight;
    }},

    // === Final comparison answer ===
    {delay:10800, fn:function(){
      var asst = document.getElementById('mock-asst');
      var txt = document.createElement('span');
      txt.className = 'text rendered';
      txt.style.display = 'block';
      txt.style.marginTop = '8px';
      txt.innerHTML =
        '<p>Here are the cheapest nonstop flights from NYC to Tokyo in April, compared across both sites:</p>' +
        '<table>' +
          '<tr><th>Airline</th><th>Route</th><th>Google Flights</th><th>Kayak</th></tr>' +
          '<tr><td>ANA</td><td>JFK \u2192 NRT</td><td>$1,247</td><td class="best-price">$1,198 \u2605</td></tr>' +
          '<tr><td>JAL</td><td>JFK \u2192 HND</td><td>$1,312</td><td class="best-price">$1,295</td></tr>' +
          '<tr><td>United</td><td>EWR \u2192 NRT</td><td class="best-price">$1,389</td><td>$1,410</td></tr>' +
          '<tr><td>Delta</td><td>JFK \u2192 HND</td><td class="best-price">$1,456</td><td>$1,478</td></tr>' +
        '</table>' +
        '<p><strong>Best deal: ANA via Kayak at $1,198</strong> (nonstop JFK \u2192 NRT, 14h 10m). That\u2019s $49 cheaper than the same flight on Google Flights.</p>';
      asst.appendChild(txt);
      chat.scrollTop = chat.scrollHeight;
      // Show replay button
      var replayBtn = document.getElementById('mock-replay');
      if (replayBtn) replayBtn.classList.add('visible');
    }},
  ];

  timeline.forEach(function(t){
    mockTimers.push(setTimeout(t.fn, t.delay));
  });
}

if ('IntersectionObserver' in window) {
  var mockObserver = new IntersectionObserver(function(entries) {
    entries.forEach(function(e) {
      if (e.isIntersecting) {
        mockObserver.disconnect();
        playMock();
      }
    });
  }, {threshold: 0.3});
  mockObserver.observe(document.getElementById('mock-section'));
}
</script>
</body>
</html>"""

# Promote V2 (haiku morph) as the main landing page.
# V1 preserved as LANDING_V1_HTML for rollback.
LANDING_HTML = LANDING_V2_HTML


# ---------------------------------------------------------------------------
# HTML — Zillow Rental Relisting Case Study
# ---------------------------------------------------------------------------

CASE_STUDY_ZILLOW_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Case Study: Rental Relisting on Zillow &mdash; Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{margin:0;padding:0;box-sizing:border-box}
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
:root{
  --bg:#0a0a0f;--surface:#111119;--subtle:#222;
  --accent:#e94560;--accent-glow:rgba(233,69,96,0.15);
  --text:#e8e8ec;--muted:#666;
}
body{
  font-family:'Inter',sans-serif;
  background:var(--bg);color:var(--text);
  overflow-x:hidden;
}
body::before{
  content:'';position:fixed;inset:0;
  background-image:
    linear-gradient(rgba(233,69,96,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(233,69,96,0.03) 1px, transparent 1px);
  background-size:60px 60px;
  pointer-events:none;z-index:0;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* ── Hero ── */
.hero{
  position:relative;z-index:1;
  max-width:800px;margin:0 auto;padding:120px 24px 80px;
  text-align:center;
}
.hero-back{
  display:inline-block;margin-bottom:32px;
  font-size:13px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;
  text-decoration:none;
}
.hero-back:hover{color:var(--accent);text-decoration:none}
.hero h1{
  font-size:clamp(28px,5vw,44px);font-weight:700;
  line-height:1.2;margin-bottom:20px;
}
.hero h1 span{color:var(--accent)}
.hero .subtitle{
  font-size:clamp(16px,2.5vw,20px);
  color:var(--muted);line-height:1.6;max-width:560px;margin:0 auto;
}

/* ── Segments ── */
.segment{
  position:relative;z-index:1;
  max-width:720px;margin:0 auto;padding:0 24px 64px;
}
.segment-label{
  font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;
  color:var(--accent);margin-bottom:12px;
}
.segment h2{
  font-size:clamp(20px,3.5vw,28px);font-weight:600;margin-bottom:24px;
}

/* ── Chat bubbles ── */
.chat{
  background:#1a1a2e;border:1px solid var(--subtle);
  border-radius:16px;padding:20px;display:flex;flex-direction:column;gap:12px;
  margin-bottom:24px;
}
.bubble{
  max-width:88%;padding:12px 16px;border-radius:14px;
  font-size:14px;line-height:1.6;word-break:break-word;
}
.bubble.user{
  align-self:flex-end;background:#2a1a3e;border:1px solid #3a2a5e;
  border-bottom-right-radius:4px;color:#eee;
}
.bubble.asst{
  align-self:flex-start;background:#1e2a3e;border:1px solid #2a3a5e;
  border-bottom-left-radius:4px;color:#eee;
}
.bubble table{
  width:100%;border-collapse:collapse;margin:8px 0;font-size:12px;
}
.bubble th,.bubble td{
  text-align:left;padding:6px 10px;border-bottom:1px solid #2a3a5e;
}
.bubble th{color:var(--accent);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:0.5px}
.bubble ul,.bubble ol{margin:8px 0 8px 18px}
.bubble li{margin:4px 0;font-size:13px;line-height:1.5}
.bubble strong{font-weight:600}

/* ── Callout ── */
.callout{
  display:flex;align-items:center;gap:14px;
  background:var(--surface);border:1px solid var(--subtle);
  border-left:3px solid var(--accent);
  border-radius:0 12px 12px 0;padding:16px 20px;
  margin-bottom:24px;
}
.callout-icon{font-size:24px;flex-shrink:0}
.callout-text{
  font-size:14px;font-weight:500;letter-spacing:0.3px;
}

/* ── CTA ── */
.cta-section{
  position:relative;z-index:1;
  max-width:720px;margin:0 auto;padding:40px 24px 100px;
  text-align:center;
}
.cta-section h2{
  font-size:clamp(22px,4vw,32px);font-weight:600;margin-bottom:12px;
}
.cta-section p{
  color:var(--muted);font-size:15px;line-height:1.7;margin-bottom:32px;
}
.cta-buttons{display:flex;gap:16px;justify-content:center;flex-wrap:wrap}
.cta-btn{
  display:inline-flex;align-items:center;gap:8px;
  padding:14px 32px;border-radius:8px;
  font-size:15px;font-weight:600;letter-spacing:0.5px;
  text-decoration:none;transition:all 0.2s;
}
.cta-btn:hover{text-decoration:none}
.cta-btn.primary{background:var(--accent);color:#fff;border:1px solid var(--accent)}
.cta-btn.primary:hover{box-shadow:0 0 30px var(--accent-glow);opacity:0.9}
.cta-btn.secondary{background:transparent;color:var(--text);border:1px solid #444}
.cta-btn.secondary:hover{border-color:#666}

/* ── Footer ── */
.footer{
  position:relative;z-index:1;
  text-align:center;padding:0 24px 48px;
  color:var(--muted);font-size:12px;letter-spacing:1px;
}
.footer-links{display:flex;gap:24px;justify-content:center;margin-bottom:16px}
.footer-links a{color:var(--muted);text-decoration:none;font-size:13px;letter-spacing:0.5px;transition:color 0.15s}
.footer-links a:hover{color:var(--accent)}

@media(max-width:640px){
  .hero{padding:80px 16px 48px}
  .segment{padding:0 16px 48px}
  .bubble{max-width:95%;font-size:13px}
  .bubble table{font-size:11px}
  .bubble th,.bubble td{padding:4px 6px}
  .cta-buttons{flex-direction:column;align-items:center}
}
</style>
</head>
<body>

<!-- Hero -->
<div class="hero">
  <a href="/" class="hero-back">&larr; Unchained</a>
  <h1>Case Study: <span>Rental Relisting</span> on Zillow</h1>
  <p class="subtitle">From market research to published listing in one conversation. See how the agent helped a landlord price, configure, and publish a rental&mdash;hands-free.</p>
</div>

<!-- Segment 1: Market Research -->
<div class="segment">
  <div class="segment-label">Step 1</div>
  <h2>Research Rental Comps</h2>
  <div class="chat">
    <div class="bubble user">Go to Zillow and review my property in Lakeview, Chicago. I&rsquo;m the owner, I want to put it up for rent for end of April.</div>
    <div class="bubble asst">
      <strong>Comparable 2BD/2BA Rentals Nearby</strong>
      <table>
        <tr><th>Property</th><th>Rent</th><th>Sqft</th><th>Notes</th></tr>
        <tr><td>1144 W Roscoe 3E</td><td>$4,400</td><td>1,350</td><td>Closest comp &mdash; utilities incl, pets OK</td></tr>
        <tr><td>The Yellin, 1348 W Roscoe</td><td>$4,395</td><td>1,088</td><td>Newer build</td></tr>
        <tr><td>Five Points, 3605 N Ravenswood</td><td>$3,750+</td><td>1,110</td><td></td></tr>
        <tr><td>3419 N Paulina St</td><td>$3,450</td><td>1,230</td><td></td></tr>
        <tr><td>3052 N Lincoln Ave 3F</td><td>$3,400</td><td>1,200</td><td></td></tr>
      </table>
      <p><strong>Your current rent ($3,500)</strong> is significantly below market. Zillow Rent Zestimate: <strong>$4,201/mo</strong>. Market range for 2bd/2ba in Lakeview: $3,125&ndash;$4,900.</p>
      <p>My suggestion: List at <strong>$4,200/mo</strong>.</p>
    </div>
  </div>
  <div class="callout">
    <div class="callout-icon">&#127760;</div>
    <div class="callout-text">Researches live market data from active listings and builds a comp table automatically.</div>
  </div>
</div>

<!-- Segment 2: Price Negotiation -->
<div class="segment">
  <div class="segment-label">Step 2</div>
  <h2>Adjust Pricing to Your Judgment</h2>
  <div class="chat">
    <div class="bubble user">I think $4,200 is too much, this is a 2 bed.</div>
    <div class="bubble asst">
      <p>You make a fair point. The $4,200+ listings are newer builds or include utilities. Your more realistic comparables:</p>
      <table>
        <tr><th>Property</th><th>Rent</th><th>Sqft</th></tr>
        <tr><td>3419 N Paulina St</td><td>$3,450</td><td>1,230</td></tr>
        <tr><td>3052 N Lincoln Ave 3F</td><td>$3,400</td><td>1,200</td></tr>
        <tr><td>Five Points Lake View</td><td>$3,750+</td><td>1,110</td></tr>
      </table>
      <p>A sweet spot might be <strong>$3,700/mo</strong> &mdash; a reasonable ~6% increase over current rent, competitive enough to attract quality tenants quickly.</p>
    </div>
    <div class="bubble user">Okay $3,700 sounds good.</div>
  </div>
  <div class="callout">
    <div class="callout-icon">&#128200;</div>
    <div class="callout-text">Adapts recommendations to your judgment instead of pushing a single answer.</div>
  </div>
</div>

<!-- Segment 3: Lease Review -->
<div class="segment">
  <div class="segment-label">Step 3</div>
  <h2>Review Lease Terms &amp; Catch Errors</h2>
  <div class="chat">
    <div class="bubble user">Can you review the lease terms to see we are up to date?</div>
    <div class="bubble asst">
      <p><strong>Issues found:</strong></p>
      <ul>
        <li>&ldquo;None smoking indoor&rdquo; &mdash; <strong>typo</strong>, should be &ldquo;No smoking indoors&rdquo;</li>
        <li>Application Fee: $75/applicant &mdash; can be removed if using Zillow-only applications ($35 handled by Zillow)</li>
      </ul>
      <p><strong>Suggested additions:</strong> renter&rsquo;s insurance requirement, early termination clause, Cook County Just Housing Initiative compliance.</p>
    </div>
    <div class="bubble user">Can we make the application Zillow-only so we don&rsquo;t have to state the $75 fee?</div>
    <div class="bubble asst">
      <p><strong>Changes made:</strong></p>
      <ul>
        <li>Fixed typo: &ldquo;None smoking indoor&rdquo; &rarr; &ldquo;No smoking indoors&rdquo;</li>
        <li>Removed: Application Fee $75 per applicant (Zillow handles at $35)</li>
      </ul>
    </div>
  </div>
  <div class="callout">
    <div class="callout-icon">&#128270;</div>
    <div class="callout-text">Reviews documents, catches typos, and suggests practical improvements.</div>
  </div>
</div>

<!-- Segment 4: Tour Scheduling -->
<div class="segment">
  <div class="segment-label">Step 4</div>
  <h2>Schedule Property Tours</h2>
  <div class="chat">
    <div class="bubble user">The renter agreed we can tour while they&rsquo;re there. Make it available starting the 15th. Also add weekday evenings&mdash;my helper can show the place after 6pm.</div>
    <div class="bubble asst">
      <p><strong>All 6 tour dates set:</strong></p>
      <table>
        <tr><th>Day</th><th>Date</th><th>Time</th></tr>
        <tr><td>Sunday</td><td>March 15</td><td>10:00am &ndash; 2:00pm</td></tr>
        <tr><td>Tuesday</td><td>March 17</td><td>6:00pm &ndash; 8:00pm</td></tr>
        <tr><td>Wednesday</td><td>March 18</td><td>6:00pm &ndash; 8:00pm</td></tr>
        <tr><td>Thursday</td><td>March 19</td><td>6:00pm &ndash; 8:00pm</td></tr>
        <tr><td>Saturday</td><td>March 21</td><td>10:00am &ndash; 2:00pm</td></tr>
        <tr><td>Sunday</td><td>March 22</td><td>10:00am &ndash; 2:00pm</td></tr>
      </table>
      <p>Good mix of weekends (daytime) and weekday evenings (after work).</p>
    </div>
  </div>
  <div class="callout">
    <div class="callout-icon">&#128197;</div>
    <div class="callout-text">Navigates date pickers and complex multi-step form workflows.</div>
  </div>
</div>

<!-- Segment 5: Published -->
<div class="segment">
  <div class="segment-label">Step 5</div>
  <h2>Listing Published &amp; Verified</h2>
  <div class="chat">
    <div class="bubble user">Go ahead and after you post, make sure to review it.</div>
    <div class="bubble asst">
      <p>Your listing is <strong>live!</strong> Here&rsquo;s how renters see it:</p>
      <table>
        <tr><th>Detail</th><th>Value</th></tr>
        <tr><td>Monthly rent</td><td><strong>$3,700</strong></td></tr>
        <tr><td>Security deposit</td><td>$3,700</td></tr>
        <tr><td>Available</td><td>April 18, 2026</td></tr>
        <tr><td>Size</td><td>2 bd / 2 ba / 1,300 sqft</td></tr>
        <tr><td>Walk Score</td><td>96/100</td></tr>
        <tr><td>Transit Score</td><td>72/100</td></tr>
      </table>
      <p>Syndicated across <strong>Zillow, Trulia, and HotPads</strong>. With April 18 availability and competitive $3,700 pricing, you should start getting leads soon.</p>
    </div>
  </div>
  <div class="callout">
    <div class="callout-icon">&#9989;</div>
    <div class="callout-text">End-to-end task completion&mdash;from research to published listing in one session.</div>
  </div>
</div>

<!-- Segment 6: Ongoing Monitoring -->
<div class="segment">
  <div class="segment-label">Step 6</div>
  <h2>Set Up Ongoing Monitoring</h2>
  <div class="chat">
    <div class="bubble user">I&rsquo;d like to set up a task for weekly review. Can you give me a prompt that would run every Monday?</div>
    <div class="bubble asst">
      <p>Here&rsquo;s a ready-to-use weekly prompt:</p>
      <ul>
        <li><strong>Listing status</strong> &mdash; confirm still active, flag errors</li>
        <li><strong>Lead activity</strong> &mdash; views, saves, new applications, pending tour requests</li>
        <li><strong>Market check</strong> &mdash; active comps, price range, new competitors</li>
        <li><strong>Rent Zestimate</strong> &mdash; flag changes &gt; $50</li>
        <li><strong>Tour calendar</strong> &mdash; ensure 3+ upcoming slots</li>
        <li><strong>Action items</strong> &mdash; specific recommendations for the week</li>
      </ul>
      <p>Schedule it as a recurring Monday 8am reminder and paste it into the agent.</p>
    </div>
  </div>
  <div class="callout">
    <div class="callout-icon">&#128260;</div>
    <div class="callout-text">Generates reusable automation prompts you can schedule and repeat.</div>
  </div>
</div>

<!-- CTA Section -->
<div class="cta-section">
  <h2>Your browser agent, ready when you are</h2>
  <p>The agent works with your real browser, your real logins, and your real data. No screenshots to upload, no copy-paste&mdash;just tell it what to do.</p>
  <div class="cta-buttons">
    <a href="/first-look" class="cta-btn primary">Try the Demo &rarr;</a>
    <a href="/trial" class="cta-btn secondary">Connect Your Browser &rarr;</a>
  </div>
</div>

<div class="footer">
  <div class="footer-links">
    <a href="/">Home</a>
    <a href="/first-look">Demo</a>
    <a href="/trial">Free Tier</a>
    <a href="/mcp">MCP</a>
    <a href="mailto:__CONTACT_EMAIL__">Contact</a>
  </div>
  <div>UNCHAINED &mdash; YOUR BROWSER. YOUR DATA. NO WALLS.</div>
</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# HTML — browser control UI (moved to /test)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#1a1a2e;--surface:#16213e;--accent:#e94560;
  --text:#eee;--muted:#888;
  --mono:'SF Mono','Menlo','Monaco','Consolas',monospace;
}
body{
  font-family:-apple-system,system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  height:100dvh;overflow:hidden;
}

/* === Login === */
#login{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100dvh;padding:24px;gap:16px;
}
#login h1{font-size:28px;color:var(--accent);margin-bottom:8px;letter-spacing:1px}
#login .sub{color:var(--muted);font-size:13px;margin-bottom:8px}
#login input{
  width:100%;max-width:360px;height:48px;padding:0 16px;
  border:1px solid #333;border-radius:8px;
  background:var(--surface);color:var(--text);font-size:16px;
}
#login input:focus{outline:none;border-color:var(--accent)}
#login button{
  width:100%;max-width:360px;height:48px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:16px;font-weight:600;cursor:pointer;
}
#login button:active{opacity:0.8}
#loginerr{color:#ff6b6b;font-size:14px;min-height:20px}

/* === Main === */
#main{display:none;flex-direction:column;height:100dvh}

#topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:8px 16px;padding-top:max(8px,env(safe-area-inset-top));
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#topbar .agent{font-family:var(--mono);font-size:14px;color:var(--accent)}
#topbar button{
  background:none;border:1px solid #555;color:var(--muted);
  padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;
}

/* === Output === */
#output{
  flex:1;overflow-y:auto;padding:12px 16px;
  font-family:var(--mono);font-size:13px;line-height:1.5;
  -webkit-overflow-scrolling:touch;
}
.msg{margin-bottom:12px}
.msg-head{display:flex;gap:8px;align-items:baseline;margin-bottom:2px}
.msg-time{color:var(--muted);font-size:11px}
.msg-cmd{color:var(--accent);font-weight:600;font-size:12px}
.msg-body{white-space:pre-wrap;word-break:break-word}
.msg-body.error{color:#ff6b6b}
.msg-body img{max-width:100%;border-radius:4px;margin-top:4px}
.msg-body em{animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

/* === Quick actions === */
#quickbar{
  display:flex;gap:8px;padding:8px 16px;
  overflow-x:auto;flex-shrink:0;border-top:1px solid #333;
}
#quickbar button{
  flex-shrink:0;height:44px;padding:0 16px;
  border:1px solid #444;border-radius:8px;
  background:var(--surface);color:var(--text);font-size:14px;cursor:pointer;
  white-space:nowrap;
}
#quickbar button:active{background:var(--accent)}

/* === Input === */
#inputbar{
  display:flex;flex-direction:column;gap:8px;
  padding:8px 16px;padding-bottom:max(16px,env(safe-area-inset-bottom));
  background:var(--surface);border-top:1px solid #333;flex-shrink:0;
}
#modesel{display:flex;gap:6px;overflow-x:auto}
#modesel button{
  height:32px;padding:0 12px;border:1px solid #444;border-radius:6px;
  background:transparent;color:var(--muted);font-size:12px;cursor:pointer;
  white-space:nowrap;flex-shrink:0;
}
#modesel button.active{border-color:var(--accent);color:var(--accent)}
#inputrow{display:flex;gap:8px}
#cmdinput{
  flex:1;height:44px;padding:0 12px;
  border:1px solid #444;border-radius:8px;
  background:var(--bg);color:var(--text);
  font-size:15px;font-family:var(--mono);
}
#cmdinput:focus{outline:none;border-color:var(--accent)}
#sendbtn{
  width:44px;height:44px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;
}
#sendbtn:active{opacity:0.8}
</style>
</head>
<body>

<!-- Login -->
<div id="login">
  <h1>Unchained</h1>
  <div class="sub">Remote browser control</div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="outline"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
</div>

<!-- Pending -->
<div id="pending" style="display:none;flex-direction:column;align-items:center;justify-content:center;height:100dvh;padding:24px;gap:16px;text-align:center">
  <h1 style="font-size:28px;color:var(--accent);letter-spacing:1px">Request Being Reviewed</h1>
  <p style="color:var(--muted);font-size:15px;max-width:400px">Your sign-up request is being reviewed. We'll send you an email once your account is approved.</p>
  <button onclick="checkApproval()" style="margin-top:16px;padding:10px 24px;border:1px solid var(--accent);border-radius:8px;background:transparent;color:var(--accent);font-size:14px;cursor:pointer">Check Again</button>
  <button onclick="backToLogin()" style="margin-top:8px;padding:10px 24px;border:1px solid #444;border-radius:8px;background:transparent;color:var(--muted);font-size:14px;cursor:pointer">Use a Different Account</button>
  <div id="pendingmsg" style="color:var(--muted);font-size:13px;min-height:20px"></div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <span class="agent" id="agentlabel"></span>
    <button onclick="doDisconnect()">Disconnect</button>
  </div>

  <div id="output"></div>

  <div id="quickbar">
    <button onclick="quick('ddm')">DDM</button>
    <button onclick="quick('screenshot')">Screenshot</button>
    <button onclick="quick('text')">Text</button>
    <button onclick="quick('intel')">Intel</button>
  </div>

  <div id="inputbar">
    <div id="modesel">
      <button class="active" data-m="url" onclick="setMode(this)">URL</button>
      <button data-m="js" onclick="setMode(this)">JS</button>
      <button data-m="click" onclick="setMode(this)">Click x,y</button>
      <button data-m="type" onclick="setMode(this)">Type</button>
      <button data-m="find" onclick="setMode(this)">Find</button>
    </div>
    <div id="inputrow">
      <input id="cmdinput" placeholder="Enter URL..." autocomplete="off"
             onkeydown="if(event.key==='Enter')doSend()">
      <button id="sendbtn" onclick="doSend()">&#9654;</button>
    </div>
  </div>
</div>

<script>
let agentId = '';
let mode = 'url';

async function handleGoogleCredential(response) {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential}),
    });
    const data = await r.json();
    if (data.pending) { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    agentId = data.agent_id;
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) { agentId = data.agent_id; showMain(); return; }
    if (data.pending) { showPending(); return; }
  } catch(e) {}
  document.getElementById('login').style.display = 'flex';
}

async function checkApproval() {
  const msg = document.getElementById('pendingmsg');
  msg.textContent = 'Checking...';
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) { agentId = data.agent_id; showMain(); return; }
    if (data.pending) { msg.textContent = 'Still under review. Check back soon!'; return; }
    msg.textContent = 'Still under review.';
  } catch(e) { msg.textContent = 'Could not check status.'; }
}

async function doDisconnect() {
  await fetch('/auth/logout', {method: 'POST'});
  agentId = '';
  document.getElementById('login').style.display = 'flex';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
}

async function backToLogin() {
  await fetch('/auth/logout', {method: 'POST'});
  document.getElementById('pending').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

function showPending() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'flex';
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  document.getElementById('agentlabel').textContent = agentId;
}

checkSession();

function setMode(btn) {
  mode = btn.dataset.m;
  document.querySelectorAll('#modesel button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const ph = {url:'Enter URL...',js:'JS expression...',click:'x, y',type:'Text to type...',find:'Search text...'};
  document.getElementById('cmdinput').placeholder = ph[mode] || '';
  document.getElementById('cmdinput').focus();
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function addMsg(cmd) {
  const out = document.getElementById('output');
  const t = new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'msg';
  div.innerHTML = '<div class="msg-head"><span class="msg-time">' + t +
    '</span><span class="msg-cmd">' + esc(cmd) + '</span></div>' +
    '<div class="msg-body"><em>running\u2026</em></div>';
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
  return div;
}

function updateMsg(div, html, isError) {
  const body = div.querySelector('.msg-body');
  body.className = 'msg-body' + (isError ? ' error' : '');
  body.innerHTML = html;
  document.getElementById('output').scrollTop =
    document.getElementById('output').scrollHeight;
}

async function sendCmd(body) {
  const div = addMsg(body.action);
  try {
    const r = await fetch('/web/cmd', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({agent_id: agentId}, body)),
    });
    const data = await r.json();
    if (!r.ok) {
      updateMsg(div, esc(data.error || 'Request failed'), true);
      if (r.status === 401 || r.status === 403) doDisconnect();
      return;
    }
    if (data.type === 'image') {
      updateMsg(div, '<img src="data:image/png;base64,' + data.data + '">');
    } else {
      updateMsg(div, esc(data.data));
    }
  } catch(e) {
    updateMsg(div, esc(e.message), true);
  }
}

function quick(action) { sendCmd({action}); }

function doSend() {
  const input = document.getElementById('cmdinput');
  const val = input.value.trim();
  if (!val) return;
  input.value = '';

  let body;
  if (mode === 'url') {
    let u = val;
    if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
    body = {action:'navigate', url:u};
  } else if (mode === 'js') {
    body = {action:'js', expression:val};
  } else if (mode === 'click') {
    const p = val.split(/[,\s]+/).map(Number);
    if (p.length < 2 || isNaN(p[0]) || isNaN(p[1])) {
      const d = addMsg('click');
      updateMsg(d, 'Format: x, y (e.g. 500, 300)', true);
      return;
    }
    body = {action:'click', x:p[0], y:p[1]};
  } else if (mode === 'type') {
    body = {action:'type', text:val};
  } else if (mode === 'find') {
    body = {action:'text', find:val};
  }
  sendCmd(body);
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Chat HTML — split into Trial (OpenRouter) and Claude templates
# ---------------------------------------------------------------------------

TRIAL_CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Unchained Trial</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#1a1a2e;--surface:#16213e;--accent:#e94560;
  --text:#eee;--muted:#888;--user-bg:#2a1a3e;--asst-bg:#1e2a3e;
  --mono:'SF Mono','Menlo','Monaco','Consolas',monospace;
}
body{
  font-family:-apple-system,system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  height:100dvh;overflow:hidden;
}

/* === Login === */
#login{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100dvh;padding:24px;gap:16px;
}
#login h1{font-size:28px;color:var(--accent);margin-bottom:8px;letter-spacing:1px}
#login .sub{color:var(--muted);font-size:13px;margin-bottom:8px}
#login input{
  width:100%;max-width:360px;height:48px;padding:0 16px;
  border:1px solid #333;border-radius:8px;
  background:var(--surface);color:var(--text);font-size:16px;
}
#login input:focus{outline:none;border-color:var(--accent)}
#login button{
  width:100%;max-width:360px;height:48px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:16px;font-weight:600;cursor:pointer;
}
#login button:active{opacity:0.8}
#loginerr{color:#ff6b6b;font-size:14px;min-height:20px}

/* === Main === */
#main{display:none;flex-direction:column;height:100dvh}

#topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:8px 16px;padding-top:max(8px,env(safe-area-inset-top));
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .agent{font-family:var(--mono);font-size:14px;color:var(--accent)}
#topbar .status-stack{display:flex;flex-wrap:wrap;gap:6px}
#topbar .status{font-size:11px;color:var(--muted);border:1px solid #444;padding:3px 8px;border-radius:999px}
#topbar .status.online{color:#4caf50;border-color:#2f6f39}
#topbar .status.warn{color:#f0d58b;border-color:#7a6326}
#topbar .nav{display:flex;gap:8px}
#topbar .nav a{
  color:var(--muted);text-decoration:none;font-size:12px;
  border:1px solid #555;padding:4px 10px;border-radius:6px;
}
#topbar .nav a:hover{border-color:var(--accent);color:var(--accent)}

/* === Chat === */
#chat{
  flex:1;overflow-y:auto;padding:12px 16px;
  -webkit-overflow-scrolling:touch;
  display:flex;flex-direction:column;gap:12px;
}
.bubble{
  max-width:85%;padding:10px 14px;border-radius:12px;
  font-size:14px;line-height:1.5;word-break:break-word;
}
.bubble.user{
  align-self:flex-end;background:var(--user-bg);
  border:1px solid #3a2a5e;border-bottom-right-radius:4px;
}
.bubble.asst{
  align-self:flex-start;background:var(--asst-bg);
  border:1px solid #2a3a5e;border-bottom-left-radius:4px;
}
.bubble.asst .text{white-space:pre-wrap}
.bubble.asst .text.rendered{white-space:normal}
.bubble.asst .text.rendered p{margin:0.5em 0}
.bubble.asst .text.rendered p:first-child{margin-top:0}
.bubble.asst .text.rendered p:last-child{margin-bottom:0}
.bubble.asst .text.rendered h1{font-size:1.3em;margin:0.8em 0 0.4em;font-weight:700;color:var(--accent)}
.bubble.asst .text.rendered h2{font-size:1.15em;margin:0.7em 0 0.35em;font-weight:600;color:var(--accent)}
.bubble.asst .text.rendered h3{font-size:1.05em;margin:0.6em 0 0.3em;font-weight:600}
.bubble.asst .text.rendered h4{font-size:1em;margin:0.5em 0 0.25em;font-weight:600}
.bubble.asst .text.rendered code{
  background:rgba(255,255,255,0.08);padding:2px 5px;border-radius:3px;
  font-family:var(--mono);font-size:0.88em;
}
.bubble.asst .text.rendered pre{
  background:#0d1117;border-radius:6px;padding:12px;margin:8px 0;
  overflow-x:auto;white-space:pre-wrap;word-break:break-word;
}
.bubble.asst .text.rendered pre code{background:none;padding:0;font-size:12px;line-height:1.5}
.bubble.asst .text.rendered a{color:var(--accent);text-decoration:underline}
.bubble.asst .text.rendered ul,.bubble.asst .text.rendered ol{margin:0.4em 0;padding-left:1.5em}
.bubble.asst .text.rendered li{margin:0.2em 0}
.bubble.asst .text.rendered blockquote{
  border-left:3px solid var(--accent);padding-left:12px;margin:0.5em 0;color:var(--muted);
}
.bubble.asst .text.rendered hr{border:none;border-top:1px solid #333;margin:1em 0}
.bubble.asst .text.rendered table{border-collapse:collapse;margin:0.5em 0;width:100%;font-size:13px}
.bubble.asst .text.rendered th,.bubble.asst .text.rendered td{border:1px solid #333;padding:6px 10px}
.bubble.asst .text.rendered th{background:rgba(255,255,255,0.05);font-weight:600}
.bubble.asst .text.rendered strong{font-weight:600}
.bubble.asst .text.rendered em{font-style:italic}
.bubble.asst .text .math-display{margin:0.8em 0;text-align:center;overflow-x:auto}
.bubble.asst .text .katex{color:inherit;font-size:1.05em}
.bubble.asst .text .katex-display{margin:0.5em 0;overflow-x:auto;padding:4px 0}
.bubble.asst .thinking{color:var(--muted);font-style:italic;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
/* === Action groups (browser activity cards) === */
@keyframes slideIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.action-group{margin:6px 0;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;font-size:12px;font-family:var(--mono);animation:slideIn 0.2s ease-out}
.ag-header{display:flex;align-items:center;gap:8px;padding:7px 12px;background:#111;border-bottom:1px solid #1e1e1e}
.ag-emoji{font-size:14px;flex-shrink:0}
.ag-site{color:var(--accent);font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ag-count{color:var(--muted);font-size:10px;flex-shrink:0}
.ag-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.ag-dot.running{animation:pulse 1.2s ease-in-out infinite}
.ag-dot.done{color:#4ade80}
.ag-steps{padding:2px 0 4px}
.action-step{display:flex;align-items:center;gap:7px;padding:3px 12px 3px 28px}
.as-emoji{font-size:11px;flex-shrink:0;width:16px;text-align:center}
.as-label{color:var(--text);font-size:11px;flex-shrink:0}
.as-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.as-dot{margin-left:auto;flex-shrink:0;font-size:10px;color:var(--muted)}
.as-dot.running{animation:pulse 1.2s ease-in-out infinite}
.as-dot.done{color:#4ade80}
.as-screenshot{padding:4px 12px 6px 28px}
.as-screenshot img{max-width:100%;border-radius:4px}
.intel-bars{padding:4px 12px 6px 28px;display:flex;flex-direction:column;gap:4px}
.intel-bar{display:flex;align-items:center;gap:8px;font-size:10.5px;font-family:var(--mono)}
.intel-bar-label{width:96px;color:var(--muted);flex-shrink:0;overflow:hidden;text-overflow:ellipsis}
.intel-bar-track{flex:1;height:3px;background:#2a2a2a;border-radius:2px;overflow:hidden}
.intel-bar-fill{height:100%;border-radius:2px;background:var(--accent)}
.intel-bar-pct{color:var(--text);width:32px;text-align:right;flex-shrink:0}
.action-standalone{display:flex;align-items:center;gap:8px;padding:5px 12px;margin:4px 0;border:1px solid #2a2a2a;border-radius:6px;font-size:12px;font-family:var(--mono);animation:slideIn 0.2s ease-out;background:#111}
.standalone-emoji{font-size:13px;flex-shrink:0}
.standalone-label{color:var(--accent);font-weight:500;flex-shrink:0}
.standalone-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.standalone-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.standalone-dot.running{animation:pulse 1.2s ease-in-out infinite}
.standalone-dot.done{color:#4ade80}
#agent-bar{
  display:none;padding:3px 16px;background:var(--surface);
  border-bottom:1px solid #222;font-size:11px;font-family:var(--mono);
  color:var(--muted);align-items:center;gap:10px;min-height:22px;
}
#agent-bar.active{display:flex}
#agent-action{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#nav-trail{display:flex;align-items:center;gap:0;overflow:hidden;font-size:10px;opacity:0.5;flex-shrink:0;max-width:45%}
#nav-trail span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100px}
#nav-trail span+span::before{content:' › ';opacity:0.6}
#turn-ctr{flex-shrink:0;opacity:0.4;font-size:10px}
.bubble.asst{position:relative}
.bubble.asst .copy-btn{
  position:absolute;top:6px;right:6px;
  background:rgba(255,255,255,0.1);border:none;color:var(--muted);
  padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;
  opacity:0;transition:opacity 0.15s;font-family:var(--mono);
}
.bubble.asst:hover .copy-btn{opacity:1}
.bubble.asst .copy-btn.copied{opacity:1;color:var(--accent)}

/* === Slot bar === */
#slotbar{
  display:flex;gap:6px;padding:4px 16px;
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#slotbar button{
  flex:1;height:32px;border:1px solid #444;border-radius:6px;
  background:transparent;color:var(--muted);font-size:12px;
  font-family:var(--mono);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:border-color 0.15s,color 0.15s;
}
#slotbar button:hover{border-color:var(--accent);color:var(--text)}
#slotbar button:active{transform:scale(0.95)}
#slotbar button.active{border-color:var(--accent);color:var(--accent);font-weight:600}
#slotbar button.empty{color:#555;font-style:italic}
#slotbar button.empty.active{color:var(--accent);font-style:normal}
#slotbar.locked button{pointer-events:none;opacity:0.4}
#slotbar.locked button.active{opacity:0.7}

/* === Model selector === */
#modelrow{
  display:flex;align-items:center;gap:8px;
  padding:4px 16px 0;flex-shrink:0;
}
#modelrow label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
#modelsel{
  height:28px;padding:0 8px;border:1px solid #444;border-radius:6px;
  background:var(--bg);color:var(--text);font-size:12px;
  font-family:var(--mono);cursor:pointer;
}
#modelsel:focus{outline:none;border-color:var(--accent)}
#profilesel{
  height:28px;padding:0 8px;border:1px solid #444;border-radius:6px;
  background:var(--bg);color:var(--text);font-size:12px;
  font-family:var(--mono);cursor:pointer;min-width:190px;
}
#profilesel:focus{outline:none;border-color:var(--accent)}
#model-notice{
  display:none;margin:0 16px;padding:8px 10px;border-radius:8px;
  border:1px solid #5a4a22;background:#2d2515;color:#dcc58a;
  font-size:12px;line-height:1.4;flex-shrink:0;
}
#model-notice strong{color:#f0d58b}

/* === Input === */
#inputbar{
  display:flex;gap:8px;
  padding:8px 16px;padding-bottom:max(16px,env(safe-area-inset-bottom));
  background:var(--surface);border-top:1px solid #333;flex-shrink:0;
}
#msginput{
  flex:1;min-width:0;min-height:44px;max-height:120px;padding:10px 12px;
  border:1px solid #444;border-radius:12px;
  background:var(--bg);color:var(--text);
  font-size:15px;font-family:-apple-system,system-ui,sans-serif;
  resize:none;line-height:1.4;
}
#msginput:focus{outline:none;border-color:var(--accent)}
#sendbtn{
  width:44px;height:44px;border:none;border-radius:12px;
  background:var(--accent);color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;align-self:flex-end;
}
#sendbtn:active{opacity:0.8}
#sendbtn:disabled{opacity:0.4;cursor:default}
#cancelbtn{
  width:44px;height:44px;border:none;border-radius:12px;
  background:#ff4444;color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;align-self:flex-end;
  display:none;
}
#cancelbtn:active{opacity:0.8}

/* === Download banner === */
#download-banner{
  display:flex;align-items:center;justify-content:center;gap:12px;
  padding:8px 16px;background:#2a1a1a;border-bottom:1px solid #444;
  font-size:13px;color:var(--muted);flex-shrink:0;
}
#download-banner .copy{display:flex;flex-direction:column;gap:2px;min-width:0}
#download-banner .detail{font-size:11px;color:#8f9199}
#download-banner a{
  color:var(--accent);text-decoration:none;font-weight:600;
  border:1px solid var(--accent);padding:4px 12px;border-radius:6px;
}
#download-banner a:hover{background:var(--accent);color:#fff}

/* === Chat hints === */
#chat-hints{
  display:flex;flex-direction:column;align-items:center;
  justify-content:flex-start;height:100%;padding-top:24px;text-align:center;
}
.hint-title{font-size:20px;font-weight:600;color:var(--accent);margin-bottom:8px}
.hint-sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.hint-examples{display:flex;flex-direction:column;gap:8px;width:100%;max-width:340px}
.hint-item{
  padding:10px 14px;border:1px solid #333;border-radius:10px;
  font-size:13px;color:var(--text);cursor:pointer;text-align:left;
  transition:border-color 0.15s;display:flex;align-items:center;gap:10px;
}
.hint-item:hover{border-color:var(--accent)}
.hint-emoji{font-size:18px;flex-shrink:0}

/* === Login value prop === */
.login-why{
  max-width:400px;margin:16px auto 0;text-align:left;
}
.login-why h3{font-size:14px;color:var(--accent);margin-bottom:10px;text-align:center;letter-spacing:1px;text-transform:uppercase}
.login-why-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.login-why-item{
  padding:10px;border:1px solid #2a2a3e;border-radius:8px;background:rgba(255,255,255,0.02);
}
.login-why-item strong{display:block;font-size:13px;color:var(--text);margin-bottom:2px}
.login-why-item span{font-size:11px;color:var(--muted);line-height:1.4}

/* === Upgrade banner === */
#upgrade-banner{
  display:none;margin:0 16px 6px;padding:10px 14px;border-radius:10px;
  border:1px solid rgba(251,191,36,0.3);background:rgba(251,191,36,0.06);
  font-size:13px;color:#dcc58a;line-height:1.5;flex-shrink:0;
  position:relative;
}
#upgrade-banner a{color:#fbbf24;font-weight:600}
#upgrade-banner .dismiss{
  position:absolute;top:8px;right:10px;background:none;border:none;
  color:var(--muted);font-size:14px;cursor:pointer;line-height:1;
}
#upgrade-banner .dismiss:hover{color:var(--text)}
#claude-request-banner{
  display:none;margin:0 16px 6px;padding:10px 14px;border-radius:10px;
  border:1px solid rgba(56,189,248,0.35);background:rgba(56,189,248,0.08);
  color:#bae6fd;font-size:13px;line-height:1.45;align-items:center;gap:12px;justify-content:space-between;
}
#claude-request-text{flex:1}
#claude-request-btn{
  border:none;background:#0ea5e9;color:#04111a;padding:7px 12px;border-radius:8px;
  font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;
}
#claude-request-btn:hover{opacity:0.92}
#claude-request-btn[disabled]{opacity:0.55;cursor:not-allowed}
@media (max-width:680px){
  #topbar .status{
    border:none;
    padding:0;
    border-radius:0;
    background:transparent;
  }
  #topbar .status.online,
  #topbar .status.warn{border-color:transparent}
}

/* Archive panel */
#archive-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;justify-content:center;align-items:center}
#archive-overlay.open{display:flex}
#archive-panel{background:var(--surface,#11161d);border:1px solid var(--line,#2a3341);border-radius:12px;width:90%;max-width:480px;max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
#archive-panel .archive-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line,#2a3341)}
#archive-panel .archive-header h3{font-size:16px;font-weight:600;color:var(--text,#edf2f7)}
#archive-panel .archive-close{background:none;border:none;color:var(--muted,#9da7b7);font-size:20px;cursor:pointer;padding:4px 8px}
#archive-panel .archive-close:hover{color:var(--text,#edf2f7)}
#archive-list{overflow-y:auto;flex:1;padding:8px}
.archive-item{display:flex;align-items:flex-start;gap:12px;padding:12px;border-radius:8px;cursor:default}
.archive-item:hover{background:var(--surface-elev,#171d26)}
.archive-item .archive-info{flex:1;min-width:0}
.archive-item .archive-preview{font-size:13px;color:var(--text,#edf2f7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.archive-item .archive-meta{font-size:11px;color:var(--muted,#9da7b7);margin-top:4px}
.archive-item .archive-actions{display:flex;gap:6px;flex-shrink:0}
.archive-item .archive-actions button{background:none;border:1px solid var(--line,#2a3341);color:var(--muted,#9da7b7);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
.archive-item .archive-actions button:hover{color:var(--text,#edf2f7);border-color:var(--muted,#9da7b7)}
.archive-item .archive-actions .restore-btn:hover{border-color:var(--accent,#ff6b4a);color:var(--accent,#ff6b4a)}
.archive-item .archive-actions .delete-btn:hover{border-color:#e53e3e;color:#e53e3e}
.archive-empty{text-align:center;color:var(--muted,#9da7b7);padding:40px 20px;font-size:14px}
</style>
</head>
<body>

<!-- Login -->
<div id="login">
  <h1>Unchained Trial</h1>
  <div class="sub">Free AI browser agent — no API key needed</div>
  <div style="margin:8px 0 4px;padding:6px 16px;border-radius:6px;background:#1a1a2d;border:1px solid #2d2d4a;color:#a78bfa;font-size:13px;letter-spacing:0.5px">Signing up as: <strong>Trial</strong></div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="outline"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
  <div class="login-why">
    <h3>Why connect your own browser?</h3>
    <div class="login-why-grid">
      <div class="login-why-item"><strong>Your logins</strong><span>Already signed into Gmail, GitHub? The agent uses them.</span></div>
      <div class="login-why-item"><strong>Your cookies</strong><span>No CAPTCHAs &mdash; sites see you, not a bot.</span></div>
      <div class="login-why-item"><strong>Your 2FA</strong><span>Works with authenticator apps and hardware keys.</span></div>
      <div class="login-why-item"><strong>Your IP</strong><span>Residential connection &mdash; no datacenter flags.</span></div>
    </div>
  </div>
  <a href="/local" style="color:#888;font-size:12px;margin-top:4px;text-decoration:none">Want full Claude access? Sign up here &rarr;</a>
</div>

<!-- Pending -->
<div id="pending" style="display:none;flex-direction:column;align-items:center;justify-content:center;height:100dvh;padding:24px;gap:16px;text-align:center">
  <h1 style="font-size:28px;color:var(--accent);letter-spacing:1px">Request Being Reviewed</h1>
  <p style="color:var(--muted);font-size:15px;max-width:400px">Your sign-up request is being reviewed. We'll send you an email once your account is approved.</p>
  <button onclick="checkApproval()" style="margin-top:16px;padding:10px 24px;border:1px solid var(--accent);border-radius:8px;background:transparent;color:var(--accent);font-size:14px;cursor:pointer">Check Again</button>
  <button onclick="backToLogin()" style="margin-top:8px;padding:10px 24px;border:1px solid #444;border-radius:8px;background:transparent;color:var(--muted);font-size:14px;cursor:pointer">Use a Different Account</button>
  <div id="pendingmsg" style="color:var(--muted);font-size:13px;min-height:20px"></div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <div class="left">
      <span class="agent" id="agentlabel"></span>
      <div class="status-stack">
        <span class="status" id="agentstatus">agent offline</span>
        <span class="status" id="bridgestatus">bridge offline</span>
      </div>
    </div>
    <div class="nav">
      <a href="/">Home</a>
      <a href="/demo">Demo</a>
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a href="#" onclick="openArchives();return false">Archives</a>
      <a href="/test" id="control-link" style="display:none">Control</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doDisconnect();return false">Logout</a>
    </div>
  </div>

  <div id="archive-overlay" onclick="if(event.target===this)closeArchives()">
    <div id="archive-panel">
      <div class="archive-header">
        <h3>Archived Chats</h3>
        <button class="archive-close" onclick="closeArchives()">&times;</button>
      </div>
      <div id="archive-list"></div>
    </div>
  </div>

  <div id="model-notice" aria-live="polite" style="display:block"><strong>Free tier</strong> &mdash; using lightweight models. <a href="/setup">Upgrade to Claude, Gemini, or Codex</a> for 10x better results.</div>
  <div id="claude-request-banner">
    <span id="claude-request-text"></span>
    <button id="claude-request-btn" onclick="requestClaudeAccess()">Request Claude Access</button>
  </div>

  <div id="slotbar">
    <button onclick="switchSlot(1)" id="slot1" title="Independent conversation session">Chat A</button>
    <button onclick="switchSlot(2)" id="slot2" title="Independent conversation session">Chat B</button>
    <button onclick="switchSlot(3)" id="slot3" title="Independent conversation session">Chat C</button>
  </div>

  <div id="agent-bar">
    <span id="agent-action"></span>
    <span id="nav-trail"></span>
    <span id="turn-ctr"></span>
  </div>

  <div id="download-banner" style="display:none">
    <span id="banner-msg">Connect your browser to browse.</span>
    <a href="#" onclick="showBannerInstall();return false" id="banner-curl">Install (curl)</a>
    <a href="/install" id="banner-connect">Download Agent Installer</a>
  </div>

  <!-- Install modal -->
  <div id="install-modal" style="display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.7);display:none;align-items:center;justify-content:center">
    <div style="background:var(--surface);border:1px solid #444;border-radius:12px;padding:24px;max-width:520px;width:90%;position:relative">
      <button onclick="closeInstallModal()" style="position:absolute;top:12px;right:12px;background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer">&times;</button>
      <h3 id="install-modal-title" style="color:var(--accent);margin-bottom:8px;font-size:16px">Install Agent</h3>
      <p id="install-modal-desc" style="color:var(--muted);font-size:13px;margin-bottom:16px">Installs a lightweight Python agent (~2MB) that connects your Chrome browser. Run in your terminal:</p>
      <div style="background:var(--bg);border:1px solid #333;border-radius:8px;padding:12px;font-family:var(--mono);font-size:12px;word-break:break-all;position:relative">
        <code id="install-cmd" style="color:var(--text)">Loading...</code>
        <button onclick="copyInstallCmd()" style="position:absolute;top:8px;right:8px;background:var(--accent);border:none;color:#fff;padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer" id="copy-btn">Copy</button>
      </div>
      <p id="install-modal-note" style="color:var(--muted);font-size:11px;margin-top:12px">Link expires in 15 minutes. Requires Python 3.8+ and curl.</p>
    </div>
  </div>

  <div id="chat">
      <div id="chat-hints">
        <div class="hint-title">Unchained Chat</div>
      <div class="hint-sub">Your AI-powered web assistant. Try a prompt below or type your own.</div>
      <div class="hint-examples">
        <div class="hint-item" onclick="fillMsg('Go to Wikipedia and look up the Eiffel Tower')"><span class="hint-emoji">&#127758;</span> Look up the Eiffel Tower on Wikipedia</div>
        <div class="hint-item" onclick="fillMsg('Check the weather forecast on weather.gov for New York City')"><span class="hint-emoji">&#9925;</span> Check the NYC weather on weather.gov</div>
        <div class="hint-item" onclick="fillMsg('Open Hacker News and list the top 5 stories right now')"><span class="hint-emoji">&#128240;</span> List the top 5 Hacker News stories</div>
      </div>
    </div>
  </div>

  <div id="modelrow">
    <label for="modelsel">Model</label>
    <select id="modelsel" onchange="onModelChange(this.value)">
      <option value="google/gemini-3-flash-preview">Gemini 3 Flash Preview &mdash; Default</option>
      <option value="arcee-ai/trinity-large-preview:free">Trinity &mdash; Fast</option>
      <option value="stepfun/step-3.5-flash:free">StepFun 3.5 Flash &mdash; Balanced</option>
      <option value="__custom_openrouter__" id="modelsel-custom-option" style="display:none">Custom OpenRouter (Admin)</option>
    </select>
  </div>
  <div id="model-custom-row" style="display:none;padding:4px 16px 0">
    <input id="model-custom-input"
           type="text"
           placeholder="Paste OpenRouter model, e.g. qwen/qwen3.5-flash-02-23"
           oninput="onCustomModelInput(this.value)"
           style="width:100%;height:28px;padding:0 8px;border:1px solid #444;border-radius:6px;background:var(--bg);color:var(--text);font-size:12px;font-family:var(--mono)">
  </div>
  <div id="upgrade-banner">
    Want better models? Claude, Gemini, and Codex are available with your own API key. <a href="/setup">Set up now &rarr;</a>
    <button class="dismiss" onclick="dismissUpgrade()">&times;</button>
  </div>
  <div id="inputbar">
    <textarea id="msginput" rows="1" placeholder="Ask the agent anything..."
              onkeydown="handleKey(event)" oninput="autoGrow(this)"></textarea>
    <button id="sendbtn" onclick="doSend()">&#9654;</button>
    <button id="cancelbtn" onclick="doCancel()">&#9632;</button>
  </div>
</div>
<script>

let agentId = '';
let sessionId = '';
let sending = false;
let _cancelCtrl = null;
let _isAdmin = false;
let _userName = '';
let _userPicture = '';
let _openrouterUsage = null;
let _accountStatus = 'approved';
let _claudeAccessRequested = false;
let _POST_CAP_ALLOWED_MODELS = ['arcee-ai/trinity-large-preview:free', 'stepfun/step-3.5-flash:free'];

function _nextAfterLogin() {
  const raw = (new URLSearchParams(window.location.search).get('next') || '').trim();
  if (!raw) return '';
  if (!raw.startsWith('/')) return '';
  if (raw.startsWith('//')) return '';
  if (raw.includes('://')) return '';
  return raw;
}

function _redirectAfterLoginIfNeeded() {
  const next = _nextAfterLogin();
  if (!next) return false;
  if (next === window.location.pathname) return false;
  window.location.href = next;
  return true;
}

function _applyAuthState(data) {
  _isAdmin = !!data.is_admin;
  _userName = data.name || '';
  _userPicture = data.picture || '';
  _openrouterUsage = data.openrouter_usage || null;
  _accountStatus = data.status || (data.pending ? 'pending' : 'approved');
  _claudeAccessRequested = !!data.claude_access_requested;
}

async function handleGoogleCredential(response) {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/google', {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential, source: 'trial'}),
    });
    const data = await r.json();
    _applyAuthState(data);
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    // Confirm the cookie-backed session is readable before leaving login UI.
    for (let i = 0; i < 5; i++) {
      try {
        const meResp = await fetch('/auth/me', {
          credentials: 'include',
          cache: 'no-store',
        });
        const me = await meResp.json();
        _applyAuthState(me);
        if (me.authenticated) {
          agentId = me.agent_id || data.agent_id || '';
          if (_redirectAfterLoginIfNeeded()) return;
          showMain();
          return;
        }
        if (me.pending || me.status === 'pending') {
          showPending();
          return;
        }
      } catch (e) {}
      await new Promise(resolve => setTimeout(resolve, 150 * (i + 1)));
    }
    if (data.pending || data.status === 'pending' || data.review_pending) {
      showPending();
      return;
    }
    if (data.agent_id) {
      agentId = data.agent_id;
      if (_redirectAfterLoginIfNeeded()) return;
      showMain();
      return;
    }
    errEl.textContent = 'Sign-in succeeded, but session was not established. Refresh and try again.';
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me', {
      credentials: 'include',
      cache: 'no-store',
    });
    const data = await r.json();
    _applyAuthState(data);
    if (data.authenticated) { agentId = data.agent_id || ''; showMain(); return; }
    if (data.pending || data.status === 'pending') { showPending(); return; }
  } catch(e) {}
  document.getElementById('login').style.display = 'flex';
}

async function checkApproval() {
  const msg = document.getElementById('pendingmsg');
  msg.textContent = 'Checking...';
  try {
    const r = await fetch('/auth/me', {
      credentials: 'include',
      cache: 'no-store',
    });
    const data = await r.json();
    _applyAuthState(data);
    if (data.authenticated) { agentId = data.agent_id || ''; showMain(); return; }
    if (data.pending || data.status === 'pending') {
      msg.textContent = 'Still under review. Check back soon!';
      return;
    }
    msg.textContent = 'Still under review.';
  } catch(e) { msg.textContent = 'Could not check status.'; }
}

async function doDisconnect() {
  await fetch('/auth/logout', {method: 'POST'});
  agentId = '';
  sessionId = '';
  _accountStatus = 'approved';
  _claudeAccessRequested = false;
  document.getElementById('login').style.display = 'flex';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
}

function renderClaudeRequestBanner() {
  const banner = document.getElementById('claude-request-banner');
  const text = document.getElementById('claude-request-text');
  const btn = document.getElementById('claude-request-btn');
  if (!banner || !text || !btn) return;
  if (_accountStatus !== 'pending') {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'flex';
  if (_claudeAccessRequested) {
    text.textContent = 'Claude access request submitted. An admin will review and unlock all routes after approval.';
    btn.style.display = 'none';
    return;
  }
  text.textContent = 'Need full access to /local and /setup? Request Claude access and we will route it for admin approval.';
  btn.style.display = 'inline-flex';
  btn.disabled = false;
  btn.textContent = 'Request Claude Access';
}

async function requestClaudeAccess() {
  const text = document.getElementById('claude-request-text');
  const btn = document.getElementById('claude-request-btn');
  if (!text || !btn || _claudeAccessRequested) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Sending...';
  try {
    const r = await fetch('/auth/request-claude-access', {method: 'POST'});
    const data = await r.json();
    if (!r.ok) {
      text.textContent = data.error || 'Could not submit request right now.';
      btn.disabled = false;
      btn.textContent = prev;
      return;
    }
    _accountStatus = data.status || _accountStatus;
    _claudeAccessRequested = !!data.claude_access_requested;
    renderClaudeRequestBanner();
  } catch (e) {
    text.textContent = 'Could not submit request right now.';
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function showPending() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'flex';
}

async function backToLogin() {
  await fetch('/auth/logout', {method: 'POST'});
  _accountStatus = 'approved';
  _claudeAccessRequested = false;
  document.getElementById('pending').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

function currentModel() {
  const selected = document.getElementById('modelsel').value;
  if (selected === '__custom_openrouter__') {
    const custom = (document.getElementById('model-custom-input')?.value || '').trim();
    if (custom) return custom;
    return _defaultTrialModel();
  }
  return selected;
}

function _defaultTrialModel() {
  const sel = document.getElementById('modelsel');
  if (!sel) return '';
  if (_openrouterUsage && _openrouterUsage.capped) {
    for (const model of _POST_CAP_ALLOWED_MODELS) {
      if (_modelOptionExists(model)) return model;
    }
  }
  for (const opt of sel.options) {
    if (opt.value !== '__custom_openrouter__') return opt.value;
  }
  return '';
}

function _modelOptionExists(value) {
  return !!document.querySelector('#modelsel option[value="' + CSS.escape(value) + '"]');
}

function _isOpenRouterModelId(value) {
  return (value || '').includes('/');
}

function _isPostCapAllowedModel(value) {
  return _POST_CAP_ALLOWED_MODELS.includes((value || '').trim());
}

function _applyOpenRouterCapUi() {
  const sel = document.getElementById('modelsel');
  if (!sel) return;
  const notice = document.getElementById('model-notice');
  const capped = !!(_openrouterUsage && _openrouterUsage.capped);
  for (const opt of Array.from(sel.options)) {
    const v = opt.value;
    if (v === '__custom_openrouter__') continue;
    if (!capped) {
      opt.disabled = false;
      opt.style.display = '';
      continue;
    }
    const allowed = _isPostCapAllowedModel(v);
    opt.disabled = !allowed;
    opt.style.display = allowed ? '' : 'none';
  }
  if (capped) {
    const current = currentModel();
    if (!_isPostCapAllowedModel(current)) {
      const forced = _defaultTrialModel();
      if (_modelOptionExists(forced)) {
        sel.value = forced;
        localStorage.setItem('unchained_model', forced);
      }
    }
    if (notice) {
      notice.innerHTML = '<strong>Trial budget reached</strong> &mdash; available models are Trinity and StepFun.';
    }
  } else if (notice) {
    notice.innerHTML = '<strong>Free tier</strong> &mdash; using lightweight models. <a href="/setup">Upgrade to Claude, Gemini, or Codex</a> for 10x better results.';
  }
}

function _syncCustomModelUi() {
  const sel = document.getElementById('modelsel');
  const customOption = document.getElementById('modelsel-custom-option');
  const customRow = document.getElementById('model-custom-row');
  if (!sel) return;
  const capped = !!(_openrouterUsage && _openrouterUsage.capped);
  if (customOption) customOption.style.display = (_isAdmin && !capped) ? '' : 'none';
  if ((!_isAdmin || capped) && sel.value === '__custom_openrouter__') {
    sel.value = _defaultTrialModel();
  }
  if (customRow) {
    customRow.style.display = (_isAdmin && !capped && sel.value === '__custom_openrouter__') ? 'block' : 'none';
  }
}

function _sessionStoreKey() {
  return 'unchained_session_' + agentId + '_openrouter';
}

function _restoreSessionId() {
  const sid = localStorage.getItem(_sessionStoreKey()) || '';
  const prefix = 's-' + agentId + '-';
  return (sid.startsWith(prefix) && sid.length > prefix.length) ? sid : '';
}

function _persistSessionId(sid) {
  if (sid && sid.startsWith('s-' + agentId)) {
    localStorage.setItem(_sessionStoreKey(), sid);
  }
}

let activeSlot = 1;

function _slotLabel(n) {
  return (['Chat A', 'Chat B', 'Chat C'][n - 1] || ('Chat ' + n));
}

function _slotStateKey() {
  return _sessionStoreKey() + '_slots_v1';
}

function _newSessionId() {
  return 's-' + agentId + '-' + Date.now().toString(36);
}

function _loadSlotState() {
  let state = {active_slot: 1, slots: {}};
  try {
    const raw = localStorage.getItem(_slotStateKey()) || '';
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        state = parsed;
      }
    }
  } catch(e) {}

  const slots = (state && typeof state.slots === 'object' && state.slots) ? state.slots : {};
  const normalized = {};
  for (let i = 1; i <= 3; i++) {
    const sid = String(slots[String(i)] || '').trim();
    normalized[String(i)] = (sid.startsWith('s-' + agentId + '-') ? sid : '');
  }
  let active = Number(state && state.active_slot);
  if (active !== 1 && active !== 2 && active !== 3) active = 1;
  return {active_slot: active, slots: normalized};
}

function _saveSlotState(state) {
  try {
    localStorage.setItem(_slotStateKey(), JSON.stringify(state));
  } catch(e) {}
}

function _ensureSlotState() {
  const state = _loadSlotState();
  const restored = _restoreSessionId();
  if (!state.slots['1']) state.slots['1'] = restored || _newSessionId();
  for (let i = 1; i <= 3; i++) {
    if (!state.slots[String(i)]) state.slots[String(i)] = _newSessionId();
  }
  if (!state.slots[String(state.active_slot)]) state.active_slot = 1;
  _saveSlotState(state);
  return state;
}

function _setActiveSlotSession(sid) {
  if (!sid || !sid.startsWith('s-' + agentId + '-')) return;
  const state = _loadSlotState();
  state.slots[String(activeSlot)] = sid;
  state.active_slot = activeSlot;
  _saveSlotState(state);
}

function _syncSlotButtons() {
  const state = _loadSlotState();
  activeSlot = state.active_slot;
  for (let i = 1; i <= 3; i++) {
    const btn = document.getElementById('slot' + i);
    if (!btn) continue;
    btn.className = '';
    btn.textContent = _slotLabel(i);
    if (i === activeSlot) btn.classList.add('active');
  }
}

async function switchSlot(n) {
  if (n === activeSlot) return;
  if (sending) return;
  const state = _loadSlotState();
  state.active_slot = (n === 1 || n === 2 || n === 3) ? n : 1;
  if (!state.slots[String(state.active_slot)]) state.slots[String(state.active_slot)] = _newSessionId();
  _saveSlotState(state);
  activeSlot = state.active_slot;
  sessionId = state.slots[String(activeSlot)];
  _persistSessionId(sessionId);
  _syncSlotButtons();
  document.getElementById('chat').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>';
  await loadHistory();
}

function onModelChange(model) {
  if (_openrouterUsage && _openrouterUsage.capped && !_isPostCapAllowedModel(model)) {
    const forced = _defaultTrialModel();
    if (_modelOptionExists(forced)) {
      document.getElementById('modelsel').value = forced;
      localStorage.setItem('unchained_model', forced);
    }
    _syncCustomModelUi();
    checkAgentStatus();
    return;
  }
  _syncCustomModelUi();
  if (model === '__custom_openrouter__') {
    const custom = (document.getElementById('model-custom-input')?.value || '').trim();
    if (custom) localStorage.setItem('unchained_model', custom);
  } else {
    localStorage.setItem('unchained_model', model);
  }
  // Refresh model-scoped agent status immediately on selector change.
  checkAgentStatus();
}

function onCustomModelInput(value) {
  const model = (value || '').trim();
  if (model) localStorage.setItem('unchained_model', model);
}

let lastAgentConnected = false;
let lastCodexCliSupported = true;

function updateStatusPill(el, text, mode) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status' + (mode ? ' ' + mode : '');
}

function updateAgentStatusUI(data) {
  const el = document.getElementById('agentstatus');
  const bridgeEl = document.getElementById('bridgestatus');
  const banner = document.getElementById('download-banner');
  const bannerMsg = document.getElementById('banner-msg');
  const bannerCurl = document.getElementById('banner-curl');
  const bannerConnect = document.getElementById('banner-connect');
  const chatConnected = !!data.chat_connected;
  const bridgeConnected = !!data.bridge_connected;
  const mismatch = !!data.mismatch;

  if (chatConnected) updateStatusPill(el, 'agent online', 'online');
  else if (mismatch) updateStatusPill(el, 'agent mismatch', 'warn');
  else updateStatusPill(el, 'agent offline', '');

  if (bridgeConnected) updateStatusPill(bridgeEl, 'bridge online', 'online');
  else updateStatusPill(bridgeEl, 'bridge offline', '');

  if (bannerMsg) bannerMsg.textContent = 'Connect your browser to browse.';
  if (bannerCurl) { const w = typeof _isWindows==='function'&&_isWindows(); bannerCurl.textContent = mismatch ? (w ? 'Reinstall (PowerShell)' : 'Reinstall (curl)') : (w ? 'Install (PowerShell)' : 'Install (curl)'); }
  if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';

  if (banner) {
    if (chatConnected && bridgeConnected) {
      banner.style.display = 'none';
    } else {
      if (chatConnected && !bridgeConnected && bannerMsg) {
        bannerMsg.textContent = 'Your browser bridge is offline on this machine.';
      } else if (mismatch && bannerMsg) {
        bannerMsg.textContent = 'A different local chat agent is connected for this account.';
      }
      banner.style.display = 'flex';
    }
  }
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  renderClaudeRequestBanner();
  document.getElementById('agentlabel').textContent = _userName || 'Unchained';
  if (_isAdmin) { const cl = document.getElementById('control-link'); if (cl) cl.style.display = ''; }
  try { localStorage.setItem('unchained_last_route', '/trial'); } catch(e){}
  _syncCustomModelUi();
  const params = new URLSearchParams(window.location.search);
  const fromQuery = (params.get('model') || '').trim();
  const saved = (localStorage.getItem('unchained_model') || '').trim();
  const requestedModel = fromQuery || saved;
  if (_isAdmin && requestedModel && _isOpenRouterModelId(requestedModel) && !_modelOptionExists(requestedModel)) {
    document.getElementById('modelsel').value = '__custom_openrouter__';
    const customInput = document.getElementById('model-custom-input');
    if (customInput) customInput.value = requestedModel;
  } else if (requestedModel && _modelOptionExists(requestedModel)) {
    document.getElementById('modelsel').value = requestedModel;
  }
  _applyOpenRouterCapUi();
  _syncCustomModelUi();
  const slotState = _ensureSlotState();
  activeSlot = slotState.active_slot;
  sessionId = slotState.slots[String(activeSlot)] || _restoreSessionId() || _newSessionId();
  _persistSessionId(sessionId);
  _setActiveSlotSession(sessionId);
  _syncSlotButtons();
  checkAgentStatus();
  setInterval(checkAgentStatus, 10000);
  loadHistory();
}

async function checkAgentStatus() {
  try {
    const r = await fetch('/web/chat/status?model=' + encodeURIComponent(currentModel()));
    if (r.ok) {
      const data = await r.json();
      lastAgentConnected = data.connected;
      updateAgentStatusUI(data);
    }
  } catch(e) {}
}

async function loadHistory() {
  _syncSlotButtons();
  try {
    const qs = new URLSearchParams({
      model: currentModel(),
      session_id: sessionId,
    });
    const r = await fetch('/web/chat/history?' + qs.toString());
    if (!r.ok) return;
    const data = await r.json();
    const chatEl = document.getElementById('chat');
    if (chatEl) chatEl.innerHTML = '';
    if (data.session_id) {
      sessionId = data.session_id;
      _persistSessionId(sessionId);
      _setActiveSlotSession(sessionId);
    }
    if (!data.messages || data.messages.length === 0) {
      showHintsIfEmpty();
      return;
    }
    hideHints();
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        addUserBubble(msg.content);
      } else if (msg.role === 'assistant') {
        const bubble = addAsstBubble();
        const thinking = bubble.querySelector('.thinking');
        if (thinking) thinking.remove();
        appendText(bubble, msg.content);
      }
    }
    showClaudeUpgradeCard();
  } catch(e) {}
}

function showHintsIfEmpty() {
  if (document.getElementById('chat-hints')) return;
  document.getElementById('chat').innerHTML =
    '<div id="chat-hints">' +
    '<div class="hint-title">Unchained Chat</div>' +
    '<div class="hint-sub">Your AI-powered web assistant. Try a prompt below or type your own.</div>' +
    '<div class="hint-examples">' +
    '<div class="hint-item" onclick="fillMsg(\'Go to Wikipedia and look up the Eiffel Tower\')"><span class="hint-emoji">&#127758;</span> Look up the Eiffel Tower on Wikipedia</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Check the weather forecast on weather.gov for New York City\')"><span class="hint-emoji">&#9925;</span> Check the NYC weather on weather.gov</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Open Hacker News and list the top 5 stories right now\')"><span class="hint-emoji">&#128240;</span> List the top 5 Hacker News stories</div>' +
    '</div></div>';
}

async function doNewChat() {
  if (sending) return;
  document.getElementById('chat').innerHTML = '';
  showHintsIfEmpty();
  try {
    const r = await fetch('/web/chat/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        model: currentModel(),
        session_id: sessionId,
      }),
    });
    if (r.ok) {
      const data = await r.json();
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
        _setActiveSlotSession(sessionId);
      }
    }
  } catch(e) {}
  _syncSlotButtons();
}

async function openArchives() {
  const overlay = document.getElementById('archive-overlay');
  overlay.classList.add('open');
  const list = document.getElementById('archive-list');
  list.innerHTML = '<div class="archive-empty">Loading...</div>';
  try {
    const r = await fetch('/web/chat/archives?model=' + encodeURIComponent(currentModel()));
    if (!r.ok) { list.innerHTML = '<div class="archive-empty">Failed to load archives</div>'; return; }
    const data = await r.json();
    const archives = data.archives || [];
    if (archives.length === 0) {
      list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
      return;
    }
    list.innerHTML = '';
    for (const arc of archives) {
      const d = new Date(arc.archived_at * 1000);
      const dateStr = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      const div = document.createElement('div');
      div.className = 'archive-item';
      div.innerHTML =
        '<div class="archive-info">' +
          '<div class="archive-preview">' + esc(arc.preview || '(empty)') + '</div>' +
          '<div class="archive-meta">' + arc.message_count + ' messages &middot; ' + dateStr + '</div>' +
        '</div>' +
        '<div class="archive-actions">' +
          '<button class="restore-btn" data-id="' + esc(arc.id) + '">Restore</button>' +
          '<button class="delete-btn" data-id="' + esc(arc.id) + '">Delete</button>' +
        '</div>';
      div.querySelector('.restore-btn').onclick = () => restoreArchive(arc.id);
      div.querySelector('.delete-btn').onclick = () => deleteArchive(arc.id, div);
      list.appendChild(div);
    }
  } catch(e) {
    list.innerHTML = '<div class="archive-empty">Error loading archives</div>';
  }
}

function closeArchives() {
  document.getElementById('archive-overlay').classList.remove('open');
}

async function restoreArchive(id) {
  if (!confirm('Restore this archived conversation? It will replace your current chat.')) return;
  try {
    const r = await fetch('/web/chat/restore-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
        _setActiveSlotSession(sessionId);
      }
      closeArchives();
      location.reload();
    } else {
      const data = await r.json().catch(() => ({}));
      alert(data.error || 'Failed to restore archive');
    }
  } catch(e) { alert('Failed to restore archive'); }
}

async function deleteArchive(id, el) {
  if (!confirm('Delete this archived chat permanently?')) return;
  try {
    const r = await fetch('/web/chat/delete-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (r.ok) {
      el.remove();
      const list = document.getElementById('archive-list');
      if (!list.querySelector('.archive-item')) {
        list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
      }
    }
  } catch(e) {}
}

checkSession();
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    doSend();
  }
}

function scrollToBottom() {
  const chat = document.getElementById('chat');
  chat.scrollTop = chat.scrollHeight;
}

function fillMsg(text) {
  const input = document.getElementById('msginput');
  input.value = text;
  input.focus();
  autoGrow(input);
}

function hideHints() {
  const hints = document.getElementById('chat-hints');
  if (hints) hints.remove();
}

function addUserBubble(text) {
  hideHints();
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble user';
  div.textContent = text;
  chat.appendChild(div);
  scrollToBottom();
}

function addAsstBubble() {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble asst';
  div.innerHTML = '<button class="copy-btn" onclick="copyBubbleText(this)">Copy</button><div class="thinking">thinking...</div>';
  chat.appendChild(div);
  scrollToBottom();
  return div;
}

const TOOL_META = {
  navigate:   {emoji:'🌐', label:'Navigate'},
  ddm:        {emoji:'👁', label:'Look'},
  click:      {emoji:'👆', label:'Click'},
  type:       {emoji:'⌨️', label:'Type'},
  intel:      {emoji:'🔬', label:'Analyze'},
  screenshot: {emoji:'📷', label:'Screenshot'},
  js:         {emoji:'⚡', label:'Script'},
  websearch:  {emoji:'🔍', label:'Search'},
  webfetch:   {emoji:'📥', label:'Fetch'},
  bash:       {emoji:'⚙️', label:'Command'},
  intervention: {emoji:'🛑', label:'Nudge'},
  intervention_screenshot: {emoji:'📷', label:'Nudge Screenshot'},
};

function toolFriendlyDesc(name, input) {
  if (!input) return '';
  const i = input.trim();
  if (name === 'navigate') {
    try { const u = new URL(i); return u.hostname + (u.pathname !== '/' ? u.pathname.slice(0,30) : ''); }
    catch { return i.slice(0,50); }
  }
  if (name === 'ddm') {
    if (i.includes('--at')) return 'inspect element';
    if (i.includes('--tabs')) return 'list tabs';
    if (i.includes('--new')) { const m=i.match(/https?:\/\/([^/\s]+)/); return m ? 'open '+m[1] : 'new tab'; }
    if (i.includes('--close')) return 'close tab';
    if (i.includes('--text')) { const m=i.match(/--find\s+"?([^"\s]+)/); return m ? 'find "'+m[1]+'"' : 'read text'; }
    if (i.includes('--llm-2pass')) return 'map layout';
    if (i.includes('--js')) return 'run js';
    if (i.includes('--api')) return 'find api';
    if (i.includes('--forms')) return 'find forms';
    return 'scan';
  }
  if (name === 'intel') {
    if (i.includes('--probe')) return 'probe page type';
    if (i.includes('--extract')) { const m=i.match(/--strategy\s+(\S+)/); return m ? 'extract ('+m[1]+')' : 'extract data'; }
    if (i.includes('--stores')) return 'find data stores';
    if (i.includes('--shape')) { const m=i.match(/--shape\s+(\S+)/); return m ? 'map '+m[1] : 'map data'; }
    if (i.includes('--find-paths')) return 'find paths';
    return 'analyze';
  }
  if (name === 'click') { const m=i.match(/(\d+)\s+(\d+)/); return m ? '('+m[1]+','+m[2]+')' : i.slice(0,30); }
  if (name === 'type') return '"' + i.replace(/^["']|["']$/g,'').slice(0,40) + '"';
  if (name === 'js') return i.slice(0,50).replace(/\n/g,' ');
  if (name === 'websearch') return '"' + i.slice(0,50) + '"';
  if (name === 'webfetch') { try { return new URL(i).hostname; } catch { return i.slice(0,40); } }
  return i.slice(0,50);
}

const BROWSER_TOOLS = new Set(['navigate','ddm','click','type','intel','screenshot','js','intervention','intervention_screenshot']);
let _currentGroup = null;
let _currentGroupSteps = 0;
let _currentGroupDot = null;
let _turnCount = 0;
let _navTrail = [];

function renderNavTrail() {
  const el = document.getElementById('nav-trail');
  if (el) el.innerHTML = _navTrail.map(s => '<span>' + esc(s) + '</span>').join('');
}

function _finalizeGroup() {
  if (!_currentGroup) return;
  if (_currentGroupDot) {
    _currentGroupDot.className = 'ag-dot done';
    _currentGroupDot.textContent = '✓';
  }
  const countEl = _currentGroup.querySelector('.ag-count');
  if (countEl && _currentGroupSteps > 0) {
    countEl.textContent = _currentGroupSteps + (_currentGroupSteps !== 1 ? ' steps' : ' step');
  }
  _currentGroup = null;
  _currentGroupDot = null;
  _currentGroupSteps = 0;
}

function addToolCall(bubble, name, input) {
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  const meta = TOOL_META[name] || {emoji:'⚙️', label:name};
  const desc = toolFriendlyDesc(name, input);

  if (name === 'navigate' && desc) {
    _navTrail.push(desc);
    if (_navTrail.length > 4) _navTrail.shift();
    renderNavTrail();
  }

  _turnCount++;
  const actionEl = document.getElementById('agent-action');
  if (actionEl) actionEl.textContent = meta.label + (desc ? '  ' + desc : '');
  const turnEl = document.getElementById('turn-ctr');
  if (turnEl) turnEl.textContent = 't' + _turnCount;
  document.getElementById('agent-bar').classList.add('active');

  if (BROWSER_TOOLS.has(name)) {
    // Determine site hostname for grouping
    let hostname = '';
    if (name === 'navigate') {
      try { hostname = new URL(input.trim()).hostname; } catch { hostname = input.trim().slice(0,30); }
    } else {
      hostname = _currentGroup ? _currentGroup.dataset.site : 'browser';
    }
    // Navigate to a new site starts a new group
    if (name === 'navigate' && (!_currentGroup || _currentGroup.dataset.site !== hostname)) {
      _finalizeGroup();
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = hostname;
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">🌐</span>' +
          '<span class="ag-site">' + esc(hostname) + '</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">●</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    } else if (!_currentGroup) {
      // No group yet — create a generic browser group
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = 'browser';
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">🌐</span>' +
          '<span class="ag-site">browser</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">●</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    }
    // Add step row inside current group
    _currentGroupSteps++;
    const steps = _currentGroup.querySelector('.ag-steps');
    const step = document.createElement('div');
    step.className = 'action-step';
    step.innerHTML =
      '<span class="as-emoji">' + meta.emoji + '</span>' +
      '<span class="as-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="as-desc">' + esc(desc) + '</span>' : '') +
      '<span class="as-dot running">●</span>';
    steps.appendChild(step);
    scrollToBottom();
    return step;
  } else {
    // Standalone action (websearch, webfetch, bash)
    const sa = document.createElement('div');
    sa.className = 'action-standalone';
    sa.innerHTML =
      '<span class="standalone-emoji">' + meta.emoji + '</span>' +
      '<span class="standalone-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="standalone-desc">' + esc(desc) + '</span>' : '') +
      '<span class="standalone-dot running">●</span>';
    bubble.appendChild(sa);
    scrollToBottom();
    return sa;
  }
}

function parseIntelBars(text) {
  const m = text.match(/strategy:\s*(\S+)\s*\((\d+)%\)(?:.*?runner-up:\s*(\S+)\s*\((\d+)%\))?/);
  if (!m) return null;
  const bars = [{label: m[1], pct: parseInt(m[2])}];
  if (m[3]) bars.push({label: m[3], pct: parseInt(m[4])});
  return bars;
}

function setToolResult(el, result, isScreenshot, visible) {
  const isStep = el.classList.contains('action-step');
  const dotCls = isStep ? 'as-dot' : 'standalone-dot';
  const dot = el.querySelector('.' + dotCls);
  if (dot) { dot.className = dotCls + ' done'; dot.textContent = '✓'; }

  if (isScreenshot && visible) {
    const imgDiv = document.createElement('div');
    imgDiv.className = 'as-screenshot';
    imgDiv.innerHTML = '<img src="data:image/png;base64,' + result + '">';
    el.after(imgDiv);
  } else if (!isScreenshot) {
    const bars = parseIntelBars(result);
    if (bars) {
      const barsDiv = document.createElement('div');
      barsDiv.className = 'intel-bars';
      barsDiv.innerHTML = bars.map(b =>
        '<div class="intel-bar">' +
          '<span class="intel-bar-label">' + esc(b.label) + '</span>' +
          '<span class="intel-bar-track"><span class="intel-bar-fill" style="width:' + b.pct + '%"></span></span>' +
          '<span class="intel-bar-pct">' + b.pct + '%</span>' +
        '</div>'
      ).join('');
      el.after(barsDiv);
    } else {
      // For navigate results, extract and show the page title in the site card header
      const titleM = result.match(/Title:\s*(.+)/);
      if (titleM && _currentGroup) {
        const siteEl = _currentGroup.querySelector('.ag-site');
        if (siteEl) {
          const title = titleM[1].trim().slice(0, 60);
          siteEl.title = title;  // tooltip with full title
          // Show truncated title next to hostname
          const host = _currentGroup.dataset.site;
          siteEl.textContent = host + (title ? '  —  ' + title : '');
        }
      }
    }
  }
  scrollToBottom();
}

// --- Markdown + KaTeX rendering ---
let _markedConfigured = false;
function ensureMarkedConfigured() {
  if (_markedConfigured || typeof marked === 'undefined') return;
  _markedConfigured = true;
  const blockMath = {
    name: 'blockMath',
    level: 'block',
    start(src) { return src.indexOf('$$'); },
    tokenizer(src) {
      const m = src.match(/^\$\$([\s\S]+?)\$\$/);
      if (m) return { type: 'blockMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<pre>' + esc(token.raw) + '</pre>';
      try { return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>'; }
      catch(e) { return '<pre>' + esc(token.raw) + '</pre>'; }
    }
  };
  const inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start(src) { return src.indexOf('$'); },
    tokenizer(src) {
      const m = src.match(/^\$([^\$\n]+?)\$/);
      if (m) return { type: 'inlineMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<code>' + esc(token.raw) + '</code>';
      const isDisplay = /\\begin\{/.test(token.text);
      try {
        if (isDisplay) return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>';
        return katex.renderToString(token.text, {displayMode:false,throwOnError:false});
      }
      catch(e) { return '<code>' + esc(token.raw) + '</code>'; }
    }
  };
  marked.use({ extensions: [blockMath, inlineMath] });
}

function appendText(bubble, text) {
  // Remove thinking indicator if present
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  if (!bubble._rawText) bubble._rawText = '';
  bubble._rawText += text;

  let span = bubble.querySelector('.text');
  if (!span) {
    span = document.createElement('div');
    span.className = 'text';
    bubble.appendChild(span);
  }

  ensureMarkedConfigured();
  if (typeof marked !== 'undefined') {
    try {
      span.innerHTML = marked.parse(bubble._rawText);
      span.classList.add('rendered');
    } catch(e) {
      span.textContent = bubble._rawText;
      span.classList.remove('rendered');
    }
  } else {
    span.textContent = bubble._rawText;
  }
  scrollToBottom();
}

function copyBubbleText(btn) {
  const bubble = btn.closest('.bubble.asst');
  if (!bubble) return;
  const text = bubble._rawText || (bubble.querySelector('.text') ? bubble.querySelector('.text').textContent : '');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

async function doCancel() {
  try {
    await fetch('/web/chat/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId}),
    });
  } catch(e) {}
  if (_cancelCtrl) _cancelCtrl.abort();
}

async function doSend() {
  if (sending) return;
  const input = document.getElementById('msginput');
  const msg = input.value.trim();
  if (!msg) return;
  const model = currentModel();
  if (_isAdmin && document.getElementById('modelsel').value === '__custom_openrouter__' && !_isOpenRouterModelId(model)) {
    alert('Enter a valid OpenRouter model ID like qwen/qwen3.5-flash-02-23');
    return;
  }
  input.value = '';
  input.style.height = 'auto';

  sending = true;
  document.getElementById('sendbtn').style.display = 'none';
  document.getElementById('cancelbtn').style.display = 'block';
  const slotbar = document.getElementById('slotbar');
  if (slotbar) slotbar.classList.add('locked');

  _incTrialMsgCount();
  addUserBubble(msg);
  const bubble = addAsstBubble();

  let currentTool = null;
  _cancelCtrl = new AbortController();

  try {
    const r = await fetch('/web/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg,
        agent_id: agentId,
        session_id: sessionId,
        model: model,
        slot: activeSlot,
      }),
      signal: _cancelCtrl.signal,
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({error: 'Request failed'}));
      const thinking = bubble.querySelector('.thinking');
      if (thinking) thinking.remove();
      if (r.status === 429 && err.error === 'turn_rate_limit') {
        bubble.parentNode.removeChild(bubble);
        const userBubbles = document.querySelectorAll('.bubble.user');
        const lastUser = userBubbles[userBubbles.length - 1];
        if (lastUser) lastUser.remove();
        let statusMsg;
        if (err.daily_remaining === 0) {
          statusMsg = 'Daily limit reached. Resets tomorrow.';
        } else {
          const s = Math.ceil(err.resets_in || 0);
          const m = Math.floor(s / 60);
          const rs = s % 60;
          statusMsg = 'Rate limit reached. Try again in ' + (m > 0 ? m + 'm ' : '') + rs + 's.';
        }
        const notice = document.createElement('div');
        notice.className = 'bubble asst';
        notice.style.cssText = 'border-color:#5a4a22;background:#2d2515;color:#dcc58a;font-size:13px';
        notice.textContent = statusMsg;
        document.getElementById('chat').appendChild(notice);
        scrollToBottom();
        return;
      }
      appendText(bubble, 'Error: ' + (err.error || r.statusText));
      if (r.status === 401 || r.status === 403) doDisconnect();
      return;
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});

      let nl;
      while ((nl = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, nl);
        buf = buf.slice(nl + 2);

        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6);
          let evt;
          try { evt = JSON.parse(raw); } catch { continue; }

          if (evt.type === 'tool_start') {
            currentTool = addToolCall(bubble, evt.name, evt.input);
          } else if (evt.type === 'tool_result') {
            if (currentTool) {
              setToolResult(currentTool, evt.data, evt.is_screenshot, evt.visible);
              currentTool = null;
            }
          } else if (evt.type === 'text') {
            appendText(bubble, evt.data);
          } else if (evt.type === 'model_forced') {
            if (Array.isArray(evt.allowed_models) && evt.allowed_models.length > 0) {
              _POST_CAP_ALLOWED_MODELS = evt.allowed_models
                .map(v => (v || '').trim())
                .filter(Boolean);
            }
            if (evt.budget && typeof evt.budget === 'object') {
              _openrouterUsage = evt.budget;
            } else if (!_openrouterUsage) {
              _openrouterUsage = { capped: true };
            } else {
              _openrouterUsage.capped = true;
            }
            if (evt.model && _modelOptionExists(evt.model)) {
              document.getElementById('modelsel').value = evt.model;
              localStorage.setItem('unchained_model', evt.model);
            }
            _applyOpenRouterCapUi();
            _syncCustomModelUi();
          } else if (evt.type === 'cancelled') {
            appendText(bubble, '[Cancelled by user]');
          } else if (evt.type === 'error') {
            appendText(bubble, 'Error: ' + evt.data);
          } else if (evt.type === 'done') {
            _finalizeGroup();
            document.getElementById('agent-bar').classList.remove('active');
            _turnCount = 0;
            _navTrail = [];
            renderNavTrail();
          }
        }
      }
    }
  } catch(e) {
    const thinking = bubble.querySelector('.thinking');
    if (thinking) thinking.remove();
    if (e.name === 'AbortError') {
      appendText(bubble, '[Cancelled by user]');
    } else if (!bubble.querySelector('.text') && !bubble.querySelector('.action-group') && !bubble.querySelector('.action-standalone')) {
      appendText(bubble, 'Connection error: ' + e.message);
    }
  } finally {
    _cancelCtrl = null;
    sending = false;
    _finalizeGroup();
    document.getElementById('sendbtn').style.display = 'block';
    document.getElementById('cancelbtn').style.display = 'none';
    const slotbar2 = document.getElementById('slotbar');
    if (slotbar2) slotbar2.classList.remove('locked');
    document.getElementById('agent-bar').classList.remove('active');
    _turnCount = 0;
    _navTrail = [];
    renderNavTrail();
    maybeShowUpgrade();
  }
}

let _upgradeDismissed = false;

function _trialMsgCount() {
  return parseInt(localStorage.getItem('unchained_trial_msg_count') || '0', 10);
}
function _incTrialMsgCount() {
  const n = _trialMsgCount() + 1;
  localStorage.setItem('unchained_trial_msg_count', String(n));
  return n;
}
function maybeShowUpgrade() {
  if (_upgradeDismissed) return;
  if (_trialMsgCount() >= 5) {
    document.getElementById('upgrade-banner').style.display = 'block';
  }
}
function dismissUpgrade() {
  _upgradeDismissed = true;
  document.getElementById('upgrade-banner').style.display = 'none';
}

function _isWindows() { return navigator.userAgent.indexOf('Windows') !== -1; }

async function showBannerInstall() {
  await showTrialInstallCmd();
}

async function showTrialInstallCmd() {
  const isWin = _isWindows();
  document.getElementById('install-modal-title').textContent = 'Connect Your Browser';
  document.getElementById('install-modal-desc').textContent = isWin
    ? 'Run this in PowerShell to connect Chrome (Python 3.8+ required):'
    : 'Run this in your terminal to connect Chrome (Python 3 + curl required):';
  document.getElementById('install-modal-note').textContent = 'Link expires in 15 minutes. Only installs websockets \u2014 no API key needed.';
  const modal = document.getElementById('install-modal');
  modal.style.display = 'flex';
  document.getElementById('install-cmd').textContent = 'Generating link...';
  try {
    const r = await fetch('/trial/token', {method: 'POST'});
    if (!r.ok) { document.getElementById('install-cmd').textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    document.getElementById('install-cmd').textContent = isWin ? data.powershell_command : data.curl_command;
  } catch(e) {
    document.getElementById('install-cmd').textContent = 'Error: ' + e.message;
  }
}

function copyInstallCmd() {
  const cmd = document.getElementById('install-cmd').textContent;
  navigator.clipboard.writeText(cmd).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 2000);
  });
}

function closeInstallModal() {
  document.getElementById('install-modal').style.display = 'none';
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTML — Gemini SDK chat page (per-user provisioned Gemini key)
# ---------------------------------------------------------------------------

CHAT_GEMINI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Unchained — Gemini API</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#1a1a2e;--surface:#16213e;--accent:#e94560;
  --text:#eee;--muted:#888;--user-bg:#2a1a3e;--asst-bg:#1e2a3e;
  --mono:'SF Mono','Menlo','Monaco','Consolas',monospace;
}
body{
  font-family:-apple-system,system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  height:100dvh;overflow:hidden;
}

/* === Login === */
#login{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100dvh;padding:24px;gap:16px;
}
#login h1{font-size:28px;color:var(--accent);margin-bottom:8px;letter-spacing:1px}
#login .sub{color:var(--muted);font-size:13px;margin-bottom:8px}
#login input{
  width:100%;max-width:360px;height:48px;padding:0 16px;
  border:1px solid #333;border-radius:8px;
  background:var(--surface);color:var(--text);font-size:16px;
}
#login input:focus{outline:none;border-color:var(--accent)}
#login button{
  width:100%;max-width:360px;height:48px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:16px;font-weight:600;cursor:pointer;
}
#login button:active{opacity:0.8}
#loginerr{color:#ff6b6b;font-size:14px;min-height:20px}

/* === Main === */
#main{display:none;flex-direction:column;height:100dvh}

#topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:8px 16px;padding-top:max(8px,env(safe-area-inset-top));
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .agent{font-family:var(--mono);font-size:14px;color:var(--accent)}
#topbar .status-stack{display:flex;flex-direction:column;gap:2px}
#topbar .status{font-size:11px;color:var(--muted)}
#topbar .status.online{color:#4caf50}
#topbar .status.warn{color:#fbbf24}
#topbar .nav{display:flex;gap:8px}
#topbar .nav a{
  color:var(--muted);text-decoration:none;font-size:12px;
  border:1px solid #555;padding:4px 10px;border-radius:6px;
}
#topbar .nav a:hover{border-color:var(--accent);color:var(--accent)}
#topbar .client-update-wrap{display:flex;flex-direction:column;gap:3px;align-items:flex-start}
#topbar .client-update{
  border:1px solid #555;
  background:rgba(255,255,255,0.03);
  color:var(--muted);
  border-radius:999px;
  padding:5px 10px;
  font-size:11px;
  font-family:var(--mono);
  cursor:pointer;
  transition:border-color 0.15s ease,background 0.15s ease,color 0.15s ease,box-shadow 0.15s ease;
}
#topbar .client-update:hover{border-color:var(--accent);color:var(--accent);background:rgba(255,255,255,0.05)}
#topbar .client-update.warn{
  color:#f2d18a;
  border-color:#7a6326;
  background:#1d1607;
  animation:providerClientUpdatePulse 1.8s ease-in-out infinite;
}
#topbar .client-update.busy{
  color:#f8d1dd;
  border-color:rgba(233,69,96,0.45);
  background:rgba(233,69,96,0.12);
  animation:none;
}
#topbar .client-update:disabled{opacity:0.5;cursor:default;box-shadow:none}
#topbar .client-update-note{
  min-height:12px;
  font-size:10px;
  color:var(--muted);
  font-family:var(--mono);
  line-height:1.1;
}
@keyframes providerClientUpdatePulse{0%,100%{box-shadow:none}50%{box-shadow:0 0 14px rgba(245,191,36,0.16)}}

/* === Chat === */
#chat{
  flex:1;overflow-y:auto;padding:12px 16px;
  -webkit-overflow-scrolling:touch;
  display:flex;flex-direction:column;gap:12px;
}
.bubble{
  max-width:85%;padding:10px 14px;border-radius:12px;
  font-size:14px;line-height:1.5;word-break:break-word;
}
.bubble.user{
  align-self:flex-end;background:var(--user-bg);
  border:1px solid #3a2a5e;border-bottom-right-radius:4px;
}
.bubble.asst{
  align-self:flex-start;background:var(--asst-bg);
  border:1px solid #2a3a5e;border-bottom-left-radius:4px;
}
.bubble.asst .text{white-space:pre-wrap}
.bubble.asst .text.rendered{white-space:normal}
.bubble.asst .text.rendered p{margin:0.5em 0}
.bubble.asst .text.rendered p:first-child{margin-top:0}
.bubble.asst .text.rendered p:last-child{margin-bottom:0}
.bubble.asst .text.rendered h1{font-size:1.3em;margin:0.8em 0 0.4em;font-weight:700;color:var(--accent)}
.bubble.asst .text.rendered h2{font-size:1.15em;margin:0.7em 0 0.35em;font-weight:600;color:var(--accent)}
.bubble.asst .text.rendered h3{font-size:1.05em;margin:0.6em 0 0.3em;font-weight:600}
.bubble.asst .text.rendered h4{font-size:1em;margin:0.5em 0 0.25em;font-weight:600}
.bubble.asst .text.rendered code{
  background:rgba(255,255,255,0.08);padding:2px 5px;border-radius:3px;
  font-family:var(--mono);font-size:0.88em;
}
.bubble.asst .text.rendered pre{
  background:#0d1117;border-radius:6px;padding:12px;margin:8px 0;
  overflow-x:auto;white-space:pre-wrap;word-break:break-word;
}
.bubble.asst .text.rendered pre code{background:none;padding:0;font-size:12px;line-height:1.5}
.bubble.asst .text.rendered a{color:var(--accent);text-decoration:underline}
.bubble.asst .text.rendered ul,.bubble.asst .text.rendered ol{margin:0.4em 0;padding-left:1.5em}
.bubble.asst .text.rendered li{margin:0.2em 0}
.bubble.asst .text.rendered blockquote{
  border-left:3px solid var(--accent);padding-left:12px;margin:0.5em 0;color:var(--muted);
}
.bubble.asst .text.rendered hr{border:none;border-top:1px solid #333;margin:1em 0}
.bubble.asst .text.rendered table{border-collapse:collapse;margin:0.5em 0;width:100%;font-size:13px}
.bubble.asst .text.rendered th,.bubble.asst .text.rendered td{border:1px solid #333;padding:6px 10px}
.bubble.asst .text.rendered th{background:rgba(255,255,255,0.05);font-weight:600}
.bubble.asst .text.rendered strong{font-weight:600}
.bubble.asst .text.rendered em{font-style:italic}
.bubble.asst .text .math-display{margin:0.8em 0;text-align:center;overflow-x:auto}
.bubble.asst .text .katex{color:inherit;font-size:1.05em}
.bubble.asst .text .katex-display{margin:0.5em 0;overflow-x:auto;padding:4px 0}
.bubble.asst .thinking{color:var(--muted);font-style:italic;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
/* === Action groups (browser activity cards) === */
@keyframes slideIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.action-group{margin:6px 0;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;font-size:12px;font-family:var(--mono);animation:slideIn 0.2s ease-out}
.ag-header{display:flex;align-items:center;gap:8px;padding:7px 12px;background:#111;border-bottom:1px solid #1e1e1e}
.ag-emoji{font-size:14px;flex-shrink:0}
.ag-site{color:var(--accent);font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ag-count{color:var(--muted);font-size:10px;flex-shrink:0}
.ag-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.ag-dot.running{animation:pulse 1.2s ease-in-out infinite}
.ag-dot.done{color:#4ade80}
.ag-steps{padding:2px 0 4px}
.action-step{display:flex;align-items:center;gap:7px;padding:3px 12px 3px 28px}
.as-emoji{font-size:11px;flex-shrink:0;width:16px;text-align:center}
.as-label{color:var(--text);font-size:11px;flex-shrink:0}
.as-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.as-dot{margin-left:auto;flex-shrink:0;font-size:10px;color:var(--muted)}
.as-dot.running{animation:pulse 1.2s ease-in-out infinite}
.as-dot.done{color:#4ade80}
.as-screenshot{padding:4px 12px 6px 28px}
.as-screenshot img{max-width:100%;border-radius:4px}
.intel-bars{padding:4px 12px 6px 28px;display:flex;flex-direction:column;gap:4px}
.intel-bar{display:flex;align-items:center;gap:8px;font-size:10.5px;font-family:var(--mono)}
.intel-bar-label{width:96px;color:var(--muted);flex-shrink:0;overflow:hidden;text-overflow:ellipsis}
.intel-bar-track{flex:1;height:3px;background:#2a2a2a;border-radius:2px;overflow:hidden}
.intel-bar-fill{height:100%;border-radius:2px;background:var(--accent)}
.intel-bar-pct{color:var(--text);width:32px;text-align:right;flex-shrink:0}
.action-standalone{display:flex;align-items:center;gap:8px;padding:5px 12px;margin:4px 0;border:1px solid #2a2a2a;border-radius:6px;font-size:12px;font-family:var(--mono);animation:slideIn 0.2s ease-out;background:#111}
.standalone-emoji{font-size:13px;flex-shrink:0}
.standalone-label{color:var(--accent);font-weight:500;flex-shrink:0}
.standalone-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.standalone-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.standalone-dot.running{animation:pulse 1.2s ease-in-out infinite}
.standalone-dot.done{color:#4ade80}
#agent-bar{
  display:none;padding:3px 16px;background:var(--surface);
  border-bottom:1px solid #222;font-size:11px;font-family:var(--mono);
  color:var(--muted);align-items:center;gap:10px;min-height:22px;
}
#agent-bar.active{display:flex}
#agent-action{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#nav-trail{display:flex;align-items:center;gap:0;overflow:hidden;font-size:10px;opacity:0.5;flex-shrink:0;max-width:45%}
#nav-trail span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100px}
#nav-trail span+span::before{content:' > ';opacity:0.6}
#turn-ctr{flex-shrink:0;opacity:0.4;font-size:10px}
.bubble.asst{position:relative}
.bubble.asst .copy-btn{
  position:absolute;top:6px;right:6px;
  background:rgba(255,255,255,0.1);border:none;color:var(--muted);
  padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;
  opacity:0;transition:opacity 0.15s;font-family:var(--mono);
}
.bubble.asst:hover .copy-btn{opacity:1}
.bubble.asst .copy-btn.copied{opacity:1;color:var(--accent)}

/* === Model selector === */
#modelrow{
  display:flex;align-items:center;gap:8px;
  padding:4px 16px 0;flex-shrink:0;
}
#modelrow label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
#modelsel{
  height:28px;padding:0 8px;border:1px solid #444;border-radius:6px;
  background:var(--bg);color:var(--text);font-size:12px;
  font-family:var(--mono);cursor:pointer;
}
#modelsel:focus{outline:none;border-color:var(--accent)}

/* === No-key banner === */
#nokey-banner{
  display:none;align-items:center;justify-content:center;gap:12px;
  padding:10px 16px;background:#2d2515;border-bottom:1px solid #5a4a22;
  font-size:13px;color:#dcc58a;flex-shrink:0;
}
#nokey-banner a{
  color:var(--accent);text-decoration:none;font-weight:600;
  border:1px solid var(--accent);padding:4px 12px;border-radius:6px;
}
#nokey-banner a:hover{background:var(--accent);color:#fff}

/* === Installer banner === */
#download-banner{
  display:none;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 16px;background:#2b1f28;border-bottom:1px solid #553040;
  font-size:13px;color:#f1c7d6;flex-shrink:0;
}
#download-banner a{
  color:var(--accent);text-decoration:none;font-weight:600;
  border:1px solid var(--accent);padding:4px 12px;border-radius:6px;
  background:transparent;
}
#download-banner a:hover{background:var(--accent);color:#fff}

/* === Input === */
#inputbar{
  display:flex;gap:8px;
  padding:8px 16px;padding-bottom:max(16px,env(safe-area-inset-bottom));
  background:var(--surface);border-top:1px solid #333;flex-shrink:0;
}
#msginput{
  flex:1;min-width:0;min-height:44px;max-height:120px;padding:10px 12px;
  border:1px solid #444;border-radius:12px;
  background:var(--bg);color:var(--text);
  font-size:15px;font-family:-apple-system,system-ui,sans-serif;
  resize:none;line-height:1.4;
}
#msginput:focus{outline:none;border-color:var(--accent)}
#sendbtn{
  width:44px;height:44px;border:none;border-radius:12px;
  background:var(--accent);color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;align-self:flex-end;
}
#sendbtn:active{opacity:0.8}
#sendbtn:disabled{opacity:0.4;cursor:default}
#cancelbtn{
  width:44px;height:44px;border:none;border-radius:12px;
  background:#ff4444;color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;align-self:flex-end;
  display:none;
}
#cancelbtn:active{opacity:0.8}

/* === Chat hints === */
#chat-hints{
  display:flex;flex-direction:column;align-items:center;
  justify-content:flex-start;height:100%;padding-top:24px;text-align:center;
}
.hint-title{font-size:20px;font-weight:600;color:var(--accent);margin-bottom:8px}
.hint-sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.hint-examples{display:flex;flex-direction:column;gap:8px;width:100%;max-width:340px}
.hint-item{
  padding:10px 14px;border:1px solid #333;border-radius:10px;
  font-size:13px;color:var(--text);cursor:pointer;text-align:left;
  transition:border-color 0.15s;
}
.hint-item:hover{border-color:var(--accent)}
</style>
</head>
<body>

<!-- Login -->
<div id="login">
  <h1>Unchained Gemini API</h1>
  <div class="sub">AI browser agent powered by your Gemini API key</div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="outline"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
  <a href="/setup" style="color:#888;font-size:12px;margin-top:4px;text-decoration:none">Need a Gemini API key? Set up here &rarr;</a>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <div class="left">
      <span class="agent" id="agentlabel"></span>
      <div class="status-stack">
        <span class="status" id="agentstatus">agent offline</span>
        <span class="status" id="bridgestatus">bridge offline</span>
      </div>
      <div class="client-update-wrap">
        <button id="client-update-btn" class="client-update" type="button" onclick="triggerClientUpdate()">Client current</button>
        <span id="client-update-note" class="client-update-note"></span>
      </div>
    </div>
    <div class="nav">
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a href="/setup">Setup</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doDisconnect();return false">Logout</a>
    </div>
  </div>

  <div id="agent-bar">
    <span id="agent-action"></span>
    <span id="nav-trail"></span>
    <span id="turn-ctr"></span>
  </div>

  <div id="nokey-banner">
    <span>No Gemini API key provisioned.</span>
    <a href="/setup">Provision Key</a>
  </div>

  <div id="download-banner" style="display:none">
    <span id="banner-msg">Local chat agent is offline on this machine.</span>
    <a href="#" onclick="showBannerInstall();return false" id="banner-curl">Install (curl)</a>
    <a href="/install" id="banner-connect">Download Agent Installer</a>
  </div>

  <div id="install-modal" style="display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.7);display:none;align-items:center;justify-content:center">
    <div style="background:var(--surface);border:1px solid #444;border-radius:12px;padding:24px;max-width:520px;width:90%;position:relative">
      <button onclick="closeInstallModal()" style="position:absolute;top:12px;right:12px;background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer">&times;</button>
      <h3 id="install-modal-title" style="color:var(--accent);margin-bottom:8px;font-size:16px">Install Agent (curl)</h3>
      <p id="install-modal-desc" style="color:var(--muted);font-size:13px;margin-bottom:12px">Run this command in your terminal:</p>
      <div style="background:var(--bg);border:1px solid #333;border-radius:8px;padding:12px;font-family:var(--mono);font-size:12px;word-break:break-all;position:relative">
        <code id="install-cmd" style="color:var(--text)">Loading command...</code>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button onclick="copyInstallCmd()" style="background:#2f3140;border:1px solid #4a4d60;color:#fff;padding:8px 12px;border-radius:6px;font-size:12px;cursor:pointer" id="copy-btn">Copy Command</button>
      </div>
      <p id="install-modal-note" style="color:var(--muted);font-size:11px;margin-top:12px">Links expire in 15 minutes. Requires Python 3.8+ and curl.</p>
    </div>
  </div>

  <div id="chat">
    <div id="chat-hints">
      <div class="hint-title">Gemini API Chat</div>
      <div class="hint-sub">Browser agent powered by your provisioned Gemini API key.</div>
      <div class="hint-examples">
        <div class="hint-item" onclick="fillMsg('Go to Wikipedia and look up the Eiffel Tower')">Look up the Eiffel Tower on Wikipedia</div>
        <div class="hint-item" onclick="fillMsg('Check the weather forecast on weather.gov for New York City')">Check the NYC weather on weather.gov</div>
        <div class="hint-item" onclick="fillMsg('Open Hacker News and list the top 5 stories right now')">List the top 5 Hacker News stories</div>
      </div>
    </div>
  </div>

  <div id="modelrow">
    <label for="modelsel">Model</label>
    <select id="modelsel" onchange="onModelChange(this.value)">
      <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
      <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
      <option value="gemini-2.0-flash">Gemini 2.0 Flash</option>
    </select>
    <label for="profilesel">Profile</label>
    <select id="profilesel" onchange="onProfileChange(this.value)" title="Optional profile copy to avoid re-sign-in prompts">
      <option value="">Current browser (no profile copy)</option>
    </select>
  </div>
  <div id="inputbar">
    <textarea id="msginput" rows="1" placeholder="Ask the agent anything..."
              onkeydown="handleKey(event)" oninput="autoGrow(this)"></textarea>
    <button id="sendbtn" onclick="doSend()">&#9654;</button>
    <button id="cancelbtn" onclick="doCancel()">&#9632;</button>
  </div>
</div>
<script>

let agentId = '';
let sessionId = '';
let _userName = '';
let sending = false;
let _cancelCtrl = null;
let geminiProvisioned = false;
let selectedProfilePath = '';
let clientUpdateInFlight = false;
let clientUpdateSawDisconnect = false;
let clientUpdateError = '';
const CLIENT_UPDATE_TIMEOUT_MS = 90000;
let clientUpdateStartedAt = 0;
let lastClientStatus = null;

async function handleGoogleCredential(response) {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential, source: 'gemini'}),
    });
    const data = await r.json();
    if (data.pending) { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    agentId = data.agent_id;
    _userName = data.name || '';
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) { agentId = data.agent_id; _userName = data.name || ''; showMain(); return; }
  } catch(e) {}
  document.getElementById('login').style.display = 'flex';
}

function showPending() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'none';
}

async function doDisconnect() {
  await fetch('/auth/logout', {method: 'POST'});
  agentId = '';
  sessionId = '';
  document.getElementById('login').style.display = 'flex';
  document.getElementById('main').style.display = 'none';
}

function currentModel() {
  return document.getElementById('modelsel').value;
}

function _sessionStoreKey() {
  return 'unchained_session_' + agentId + '_gemini';
}

function _restoreSessionId() {
  const sid = localStorage.getItem(_sessionStoreKey()) || '';
  const prefix = 's-' + agentId + '-';
  return (sid.startsWith(prefix) && sid.length > prefix.length) ? sid : '';
}

function _persistSessionId(sid) {
  if (sid && sid.startsWith('s-' + agentId)) {
    localStorage.setItem(_sessionStoreKey(), sid);
  }
}

function onModelChange(model) {
  localStorage.setItem('unchained_gemini_model', model);
}

function _profileStoreKey() {
  return 'unchained_chat_profile_' + agentId + '_' + window.location.pathname;
}

function onProfileChange(profilePath) {
  selectedProfilePath = String(profilePath || '');
  try { localStorage.setItem(_profileStoreKey(), selectedProfilePath); } catch(e) {}
}

function currentProfilePath() {
  return selectedProfilePath || '';
}

async function loadChatProfiles() {
  const sel = document.getElementById('profilesel');
  if (!sel) return;

  let remembered = '';
  try { remembered = localStorage.getItem(_profileStoreKey()) || ''; } catch(e) {}
  selectedProfilePath = remembered;
  sel.innerHTML = '';

  const defaultOpt = document.createElement('option');
  defaultOpt.value = '';
  defaultOpt.textContent = 'Current browser (no profile copy)';
  sel.appendChild(defaultOpt);

  let gotProfiles = false;
  try {
    const r = await fetch('/web/provision/profiles');
    if (r.ok) {
      const data = await r.json();
      for (const p of (data.profiles || [])) {
        const path = String(p.profile_path || p.path || '').trim();
        if (!path) continue;
        gotProfiles = true;
        const label = String(p.name || p.dir_name || 'Profile').trim();
        const email = String(p.email || '').trim();
        const opt = document.createElement('option');
        opt.value = path;
        opt.textContent = email ? (label + ' (' + email + ')') : label;
        sel.appendChild(opt);
      }
    }
  } catch(e) {}

  if ([...sel.options].some(opt => opt.value === remembered)) {
    sel.value = remembered;
  } else if (!gotProfiles && remembered) {
    const opt = document.createElement('option');
    opt.value = remembered;
    opt.textContent = 'Saved profile (bridge offline)';
    sel.appendChild(opt);
    sel.value = remembered;
  } else {
    sel.value = '';
  }
  selectedProfilePath = sel.value || '';
}

function updateStatusPill(el, text, mode) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status' + (mode ? ' ' + mode : '');
}

function updateClientUpdateUI(data) {
  const btn = document.getElementById('client-update-btn');
  const note = document.getElementById('client-update-note');
  if (!btn || !note) return;
  const clientConnected = !!data.client_connected;
  const updateSupported = !!data.client_update_supported;
  const outdated = !!data.client_outdated;
  const required = !!data.client_update_required;
  const localVersion = String(data.client_version || '').trim();
  const serverVersion = String(data.server_version || '').trim();
  btn.className = 'client-update';
  if (clientUpdateInFlight) {
    btn.classList.add('busy');
    btn.disabled = true;
    btn.textContent = 'Updating...';
    note.textContent = 'Client will reconnect after restart.';
    return;
  }
  if (outdated) btn.classList.add('warn');
  btn.disabled = !clientConnected || !updateSupported || !outdated;
  if (!clientConnected) {
    btn.textContent = 'Client offline';
    note.textContent = localVersion ? ('Last seen v' + localVersion) : '';
  } else if (!updateSupported) {
    btn.textContent = 'Manual update';
    note.textContent = localVersion ? ('Client v' + localVersion) : 'Reconnect with the latest package once.';
  } else if (outdated) {
    btn.textContent = required ? 'Update required' : 'Update client';
    note.textContent = (localVersion && serverVersion) ? ('v' + localVersion + ' -> v' + serverVersion) : 'New client available';
  } else {
    btn.textContent = 'Client current';
    note.textContent = localVersion ? ('v' + localVersion) : '';
  }
  if (clientUpdateError) note.textContent = clientUpdateError;
}

async function triggerClientUpdate() {
  if (clientUpdateInFlight) return;
  clientUpdateError = '';
  clientUpdateInFlight = true;
  clientUpdateSawDisconnect = false;
  clientUpdateStartedAt = Date.now();
  updateClientUpdateUI(lastClientStatus || {});
  try {
    const r = await fetch('/web/chat/update-client', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    checkAgentStatus();
  } catch(e) {
    clientUpdateInFlight = false;
    clientUpdateSawDisconnect = false;
    clientUpdateStartedAt = 0;
    clientUpdateError = e.message || 'Update failed';
    updateClientUpdateUI(lastClientStatus || {});
  }
}

function updateAgentStatusUI(data) {
  const chatEl = document.getElementById('agentstatus');
  const bridgeEl = document.getElementById('bridgestatus');
  const banner = document.getElementById('download-banner');
  const bannerMsg = document.getElementById('banner-msg');
  const bannerCurl = document.getElementById('banner-curl');
  const chatConnected = !!data.chat_connected;
  const bridgeConnected = !!data.bridge_connected;
  const mismatch = !!data.mismatch;

  if (chatConnected) updateStatusPill(chatEl, 'agent online', 'online');
  else if (mismatch) updateStatusPill(chatEl, 'agent mismatch', 'warn');
  else updateStatusPill(chatEl, 'agent offline', '');

  if (bridgeConnected) updateStatusPill(bridgeEl, 'bridge online', 'online');
  else updateStatusPill(bridgeEl, 'bridge offline', '');

  if (bannerMsg) bannerMsg.textContent = 'Local chat agent is offline on this machine.';
  if (bannerCurl) { const w = typeof _isWindows==='function'&&_isWindows(); bannerCurl.textContent = mismatch ? (w ? 'Reinstall (PowerShell)' : 'Reinstall (curl)') : (w ? 'Install (PowerShell)' : 'Install (curl)'); }

  if (banner) {
    if (chatConnected && bridgeConnected) {
      banner.style.display = 'none';
    } else {
      if (chatConnected && !bridgeConnected && bannerMsg) {
        bannerMsg.textContent = 'Your browser bridge is offline on this machine.';
      } else if (mismatch && bannerMsg) {
        bannerMsg.textContent = 'A different local chat agent is connected for this account.';
      }
      banner.style.display = 'flex';
    }
  }
}

function _isWindows() { return navigator.userAgent.indexOf('Windows') !== -1; }

async function showBannerInstall() {
  await showInstallCmd();
}

function _normalizeLocalUrl(raw) {
  const s = String(raw || '');
  const isLocalHost = location.hostname === 'localhost' || location.hostname === '127.0.0.1';
  if (isLocalHost) return s.replace(/^https:\/\//i, 'http://');
  return s;
}

async function showInstallCmd() {
  const isWin = _isWindows();
  document.getElementById('install-modal-title').textContent = isWin ? 'Install Agent (PowerShell)' : 'Install Agent (curl)';
  document.getElementById('install-modal-desc').textContent = isWin ? 'Run this command in PowerShell:' : 'Run this command in your terminal:';
  document.getElementById('install-modal-note').textContent = isWin ? 'Links expire in 15 minutes. Requires Python 3.8+.' : 'Links expire in 15 minutes. Requires Python 3.8+ and curl.';
  document.getElementById('copy-btn').textContent = 'Copy Command';
  const modal = document.getElementById('install-modal');
  modal.style.display = 'flex';
  document.getElementById('install-cmd').textContent = 'Generating install command...';
  try {
    const r = await fetch('/web/install-token', {method: 'POST'});
    if (!r.ok) { document.getElementById('install-cmd').textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    const command = _normalizeLocalUrl(isWin ? (data.powershell_command || '') : (data.curl_command || ''));
    document.getElementById('install-cmd').textContent = command || 'No install command available.';
  } catch(e) {
    document.getElementById('install-cmd').textContent = 'Error: ' + e.message;
  }
}

function copyInstallCmd() {
  const cmd = document.getElementById('install-cmd').textContent;
  navigator.clipboard.writeText(cmd).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy Command', 2000);
  });
}

function closeInstallModal() {
  document.getElementById('install-modal').style.display = 'none';
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  document.getElementById('agentlabel').textContent = _userName || 'Unchained';
  try { localStorage.setItem('unchained_last_route', window.location.pathname); } catch(e){}
  const saved = localStorage.getItem('unchained_gemini_model');
  if (saved && document.querySelector('#modelsel option[value="' + CSS.escape(saved) + '"]')) {
    document.getElementById('modelsel').value = saved;
  }
  sessionId = _restoreSessionId() || ('s-' + agentId + '-' + Date.now().toString(36));
  _persistSessionId(sessionId);
  loadChatProfiles();
  checkProvisionStatus();
  checkAgentStatus();
  setInterval(checkAgentStatus, 10000);
  loadHistory();
}

async function checkProvisionStatus() {
  try {
    const r = await fetch('/web/provision/status');
    if (!r.ok) return;
    const data = await r.json();
    const gemini = (data.providers || []).find(p => p.name === 'gemini');
    geminiProvisioned = gemini && gemini.provisioned;
    document.getElementById('nokey-banner').style.display = geminiProvisioned ? 'none' : 'flex';
  } catch(e) {}
}

async function checkAgentStatus() {
  try {
    const r = await fetch('/web/chat/status?gemini=1');
    if (r.ok) {
      const data = await r.json();
      if (clientUpdateInFlight) {
        if (!data.client_connected) clientUpdateSawDisconnect = true;
        else if (clientUpdateSawDisconnect || !data.client_outdated) {
          clientUpdateInFlight = false;
          clientUpdateSawDisconnect = false;
          clientUpdateStartedAt = 0;
          clientUpdateError = '';
        } else if (clientUpdateStartedAt && (Date.now() - clientUpdateStartedAt) >= CLIENT_UPDATE_TIMEOUT_MS) {
          clientUpdateInFlight = false;
          clientUpdateSawDisconnect = false;
          clientUpdateStartedAt = 0;
          clientUpdateError = 'Update timed out. Check the local client logs and retry.';
        }
      }
      if (!clientUpdateInFlight && !data.client_outdated) clientUpdateError = '';
      lastClientStatus = data;
      updateClientUpdateUI(data);
      updateAgentStatusUI({
        chat_connected: data.gemini_connected || false,
        bridge_connected: !!data.bridge_connected,
        mismatch: !!data.mismatch,
      });
      if (data.gemini_agent_id && !_userName) {
        document.getElementById('agentlabel').textContent = data.gemini_agent_id;
      }
    }
  } catch(e) {}
}

async function loadHistory() {
  try {
    const qs = new URLSearchParams({
      model: currentModel(),
      session_id: sessionId,
    });
    const r = await fetch('/web/chat/history?' + qs.toString());
    if (!r.ok) return;
    const data = await r.json();
    if (data.session_id) {
      sessionId = data.session_id;
      _persistSessionId(sessionId);
    }
    if (!data.messages || data.messages.length === 0) {
      showHintsIfEmpty();
      return;
    }
    hideHints();
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        addUserBubble(msg.content);
      } else if (msg.role === 'assistant') {
        const bubble = addAsstBubble();
        const thinking = bubble.querySelector('.thinking');
        if (thinking) thinking.remove();
        appendText(bubble, msg.content);
      }
    }
  } catch(e) {}
}

function showHintsIfEmpty() {
  if (document.getElementById('chat-hints')) return;
  document.getElementById('chat').innerHTML =
    '<div id="chat-hints">' +
    '<div class="hint-title">Gemini API Chat</div>' +
    '<div class="hint-sub">Browser agent powered by your provisioned Gemini API key.</div>' +
    '<div class="hint-examples">' +
    '<div class="hint-item" onclick="fillMsg(\'Go to Wikipedia and look up the Eiffel Tower\')">Look up the Eiffel Tower on Wikipedia</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Check the weather forecast on weather.gov for New York City\')">Check the NYC weather on weather.gov</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Open Hacker News and list the top 5 stories right now\')">List the top 5 Hacker News stories</div>' +
    '</div></div>';
}

async function doNewChat() {
  if (sending) return;
  document.getElementById('chat').innerHTML = '';
  showHintsIfEmpty();
  try {
    const r = await fetch('/web/chat/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        model: currentModel(),
        session_id: sessionId,
      }),
    });
    if (r.ok) {
      const data = await r.json();
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
      }
    }
  } catch(e) {}
}

checkSession();
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    doSend();
  }
}

function scrollToBottom() {
  const chat = document.getElementById('chat');
  chat.scrollTop = chat.scrollHeight;
}

function fillMsg(text) {
  const input = document.getElementById('msginput');
  input.value = text;
  input.focus();
  autoGrow(input);
}

function hideHints() {
  const hints = document.getElementById('chat-hints');
  if (hints) hints.remove();
}

function addUserBubble(text) {
  hideHints();
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble user';
  div.textContent = text;
  chat.appendChild(div);
  scrollToBottom();
}

function addAsstBubble() {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble asst';
  div.innerHTML = '<button class="copy-btn" onclick="copyBubbleText(this)">Copy</button><div class="thinking">thinking...</div>';
  chat.appendChild(div);
  scrollToBottom();
  return div;
}

const TOOL_META = {
  navigate:   {emoji:'🌐', label:'Navigate'},
  ddm:        {emoji:'👁', label:'Look'},
  click:      {emoji:'👆', label:'Click'},
  type:       {emoji:'⌨️', label:'Type'},
  intel:      {emoji:'🔬', label:'Analyze'},
  screenshot: {emoji:'📷', label:'Screenshot'},
  js:         {emoji:'⚡', label:'Script'},
  websearch:  {emoji:'🔍', label:'Search'},
  webfetch:   {emoji:'📥', label:'Fetch'},
  bash:       {emoji:'⚙️', label:'Command'},
  intervention: {emoji:'🛑', label:'Nudge'},
  intervention_screenshot: {emoji:'📷', label:'Nudge Screenshot'},
};

function toolFriendlyDesc(name, input) {
  if (!input) return '';
  const i = input.trim();
  if (name === 'navigate') {
    try { const u = new URL(i); return u.hostname + (u.pathname !== '/' ? u.pathname.slice(0,30) : ''); }
    catch { return i.slice(0,50); }
  }
  if (name === 'ddm') {
    if (i.includes('--at')) return 'inspect element';
    if (i.includes('--tabs')) return 'list tabs';
    if (i.includes('--new')) { const m=i.match(/https?:\/\/([^/\s]+)/); return m ? 'open '+m[1] : 'new tab'; }
    if (i.includes('--close')) return 'close tab';
    if (i.includes('--text')) { const m=i.match(/--find\s+"?([^"\s]+)/); return m ? 'find "'+m[1]+'"' : 'read text'; }
    if (i.includes('--llm-2pass')) return 'map layout';
    if (i.includes('--js')) return 'run js';
    if (i.includes('--api')) return 'find api';
    if (i.includes('--forms')) return 'find forms';
    return 'scan';
  }
  if (name === 'intel') {
    if (i.includes('--probe')) return 'probe page type';
    if (i.includes('--extract')) { const m=i.match(/--strategy\s+(\S+)/); return m ? 'extract ('+m[1]+')' : 'extract data'; }
    if (i.includes('--stores')) return 'find data stores';
    if (i.includes('--shape')) { const m=i.match(/--shape\s+(\S+)/); return m ? 'map '+m[1] : 'map data'; }
    if (i.includes('--find-paths')) return 'find paths';
    return 'analyze';
  }
  if (name === 'click') { const m=i.match(/(\d+)\s+(\d+)/); return m ? '('+m[1]+','+m[2]+')' : i.slice(0,30); }
  if (name === 'type') return '"' + i.replace(/^["']|["']$/g,'').slice(0,40) + '"';
  if (name === 'js') return i.slice(0,50).replace(/\n/g,' ');
  if (name === 'websearch') return '"' + i.slice(0,50) + '"';
  if (name === 'webfetch') { try { return new URL(i).hostname; } catch { return i.slice(0,40); } }
  return i.slice(0,50);
}

const BROWSER_TOOLS = new Set(['navigate','ddm','click','type','intel','screenshot','js','intervention','intervention_screenshot']);
let _currentGroup = null;
let _currentGroupSteps = 0;
let _currentGroupDot = null;
let _turnCount = 0;
let _navTrail = [];

function renderNavTrail() {
  const el = document.getElementById('nav-trail');
  if (el) el.innerHTML = _navTrail.map(s => '<span>' + esc(s) + '</span>').join('');
}

function _finalizeGroup() {
  if (!_currentGroup) return;
  if (_currentGroupDot) {
    _currentGroupDot.className = 'ag-dot done';
    _currentGroupDot.textContent = '✓';
  }
  const countEl = _currentGroup.querySelector('.ag-count');
  if (countEl && _currentGroupSteps > 0) {
    countEl.textContent = _currentGroupSteps + (_currentGroupSteps !== 1 ? ' steps' : ' step');
  }
  _currentGroup = null;
  _currentGroupDot = null;
  _currentGroupSteps = 0;
}

function addToolCall(bubble, name, input) {
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  const meta = TOOL_META[name] || {emoji:'⚙️', label:name};
  const desc = toolFriendlyDesc(name, input);

  if (name === 'navigate' && desc) {
    _navTrail.push(desc);
    if (_navTrail.length > 4) _navTrail.shift();
    renderNavTrail();
  }

  _turnCount++;
  const actionEl = document.getElementById('agent-action');
  if (actionEl) actionEl.textContent = meta.label + (desc ? '  ' + desc : '');
  const turnEl = document.getElementById('turn-ctr');
  if (turnEl) turnEl.textContent = 't' + _turnCount;
  document.getElementById('agent-bar').classList.add('active');

  if (BROWSER_TOOLS.has(name)) {
    let hostname = '';
    if (name === 'navigate') {
      try { hostname = new URL(input.trim()).hostname; } catch { hostname = input.trim().slice(0,30); }
    } else {
      hostname = _currentGroup ? _currentGroup.dataset.site : 'browser';
    }
    if (name === 'navigate' && (!_currentGroup || _currentGroup.dataset.site !== hostname)) {
      _finalizeGroup();
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = hostname;
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">🌐</span>' +
          '<span class="ag-site">' + esc(hostname) + '</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">●</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    } else if (!_currentGroup) {
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = 'browser';
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">🌐</span>' +
          '<span class="ag-site">browser</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">●</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    }
    _currentGroupSteps++;
    const steps = _currentGroup.querySelector('.ag-steps');
    const step = document.createElement('div');
    step.className = 'action-step';
    step.innerHTML =
      '<span class="as-emoji">' + meta.emoji + '</span>' +
      '<span class="as-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="as-desc">' + esc(desc) + '</span>' : '') +
      '<span class="as-dot running">●</span>';
    steps.appendChild(step);
    scrollToBottom();
    return step;
  } else {
    const sa = document.createElement('div');
    sa.className = 'action-standalone';
    sa.innerHTML =
      '<span class="standalone-emoji">' + meta.emoji + '</span>' +
      '<span class="standalone-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="standalone-desc">' + esc(desc) + '</span>' : '') +
      '<span class="standalone-dot running">●</span>';
    bubble.appendChild(sa);
    scrollToBottom();
    return sa;
  }
}

function parseIntelBars(text) {
  const m = text.match(/strategy:\s*(\S+)\s*\((\d+)%\)(?:.*?runner-up:\s*(\S+)\s*\((\d+)%\))?/);
  if (!m) return null;
  const bars = [{label: m[1], pct: parseInt(m[2])}];
  if (m[3]) bars.push({label: m[3], pct: parseInt(m[4])});
  return bars;
}

function setToolResult(el, result, isScreenshot, visible) {
  const isStep = el.classList.contains('action-step');
  const dotCls = isStep ? 'as-dot' : 'standalone-dot';
  const dot = el.querySelector('.' + dotCls);
  if (dot) { dot.className = dotCls + ' done'; dot.textContent = '✓'; }

  if (isScreenshot && visible) {
    const imgDiv = document.createElement('div');
    imgDiv.className = 'as-screenshot';
    imgDiv.innerHTML = '<img src="data:image/png;base64,' + result + '">';
    el.after(imgDiv);
  } else if (!isScreenshot) {
    const bars = parseIntelBars(result);
    if (bars) {
      const barsDiv = document.createElement('div');
      barsDiv.className = 'intel-bars';
      barsDiv.innerHTML = bars.map(b =>
        '<div class="intel-bar">' +
          '<span class="intel-bar-label">' + esc(b.label) + '</span>' +
          '<span class="intel-bar-track"><span class="intel-bar-fill" style="width:' + b.pct + '%"></span></span>' +
          '<span class="intel-bar-pct">' + b.pct + '%</span>' +
        '</div>'
      ).join('');
      el.after(barsDiv);
    } else {
      const titleM = result.match(/Title:\s*(.+)/);
      if (titleM && _currentGroup) {
        const siteEl = _currentGroup.querySelector('.ag-site');
        if (siteEl) {
          const title = titleM[1].trim().slice(0, 60);
          siteEl.title = title;
          const host = _currentGroup.dataset.site;
          siteEl.textContent = host + (title ? '  —  ' + title : '');
        }
      }
    }
  }
  scrollToBottom();
}

// --- Markdown + KaTeX rendering ---
let _markedConfigured = false;
function ensureMarkedConfigured() {
  if (_markedConfigured || typeof marked === 'undefined') return;
  _markedConfigured = true;
  const blockMath = {
    name: 'blockMath',
    level: 'block',
    start(src) { return src.indexOf('$$'); },
    tokenizer(src) {
      const m = src.match(/^\$\$([\s\S]+?)\$\$/);
      if (m) return { type: 'blockMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<pre>' + esc(token.raw) + '</pre>';
      try { return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>'; }
      catch(e) { return '<pre>' + esc(token.raw) + '</pre>'; }
    }
  };
  const inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start(src) { return src.indexOf('$'); },
    tokenizer(src) {
      const m = src.match(/^\$([^\$\n]+?)\$/);
      if (m) return { type: 'inlineMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<code>' + esc(token.raw) + '</code>';
      const isDisplay = /\\begin\{/.test(token.text);
      try {
        if (isDisplay) return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>';
        return katex.renderToString(token.text, {displayMode:false,throwOnError:false});
      }
      catch(e) { return '<code>' + esc(token.raw) + '</code>'; }
    }
  };
  marked.use({ extensions: [blockMath, inlineMath] });
}

function appendText(bubble, text) {
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  if (!bubble._rawText) bubble._rawText = '';
  bubble._rawText += text;

  let span = bubble.querySelector('.text');
  if (!span) {
    span = document.createElement('div');
    span.className = 'text';
    bubble.appendChild(span);
  }

  ensureMarkedConfigured();
  if (typeof marked !== 'undefined') {
    try {
      span.innerHTML = marked.parse(bubble._rawText);
      span.classList.add('rendered');
    } catch(e) {
      span.textContent = bubble._rawText;
      span.classList.remove('rendered');
    }
  } else {
    span.textContent = bubble._rawText;
  }
  scrollToBottom();
}

function copyBubbleText(btn) {
  const bubble = btn.closest('.bubble.asst');
  if (!bubble) return;
  const text = bubble._rawText || (bubble.querySelector('.text') ? bubble.querySelector('.text').textContent : '');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

async function doCancel() {
  try {
    await fetch('/web/chat/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId}),
    });
  } catch(e) {}
  if (_cancelCtrl) _cancelCtrl.abort();
}

async function doSend() {
  if (sending) return;
  if (!geminiProvisioned) {
    alert('No Gemini API key provisioned. Visit /setup to get one.');
    return;
  }
  const input = document.getElementById('msginput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';

  sending = true;
  document.getElementById('sendbtn').style.display = 'none';
  document.getElementById('cancelbtn').style.display = 'block';

  addUserBubble(msg);
  const bubble = addAsstBubble();

  let currentTool = null;
  _cancelCtrl = new AbortController();

  try {
    const payload = {
      message: msg,
      agent_id: agentId,
      session_id: sessionId,
      model: currentModel(),
      slot: activeSlot,
    };
    const profilePath = currentProfilePath();
    if (profilePath) {
      payload.profile_path = profilePath;
    }
    const r = await fetch('/web/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
      signal: _cancelCtrl.signal,
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({error: 'Request failed'}));
      const thinking = bubble.querySelector('.thinking');
      if (thinking) thinking.remove();
      appendText(bubble, 'Error: ' + (err.error || r.statusText));
      if (r.status === 401 || r.status === 403) doDisconnect();
      return;
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});

      let nl;
      while ((nl = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, nl);
        buf = buf.slice(nl + 2);

        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6);
          let evt;
          try { evt = JSON.parse(raw); } catch { continue; }

          if (evt.type === 'tool_start') {
            currentTool = addToolCall(bubble, evt.name, evt.input);
          } else if (evt.type === 'tool_result') {
            if (currentTool) {
              setToolResult(currentTool, evt.data, evt.is_screenshot, evt.visible);
              currentTool = null;
            }
          } else if (evt.type === 'text') {
            appendText(bubble, evt.data);
          } else if (evt.type === 'cancelled') {
            appendText(bubble, '[Cancelled by user]');
          } else if (evt.type === 'error') {
            appendText(bubble, 'Error: ' + evt.data);
          } else if (evt.type === 'done') {
            _finalizeGroup();
            document.getElementById('agent-bar').classList.remove('active');
            _turnCount = 0;
            _navTrail = [];
            renderNavTrail();
          }
        }
      }
    }
  } catch(e) {
    const thinking = bubble.querySelector('.thinking');
    if (thinking) thinking.remove();
    if (e.name === 'AbortError') {
      appendText(bubble, '[Cancelled by user]');
    } else if (!bubble.querySelector('.text') && !bubble.querySelector('.action-group') && !bubble.querySelector('.action-standalone')) {
      appendText(bubble, 'Connection error: ' + e.message);
    }
  } finally {
    _cancelCtrl = null;
    sending = false;
    _finalizeGroup();
    document.getElementById('sendbtn').style.display = 'block';
    document.getElementById('cancelbtn').style.display = 'none';
    document.getElementById('agent-bar').classList.remove('active');
    _turnCount = 0;
    _navTrail = [];
    renderNavTrail();
  }
}
</script>
</body>
</html>"""

CHAT_CLAUDE_SDK_HTML = (
    CHAT_GEMINI_HTML
    .replace("Unchained — Gemini API", "Unchained — Claude API")
    .replace("Unchained Gemini API", "Unchained Claude API")
    .replace("AI browser agent powered by your Gemini API key", "AI browser agent powered by your Claude API key")
    .replace("Need a Gemini API key? Set up here &rarr;", "Need a Claude API key? Set up here &rarr;")
    .replace('href="/setup"', 'href="/setup?provider=claude-sdk"')
    .replace("No Gemini API key provisioned.", "No Claude API key provisioned.")
    .replace(
        """      <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
      <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
      <option value="gemini-2.0-flash">Gemini 2.0 Flash</option>""",
        """      <option value="claude-sdk:claude-sonnet-4-6">Claude API · Sonnet 4.6</option>
      <option value="claude-sdk:claude-opus-4-6">Claude API · Opus 4.6</option>
      <option value="claude-sdk:claude-haiku-4-5-20251001">Claude API · Haiku 4.5</option>""",
    )
    .replace("let geminiProvisioned = false;", "let claudeSdkProvisioned = false;")
    .replace("source: 'gemini'", "source: 'claude-sdk'")
    .replace("'unchained_session_' + agentId + '_gemini'", "'unchained_session_' + agentId + '_claude_sdk'")
    .replace("unchained_gemini_model", "unchained_claude_sdk_model")
    .replace(
        """    const gemini = (data.providers || []).find(p => p.name === 'gemini');
    geminiProvisioned = gemini && gemini.provisioned;
    document.getElementById('nokey-banner').style.display = geminiProvisioned ? 'none' : 'flex';""",
        """    const claudeSdk = (data.providers || []).find(p => p.name === 'claude-sdk');
    claudeSdkProvisioned = claudeSdk && claudeSdk.provisioned;
    document.getElementById('nokey-banner').style.display = claudeSdkProvisioned ? 'none' : 'flex';""",
    )
    .replace(
        "fetch('/web/chat/status?gemini=1')",
        "fetch('/web/chat/status?claude_sdk=1')",
    )
    .replace(
        """      updateAgentStatusUI({
        chat_connected: data.gemini_connected || false,
        bridge_connected: !!data.bridge_connected,
        mismatch: !!data.mismatch,
      });""",
        """      updateAgentStatusUI({
        chat_connected: data.claude_sdk_connected || false,
        bridge_connected: !!data.bridge_connected,
        mismatch: !!data.mismatch,
      });""",
    )
    .replace("if (data.gemini_agent_id) {", "if (data.claude_sdk_agent_id) {")
    .replace(
        "document.getElementById('agentlabel').textContent = data.gemini_agent_id;",
        "document.getElementById('agentlabel').textContent = data.claude_sdk_agent_id;",
    )
    .replace("if (!geminiProvisioned) {", "if (!claudeSdkProvisioned) {")
    .replace(
        "No Gemini API key provisioned. Visit /setup to get one.",
        "No Claude API key provisioned. Visit /setup to get one.",
    )
    .replace("Gemini API Chat", "Claude API Chat")
    .replace(
        "Browser agent powered by your provisioned Gemini API key.",
        "Browser agent powered by your provisioned Claude API key.",
    )
    .replace(
        """  const saved = localStorage.getItem('unchained_claude_sdk_model');
  if (saved && document.querySelector('#modelsel option[value="' + CSS.escape(saved) + '"]')) {
    document.getElementById('modelsel').value = saved;
  }""",
        """  const params = new URLSearchParams(window.location.search);
  const fromQuery = (params.get('model') || '').trim();
  if (fromQuery && document.querySelector('#modelsel option[value="' + CSS.escape(fromQuery) + '"]')) {
    document.getElementById('modelsel').value = fromQuery;
  } else {
    const saved = localStorage.getItem('unchained_claude_sdk_model');
    if (saved && document.querySelector('#modelsel option[value="' + CSS.escape(saved) + '"]')) {
      document.getElementById('modelsel').value = saved;
    }
  }""",
    )
)

CHAT_CODEX_HTML = (
    CHAT_GEMINI_HTML
    .replace("Unchained — Gemini API", "Unchained — Codex")
    .replace("Unchained Gemini API", "Unchained Codex")
    .replace("AI browser agent powered by your Gemini API key", "AI browser agent powered by your Codex API key")
    .replace("Need a Gemini API key? Set up here &rarr;", "Need a Codex key? Set up here &rarr;")
    .replace("No Gemini API key provisioned.", "No Codex key provisioned.")
    .replace("Gemini API Chat", "Codex Chat")
    .replace(
        "Browser agent powered by your provisioned Gemini API key.",
        "Browser agent powered by your provisioned Codex API key.",
    )
    .replace(
        """      <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
      <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
      <option value="gemini-2.0-flash">Gemini 2.0 Flash</option>""",
        """      <option value="codex-sdk:codex-mini-latest">Codex API · codex-mini-latest</option>
      <option value="codex-sdk:gpt-5-codex">Codex API · gpt-5-codex</option>
      <option value="codex-cli:gpt-5.1-codex-mini">Codex CLI · gpt-5.1-codex-mini</option>
      <option value="codex-cli:gpt-5-codex">Codex CLI · gpt-5-codex</option>""",
    )
    .replace("let geminiProvisioned = false;", "let codexProvisioned = false;")
    .replace("source: 'gemini'", "source: 'codex'")
    .replace("'unchained_session_' + agentId + '_gemini'", "'unchained_session_' + agentId + '_codex'")
    .replace("unchained_gemini_model", "unchained_codex_model")
    .replace(
        """    const gemini = (data.providers || []).find(p => p.name === 'gemini');
    geminiProvisioned = gemini && gemini.provisioned;
    document.getElementById('nokey-banner').style.display = geminiProvisioned ? 'none' : 'flex';""",
        """    const codexProviders = (data.providers || []).filter(p => p.name === 'codex-sdk' || p.name === 'codex-cli');
    codexProvisioned = codexProviders.some(p => p.provisioned);
    const _isCodexCli = currentModel().startsWith('codex-cli:');
    document.getElementById('nokey-banner').style.display = (codexProvisioned || _isCodexCli) ? 'none' : 'flex';""",
    )
    .replace(
        "fetch('/web/chat/status?gemini=1')",
        "fetch('/web/chat/status?codex=1&model=' + encodeURIComponent(currentModel()))",
    )
    .replace(
        """      updateAgentStatusUI({
        chat_connected: data.gemini_connected || false,
        bridge_connected: !!data.bridge_connected,
        mismatch: !!data.mismatch,
      });""",
        """      updateAgentStatusUI({
        chat_connected: data.codex_connected || false,
        bridge_connected: !!data.bridge_connected,
        mismatch: !!data.mismatch,
      });""",
    )
    .replace("if (data.gemini_agent_id) {", "if (data.codex_agent_id) {")
    .replace("document.getElementById('agentlabel').textContent = data.gemini_agent_id;", "document.getElementById('agentlabel').textContent = data.codex_agent_id;")
    .replace(
        """  const saved = localStorage.getItem('unchained_codex_model');
  if (saved && document.querySelector('#modelsel option[value="' + CSS.escape(saved) + '"]')) {
    document.getElementById('modelsel').value = saved;
  }""",
        """  const params = new URLSearchParams(window.location.search);
  const fromQuery = (params.get('model') || '').trim();
  if (fromQuery && document.querySelector('#modelsel option[value="' + CSS.escape(fromQuery) + '"]')) {
    document.getElementById('modelsel').value = fromQuery;
  } else {
    const saved = localStorage.getItem('unchained_codex_model');
    if (saved && document.querySelector('#modelsel option[value="' + CSS.escape(saved) + '"]')) {
      document.getElementById('modelsel').value = saved;
    }
  }
  if (document.getElementById('modelsel').value.startsWith('codex-cli:')) {
    document.querySelectorAll('#modelsel option[value^="codex-sdk:"]').forEach(o => o.remove());
  }""",
    )
    .replace(
        "if (!geminiProvisioned) {",
        "if (!codexProvisioned && !currentModel().startsWith('codex-cli:')) {",
    )
    .replace("No Gemini API key provisioned. Visit /setup to get one.", "No Codex key provisioned. Visit /setup to get one.")
    .replace(
        "/* === Model selector === */",
        """/* === Slot bar === */
#slotbar{
  display:flex;gap:6px;padding:4px 16px;
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#slotbar button{
  flex:1;height:32px;border:1px solid #444;border-radius:6px;
  background:transparent;color:var(--muted);font-size:12px;
  font-family:var(--mono);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:border-color 0.15s,color 0.15s;
}
#slotbar button:hover{border-color:var(--accent);color:var(--text)}
#slotbar button:active{transform:scale(0.95)}
#slotbar button.active{border-color:var(--accent);color:var(--accent);font-weight:600}
#slotbar button.empty{color:#555;font-style:italic}
#slotbar button.empty.active{color:var(--accent);font-style:normal}
#slotbar.locked button{pointer-events:none;opacity:0.4}
#slotbar.locked button.active{opacity:0.7}

/* === Model selector === */""",
    )
    .replace(
        '  <div id="agent-bar">',
        """  <div id="slotbar">
    <button onclick="switchSlot(1)" id="slot1" title="Independent conversation session">Chat A</button>
    <button onclick="switchSlot(2)" id="slot2" title="Independent conversation session">Chat B</button>
    <button onclick="switchSlot(3)" id="slot3" title="Independent conversation session">Chat C</button>
  </div>

  <div id="agent-bar">""",
    )
    .replace(
        """function currentModel() {
  return document.getElementById('modelsel').value;
}""",
        """function currentModel() {
  return document.getElementById('modelsel').value;
}

let activeSlot = 1;

async function loadSlots() {
  try {
    const qs = new URLSearchParams({
      model: currentModel(),
      session_id: sessionId,
    });
    const r = await fetch('/web/chat/slots?' + qs.toString());
    if (!r.ok) return;
    const data = await r.json();
    if (data.session_id) {
      sessionId = data.session_id;
      _persistSessionId(sessionId);
    }
    activeSlot = data.active_slot || 1;
    for (const s of (data.slots || [])) {
      const btn = document.getElementById('slot' + s.slot);
      if (!btn) continue;
      btn.className = '';
      if (s.slot === activeSlot) btn.classList.add('active');
      if (s.empty) {
        btn.classList.add('empty');
        btn.textContent = (['Chat A', 'Chat B', 'Chat C'][s.slot - 1] || ('Chat ' + s.slot));
      } else {
        btn.textContent = s.preview || (['Chat A', 'Chat B', 'Chat C'][s.slot - 1] || ('Chat ' + s.slot));
      }
    }
  } catch(e) {}
}

async function switchSlot(n) {
  if (n === activeSlot) return;
  if (sending) return;
  activeSlot = n;
  for (let i = 1; i <= 3; i++) {
    const btn = document.getElementById('slot' + i);
    if (btn) btn.classList.toggle('active', i === n);
  }
  document.getElementById('chat').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>';
  try {
    await fetch('/web/chat/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        slot: n,
        model: currentModel(),
        session_id: sessionId,
      }),
    });
  } catch(e) {}
  document.getElementById('chat').innerHTML = '';
  await loadHistory();
}""",
    )
    .replace(
        "async function loadHistory() {\n  try {",
        "async function loadHistory() {\n  await loadSlots();\n  try {",
    )
    .replace(
        "  } catch(e) {}\n}\n\ncheckSession();",
        "  } catch(e) {}\n  await loadSlots();\n}\n\ncheckSession();",
    )
    .replace(
        "  document.getElementById('cancelbtn').style.display = 'block';",
        """  document.getElementById('cancelbtn').style.display = 'block';
  const slotbar = document.getElementById('slotbar');
  if (slotbar) slotbar.classList.add('locked');""",
    )
    .replace(
        """    document.getElementById('cancelbtn').style.display = 'none';
    document.getElementById('agent-bar').classList.remove('active');""",
        """    document.getElementById('cancelbtn').style.display = 'none';
    const slotbar2 = document.getElementById('slotbar');
    if (slotbar2) slotbar2.classList.remove('locked');
    document.getElementById('agent-bar').classList.remove('active');""",
    )
)

def _inject_client_slots_ui(html: str) -> str:
    """Inject 3 local conversation slots for API-backed chat pages."""
    # Idempotency guard: some pages (for example TRIAL_CHAT_HTML) may already
    # contain the slot runtime. Re-injecting duplicates `let activeSlot`.
    if "let activeSlot = 1;" in html:
        return html
    return (
        html
        .replace(
            "/* === Model selector === */",
            """/* === Slot bar === */
#slotbar{
  display:flex;gap:6px;padding:4px 16px;
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#slotbar button{
  flex:1;height:32px;border:1px solid #444;border-radius:6px;
  background:transparent;color:var(--muted);font-size:12px;
  font-family:var(--mono);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:border-color 0.15s,color 0.15s;
}
#slotbar button:hover{border-color:var(--accent);color:var(--text)}
#slotbar button:active{transform:scale(0.95)}
#slotbar button.active{border-color:var(--accent);color:var(--accent);font-weight:600}
#slotbar button.empty{color:#555;font-style:italic}
#slotbar button.empty.active{color:var(--accent);font-style:normal}
#slotbar.locked button{pointer-events:none;opacity:0.4}
#slotbar.locked button.active{opacity:0.7}

/* === Model selector === */""",
        )
        .replace(
            '  <div id="agent-bar">',
            """  <div id="slotbar">
    <button onclick="switchSlot(1)" id="slot1" title="Independent conversation session">Chat A</button>
    <button onclick="switchSlot(2)" id="slot2" title="Independent conversation session">Chat B</button>
    <button onclick="switchSlot(3)" id="slot3" title="Independent conversation session">Chat C</button>
  </div>

  <div id="agent-bar">""",
        )
        .replace(
            """function _persistSessionId(sid) {
  if (sid && sid.startsWith('s-' + agentId)) {
    localStorage.setItem(_sessionStoreKey(), sid);
  }
}""",
            """function _persistSessionId(sid) {
  if (sid && sid.startsWith('s-' + agentId)) {
    localStorage.setItem(_sessionStoreKey(), sid);
  }
}

let activeSlot = 1;

function _slotLabel(n) {
  return (['Chat A', 'Chat B', 'Chat C'][n - 1] || ('Chat ' + n));
}

function _slotStateKey() {
  return _sessionStoreKey() + '_slots_v1';
}

function _newSessionId() {
  return 's-' + agentId + '-' + Date.now().toString(36);
}

function _loadSlotState() {
  let state = {active_slot: 1, slots: {}};
  try {
    const raw = localStorage.getItem(_slotStateKey()) || '';
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') state = parsed;
    }
  } catch(e) {}
  const slots = (state && typeof state.slots === 'object' && state.slots) ? state.slots : {};
  const normalized = {};
  for (let i = 1; i <= 3; i++) {
    const sid = String(slots[String(i)] || '').trim();
    normalized[String(i)] = (sid.startsWith('s-' + agentId + '-') ? sid : '');
  }
  let active = Number(state && state.active_slot);
  if (active !== 1 && active !== 2 && active !== 3) active = 1;
  return {active_slot: active, slots: normalized};
}

function _saveSlotState(state) {
  try { localStorage.setItem(_slotStateKey(), JSON.stringify(state)); } catch(e) {}
}

function _ensureSlotState() {
  const state = _loadSlotState();
  const restored = _restoreSessionId();
  if (!state.slots['1']) state.slots['1'] = restored || _newSessionId();
  for (let i = 1; i <= 3; i++) {
    if (!state.slots[String(i)]) state.slots[String(i)] = _newSessionId();
  }
  if (!state.slots[String(state.active_slot)]) state.active_slot = 1;
  _saveSlotState(state);
  return state;
}

function _setActiveSlotSession(sid) {
  if (!sid || !sid.startsWith('s-' + agentId + '-')) return;
  const state = _loadSlotState();
  state.slots[String(activeSlot)] = sid;
  state.active_slot = activeSlot;
  _saveSlotState(state);
}

function _syncSlotButtons() {
  const state = _loadSlotState();
  activeSlot = state.active_slot;
  for (let i = 1; i <= 3; i++) {
    const btn = document.getElementById('slot' + i);
    if (!btn) continue;
    btn.className = '';
    btn.textContent = _slotLabel(i);
    if (i === activeSlot) btn.classList.add('active');
  }
}

async function switchSlot(n) {
  if (n === activeSlot) return;
  if (sending) return;
  const state = _loadSlotState();
  state.active_slot = (n === 1 || n === 2 || n === 3) ? n : 1;
  if (!state.slots[String(state.active_slot)]) state.slots[String(state.active_slot)] = _newSessionId();
  _saveSlotState(state);
  activeSlot = state.active_slot;
  sessionId = state.slots[String(activeSlot)];
  _persistSessionId(sessionId);
  _syncSlotButtons();
  document.getElementById('chat').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>';
  await loadHistory();
}""",
        )
        .replace(
            "  sessionId = _restoreSessionId() || ('s-' + agentId + '-' + Date.now().toString(36));\n  _persistSessionId(sessionId);",
            """  const slotState = _ensureSlotState();
  activeSlot = slotState.active_slot;
  sessionId = slotState.slots[String(activeSlot)] || _restoreSessionId() || _newSessionId();
  _persistSessionId(sessionId);
  _setActiveSlotSession(sessionId);
  _syncSlotButtons();""",
        )
        .replace(
            "async function loadHistory() {\n  try {",
            "async function loadHistory() {\n  _syncSlotButtons();\n  try {",
        )
        .replace(
            """    const data = await r.json();
    if (data.session_id) {""",
            """    const data = await r.json();
    const chatEl = document.getElementById('chat');
    if (chatEl) chatEl.innerHTML = '';
    if (data.session_id) {""",
        )
        .replace(
            """      sessionId = data.session_id;
      _persistSessionId(sessionId);""",
            """      sessionId = data.session_id;
      _persistSessionId(sessionId);
      _setActiveSlotSession(sessionId);""",
        )
        .replace(
            "  } catch(e) {}\n}\n\ncheckSession();",
            "  } catch(e) {}\n  _syncSlotButtons();\n}\n\ncheckSession();",
        )
        .replace(
            "  document.getElementById('cancelbtn').style.display = 'block';",
            """  document.getElementById('cancelbtn').style.display = 'block';
  const slotbar = document.getElementById('slotbar');
  if (slotbar) slotbar.classList.add('locked');""",
        )
        .replace(
            """    document.getElementById('cancelbtn').style.display = 'none';
    document.getElementById('agent-bar').classList.remove('active');""",
            """    document.getElementById('cancelbtn').style.display = 'none';
    const slotbar2 = document.getElementById('slotbar');
    if (slotbar2) slotbar2.classList.remove('locked');
    document.getElementById('agent-bar').classList.remove('active');""",
        )
    )


TRIAL_CHAT_HTML = _inject_client_slots_ui(TRIAL_CHAT_HTML)
CHAT_GEMINI_HTML = _inject_client_slots_ui(CHAT_GEMINI_HTML)
CHAT_CLAUDE_SDK_HTML = _inject_client_slots_ui(CHAT_CLAUDE_SDK_HTML)
CHAT_CODEX_HTML = _inject_client_slots_ui(CHAT_CODEX_HTML)

# ---------------------------------------------------------------------------
# HTML — headless demo chat page (no setup, no downloads, headless Chrome)
# ---------------------------------------------------------------------------

HEADLESS_DEMO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Unchained Demo</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#1a1a2e;--surface:#16213e;--accent:#e94560;
  --text:#eee;--muted:#888;--user-bg:#2a1a3e;--asst-bg:#1e2a3e;
  --mono:'SF Mono','Menlo','Monaco','Consolas',monospace;
}
body{
  font-family:-apple-system,system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  height:100dvh;overflow:hidden;
}

/* === Login === */
#login{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100dvh;padding:24px;gap:16px;
}
#login h1{font-size:28px;color:var(--accent);margin-bottom:8px;letter-spacing:1px}
#login .sub{color:var(--muted);font-size:13px;margin-bottom:8px}
#login input{
  width:100%;max-width:360px;height:48px;padding:0 16px;
  border:1px solid #333;border-radius:8px;
  background:var(--surface);color:var(--text);font-size:16px;
}
#login input:focus{outline:none;border-color:var(--accent)}
#login button{
  width:100%;max-width:360px;height:48px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:16px;font-weight:600;cursor:pointer;
}
#login button:active{opacity:0.8}
#loginerr{color:#ff6b6b;font-size:14px;min-height:20px}

/* === Main === */
#main{display:none;flex-direction:column;height:100dvh}
#workspace{
  flex:1;min-height:0;display:flex;overflow:hidden;
}
#chat-pane{
  flex:1 1 0;min-width:0;display:flex;flex-direction:column;min-height:0;
}
#live-pane{
  flex:2 1 0;min-width:420px;width:auto;
  border-left:1px solid #2a2a2a;background:#111;
  display:flex;flex-direction:column;min-height:0;
}
#live-pane-head{
  padding:10px 12px;border-bottom:1px solid #222;
  color:#d5d5d5;font-size:12px;letter-spacing:0.4px;
  text-transform:uppercase;
}
#live-window{
  flex:1;display:flex;flex-direction:column;min-height:0;
  padding:12px;
}
#live-window-bar{
  height:28px;border:1px solid #2f2f2f;border-bottom:none;
  border-radius:8px 8px 0 0;background:#171717;
  display:flex;align-items:center;gap:6px;padding:0 10px;
}
#live-window-bar .dot{
  width:9px;height:9px;border-radius:50%;display:inline-block;
}
#live-window-bar .dot.red{background:#ff5f56}
#live-window-bar .dot.yellow{background:#ffbd2e}
#live-window-bar .dot.green{background:#27c93f}
#live-window-bar .title{
  margin-left:8px;color:#9a9a9a;font-size:11px;font-family:var(--mono);
}
#live-canvas-wrap{
  flex:1;min-height:0;border:1px solid #2f2f2f;border-radius:0 0 8px 8px;
  background:#0b0b0b;display:flex;align-items:center;justify-content:center;position:relative;
}
#live-image{
  width:100%;height:100%;object-fit:contain;background:#0b0b0b;display:none;
}
#live-placeholder{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:var(--muted);font-size:13px;padding:16px;text-align:center;
}
#live-status{
  padding:8px 12px 12px;color:var(--muted);font-size:12px;min-height:34px;
}
@media (max-width: 1100px) {
  #live-pane{min-width:320px}
}
@media (max-width: 900px) {
  #workspace{flex-direction:column}
  #live-pane{
    width:100%;min-width:0;height:40vh;border-left:none;border-top:1px solid #2a2a2a;
  }
}

#topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:8px 16px;padding-top:max(8px,env(safe-area-inset-top));
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .agent{font-family:var(--mono);font-size:14px;color:var(--accent)}
#topbar .status{font-size:11px;color:var(--muted)}
#topbar .status.online{color:#4caf50}
#topbar .nav{display:flex;gap:8px}
#topbar .nav a{
  color:var(--muted);text-decoration:none;font-size:12px;
  border:1px solid #555;padding:4px 10px;border-radius:6px;
}
#topbar .nav a:hover{border-color:var(--accent);color:var(--accent)}

/* === Model notice === */
#model-notice{
  display:none;margin:0;padding:8px 16px;
  border-bottom:1px solid #5a4a22;background:#2d2515;color:#dcc58a;
  font-size:12px;line-height:1.4;flex-shrink:0;
}
#model-notice strong{color:#f0d58b}
#model-notice a{color:#f0d58b;text-decoration:underline}

/* === Chat === */
#chat{
  flex:1;overflow-y:auto;padding:12px 16px;
  -webkit-overflow-scrolling:touch;
  display:flex;flex-direction:column;gap:12px;
}
.bubble{
  max-width:85%;padding:10px 14px;border-radius:12px;
  font-size:14px;line-height:1.5;word-break:break-word;
}
.bubble.user{
  align-self:flex-end;background:var(--user-bg);
  border:1px solid #3a2a5e;border-bottom-right-radius:4px;
}
.bubble.asst{
  align-self:flex-start;background:var(--asst-bg);
  border:1px solid #2a3a5e;border-bottom-left-radius:4px;
}
.bubble.asst .text{white-space:pre-wrap}
.bubble.asst .text.rendered{white-space:normal}
.bubble.asst .text.rendered p{margin:0.5em 0}
.bubble.asst .text.rendered p:first-child{margin-top:0}
.bubble.asst .text.rendered p:last-child{margin-bottom:0}
.bubble.asst .text.rendered h1{font-size:1.3em;margin:0.8em 0 0.4em;font-weight:700;color:var(--accent)}
.bubble.asst .text.rendered h2{font-size:1.15em;margin:0.7em 0 0.35em;font-weight:600;color:var(--accent)}
.bubble.asst .text.rendered h3{font-size:1.05em;margin:0.6em 0 0.3em;font-weight:600}
.bubble.asst .text.rendered h4{font-size:1em;margin:0.5em 0 0.25em;font-weight:600}
.bubble.asst .text.rendered code{
  background:rgba(255,255,255,0.08);padding:2px 5px;border-radius:3px;
  font-family:var(--mono);font-size:0.88em;
}
.bubble.asst .text.rendered pre{
  background:#0d1117;border-radius:6px;padding:12px;margin:8px 0;
  overflow-x:auto;white-space:pre-wrap;word-break:break-word;
}
.bubble.asst .text.rendered pre code{background:none;padding:0;font-size:12px;line-height:1.5}
.bubble.asst .text.rendered a{color:var(--accent);text-decoration:underline}
.bubble.asst .text.rendered ul,.bubble.asst .text.rendered ol{margin:0.4em 0;padding-left:1.5em}
.bubble.asst .text.rendered li{margin:0.2em 0}
.bubble.asst .text.rendered blockquote{
  border-left:3px solid var(--accent);padding-left:12px;margin:0.5em 0;color:var(--muted);
}
.bubble.asst .text.rendered hr{border:none;border-top:1px solid #333;margin:1em 0}
.bubble.asst .text.rendered table{border-collapse:collapse;margin:0.5em 0;width:100%;font-size:13px}
.bubble.asst .text.rendered th,.bubble.asst .text.rendered td{border:1px solid #333;padding:6px 10px}
.bubble.asst .text.rendered th{background:rgba(255,255,255,0.05);font-weight:600}
.bubble.asst .text.rendered strong{font-weight:600}
.bubble.asst .text.rendered em{font-style:italic}
.bubble.asst .text .math-display{margin:0.8em 0;text-align:center;overflow-x:auto}
.bubble.asst .text .katex{color:inherit;font-size:1.05em}
.bubble.asst .text .katex-display{margin:0.5em 0;overflow-x:auto;padding:4px 0}
.bubble.asst .thinking{color:var(--muted);font-style:italic;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
/* === Action groups (browser activity cards) === */
@keyframes slideIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.action-group{margin:6px 0;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;font-size:12px;font-family:var(--mono);animation:slideIn 0.2s ease-out}
.ag-header{display:flex;align-items:center;gap:8px;padding:7px 12px;background:#111;border-bottom:1px solid #1e1e1e}
.ag-emoji{font-size:14px;flex-shrink:0}
.ag-site{color:var(--accent);font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ag-count{color:var(--muted);font-size:10px;flex-shrink:0}
.ag-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.ag-dot.running{animation:pulse 1.2s ease-in-out infinite}
.ag-dot.done{color:#4ade80}
.ag-steps{padding:2px 0 4px}
.action-step{display:flex;align-items:center;gap:7px;padding:3px 12px 3px 28px}
.as-emoji{font-size:11px;flex-shrink:0;width:16px;text-align:center}
.as-label{color:var(--text);font-size:11px;flex-shrink:0}
.as-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.as-dot{margin-left:auto;flex-shrink:0;font-size:10px;color:var(--muted)}
.as-dot.running{animation:pulse 1.2s ease-in-out infinite}
.as-dot.done{color:#4ade80}
.as-screenshot{padding:4px 12px 6px 28px}
.as-screenshot img{max-width:100%;border-radius:4px}
.intel-bars{padding:4px 12px 6px 28px;display:flex;flex-direction:column;gap:4px}
.intel-bar{display:flex;align-items:center;gap:8px;font-size:10.5px;font-family:var(--mono)}
.intel-bar-label{width:96px;color:var(--muted);flex-shrink:0;overflow:hidden;text-overflow:ellipsis}
.intel-bar-track{flex:1;height:3px;background:#2a2a2a;border-radius:2px;overflow:hidden}
.intel-bar-fill{height:100%;border-radius:2px;background:var(--accent)}
.intel-bar-pct{color:var(--text);width:32px;text-align:right;flex-shrink:0}
.action-standalone{display:flex;align-items:center;gap:8px;padding:5px 12px;margin:4px 0;border:1px solid #2a2a2a;border-radius:6px;font-size:12px;font-family:var(--mono);animation:slideIn 0.2s ease-out;background:#111}
.standalone-emoji{font-size:13px;flex-shrink:0}
.standalone-label{color:var(--accent);font-weight:500;flex-shrink:0}
.standalone-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.standalone-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.standalone-dot.running{animation:pulse 1.2s ease-in-out infinite}
.standalone-dot.done{color:#4ade80}
#agent-bar{
  display:none;padding:3px 16px;background:var(--surface);
  border-bottom:1px solid #222;font-size:11px;font-family:var(--mono);
  color:var(--muted);align-items:center;gap:10px;min-height:22px;
}
#agent-bar.active{display:flex}
#agent-action{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#nav-trail{display:flex;align-items:center;gap:0;overflow:hidden;font-size:10px;opacity:0.5;flex-shrink:0;max-width:45%}
#nav-trail span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100px}
#nav-trail span+span::before{content:' > ';opacity:0.6}
#turn-ctr{flex-shrink:0;opacity:0.4;font-size:10px}
.bubble.asst{position:relative}
.bubble.asst .copy-btn{
  position:absolute;top:6px;right:6px;
  background:rgba(255,255,255,0.1);border:none;color:var(--muted);
  padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;
  opacity:0;transition:opacity 0.15s;font-family:var(--mono);
}
.bubble.asst:hover .copy-btn{opacity:1}
.bubble.asst .copy-btn.copied{opacity:1;color:var(--accent)}

/* === Input === */
#inputbar{
  display:flex;gap:8px;
  padding:8px 16px;padding-bottom:max(16px,env(safe-area-inset-bottom));
  background:var(--surface);border-top:1px solid #333;flex-shrink:0;
}
#msginput{
  flex:1;min-width:0;min-height:44px;max-height:120px;padding:10px 12px;
  border:1px solid #444;border-radius:12px;
  background:var(--bg);color:var(--text);
  font-size:15px;font-family:-apple-system,system-ui,sans-serif;
  resize:none;line-height:1.4;
}
#msginput:focus{outline:none;border-color:var(--accent)}
#sendbtn{
  width:44px;height:44px;border:none;border-radius:12px;
  background:var(--accent);color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;align-self:flex-end;
}
#sendbtn:active{opacity:0.8}
#sendbtn:disabled{opacity:0.4;cursor:default}
#cancelbtn{
  width:44px;height:44px;border:none;border-radius:12px;
  background:#ff4444;color:#fff;font-size:18px;
  cursor:pointer;flex-shrink:0;align-self:flex-end;
  display:none;
}
#cancelbtn:active{opacity:0.8}

/* === Chat hints === */
#chat-hints{
  display:flex;flex-direction:column;align-items:center;
  justify-content:flex-start;height:100%;padding-top:24px;text-align:center;
}
.hint-badge{
  display:inline-flex;align-items:center;gap:8px;padding:7px 12px;border-radius:999px;
  border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.03);
  color:#cfd5e4;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;
}
.hint-title{font-size:20px;font-weight:600;color:var(--accent);margin-bottom:8px}
.hint-sub{color:var(--muted);font-size:13px;margin-bottom:16px;max-width:380px;line-height:1.5}
.hint-panels{
  display:grid;grid-template-columns:1fr;gap:10px;width:100%;max-width:380px;margin-bottom:14px;
}
.hint-panel{
  padding:14px;border:1px solid #333;border-radius:14px;background:rgba(255,255,255,0.03);text-align:left;
}
.hint-panel.accent{
  border-color:rgba(233,69,96,0.35);background:rgba(233,69,96,0.08);
}
.hint-panel strong{
  display:block;margin-bottom:6px;color:var(--text);font-size:13px;
}
.hint-panel span{
  color:var(--muted);font-size:12px;line-height:1.6;
}
.hint-examples{display:flex;flex-direction:column;gap:8px;width:100%;max-width:380px}
.hint-item{
  padding:10px 14px;border:1px solid #333;border-radius:10px;
  font-size:13px;color:var(--text);cursor:pointer;text-align:left;
  transition:border-color 0.15s;display:flex;align-items:center;gap:10px;
}
.hint-item:hover{border-color:var(--accent)}
.hint-emoji{font-size:18px;flex-shrink:0}
.hint-actions{
  display:flex;justify-content:center;width:100%;margin-top:16px;
}
.hint-cta{
  display:inline-flex;align-items:center;justify-content:center;padding:12px 18px;border-radius:10px;
  background:var(--accent);color:#fff;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:0.2px;
}
.hint-cta:hover{opacity:0.92;box-shadow:0 0 18px rgba(233,69,96,0.28)}
.hint-note{
  margin:14px 0 4px;max-width:380px;color:#c7cedf;font-size:12px;line-height:1.6;
}
.hint-footer{
  margin-top:20px;font-size:11px;color:var(--muted);
  letter-spacing:0.5px;text-transform:uppercase;
}

.upgrade-card{
  align-self:stretch;padding:18px 20px;border:1px solid rgba(255,255,255,0.08);
  border-radius:16px;background:linear-gradient(135deg, rgba(233,69,96,0.16), rgba(17,17,25,0.96));
  box-shadow:0 18px 40px rgba(0,0,0,0.24);
}
.upgrade-card h3{
  margin:0 0 8px;font-size:19px;color:var(--text);letter-spacing:0.2px;
}
.upgrade-card p{
  margin:0;color:#d6dbea;font-size:13px;line-height:1.6;
}
.upgrade-card .upgrade-prompt{
  margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(255,255,255,0.05);
  color:#f0f2f8;font-size:12px;font-family:var(--mono);
}
.upgrade-card .upgrade-list{
  display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px;
}
.upgrade-card .upgrade-item{
  padding:12px;border:1px solid rgba(255,255,255,0.08);border-radius:12px;background:rgba(8,10,15,0.3);
}
.upgrade-card .upgrade-item strong{
  display:block;margin-bottom:4px;font-size:13px;color:var(--text);
}
.upgrade-card .upgrade-item span{
  color:var(--muted);font-size:12px;line-height:1.5;
}
.upgrade-card .upgrade-actions{
  display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px;flex-wrap:wrap;
}
.upgrade-card .upgrade-why{
  flex:1;min-width:220px;color:#c7cedf;font-size:12px;line-height:1.5;
}
.upgrade-card .upgrade-cta{
  display:inline-flex;align-items:center;justify-content:center;padding:11px 18px;border-radius:10px;
  background:var(--accent);color:#fff;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:0.2px;
}
.upgrade-card .upgrade-cta:hover{opacity:0.92;box-shadow:0 0 18px rgba(233,69,96,0.28)}
@media(max-width:720px){
  .upgrade-card .upgrade-list{grid-template-columns:1fr}
}

/* === Quota modal === */
#quota-modal{
  display:none;position:fixed;inset:0;z-index:200;
  background:rgba(0,0,0,0.75);align-items:center;justify-content:center;
}
#quota-modal.visible{display:flex}
.quota-box{
  background:var(--surface);border:1px solid #444;border-radius:16px;
  padding:32px;max-width:480px;width:92%;text-align:center;
}
.quota-box h2{font-size:22px;color:var(--accent);margin-bottom:8px}
.quota-box .quota-sub{color:var(--muted);font-size:14px;margin-bottom:24px;line-height:1.6}
.quota-why-free{
  margin:-12px 0 24px;color:#c9cfdb;font-size:13px;line-height:1.6;
}
.quota-grid{
  display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px;text-align:left;
}
.quota-item{
  padding:12px;border:1px solid #333;border-radius:10px;background:rgba(255,255,255,0.03);
}
.quota-item strong{display:block;font-size:14px;color:var(--text);margin-bottom:2px}
.quota-item span{font-size:12px;color:var(--muted);line-height:1.4}
.quota-cta{
  display:inline-block;padding:12px 28px;border-radius:8px;
  background:var(--accent);color:#fff;font-size:15px;font-weight:600;
  text-decoration:none;letter-spacing:0.5px;transition:opacity 0.2s;
}
.quota-cta:hover{opacity:0.9;box-shadow:0 0 20px rgba(233,69,96,0.3)}
.quota-dismiss{
  display:block;margin-top:12px;background:none;border:none;
  color:var(--muted);font-size:13px;cursor:pointer;
}
.quota-dismiss:hover{color:var(--text)}
</style>
</head>
<body>

<!-- Quota modal -->
<div id="quota-modal">
  <div class="quota-box">
    <h2>Shared demo complete</h2>
    <p class="quota-sub">You've used your 2 shared demo runs. Continue free to generate your personal connection key and move into the real product mode.</p>
    <div class="quota-grid">
      <div class="quota-item"><strong>Shared demo browser</strong><span>Public EC2 browser with a limited set of reliable public sites.</span></div>
      <div class="quota-item"><strong>Your Claude + your browser</strong><span>Use your Claude Pro or Max plan in your current Chrome profile or a clean guest profile.</span></div>
      <div class="quota-item"><strong>Why free</strong><span>You bring Claude, your browser, and your data. Unchained only provides the extraction layer.</span></div>
      <div class="quota-item"><strong>Login only connects</strong><span>Authentication just creates your personal connection key. No credit card and no extra API billing from Unchained.</span></div>
    </div>
    <a href="/local" class="quota-cta" onclick="handoffFirstLookToClaude();return false;">Connect Claude Free &rarr;</a>
    <button class="quota-dismiss" onclick="dismissQuota()">Stay in the shared demo</button>
  </div>
</div>

<!-- Login -->
<div id="login">
  <h1>Unchained First Look</h1>
  <div class="sub">AI browser agent first look &mdash; watch it browse in real time</div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="outline"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
</div>

<!-- Pending -->
<div id="pending" style="display:none;flex-direction:column;align-items:center;justify-content:center;height:100dvh;padding:24px;gap:16px;text-align:center">
  <h1 style="font-size:28px;color:var(--accent);letter-spacing:1px">Request Being Reviewed</h1>
  <p style="color:var(--muted);font-size:15px;max-width:400px">Your sign-up request is being reviewed. We'll send you an email once your account is approved.</p>
  <button onclick="checkApproval()" style="margin-top:16px;padding:10px 24px;border:1px solid var(--accent);border-radius:8px;background:transparent;color:var(--accent);font-size:14px;cursor:pointer">Check Again</button>
  <button onclick="backToLogin()" style="margin-top:8px;padding:10px 24px;border:1px solid #444;border-radius:8px;background:transparent;color:var(--muted);font-size:14px;cursor:pointer">Use a Different Account</button>
  <div id="pendingmsg" style="color:var(--muted);font-size:13px;min-height:20px"></div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <div class="left">
      <span class="agent" id="agentlabel"></span>
      <span class="status online" id="agentstatus">headless agent</span>
    </div>
    <div class="nav">
      <a href="/">Home</a>
      <a id="first-look-upgrade-link" href="/local" onclick="handoffFirstLookToClaude();return false;">Connect Claude Free</a>
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a id="first-look-scheduler-link" href="/scheduler">Scheduler</a>
      <a id="first-look-logout-link" href="#" onclick="doDisconnect();return false">Logout</a>
    </div>
  </div>

  <div id="model-notice" style="display:block"><strong>Shared demo browser:</strong> two runs on selected public sites. <a href="/local" onclick="handoffFirstLookToClaude();return false;">Connect Claude Free</a> to use your own browser and your existing Claude plan.</div>

  <div id="workspace">
    <div id="chat-pane">
      <div id="agent-bar">
        <span id="agent-action"></span>
        <span id="nav-trail"></span>
        <span id="turn-ctr"></span>
      </div>

      <div id="chat">
          <div id="chat-hints">
            <div class="hint-badge">2 shared demo runs &middot; selected public sites</div>
            <div class="hint-title">Shared demo first. Your Claude next.</div>
          <div class="hint-sub">Use the public sandbox to watch the agent browse in real time. Then connect your own Claude for free to run on your own browser.</div>
          <div class="hint-panels">
            <div class="hint-panel"><strong>Shared demo browser</strong><span>Runs on our EC2 browser and works best on a small set of reliable public sites.</span></div>
            <div class="hint-panel accent"><strong>Your Claude + your browser</strong><span>Use your Claude Pro or Max plan in your current Chrome profile or a clean guest profile.</span></div>
          </div>
          <div class="hint-examples">
            <div class="hint-item" onclick="fillMsg('On Wikipedia, compare Ada Lovelace, Grace Hopper, and Katherine Johnson. For each, give field, lifespan, and one major contribution, then rank them by birth year.')"><span class="hint-emoji">&#128187;</span> Compare three computing pioneers on Wikipedia</div>
            <div class="hint-item" onclick="fillMsg('Check weather.gov for New York City and tell me whether today or tomorrow is better for an outdoor coffee, using temperature, wind, and rain to justify the answer.')"><span class="hint-emoji">&#9749;</span> Pick the better outdoor coffee day in NYC</div>
            <div class="hint-item" onclick="fillMsg('Open Hacker News, list the top 5 stories right now, group them into 2 or 3 themes, and tell me which one a browser-tools builder should read first.')"><span class="hint-emoji">&#128240;</span> Group the top Hacker News stories into themes</div>
          </div>
          <div class="hint-note">Why free? You bring Claude, your browser, and your data. Unchained only provides the lightweight extraction layer.</div>
          <div class="hint-actions"><a class="hint-cta" href="/local" onclick="handoffFirstLookToClaude();return false;">Connect Claude Free</a></div>
          <div class="hint-footer">Shared demo browser on selected public sites only</div>
        </div>
      </div>

      <div id="inputbar">
        <textarea id="msginput" rows="1" placeholder="Ask the agent anything..."
                  onkeydown="handleKey(event)" oninput="autoGrow(this)"></textarea>
        <button id="sendbtn" onclick="doSend()">&#9654;</button>
        <button id="cancelbtn" onclick="doCancel()">&#9632;</button>
      </div>
    </div>

    <aside id="live-pane">
      <div id="live-pane-head">Shared Demo Browser</div>
      <div id="live-window">
        <div id="live-window-bar">
          <span class="dot red"></span>
          <span class="dot yellow"></span>
          <span class="dot green"></span>
          <span class="title">shared-demo-chrome</span>
        </div>
        <div id="live-canvas-wrap">
          <img id="live-image" alt="Headless browser live preview">
          <div id="live-placeholder">The browser preview appears here after navigation.</div>
        </div>
      </div>
      <div id="live-status">Waiting for first page load...</div>
    </aside>
  </div>
</div>
<script>

let agentId = '';
let sessionId = '';
let sending = false;
let _cancelCtrl = null;
let demoPromptCount = 0;
let demoUnlimited = false;
let _autoPromptFired = false;
let _userName = '';
let _userPicture = '';
let _livePreviewHasFrame = false;
let _isAuthenticatedUser = false;
const FIRST_LOOK_PROMPT_KEY = 'unchained_first_look_last_prompt';
const FIRST_LOOK_SESSION_KEY = 'unchained_first_look_last_session';

function setLiveStatus(text) {
  const el = document.getElementById('live-status');
  if (el) el.textContent = text;
}

function resetLivePreview() {
  const img = document.getElementById('live-image');
  const ph = document.getElementById('live-placeholder');
  if (img) {
    img.removeAttribute('src');
    img.style.display = 'none';
  }
  if (ph) ph.style.display = 'flex';
  _livePreviewHasFrame = false;
  setLiveStatus('Waiting for first page load...');
}

function rememberFirstLookPrompt(prompt) {
  try {
    // Intentionally same-origin readable: this is a lightweight local handoff, not a secret store.
    const text = String(prompt || '').trim();
    if (text) localStorage.setItem(FIRST_LOOK_PROMPT_KEY, text);
    const activeSessionId = (typeof sessionId !== 'undefined' && sessionId) ? sessionId : '';
    if (activeSessionId) localStorage.setItem(FIRST_LOOK_SESSION_KEY, activeSessionId);
  } catch(e) {}
}

function currentFirstLookPrompt() {
  const input = document.getElementById('msginput');
  const current = input ? String(input.value || '').trim() : '';
  return current || (localStorage.getItem(FIRST_LOOK_PROMPT_KEY) || '').trim();
}

function handoffFirstLookToClaude() {
  const prompt = currentFirstLookPrompt();
  if (prompt) rememberFirstLookPrompt(prompt);
  window.location.href = '/local';
}

function continueInResearchDesk() {
  const input = document.getElementById('msginput');
  const current = input ? String(input.value || '').trim() : '';
  const prompt = current || (localStorage.getItem(FIRST_LOOK_PROMPT_KEY) || '').trim();
  if (prompt) rememberFirstLookPrompt(prompt);
  const activeSessionId = (typeof sessionId !== 'undefined' && sessionId) ? sessionId : '';
  const qs = new URLSearchParams();
  if (prompt) qs.set('prompt', prompt.slice(0, 500)); // must match MAX_URL_PROMPT_CHARS on the Research Desk page
  if (activeSessionId) qs.set('session_id', activeSessionId);
  const suffix = qs.toString() ? ('?' + qs.toString()) : '';
  window.location.href = '/labs/research-desk' + suffix;
}

function updateLivePreview(imageB64, note) {
  if (!imageB64) return;
  const img = document.getElementById('live-image');
  const ph = document.getElementById('live-placeholder');
  if (!img) return;
  img.src = 'data:image/png;base64,' + imageB64;
  img.style.display = 'block';
  if (ph) ph.style.display = 'none';
  _livePreviewHasFrame = true;
  const stamp = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  setLiveStatus((note || 'Live page refreshed') + ' \u2022 ' + stamp);
}

async function handleGoogleCredential(response) {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential, source: 'trial'}),
    });
    const data = await r.json();
    if (data.pending || data.status === 'pending') { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    agentId = data.agent_id;
    _isAuthenticatedUser = true;
    demoPromptCount = data.demo_prompt_count || 0;
    demoUnlimited = !!data.demo_unlimited;
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me', {
      credentials: 'include',
      cache: 'no-store',
    });
    const data = await r.json();
    if (data.authenticated) {
      agentId = data.agent_id;
      _isAuthenticatedUser = true;
      demoPromptCount = data.demo_prompt_count || 0;
      demoUnlimited = !!data.demo_unlimited;
      _userName = data.name || '';
      _userPicture = data.picture || '';
      showMain();
      return;
    }
    if (data.pending || data.status === 'pending') { showPending(); return; }
  } catch(e) {}
  _isAuthenticatedUser = false;
  _userName = 'Guest';
  _userPicture = '';
  showMain();
}

async function checkApproval() {
  const msg = document.getElementById('pendingmsg');
  msg.textContent = 'Checking...';
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) {
      agentId = data.agent_id;
      _isAuthenticatedUser = true;
      _userName = data.name || _userName || 'Unchained';
      _userPicture = data.picture || '';
      demoPromptCount = data.demo_prompt_count || 0;
      demoUnlimited = !!data.demo_unlimited;
      showMain();
      return;
    }
    if (data.pending) { msg.textContent = 'Still under review. Check back soon!'; return; }
    msg.textContent = 'Still under review.';
  } catch(e) { msg.textContent = 'Could not check status.'; }
}

async function doDisconnect() {
  await fetch('/auth/logout', {method: 'POST'});
  agentId = '';
  sessionId = '';
  _isAuthenticatedUser = false;
  _userName = 'Guest';
  _userPicture = '';
  showMain();
}

function showPending() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'flex';
}

async function backToLogin() {
  await fetch('/auth/logout', {method: 'POST'});
  document.getElementById('pending').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

function showQuotaModal() {
  document.getElementById('quota-modal').classList.add('visible');
}

function dismissQuota() {
  document.getElementById('quota-modal').classList.remove('visible');
  document.getElementById('msginput').disabled = true;
  document.getElementById('sendbtn').disabled = true;
}

function currentModel() {
  return _forcedFirstLookModel || 'google/gemini-3-flash-preview';
}

function _sessionStoreKey() {
  return 'unchained_session_' + agentId + '_first_look';
}

function _restoreSessionId() {
  const sid = localStorage.getItem(_sessionStoreKey()) || '';
  const prefix = 's-' + agentId + '-';
  return (sid.startsWith(prefix) && sid.length > prefix.length) ? sid : '';
}

function _persistSessionId(sid) {
  if (sid && sid.startsWith('s-' + agentId)) {
    localStorage.setItem(_sessionStoreKey(), sid);
  }
}

let lastAgentConnected = false;
let _forcedFirstLookModel = '';

function updateAgentStatusUI(connected) {
  const el = document.getElementById('agentstatus');
  el.textContent = _isAuthenticatedUser ? 'your browser mode' : 'shared demo browser';
  el.className = 'status online';
}

function updateFirstLookChromeUI() {
  const notice = document.getElementById('model-notice');
  const upgrade = document.getElementById('first-look-upgrade-link');
  const scheduler = document.getElementById('first-look-scheduler-link');
  const logout = document.getElementById('first-look-logout-link');
  const liveHead = document.getElementById('live-pane-head');
  const liveTitle = document.querySelector('#live-window-bar .title');
  if (notice) notice.style.display = _isAuthenticatedUser ? 'none' : 'block';
  if (upgrade) upgrade.textContent = _isAuthenticatedUser ? 'Open Claude' : 'Connect Claude Free';
  if (scheduler) scheduler.style.display = _isAuthenticatedUser ? 'block' : 'none';
  if (logout) logout.style.display = _isAuthenticatedUser ? 'block' : 'none';
  if (liveHead) liveHead.textContent = _isAuthenticatedUser ? 'Your Browser' : 'Shared Demo Browser';
  if (liveTitle) liveTitle.textContent = _isAuthenticatedUser ? 'your-browser' : 'shared-demo-chrome';
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  document.getElementById('agentlabel').textContent = _userName || (_isAuthenticatedUser ? 'Unchained' : 'Guest');
  updateFirstLookChromeUI();
  resetLivePreview();
  try { localStorage.setItem('unchained_last_route', '/first-look'); } catch(e){}
  sessionId = _restoreSessionId() || ('s-' + agentId + '-' + Date.now().toString(36));
  _persistSessionId(sessionId);
  checkAgentStatus();
  setInterval(checkAgentStatus, 10000);
  loadHistory();
}

async function checkAgentStatus() {
  try {
    const r = await fetch('/web/chat/status?first_look_guest=1');
    if (r.ok) {
      const data = await r.json();
      if (data.agent_id) agentId = data.agent_id;
      lastAgentConnected = data.connected;
      updateAgentStatusUI(data.connected);
    }
  } catch(e) {}
}

async function loadHistory() {
  try {
    const qs = new URLSearchParams({
      model: currentModel(),
      session_id: sessionId,
      first_look_guest: '1',
    });
    const r = await fetch('/web/chat/history?' + qs.toString());
    if (!r.ok) return;
    const data = await r.json();
    if (data.session_id) {
      sessionId = data.session_id;
      _persistSessionId(sessionId);
    }
    if (!data.messages || data.messages.length === 0) {
      showHintsIfEmpty();
      return;
    }
    hideHints();
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        addUserBubble(msg.content);
      } else if (msg.role === 'assistant') {
        const bubble = addAsstBubble();
        const thinking = bubble.querySelector('.thinking');
        if (thinking) thinking.remove();
        appendText(bubble, msg.content);
      }
    }
  } catch(e) {}
}

async function maybeAutoPrompt() {
  if (_autoPromptFired) return;
  if (demoPromptCount > 0) return;
  _autoPromptFired = true;
  // Wait for headless agent to be connected (poll up to 10s)
  for (let i = 0; i < 20; i++) {
    if (lastAgentConnected) break;
    await new Promise(r => setTimeout(r, 500));
    try {
      const r = await fetch('/web/chat/status?first_look_guest=1');
      if (r.ok) {
        const d = await r.json();
        if (d.agent_id) agentId = d.agent_id;
        lastAgentConnected = d.connected;
      }
    } catch(e) {}
  }
  if (!lastAgentConnected) return;
  // Type the auto-prompt with animation
  const prompt = 'On Wikipedia, compare Ada Lovelace, Grace Hopper, and Katherine Johnson. For each, give field, lifespan, and one major contribution, then rank them by birth year.';
  const input = document.getElementById('msginput');
  hideHints();
  for (let i = 0; i < prompt.length; i++) {
    input.value = prompt.slice(0, i + 1);
    autoGrow(input);
    input.scrollTop = input.scrollHeight;
    await new Promise(r => setTimeout(r, 12 + Math.random() * 10));
  }
  await new Promise(r => setTimeout(r, 400));
  doSend();
}

function showHintsIfEmpty() {
  if (document.getElementById('chat-hints')) return;
  document.getElementById('chat').innerHTML =
    '<div id="chat-hints">' +
    '<div class="hint-badge">2 shared demo runs &middot; selected public sites</div>' +
    '<div class="hint-title">Shared demo first. Your Claude next.</div>' +
    '<div class="hint-sub">Use the public sandbox to watch the agent browse in real time. Then connect your own Claude for free to run on your own browser.</div>' +
    '<div class="hint-panels">' +
    '<div class="hint-panel"><strong>Shared demo browser</strong><span>Runs on our EC2 browser and works best on a small set of reliable public sites.</span></div>' +
    '<div class="hint-panel accent"><strong>Your Claude + your browser</strong><span>Use your Claude Pro or Max plan in your current Chrome profile or a clean guest profile.</span></div>' +
    '</div>' +
    '<div class="hint-examples">' +
    '<div class="hint-item" onclick="fillMsg(\'On Wikipedia, compare Ada Lovelace, Grace Hopper, and Katherine Johnson. For each, give field, lifespan, and one major contribution, then rank them by birth year.\')"><span class="hint-emoji">&#128187;</span> Compare three computing pioneers on Wikipedia</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Check weather.gov for New York City and tell me whether today or tomorrow is better for an outdoor coffee, using temperature, wind, and rain to justify the answer.\')"><span class="hint-emoji">&#9749;</span> Pick the better outdoor coffee day in NYC</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Open Hacker News, list the top 5 stories right now, group them into 2 or 3 themes, and tell me which one a browser-tools builder should read first.\')"><span class="hint-emoji">&#128240;</span> Group the top Hacker News stories into themes</div>' +
    '</div>' +
    '<div class="hint-note">Why free? You bring Claude, your browser, and your data. Unchained only provides the lightweight extraction layer.</div>' +
    '<div class="hint-actions"><a class="hint-cta" href="/local" onclick="handoffFirstLookToClaude();return false;">Connect Claude Free</a></div>' +
    '<div class="hint-footer">Shared demo browser on selected public sites only</div>' +
    '</div>';
}

async function doNewChat() {
  if (sending) return;
  document.getElementById('chat').innerHTML = '';
  showHintsIfEmpty();
  try {
    const r = await fetch('/web/chat/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        model: currentModel(),
        session_id: sessionId,
        first_look_guest: true,
      }),
    });
    if (r.ok) {
      const data = await r.json();
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
      }
    }
  } catch(e) {}
}

checkSession();
function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    doSend();
  }
}

function scrollToBottom() {
  const chat = document.getElementById('chat');
  chat.scrollTop = chat.scrollHeight;
}

function fillMsg(text) {
  const input = document.getElementById('msginput');
  input.value = text;
  input.focus();
  autoGrow(input);
}

function removeClaudeUpgradeCard() {
  document.querySelectorAll('.upgrade-card').forEach((el) => el.remove());
}

function upgradePromptPreview(prompt) {
  const text = String(prompt || '').trim();
  if (!text) return '';
  const clipped = text.length > 140 ? text.slice(0, 137) + '...' : text;
  return '<div class="upgrade-prompt">Continue this task in your browser: ' + esc(clipped) + '</div>';
}

function showClaudeUpgradeCard() {
  if (_isAuthenticatedUser) return;
  const chat = document.getElementById('chat');
  if (!chat) return;
  removeClaudeUpgradeCard();
  const prompt = currentFirstLookPrompt();
  const card = document.createElement('div');
  card.className = 'upgrade-card';
  card.innerHTML =
    '<h3>Want to run this with your Claude?</h3>' +
    '<p>Switch from the shared demo browser to your own Claude and browser for free. Login only creates your personal connection key.</p>' +
    upgradePromptPreview(prompt) +
    '<div class="upgrade-list">' +
      '<div class="upgrade-item"><strong>Your current Chrome profile</strong><span>Use your existing sessions and logged-in sites.</span></div>' +
      '<div class="upgrade-item"><strong>Clean guest profile</strong><span>Start in a fresh browser if you want an isolated run.</span></div>' +
    '</div>' +
    '<div class="upgrade-actions">' +
      '<div class="upgrade-why">Why free? You bring Claude, your browser, and your data. Unchained only provides the lightweight extraction layer.</div>' +
      '<a class="upgrade-cta" href="/local" onclick="return handoffFirstLookToClaude();">Connect Claude Free</a>' +
    '</div>';
  chat.appendChild(card);
  scrollToBottom();
}

function hideHints() {
  const hints = document.getElementById('chat-hints');
  if (hints) hints.remove();
}

function addUserBubble(text) {
  hideHints();
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble user';
  div.textContent = text;
  chat.appendChild(div);
  scrollToBottom();
}

function addAsstBubble() {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble asst';
  div.innerHTML = '<button class="copy-btn" onclick="copyBubbleText(this)">Copy</button><div class="thinking">thinking...</div>';
  chat.appendChild(div);
  scrollToBottom();
  return div;
}

const TOOL_META = {
  navigate:   {emoji:'🌐', label:'Navigate'},
  ddm:        {emoji:'👁', label:'Look'},
  click:      {emoji:'👆', label:'Click'},
  type:       {emoji:'⌨️', label:'Type'},
  intel:      {emoji:'🔬', label:'Analyze'},
  screenshot: {emoji:'📷', label:'Screenshot'},
  js:         {emoji:'⚡', label:'Script'},
  websearch:  {emoji:'🔍', label:'Search'},
  webfetch:   {emoji:'📥', label:'Fetch'},
  bash:       {emoji:'⚙️', label:'Command'},
  intervention: {emoji:'🛑', label:'Nudge'},
  intervention_screenshot: {emoji:'📷', label:'Nudge Screenshot'},
};

function toolFriendlyDesc(name, input) {
  if (!input) return '';
  const i = input.trim();
  if (name === 'navigate') {
    try { const u = new URL(i); return u.hostname + (u.pathname !== '/' ? u.pathname.slice(0,30) : ''); }
    catch { return i.slice(0,50); }
  }
  if (name === 'ddm') {
    if (i.includes('--at')) return 'inspect element';
    if (i.includes('--tabs')) return 'list tabs';
    if (i.includes('--new')) { const m=i.match(/https?:\/\/([^/\s]+)/); return m ? 'open '+m[1] : 'new tab'; }
    if (i.includes('--close')) return 'close tab';
    if (i.includes('--text')) { const m=i.match(/--find\s+"?([^"\s]+)/); return m ? 'find "'+m[1]+'"' : 'read text'; }
    if (i.includes('--llm-2pass')) return 'map layout';
    if (i.includes('--js')) return 'run js';
    if (i.includes('--api')) return 'find api';
    if (i.includes('--forms')) return 'find forms';
    return 'scan';
  }
  if (name === 'intel') {
    if (i.includes('--probe')) return 'probe page type';
    if (i.includes('--extract')) { const m=i.match(/--strategy\s+(\S+)/); return m ? 'extract ('+m[1]+')' : 'extract data'; }
    if (i.includes('--stores')) return 'find data stores';
    if (i.includes('--shape')) { const m=i.match(/--shape\s+(\S+)/); return m ? 'map '+m[1] : 'map data'; }
    if (i.includes('--find-paths')) return 'find paths';
    return 'analyze';
  }
  if (name === 'click') { const m=i.match(/(\d+)\s+(\d+)/); return m ? '('+m[1]+','+m[2]+')' : i.slice(0,30); }
  if (name === 'type') return '"' + i.replace(/^["']|["']$/g,'').slice(0,40) + '"';
  if (name === 'js') return i.slice(0,50).replace(/\n/g,' ');
  if (name === 'websearch') return '"' + i.slice(0,50) + '"';
  if (name === 'webfetch') { try { return new URL(i).hostname; } catch { return i.slice(0,40); } }
  return i.slice(0,50);
}

const BROWSER_TOOLS = new Set(['navigate','ddm','click','type','intel','screenshot','js','intervention','intervention_screenshot']);
let _currentGroup = null;
let _currentGroupSteps = 0;
let _currentGroupDot = null;
let _turnCount = 0;
let _navTrail = [];

function renderNavTrail() {
  const el = document.getElementById('nav-trail');
  if (el) el.innerHTML = _navTrail.map(s => '<span>' + esc(s) + '</span>').join('');
}

function _finalizeGroup() {
  if (!_currentGroup) return;
  if (_currentGroupDot) {
    _currentGroupDot.className = 'ag-dot done';
    _currentGroupDot.textContent = '\u2713';
  }
  const countEl = _currentGroup.querySelector('.ag-count');
  if (countEl && _currentGroupSteps > 0) {
    countEl.textContent = _currentGroupSteps + (_currentGroupSteps !== 1 ? ' steps' : ' step');
  }
  _currentGroup = null;
  _currentGroupDot = null;
  _currentGroupSteps = 0;
}

function addToolCall(bubble, name, input) {
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  const meta = TOOL_META[name] || {emoji:'\u2699\uFE0F', label:name};
  const desc = toolFriendlyDesc(name, input);

  if (name === 'navigate' && desc) {
    _navTrail.push(desc);
    if (_navTrail.length > 4) _navTrail.shift();
    renderNavTrail();
  }

  _turnCount++;
  const actionEl = document.getElementById('agent-action');
  if (actionEl) actionEl.textContent = meta.label + (desc ? '  ' + desc : '');
  const turnEl = document.getElementById('turn-ctr');
  if (turnEl) turnEl.textContent = 't' + _turnCount;
  document.getElementById('agent-bar').classList.add('active');

  if (BROWSER_TOOLS.has(name)) {
    let hostname = '';
    if (name === 'navigate') {
      try { hostname = new URL(input.trim()).hostname; } catch { hostname = input.trim().slice(0,30); }
    } else {
      hostname = _currentGroup ? _currentGroup.dataset.site : 'browser';
    }
    if (name === 'navigate' && (!_currentGroup || _currentGroup.dataset.site !== hostname)) {
      _finalizeGroup();
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = hostname;
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">\uD83C\uDF10</span>' +
          '<span class="ag-site">' + esc(hostname) + '</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">\u25CF</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    } else if (!_currentGroup) {
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = 'browser';
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">\uD83C\uDF10</span>' +
          '<span class="ag-site">browser</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">\u25CF</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    }
    _currentGroupSteps++;
    const steps = _currentGroup.querySelector('.ag-steps');
    const step = document.createElement('div');
    step.className = 'action-step';
    step.innerHTML =
      '<span class="as-emoji">' + meta.emoji + '</span>' +
      '<span class="as-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="as-desc">' + esc(desc) + '</span>' : '') +
      '<span class="as-dot running">\u25CF</span>';
    steps.appendChild(step);
    scrollToBottom();
    return step;
  } else {
    const sa = document.createElement('div');
    sa.className = 'action-standalone';
    sa.innerHTML =
      '<span class="standalone-emoji">' + meta.emoji + '</span>' +
      '<span class="standalone-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="standalone-desc">' + esc(desc) + '</span>' : '') +
      '<span class="standalone-dot running">\u25CF</span>';
    bubble.appendChild(sa);
    scrollToBottom();
    return sa;
  }
}

function parseIntelBars(text) {
  const m = text.match(/strategy:\s*(\S+)\s*\((\d+)%\)(?:.*?runner-up:\s*(\S+)\s*\((\d+)%\))?/);
  if (!m) return null;
  const bars = [{label: m[1], pct: parseInt(m[2])}];
  if (m[3]) bars.push({label: m[3], pct: parseInt(m[4])});
  return bars;
}

function setToolResult(el, result, isScreenshot, visible) {
  const isStep = el.classList.contains('action-step');
  const dotCls = isStep ? 'as-dot' : 'standalone-dot';
  const dot = el.querySelector('.' + dotCls);
  if (dot) { dot.className = dotCls + ' done'; dot.textContent = '\u2713'; }

  if (isScreenshot && visible) {
    const imgDiv = document.createElement('div');
    imgDiv.className = 'as-screenshot';
    imgDiv.innerHTML = '<img src="data:image/png;base64,' + result + '">';
    el.after(imgDiv);
  } else if (!isScreenshot) {
    const bars = parseIntelBars(result);
    if (bars) {
      const barsDiv = document.createElement('div');
      barsDiv.className = 'intel-bars';
      barsDiv.innerHTML = bars.map(b =>
        '<div class="intel-bar">' +
          '<span class="intel-bar-label">' + esc(b.label) + '</span>' +
          '<span class="intel-bar-track"><span class="intel-bar-fill" style="width:' + b.pct + '%"></span></span>' +
          '<span class="intel-bar-pct">' + b.pct + '%</span>' +
        '</div>'
      ).join('');
      el.after(barsDiv);
    } else {
      const titleM = result.match(/Title:\s*(.+)/);
      if (titleM && _currentGroup) {
        const siteEl = _currentGroup.querySelector('.ag-site');
        if (siteEl) {
          const title = titleM[1].trim().slice(0, 60);
          siteEl.title = title;
          const host = _currentGroup.dataset.site;
          siteEl.textContent = host + (title ? '  \u2014  ' + title : '');
        }
      }
    }
  }
  scrollToBottom();
}

// --- Markdown + KaTeX rendering ---
let _markedConfigured = false;
function ensureMarkedConfigured() {
  if (_markedConfigured || typeof marked === 'undefined') return;
  _markedConfigured = true;
  const blockMath = {
    name: 'blockMath',
    level: 'block',
    start(src) { return src.indexOf('$$'); },
    tokenizer(src) {
      const m = src.match(/^\$\$([\s\S]+?)\$\$/);
      if (m) return { type: 'blockMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<pre>' + esc(token.raw) + '</pre>';
      try { return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>'; }
      catch(e) { return '<pre>' + esc(token.raw) + '</pre>'; }
    }
  };
  const inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start(src) { return src.indexOf('$'); },
    tokenizer(src) {
      const m = src.match(/^\$([^\$\n]+?)\$/);
      if (m) return { type: 'inlineMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<code>' + esc(token.raw) + '</code>';
      const isDisplay = /\\begin\{/.test(token.text);
      try {
        if (isDisplay) return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>';
        return katex.renderToString(token.text, {displayMode:false,throwOnError:false});
      }
      catch(e) { return '<code>' + esc(token.raw) + '</code>'; }
    }
  };
  marked.use({ extensions: [blockMath, inlineMath] });
}

function appendText(bubble, text) {
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  if (!bubble._rawText) bubble._rawText = '';
  bubble._rawText += text;

  let span = bubble.querySelector('.text');
  if (!span) {
    span = document.createElement('div');
    span.className = 'text';
    bubble.appendChild(span);
  }

  ensureMarkedConfigured();
  if (typeof marked !== 'undefined') {
    try {
      span.innerHTML = marked.parse(bubble._rawText);
      span.classList.add('rendered');
    } catch(e) {
      span.textContent = bubble._rawText;
      span.classList.remove('rendered');
    }
  } else {
    span.textContent = bubble._rawText;
  }
  scrollToBottom();
}

function copyBubbleText(btn) {
  const bubble = btn.closest('.bubble.asst');
  if (!bubble) return;
  const text = bubble._rawText || (bubble.querySelector('.text') ? bubble.querySelector('.text').textContent : '');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

async function doCancel() {
  try {
    await fetch('/web/chat/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId}),
    });
  } catch(e) {}
  if (_cancelCtrl) _cancelCtrl.abort();
}

async function doSend() {
  if (sending) return;
  const input = document.getElementById('msginput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';

  sending = true;
  document.getElementById('sendbtn').style.display = 'none';
  document.getElementById('cancelbtn').style.display = 'block';

  // Show model notice after first message
  const mn = document.getElementById('model-notice');
  if (mn) mn.style.display = 'block';

  addUserBubble(msg);
  rememberFirstLookPrompt(msg);
  const bubble = addAsstBubble();
  resetLivePreview();

  let currentTool = null;
  let currentToolName = '';
  _cancelCtrl = new AbortController();

  try {
    const r = await fetch('/web/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg,
        agent_id: agentId,
        session_id: sessionId,
        model: currentModel(),
        headless: true,
        first_look_guest: true,
      }),
      signal: _cancelCtrl.signal,
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({error: 'Request failed'}));
      const thinking = bubble.querySelector('.thinking');
      if (thinking) thinking.remove();
      if (r.status === 429 && err.error === 'demo_quota_exceeded') {
        bubble.parentNode.removeChild(bubble);
        showQuotaModal();
        return;
      }
      if (r.status === 429 && err.error === 'turn_rate_limit') {
        bubble.parentNode.removeChild(bubble);
        // Remove the pending user bubble too
        const userBubbles = document.querySelectorAll('.bubble.user');
        const lastUser = userBubbles[userBubbles.length - 1];
        if (lastUser) lastUser.remove();
        let statusMsg;
        if (err.daily_remaining === 0) {
          statusMsg = 'Daily limit reached. Resets tomorrow.';
        } else {
          const s = Math.ceil(err.resets_in || 0);
          const m = Math.floor(s / 60);
          const rs = s % 60;
          statusMsg = 'Rate limit reached. Try again in ' + (m > 0 ? m + 'm ' : '') + rs + 's.';
        }
        const notice = document.createElement('div');
        notice.className = 'bubble asst';
        notice.style.cssText = 'border-color:#5a4a22;background:#2d2515;color:#dcc58a;font-size:13px';
        notice.textContent = statusMsg;
        document.getElementById('chat').appendChild(notice);
        scrollToBottom();
        return;
      }
      appendText(bubble, 'Error: ' + (err.error || r.statusText));
      if (r.status === 401 || r.status === 403) doDisconnect();
      return;
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});

      let nl;
      while ((nl = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, nl);
        buf = buf.slice(nl + 2);

        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6);
          let evt;
          try { evt = JSON.parse(raw); } catch { continue; }

          if (evt.type === 'tool_start') {
            currentTool = addToolCall(bubble, evt.name, evt.input);
            currentToolName = evt.name || '';
            if (currentToolName === 'navigate') {
              setLiveStatus('Loading page...');
            }
          } else if (evt.type === 'tool_result') {
            if (currentTool) {
              setToolResult(currentTool, evt.data, evt.is_screenshot, evt.visible);
              if (currentToolName === 'navigate' && !_livePreviewHasFrame) {
                setLiveStatus('Page loaded. Capturing preview...');
              }
              if (evt.is_screenshot && evt.visible) {
                updateLivePreview(evt.data, 'Screenshot captured');
              }
              currentTool = null;
              currentToolName = '';
            }
          } else if (evt.type === 'live_preview') {
            updateLivePreview(evt.data, evt.note || 'Page loaded');
          } else if (evt.type === 'text') {
            appendText(bubble, evt.data);
          } else if (evt.type === 'model_forced') {
            if (evt.model) {
              _forcedFirstLookModel = evt.model;
            }
          } else if (evt.type === 'cancelled') {
            appendText(bubble, '[Cancelled by user]');
          } else if (evt.type === 'error') {
            appendText(bubble, 'Error: ' + evt.data);
          } else if (evt.type === 'done') {
            _finalizeGroup();
            document.getElementById('agent-bar').classList.remove('active');
            _turnCount = 0;
            _navTrail = [];
            renderNavTrail();
          }
        }
      }
    }
  } catch(e) {
    const thinking = bubble.querySelector('.thinking');
    if (thinking) thinking.remove();
    if (e.name === 'AbortError') {
      appendText(bubble, '[Cancelled by user]');
    } else if (!bubble.querySelector('.text') && !bubble.querySelector('.action-group') && !bubble.querySelector('.action-standalone')) {
      appendText(bubble, 'Connection error: ' + e.message);
    }
  } finally {
    _cancelCtrl = null;
    sending = false;
    _finalizeGroup();
    document.getElementById('sendbtn').style.display = 'block';
    document.getElementById('cancelbtn').style.display = 'none';
    document.getElementById('agent-bar').classList.remove('active');
    _turnCount = 0;
    _navTrail = [];
    renderNavTrail();
  }
}
</script>
</body>
</html>"""


CLAUDE_CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Unchained Chat</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  color-scheme:dark;
  --bg:#0a0c0f;
  --surface:#11161d;
  --surface-elev:#171d26;
  --surface-soft:#1e2530;
  --accent:#ff6b4a;
  --accent-strong:#ff8768;
  --accent-soft:rgba(255,107,74,0.16);
  --text:#edf2f7;
  --muted:#9da7b7;
  --line:#2a3341;
  --line-strong:#3a4555;
  --user-bg:#2f1f1b;
  --asst-bg:#171f29;
  --success:#5cd48a;
  --mono:'IBM Plex Mono','SF Mono','Menlo','Consolas',monospace;
  --sans:'Space Grotesk','Avenir Next','Segoe UI',sans-serif;
  --shadow:0 22px 50px rgba(0,0,0,0.34);
}
body{
  font-family:var(--sans);
  background:
    radial-gradient(1200px 420px at 12% -8%, rgba(255,107,74,0.15), transparent 58%),
    radial-gradient(900px 340px at 95% 4%, rgba(64,136,124,0.14), transparent 62%),
    linear-gradient(180deg,#121720 0%,#0d1118 44%,#0a0c0f 100%);
  color:var(--text);
  height:100dvh;
  overflow:hidden;
  letter-spacing:0.01em;
}

/* === Login === */
#login{
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  height:100dvh;
  padding:24px;
  gap:16px;
}
#login h1{
  font-size:34px;
  color:var(--text);
  margin-bottom:6px;
  letter-spacing:0.06em;
  text-transform:uppercase;
  font-weight:700;
}
#login .sub{color:var(--muted);font-size:13px;margin-bottom:8px}
#login .login-badge{
  margin:8px 0 4px;
  padding:6px 16px;
  border-radius:999px;
  background:rgba(92,212,138,0.12);
  border:1px solid rgba(92,212,138,0.35);
  color:#9bf0bc;
  font-size:13px;
  letter-spacing:0.03em;
}
#login input{
  width:100%;max-width:360px;height:48px;padding:0 16px;
  border:1px solid var(--line);
  border-radius:12px;
  background:var(--surface);
  color:var(--text);
  font-size:16px;
}
#login input:focus{outline:none;border-color:var(--accent)}
#login button{
  width:100%;
  max-width:360px;
  height:48px;
  border:none;
  border-radius:12px;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong));
  color:#fff;
  font-size:15px;
  font-weight:600;
  cursor:pointer;
  transition:transform 0.14s ease,filter 0.14s ease;
}
#login button:hover{filter:brightness(1.05)}
#login button:active{transform:translateY(1px)}
#login #dev-login-btn{
  display:none;
  width:320px;
  max-width:320px;
  height:44px;
  margin-top:10px;
}
#login .trial-link{color:var(--muted);font-size:12px;margin-top:4px;text-decoration:none}
#login .trial-link:hover{color:#c9d1df}
#loginerr{color:#ff6b6b;font-size:14px;min-height:20px}

/* === Pending === */
#pending{
  display:none;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  height:100dvh;
  padding:24px;
  gap:16px;
  text-align:center;
}
#pending h1{
  font-size:30px;
  color:var(--text);
  letter-spacing:0.04em;
}
#pending p{color:var(--muted);font-size:15px;max-width:430px}
#pending .actions{
  margin-top:8px;
  display:flex;
  flex-direction:column;
  gap:8px;
}
#pending button{
  padding:10px 24px;
  border:1px solid var(--line-strong);
  border-radius:12px;
  background:rgba(255,255,255,0.02);
  color:var(--text);
  font-size:14px;
  cursor:pointer;
  transition:border-color 0.15s ease,background 0.15s ease;
}
#pending button.primary{
  border-color:rgba(255,107,74,0.45);
  color:var(--accent-strong);
}
#pending button:hover{border-color:var(--accent);background:var(--accent-soft)}
#pendingmsg{color:var(--muted);font-size:13px;min-height:20px}

/* === Main === */
#main{
  display:none;
  flex-direction:column;
  height:100dvh;
  max-width:1160px;
  margin:0 auto;
  border-left:1px solid rgba(255,255,255,0.05);
  border-right:1px solid rgba(255,255,255,0.05);
  background:linear-gradient(180deg,rgba(17,22,29,0.9) 0%,rgba(11,14,19,0.93) 100%);
  box-shadow:var(--shadow);
}

#topbar{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:14px;
  padding:12px clamp(12px,2vw,24px);
  padding-top:max(12px,env(safe-area-inset-top));
  background:rgba(17,22,29,0.88);
  border-bottom:1px solid var(--line);
  backdrop-filter:blur(8px);
  flex-shrink:0;
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .agent{
  font-family:var(--mono);
  font-size:12px;
  color:var(--accent-strong);
  letter-spacing:0.04em;
  background:var(--accent-soft);
  border:1px solid rgba(255,107,74,0.4);
  padding:4px 9px;
  border-radius:999px;
}
#topbar .status-stack{display:flex;flex-direction:column;gap:2px}
#topbar .status{font-size:11px;color:var(--muted)}
#topbar .status.online{color:var(--success)}
#topbar .nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
#topbar .nav a{
  color:var(--muted);
  text-decoration:none;
  font-size:11px;
  font-family:var(--mono);
  border:1px solid var(--line);
  background:rgba(255,255,255,0.02);
  padding:6px 10px;
  border-radius:999px;
  transition:border-color 0.15s,color 0.15s,background 0.15s;
}
#topbar .nav a:hover{border-color:var(--accent);color:#ffd2c8;background:var(--accent-soft)}
#topbar .client-update-wrap{display:flex;flex-direction:column;gap:3px;align-items:flex-start}
#topbar .client-update{
  border:1px solid var(--line);
  background:rgba(255,255,255,0.03);
  color:var(--muted);
  border-radius:999px;
  padding:6px 10px;
  font-size:11px;
  font-family:var(--mono);
  cursor:pointer;
  transition:border-color 0.15s ease,background 0.15s ease,color 0.15s ease,box-shadow 0.15s ease;
}
#topbar .client-update:hover{border-color:var(--accent);color:#ffe0d8;background:var(--accent-soft)}
#topbar .client-update.warn{
  color:#ffd8a8;
  border-color:#b57b1e;
  background:rgba(219,152,32,0.16);
  box-shadow:0 0 0 1px rgba(219,152,32,0.12) inset;
  animation:clientUpdatePulse 1.8s ease-in-out infinite;
}
#topbar .client-update.busy{
  color:#ffd9d0;
  border-color:rgba(255,107,74,0.55);
  background:rgba(255,107,74,0.16);
  animation:none;
}
#topbar .client-update:disabled{opacity:0.5;cursor:default;box-shadow:none}
#topbar .client-update-note{
  min-height:12px;
  font-size:10px;
  line-height:1.1;
  color:var(--muted);
  font-family:var(--mono);
}
@keyframes clientUpdatePulse{0%,100%{box-shadow:0 0 0 1px rgba(219,152,32,0.12) inset}50%{box-shadow:0 0 0 1px rgba(219,152,32,0.26) inset,0 0 14px rgba(219,152,32,0.18)}}

/* === Chat === */
#chat{
  flex:1;
  overflow-y:auto;
  padding:18px clamp(12px,2vw,24px);
  -webkit-overflow-scrolling:touch;
  display:flex;
  flex-direction:column;
  gap:12px;
  scroll-behavior:smooth;
}
#chat::-webkit-scrollbar{width:9px}
#chat::-webkit-scrollbar-thumb{background:#2b3341;border-radius:8px}
#chat::-webkit-scrollbar-track{background:transparent}
.bubble{
  max-width:min(86%,860px);
  padding:11px 14px;
  border-radius:14px;
  font-size:14px;
  line-height:1.55;
  word-break:break-word;
  border:1px solid var(--line);
  animation:slideIn 0.2s ease-out;
}
.bubble.user{
  align-self:flex-end;
  background:linear-gradient(145deg,var(--user-bg),rgba(255,107,74,0.14));
  border-color:rgba(255,107,74,0.28);
  border-bottom-right-radius:6px;
}
.bubble.asst{
  align-self:flex-start;
  background:linear-gradient(180deg,var(--asst-bg) 0%,rgba(23,31,41,0.94) 100%);
  border-color:#303b4a;
  border-bottom-left-radius:6px;
}
.bubble.asst .text{white-space:pre-wrap}
.bubble.asst .text.rendered{white-space:normal}
.bubble.asst .text.rendered p{margin:0.5em 0}
.bubble.asst .text.rendered p:first-child{margin-top:0}
.bubble.asst .text.rendered p:last-child{margin-bottom:0}
.bubble.asst .text.rendered h1{font-size:1.3em;margin:0.8em 0 0.4em;font-weight:700;color:#ffd1c7}
.bubble.asst .text.rendered h2{font-size:1.15em;margin:0.7em 0 0.35em;font-weight:600;color:#ffd1c7}
.bubble.asst .text.rendered h3{font-size:1.05em;margin:0.6em 0 0.3em;font-weight:600}
.bubble.asst .text.rendered h4{font-size:1em;margin:0.5em 0 0.25em;font-weight:600}
.bubble.asst .text.rendered code{
  background:rgba(255,255,255,0.08);
  padding:2px 5px;
  border-radius:5px;
  font-family:var(--mono);font-size:0.88em;
}
.bubble.asst .text.rendered pre{
  background:#0e131a;
  border:1px solid #2e3846;
  border-radius:10px;
  padding:12px;
  margin:8px 0;
  overflow-x:auto;white-space:pre-wrap;word-break:break-word;
}
.bubble.asst .text.rendered pre code{background:none;padding:0;font-size:12px;line-height:1.5}
.bubble.asst .text.rendered a{color:var(--accent-strong);text-decoration:underline}
.bubble.asst .text.rendered ul,.bubble.asst .text.rendered ol{margin:0.4em 0;padding-left:1.5em}
.bubble.asst .text.rendered li{margin:0.2em 0}
.bubble.asst .text.rendered blockquote{
  border-left:3px solid var(--accent);
  padding-left:12px;
  margin:0.5em 0;
  color:#bcc5d3;
}
.bubble.asst .text.rendered hr{border:none;border-top:1px solid var(--line);margin:1em 0}
.bubble.asst .text.rendered table{border-collapse:collapse;margin:0.5em 0;width:100%;font-size:13px}
.bubble.asst .text.rendered th,.bubble.asst .text.rendered td{border:1px solid var(--line);padding:6px 10px}
.bubble.asst .text.rendered th{background:rgba(255,255,255,0.04);font-weight:600}
.bubble.asst .text.rendered strong{font-weight:600}
.bubble.asst .text.rendered em{font-style:italic}
.bubble.asst .text .math-display{margin:0.8em 0;text-align:center;overflow-x:auto}
.bubble.asst .text .katex{color:inherit;font-size:1.05em}
.bubble.asst .text .katex-display{margin:0.5em 0;overflow-x:auto;padding:4px 0}
.bubble.asst .thinking{color:var(--muted);font-style:italic;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
/* === Action groups (browser activity cards) === */
@keyframes slideIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.action-group{
  margin:6px 0;
  border:1px solid var(--line);
  border-radius:10px;
  overflow:hidden;
  font-size:12px;
  font-family:var(--mono);
  animation:slideIn 0.2s ease-out;
  background:rgba(8,12,17,0.6);
}
.ag-header{
  display:flex;
  align-items:center;
  gap:8px;
  padding:7px 12px;
  background:rgba(255,255,255,0.03);
  border-bottom:1px solid var(--line);
}
.ag-emoji{font-size:14px;flex-shrink:0}
.ag-site{
  color:#ffd0c6;
  font-weight:500;
  flex:1;
  min-width:0;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
}
.ag-count{color:var(--muted);font-size:10px;flex-shrink:0}
.ag-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.ag-dot.running{animation:pulse 1.2s ease-in-out infinite}
.ag-dot.done{color:var(--success)}
.ag-steps{padding:2px 0 4px}
.action-step{display:flex;align-items:center;gap:7px;padding:3px 12px 3px 28px}
.as-emoji{font-size:11px;flex-shrink:0;width:16px;text-align:center}
.as-label{color:var(--text);font-size:11px;flex-shrink:0}
.as-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.as-dot{margin-left:auto;flex-shrink:0;font-size:10px;color:var(--muted)}
.as-dot.running{animation:pulse 1.2s ease-in-out infinite}
.as-dot.done{color:var(--success)}
.as-screenshot{padding:4px 12px 6px 28px}
.as-screenshot img{max-width:100%;border-radius:6px;border:1px solid var(--line)}
.intel-bars{padding:4px 12px 6px 28px;display:flex;flex-direction:column;gap:4px}
.intel-bar{display:flex;align-items:center;gap:8px;font-size:10.5px;font-family:var(--mono)}
.intel-bar-label{width:96px;color:var(--muted);flex-shrink:0;overflow:hidden;text-overflow:ellipsis}
.intel-bar-track{flex:1;height:3px;background:#2b3443;border-radius:2px;overflow:hidden}
.intel-bar-fill{height:100%;border-radius:2px;background:var(--accent)}
.intel-bar-pct{color:var(--text);width:32px;text-align:right;flex-shrink:0}
.action-standalone{
  display:flex;
  align-items:center;
  gap:8px;
  padding:5px 12px;
  margin:4px 0;
  border:1px solid var(--line);
  border-radius:8px;
  font-size:12px;
  font-family:var(--mono);
  animation:slideIn 0.2s ease-out;
  background:rgba(8,12,17,0.6);
}
.standalone-emoji{font-size:13px;flex-shrink:0}
.standalone-label{color:#ffd0c6;font-weight:500;flex-shrink:0}
.standalone-desc{color:var(--muted);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.standalone-dot{flex-shrink:0;font-size:10px;color:var(--muted)}
.standalone-dot.running{animation:pulse 1.2s ease-in-out infinite}
.standalone-dot.done{color:var(--success)}
#agent-bar{
  display:none;
  padding:5px clamp(12px,2vw,24px);
  background:rgba(14,19,25,0.78);
  border-bottom:1px solid var(--line);
  font-size:11px;
  font-family:var(--mono);
  color:var(--muted);
  align-items:center;
  gap:10px;
  min-height:24px;
}
#agent-bar.active{display:flex}
#agent-action{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#nav-trail{display:flex;align-items:center;gap:0;overflow:hidden;font-size:10px;opacity:0.5;flex-shrink:0;max-width:45%}
#nav-trail span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100px}
#nav-trail span+span::before{content:' › ';opacity:0.6}
#turn-ctr{flex-shrink:0;opacity:0.4;font-size:10px}
.bubble.asst{position:relative}
.bubble.asst .copy-btn{
  position:absolute;top:6px;right:6px;
  background:rgba(255,255,255,0.08);
  border:1px solid rgba(255,255,255,0.06);
  color:var(--muted);
  padding:3px 8px;
  border-radius:6px;
  font-size:11px;
  cursor:pointer;
  opacity:0;
  transition:opacity 0.15s,background 0.15s,color 0.15s;
  font-family:var(--mono);
}
.bubble.asst:hover .copy-btn{opacity:1}
.bubble.asst .copy-btn:hover{background:var(--accent-soft);color:#ffd7ce}
.bubble.asst .copy-btn.copied{opacity:1;color:var(--accent-strong)}

/* === Slot bar === */
#slotbar{
  display:flex;
  gap:8px;
  padding:8px clamp(12px,2vw,24px);
  background:rgba(14,19,25,0.82);
  border-bottom:1px solid var(--line);
  flex-shrink:0;
}
#slotbar button{
  flex:1;
  height:33px;
  border:1px solid var(--line);
  border-radius:999px;
  background:rgba(255,255,255,0.02);
  color:var(--muted);
  font-size:11px;
  font-family:var(--mono);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:border-color 0.15s,color 0.15s,background 0.15s;
}
#slotbar button:hover{border-color:var(--accent);color:#ffe0d8;background:var(--accent-soft)}
#slotbar button:active{transform:scale(0.95)}
#slotbar button.active{border-color:var(--accent);color:#ffd5cb;background:var(--accent-soft);font-weight:600}
#slotbar button.empty{color:#636f82;font-style:italic}
#slotbar button.empty.active{color:#ffd5cb;font-style:normal}
#slotbar.locked button{pointer-events:none;opacity:0.4}
#slotbar.locked button.active{opacity:0.7}

/* === Model selector === */
#modelrow{
  display:flex;
  align-items:center;
  gap:8px;
  padding:8px clamp(12px,2vw,24px) 0;
  flex-shrink:0;
  flex-wrap:wrap;
}
#modelrow label{
  font-size:10px;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:0.08em;
  font-family:var(--mono);
}
#modelsel{
  height:32px;
  padding:0 10px;
  border:1px solid var(--line);
  border-radius:10px;
  background:var(--surface-elev);
  color:var(--text);
  font-size:12px;
  font-family:var(--mono);cursor:pointer;
}
#modelsel:focus{outline:none;border-color:var(--accent)}
#profilesel{
  height:32px;
  padding:0 10px;
  border:1px solid var(--line);
  border-radius:10px;
  background:var(--surface-elev);
  color:var(--text);
  font-size:12px;
  font-family:var(--mono);
  cursor:pointer;
  min-width:220px;
}
#profilesel:focus{outline:none;border-color:var(--accent)}
#model-notice{
  display:none;
  margin:0 clamp(12px,2vw,24px);
  padding:8px 10px;
  border-radius:10px;
  border:1px solid #6d5931;
  background:#2d2619;
  color:#dcc58a;
  font-size:12px;
  line-height:1.4;
  flex-shrink:0;
}
#model-notice strong{color:#f0d58b}

/* === Input === */
#inputbar{
  display:flex;
  gap:8px;
  padding:10px clamp(12px,2vw,24px);
  padding-bottom:max(16px,env(safe-area-inset-bottom));
  background:rgba(15,20,27,0.9);
  border-top:1px solid var(--line);
  flex-shrink:0;
}
#msginput{
  flex:1;min-width:0;
  min-height:44px;
  max-height:120px;
  padding:10px 12px;
  border:1px solid var(--line);
  border-radius:14px;
  background:var(--surface-elev);
  color:var(--text);
  font-size:15px;
  font-family:var(--sans);
  resize:none;
  line-height:1.4;
}
#msginput::placeholder{color:#718097}
#msginput:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(255,107,74,0.15)}
#sendbtn{
  width:44px;
  height:44px;
  border:none;
  border-radius:14px;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong));
  color:#fff;
  font-size:16px;
  cursor:pointer;
  flex-shrink:0;
  align-self:flex-end;
  transition:transform 0.14s ease,filter 0.14s ease;
}
#sendbtn:hover{filter:brightness(1.06)}
#sendbtn:active{transform:translateY(1px)}
#sendbtn:disabled{opacity:0.4;cursor:default}
#cancelbtn{
  width:44px;
  height:44px;
  border:none;
  border-radius:14px;
  background:#ef5c5c;
  color:#fff;
  font-size:16px;
  cursor:pointer;
  flex-shrink:0;
  align-self:flex-end;
  display:none;
}
#cancelbtn:hover{filter:brightness(1.06)}
#cancelbtn:active{transform:translateY(1px)}

/* === Download banner === */
#download-banner{
  display:flex;
  align-items:center;
  justify-content:center;
  gap:12px;
  padding:9px clamp(12px,2vw,24px);
  background:rgba(255,107,74,0.1);
  border-bottom:1px solid rgba(255,107,74,0.3);
  font-size:13px;
  color:var(--muted);
  flex-shrink:0;
}
#download-banner .copy{
  display:flex;
  flex-direction:column;
  min-width:0;
  gap:2px;
}
#download-banner .detail{font-size:12px;color:#b5bfce}
#download-banner #banner-msg{color:#f6d5cd}
#download-banner #banner-detail{color:#b8a7a3}
#download-banner a{
  color:#ffd8cf;
  text-decoration:none;
  font-weight:500;
  border:1px solid rgba(255,107,74,0.5);
  background:rgba(255,107,74,0.2);
  padding:5px 12px;
  border-radius:999px;
  font-size:12px;
  white-space:nowrap;
  transition:background 0.15s ease,border-color 0.15s ease;
}
#download-banner a:hover{background:rgba(255,107,74,0.28);border-color:var(--accent-strong)}

/* === Chat hints === */
#chat-hints{
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:flex-start;
  height:100%;
  padding-top:min(8vh,68px);
  text-align:center;
}
.hint-title{
  font-size:26px;
  font-weight:600;
  color:var(--text);
  margin-bottom:8px;
  letter-spacing:0.01em;
}
.hint-sub{color:var(--muted);font-size:14px;margin-bottom:18px}
.hint-examples{display:flex;flex-direction:column;gap:9px;width:100%;max-width:620px}
.hint-item{
  padding:11px 14px;
  border:1px solid var(--line);
  border-radius:12px;
  font-size:13px;
  color:var(--text);
  cursor:pointer;
  text-align:left;
  background:rgba(255,255,255,0.02);
  transition:border-color 0.15s,background 0.15s,transform 0.15s;
}
.hint-item:hover{
  border-color:var(--accent);
  background:var(--accent-soft);
  transform:translateY(-1px);
}
#chat-hints .hint-item:nth-child(1){animation:slideIn 0.24s ease-out}
#chat-hints .hint-item:nth-child(2){animation:slideIn 0.3s ease-out}
#chat-hints .hint-item:nth-child(3){animation:slideIn 0.36s ease-out}

/* === Install modal === */
#install-modal{
  position:fixed;
  inset:0;
  z-index:100;
  background:rgba(0,0,0,0.7);
  display:none;
  align-items:center;
  justify-content:center;
}
#install-modal .modal-card{
  background:var(--surface-elev);
  border:1px solid var(--line-strong);
  border-radius:14px;
  padding:24px;
  max-width:520px;
  width:90%;
  position:relative;
}
#install-modal .modal-close{
  position:absolute;
  top:12px;
  right:12px;
  background:none;
  border:none;
  color:var(--muted);
  font-size:18px;
  cursor:pointer;
}
#install-modal .modal-title{color:#ffd5cc;margin-bottom:8px;font-size:16px}
#install-modal .modal-desc{color:var(--muted);font-size:13px;margin-bottom:12px}
#install-modal .modal-code{
  background:#10151d;
  border:1px solid var(--line);
  border-radius:10px;
  padding:12px;
  font-family:var(--mono);
  font-size:12px;
  word-break:break-all;
  position:relative;
}
#install-modal .modal-code code{color:var(--text)}
#install-modal .modal-actions{margin-top:10px;display:flex;gap:8px}
#install-modal .modal-copy{
  background:#2b313d;
  border:1px solid #3b4353;
  color:#fff;
  padding:8px 12px;
  border-radius:10px;
  font-size:12px;
  cursor:pointer;
}
#install-modal .modal-note{color:var(--muted);font-size:11px;margin-top:12px}

@media (max-width: 900px){
  #main{max-width:none;border:none;box-shadow:none}
  #topbar{align-items:stretch}
  #topbar .nav{justify-content:flex-start}
  #profilesel{min-width:0;flex:1}
  .bubble{max-width:92%;font-size:13px}
}
@media (max-width: 640px){
  body{
    background:
      radial-gradient(560px 240px at 12% -6%, rgba(255,107,74,0.14), transparent 62%),
      linear-gradient(180deg,#121820 0%,#0a0c0f 58%);
  }
  #topbar{
    flex-direction:column;
    align-items:flex-start;
    gap:10px;
    padding:10px;
    padding-top:max(10px,env(safe-area-inset-top));
  }
  #topbar .nav{
    width:100%;
    overflow:auto;
    flex-wrap:nowrap;
    padding-bottom:2px;
  }
  #slotbar{padding:8px 10px;gap:6px}
  #modelrow{padding:8px 10px 0}
  #chat{padding:12px 10px}
  #inputbar{padding:10px}
  #msginput{font-size:14px}
  #download-banner{flex-wrap:wrap;justify-content:flex-start;gap:8px}
  #download-banner .copy{width:100%}
}

/* Archive panel */
#archive-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;justify-content:center;align-items:center}
#archive-overlay.open{display:flex}
#archive-panel{background:var(--surface,#11161d);border:1px solid var(--line,#2a3341);border-radius:12px;width:90%;max-width:480px;max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
#archive-panel .archive-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line,#2a3341)}
#archive-panel .archive-header h3{font-size:16px;font-weight:600;color:var(--text,#edf2f7)}
#archive-panel .archive-close{background:none;border:none;color:var(--muted,#9da7b7);font-size:20px;cursor:pointer;padding:4px 8px}
#archive-panel .archive-close:hover{color:var(--text,#edf2f7)}
#archive-list{overflow-y:auto;flex:1;padding:8px}
.archive-item{display:flex;align-items:flex-start;gap:12px;padding:12px;border-radius:8px;cursor:default}
.archive-item:hover{background:var(--surface-elev,#171d26)}
.archive-item .archive-info{flex:1;min-width:0}
.archive-item .archive-preview{font-size:13px;color:var(--text,#edf2f7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.archive-item .archive-meta{font-size:11px;color:var(--muted,#9da7b7);margin-top:4px}
.archive-item .archive-actions{display:flex;gap:6px;flex-shrink:0}
.archive-item .archive-actions button{background:none;border:1px solid var(--line,#2a3341);color:var(--muted,#9da7b7);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
.archive-item .archive-actions button:hover{color:var(--text,#edf2f7);border-color:var(--muted,#9da7b7)}
.archive-item .archive-actions .restore-btn:hover{border-color:var(--accent,#ff6b4a);color:var(--accent,#ff6b4a)}
.archive-item .archive-actions .delete-btn:hover{border-color:#e53e3e;color:#e53e3e}
.archive-empty{text-align:center;color:var(--muted,#9da7b7);padding:40px 20px;font-size:14px}
</style>
</head>
<body>

<!-- Login -->
<div id="login">
  <h1>Unchained</h1>
  <div class="sub">Full Claude-powered browser agent</div>
  <div class="login-badge">Signing up as: <strong>Claude</strong></div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="outline"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
  <button id="dev-login-btn" onclick="devLogin()">Dev Login</button>
  <a href="/trial" class="trial-link">Just want to try it free? Use the trial &rarr;</a>
</div>

<!-- Pending -->
<div id="pending">
  <h1>Request Being Reviewed</h1>
  <p>Your sign-up request is being reviewed. We'll send you an email once your account is approved.</p>
  <div class="actions">
    <button class="primary" onclick="checkApproval()">Check Again</button>
    <button onclick="backToLogin()">Use a Different Account</button>
  </div>
  <div id="pendingmsg"></div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <div class="left">
      <span class="agent" id="agentlabel"></span>
      <div class="status-stack">
        <span class="status" id="agentstatus">agent offline</span>
        <span class="status" id="bridgestatus">bridge offline</span>
      </div>
      <div class="client-update-wrap">
        <button id="client-update-btn" class="client-update" type="button" onclick="triggerClientUpdate()">Client current</button>
        <span id="client-update-note" class="client-update-note"></span>
      </div>
    </div>
    <div class="nav">
      <a href="/trial">Free Trial</a>
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a href="#" onclick="openArchives();return false">Archives</a>
      <a href="/test" id="control-link" style="display:none">Control</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doDisconnect();return false">Logout</a>
    </div>
  </div>

  <div id="archive-overlay" onclick="if(event.target===this)closeArchives()">
    <div id="archive-panel">
      <div class="archive-header">
        <h3>Archived Chats</h3>
        <button class="archive-close" onclick="closeArchives()">&times;</button>
      </div>
      <div id="archive-list"></div>
    </div>
  </div>

  <div id="slotbar">
    <button onclick="switchSlot(1)" id="slot1" title="Independent conversation session">Chat A</button>
    <button onclick="switchSlot(2)" id="slot2" title="Independent conversation session">Chat B</button>
    <button onclick="switchSlot(3)" id="slot3" title="Independent conversation session">Chat C</button>
  </div>

  <div id="agent-bar">
    <span id="agent-action"></span>
    <span id="nav-trail"></span>
    <span id="turn-ctr"></span>
  </div>

  <div id="download-banner" style="display:none">
    <div class="copy">
      <span id="banner-msg">Your local chat agent is offline.</span>
      <span class="detail" id="banner-detail">Browser bridge and chat agent are tracked separately.</span>
    </div>
    <a href="#" onclick="showBannerInstall();return false" id="banner-curl">Install (curl)</a>
    <a href="/install" id="banner-connect">Download Agent Installer</a>
  </div>

  <!-- Install modal -->
  <div id="install-modal">
    <div class="modal-card">
      <button class="modal-close" onclick="closeInstallModal()">&times;</button>
      <h3 class="modal-title" id="install-modal-title">Install Agent (curl)</h3>
      <p class="modal-desc" id="install-modal-desc">Run this command in your terminal:</p>
      <div class="modal-code">
        <code id="install-cmd">Loading command...</code>
      </div>
      <div class="modal-actions">
        <button class="modal-copy" onclick="copyInstallCmd()" id="copy-btn">Copy Command</button>
      </div>
      <p class="modal-note" id="install-modal-note">Links expire in 15 minutes. Requires Python 3.8+ and curl.</p>
    </div>
  </div>

  <div id="chat">
      <div id="chat-hints">
        <div class="hint-title">Unchained Chat</div>
      <div class="hint-sub">Your AI agent controls a real browser on your machine.</div>
      <div class="hint-examples">
        <div class="hint-item" onclick="fillMsg('Research the top 3 noise-cancelling headphones on rtings.com and compare their ratings')">Compare top headphones on rtings.com</div>
        <div class="hint-item" onclick="fillMsg('Find flights from SFO to Tokyo next month on Google Flights and summarize the cheapest options')">Find cheap SFO to Tokyo flights</div>
        <div class="hint-item" onclick="fillMsg('Go to GitHub trending and give me a summary of the top repositories this week')">Summarize GitHub trending repos</div>
      </div>
    </div>
  </div>

  <div id="modelrow">
    <label for="modelsel">Model</label>
    <select id="modelsel" onchange="onModelChange(this.value)">
      <option value="claude-sonnet-4-6">Sonnet 4.6</option>
      <option value="claude-opus-4-6">Opus 4.6</option>
      <option value="claude-haiku-4-5-20251001">Haiku 4.5</option>
    </select>
    <label for="profilesel">Profile</label>
    <select id="profilesel" onchange="onProfileChange(this.value)" title="Optional profile copy to avoid re-sign-in prompts">
      <option value="">Current browser (no profile copy)</option>
    </select>
  </div>
  <div id="inputbar">
    <textarea id="msginput" rows="1" placeholder="Ask the agent anything..."
              onkeydown="handleKey(event)" oninput="autoGrow(this)"></textarea>
    <button id="sendbtn" onclick="doSend()">&#9654;</button>
    <button id="cancelbtn" onclick="doCancel()">&#9632;</button>
  </div>
</div>
<script>

let agentId = '';
let sessionId = '';
let sending = false;
let _cancelCtrl = null;
let _isAdmin = false;
let _userName = '';
let _userPicture = '';
let selectedProfilePath = '';
const hasGoogleOAuth = !!'__GOOGLE_CLIENT_ID__';
const isLocalDevHost = ['localhost', '127.0.0.1'].includes(window.location.hostname);

function _nextAfterLogin() {
  const raw = (new URLSearchParams(window.location.search).get('next') || '').trim();
  if (!raw) return '';
  if (!raw.startsWith('/')) return '';
  if (raw.startsWith('//')) return '';
  if (raw.includes('://')) return '';
  return raw;
}

function _redirectAfterLoginIfNeeded() {
  const next = _nextAfterLogin();
  if (!next) return false;
  if (next === window.location.pathname) return false;
  window.location.href = next;
  return true;
}

function maybeShowDevLogin() {
  if (hasGoogleOAuth || !isLocalDevHost) return;
  const btn = document.getElementById('dev-login-btn');
  if (btn) btn.style.display = 'block';
}

async function handleGoogleCredential(response) {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential, source: 'claude'}),
    });
    const data = await r.json();
    if (data.pending) { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    agentId = data.agent_id;
    _isAdmin = !!data.is_admin;
    if (_redirectAfterLoginIfNeeded()) return;
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function devLogin() {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/dev', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: 'dev@localhost', name: 'Dev User'}),
    });
    const data = await r.json();
    if (!r.ok) { errEl.textContent = data.error || 'Dev login failed'; return; }
    agentId = data.agent_id || '';
    if (_redirectAfterLoginIfNeeded()) return;
    showMain();
  } catch(e) {
    errEl.textContent = e.message;
  }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) {
      agentId = data.agent_id; _isAdmin = !!data.is_admin; _userName = data.name || ''; _userPicture = data.picture || '';
      if (_redirectAfterLoginIfNeeded()) return;
      showMain(); return;
    }
    if (data.pending) { showPending(); return; }
  } catch(e) {}
  document.getElementById('login').style.display = 'flex';
}

async function checkApproval() {
  const msg = document.getElementById('pendingmsg');
  msg.textContent = 'Checking...';
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) {
      agentId = data.agent_id; _isAdmin = !!data.is_admin; _userName = data.name || ''; _userPicture = data.picture || '';
      if (_redirectAfterLoginIfNeeded()) return;
      showMain(); return;
    }
    if (data.pending) { msg.textContent = 'Still under review. Check back soon!'; return; }
    msg.textContent = 'Still under review.';
  } catch(e) { msg.textContent = 'Could not check status.'; }
}

async function doDisconnect() {
  await fetch('/auth/logout', {method: 'POST'});
  agentId = '';
  sessionId = '';
  document.getElementById('login').style.display = 'flex';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
}

function showPending() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'flex';
}

async function backToLogin() {
  await fetch('/auth/logout', {method: 'POST'});
  document.getElementById('pending').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

function currentModel() {
  return document.getElementById('modelsel').value;
}

let activeSlot = 1;

function _sessionStoreKey() {
  return 'unchained_session_' + agentId + '_claude';
}

function _restoreSessionId() {
  const sid = localStorage.getItem(_sessionStoreKey()) || '';
  const prefix = 's-' + agentId + '-';
  return (sid.startsWith(prefix) && sid.length > prefix.length) ? sid : '';
}

function _persistSessionId(sid) {
  if (sid && sid.startsWith('s-' + agentId)) {
    localStorage.setItem(_sessionStoreKey(), sid);
  }
}

function _slotLabel(n) {
  return (['Chat A', 'Chat B', 'Chat C'][n - 1] || ('Chat ' + n));
}

function _slotStateKey() {
  return _sessionStoreKey() + '_slots_v1';
}

function _newSessionId() {
  return 's-' + agentId + '-' + Date.now().toString(36);
}

function _loadSlotState() {
  let state = {active_slot: 1, slots: {}};
  try {
    const raw = localStorage.getItem(_slotStateKey()) || '';
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') state = parsed;
    }
  } catch(e) {}
  const slots = (state && typeof state.slots === 'object' && state.slots) ? state.slots : {};
  const normalized = {};
  for (let i = 1; i <= 3; i++) {
    const sid = String(slots[String(i)] || '').trim();
    normalized[String(i)] = (sid.startsWith('s-' + agentId + '-') ? sid : '');
  }
  let active = Number(state && state.active_slot);
  if (active !== 1 && active !== 2 && active !== 3) active = 1;
  return {active_slot: active, slots: normalized};
}

function _saveSlotState(state) {
  try { localStorage.setItem(_slotStateKey(), JSON.stringify(state)); } catch(e) {}
}

function _ensureSlotState() {
  const state = _loadSlotState();
  const restored = _restoreSessionId();
  if (!state.slots['1']) state.slots['1'] = restored || _newSessionId();
  for (let i = 1; i <= 3; i++) {
    if (!state.slots[String(i)]) state.slots[String(i)] = _newSessionId();
  }
  if (!state.slots[String(state.active_slot)]) state.active_slot = 1;
  _saveSlotState(state);
  return state;
}

function _setActiveSlotSession(sid) {
  if (!sid || !sid.startsWith('s-' + agentId + '-')) return;
  const state = _loadSlotState();
  state.slots[String(activeSlot)] = sid;
  state.active_slot = activeSlot;
  _saveSlotState(state);
}

function _syncSlotButtons() {
  const state = _loadSlotState();
  activeSlot = state.active_slot;
  for (let i = 1; i <= 3; i++) {
    const btn = document.getElementById('slot' + i);
    if (!btn) continue;
    btn.className = '';
    btn.textContent = _slotLabel(i);
    if (i === activeSlot) btn.classList.add('active');
  }
}

function _highlightSlotButtons(slot) {
  for (let i = 1; i <= 3; i++) {
    const btn = document.getElementById('slot' + i);
    if (!btn) continue;
    btn.classList.toggle('active', i === slot);
  }
}

function onModelChange(model) {
  localStorage.setItem('unchained_model', model);
  // Refresh model-scoped status immediately when switching lanes.
  checkAgentStatus();
}

function _profileStoreKey() {
  return 'unchained_chat_profile_' + agentId + '_' + window.location.pathname;
}

function onProfileChange(profilePath) {
  selectedProfilePath = String(profilePath || '');
  try { localStorage.setItem(_profileStoreKey(), selectedProfilePath); } catch(e) {}
}

function currentProfilePath() {
  return selectedProfilePath || '';
}

async function loadChatProfiles() {
  const sel = document.getElementById('profilesel');
  if (!sel) return;

  let remembered = '';
  try { remembered = localStorage.getItem(_profileStoreKey()) || ''; } catch(e) {}
  selectedProfilePath = remembered;
  sel.innerHTML = '';

  const defaultOpt = document.createElement('option');
  defaultOpt.value = '';
  defaultOpt.textContent = 'Current browser (no profile copy)';
  sel.appendChild(defaultOpt);

  let gotProfiles = false;
  try {
    const r = await fetch('/web/provision/profiles');
    if (r.ok) {
      const data = await r.json();
      for (const p of (data.profiles || [])) {
        const path = String(p.profile_path || p.path || '').trim();
        if (!path) continue;
        gotProfiles = true;
        const label = String(p.name || p.dir_name || 'Profile').trim();
        const email = String(p.email || '').trim();
        const opt = document.createElement('option');
        opt.value = path;
        opt.textContent = email ? (label + ' (' + email + ')') : label;
        sel.appendChild(opt);
      }
    }
  } catch(e) {}

  if ([...sel.options].some(opt => opt.value === remembered)) {
    sel.value = remembered;
  } else if (!gotProfiles && remembered) {
    const opt = document.createElement('option');
    opt.value = remembered;
    opt.textContent = 'Saved profile (bridge offline)';
    sel.appendChild(opt);
    sel.value = remembered;
  } else {
    sel.value = '';
  }
  selectedProfilePath = sel.value || '';
}

let lastAgentConnected = false;
let lastCodexCliSupported = true;
let clientUpdateInFlight = false;
let clientUpdateSawDisconnect = false;
let clientUpdateError = '';
const CLIENT_UPDATE_TIMEOUT_MS = 90000;
let clientUpdateStartedAt = 0;
let lastClientStatus = null;

function updateStatusPill(el, text, mode) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status' + (mode ? ' ' + mode : '');
}

function updateClientUpdateUI(data) {
  const btn = document.getElementById('client-update-btn');
  const note = document.getElementById('client-update-note');
  if (!btn || !note) return;
  const clientConnected = !!data.client_connected;
  const updateSupported = !!data.client_update_supported;
  const outdated = !!data.client_outdated;
  const required = !!data.client_update_required;
  const localVersion = String(data.client_version || '').trim();
  const serverVersion = String(data.server_version || '').trim();
  btn.className = 'client-update';
  if (clientUpdateInFlight) {
    btn.classList.add('busy');
    btn.disabled = true;
    btn.textContent = 'Updating...';
    note.textContent = 'Client will reconnect after restart.';
    return;
  }
  if (outdated) btn.classList.add('warn');
  btn.disabled = !clientConnected || !updateSupported || !outdated;
  if (!clientConnected) {
    btn.textContent = 'Client offline';
    note.textContent = localVersion ? ('Last seen v' + localVersion) : '';
  } else if (!updateSupported) {
    btn.textContent = 'Manual update';
    note.textContent = localVersion ? ('Client v' + localVersion) : 'Reconnect with the latest package once.';
  } else if (outdated) {
    btn.textContent = required ? 'Update required' : 'Update client';
    note.textContent = (localVersion && serverVersion) ? ('v' + localVersion + ' -> v' + serverVersion) : 'New client available';
  } else {
    btn.textContent = 'Client current';
    note.textContent = localVersion ? ('v' + localVersion) : '';
  }
  if (clientUpdateError) note.textContent = clientUpdateError;
  btn.title = (localVersion && serverVersion) ? ('Local client ' + localVersion + ' / server ' + serverVersion) : 'Update and restart the local client';
}

async function triggerClientUpdate() {
  if (clientUpdateInFlight) return;
  clientUpdateError = '';
  clientUpdateInFlight = true;
  clientUpdateSawDisconnect = false;
  clientUpdateStartedAt = Date.now();
  updateClientUpdateUI(lastClientStatus || {});
  try {
    const r = await fetch('/web/chat/update-client', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    checkAgentStatus();
  } catch(e) {
    clientUpdateInFlight = false;
    clientUpdateSawDisconnect = false;
    clientUpdateStartedAt = 0;
    clientUpdateError = e.message || 'Update failed';
    updateClientUpdateUI(lastClientStatus || {});
  }
}

function updateAgentStatusUI(data) {
  const chatEl = document.getElementById('agentstatus');
  const bridgeEl = document.getElementById('bridgestatus');
  const banner = document.getElementById('download-banner');
  const bannerMsg = document.getElementById('banner-msg');
  const bannerDetail = document.getElementById('banner-detail');
  const bannerConnect = document.getElementById('banner-connect');
  const bannerCurl = document.getElementById('banner-curl');
  const model = currentModel();
  const isCodexCli = model.startsWith('codex-cli:');
  const chatConnected = !!data.chat_connected;
  const bridgeConnected = !!data.bridge_connected;
  const mismatch = !!data.mismatch;
  const codexCliSupported = data.codex_cli_supported !== false;
  if (clientUpdateInFlight) {
    if (!data.client_connected) clientUpdateSawDisconnect = true;
    else if (clientUpdateSawDisconnect || !data.client_outdated) {
      clientUpdateInFlight = false;
      clientUpdateSawDisconnect = false;
      clientUpdateStartedAt = 0;
      clientUpdateError = '';
    } else if (clientUpdateStartedAt && (Date.now() - clientUpdateStartedAt) >= CLIENT_UPDATE_TIMEOUT_MS) {
      clientUpdateInFlight = false;
      clientUpdateSawDisconnect = false;
      clientUpdateStartedAt = 0;
      clientUpdateError = 'Update timed out. Check the local client logs and retry.';
    }
  }
  if (!clientUpdateInFlight && !data.client_outdated) clientUpdateError = '';
  lastClientStatus = data;
  updateClientUpdateUI(data);
  if (bannerMsg) bannerMsg.textContent = 'Your local chat agent is offline.';
  if (bannerDetail) bannerDetail.textContent = 'Browser bridge and chat agent are tracked separately.';
  if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
  if (bannerCurl) bannerCurl.textContent = (typeof _isWindows==='function'&&_isWindows()) ? 'Install (PowerShell)' : 'Install (curl)';
  if (isCodexCli && bannerMsg) bannerMsg.textContent = 'Codex CLI lane requires the local chat agent and a Codex CLI login.';
  if (isCodexCli && !codexCliSupported && bannerMsg) {
    bannerMsg.textContent = 'Codex CLI requires an updated local chat agent package.';
  }

  if (bridgeConnected) {
    updateStatusPill(bridgeEl, 'bridge online', 'online');
  } else {
    updateStatusPill(bridgeEl, 'bridge offline', '');
  }

  if (chatConnected) {
    if (isCodexCli) updateStatusPill(chatEl, 'codex cli online', 'online');
    else updateStatusPill(chatEl, 'agent online', 'online');
    if (bridgeConnected) {
      if (banner) banner.style.display = 'none';
    } else {
      if (bannerMsg) bannerMsg.textContent = 'Your browser bridge is offline.';
      if (bannerDetail) bannerDetail.textContent = 'Chat is connected, but browser actions and setup profile detection on this machine need the bridge.';
      if (banner) banner.style.display = 'flex';
    }
  } else if (mismatch) {
    updateStatusPill(chatEl, 'agent mismatch', 'warn');
    if (bannerMsg) bannerMsg.textContent = 'A different local chat agent is connected for this account.';
    if (bannerDetail) bannerDetail.textContent = 'Your browser bridge may still be online. Reinstall only if this machine should own the active chat agent.';
    if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
    if (bannerCurl) bannerCurl.textContent = (typeof _isWindows==='function'&&_isWindows()) ? 'Reinstall (PowerShell)' : 'Reinstall (curl)';
    if (banner) banner.style.display = 'flex';
  } else {
    if (isCodexCli && !codexCliSupported) updateStatusPill(chatEl, 'codex cli needs update', 'warn');
    else if (isCodexCli) updateStatusPill(chatEl, 'codex cli offline', '');
    else updateStatusPill(chatEl, 'agent offline', '');
    if (bridgeConnected) {
      if (bannerDetail) bannerDetail.textContent = 'Browser bridge is online, so setup and browser control can still work on this machine.';
    } else if (bannerDetail) {
      bannerDetail.textContent = 'Start the full local agent package to bring both browser bridge and agent online.';
    }
    if (banner) banner.style.display = 'flex';
  }
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  document.getElementById('agentlabel').textContent = _userName || 'Unchained';
  if (_isAdmin) { const cl = document.getElementById('control-link'); if (cl) cl.style.display = ''; }
  try { localStorage.setItem('unchained_last_route', window.location.pathname); } catch(e){}
  const saved = localStorage.getItem('unchained_model');
  if (saved && document.querySelector('#modelsel option[value="' + CSS.escape(saved) + '"]')) {
    document.getElementById('modelsel').value = saved;
  }
  const slotState = _ensureSlotState();
  activeSlot = slotState.active_slot;
  sessionId = slotState.slots[String(activeSlot)] || _restoreSessionId() || _newSessionId();
  _persistSessionId(sessionId);
  _setActiveSlotSession(sessionId);
  _syncSlotButtons();
  loadChatProfiles();
  checkAgentStatus();
  setInterval(checkAgentStatus, 10000);
  loadHistory();
}

async function checkAgentStatus() {
  try {
    const r = await fetch('/web/chat/status?chat_only=1&model=' + encodeURIComponent(currentModel()));
    if (r.ok) {
      const data = await r.json();
      lastAgentConnected = data.connected;
      lastCodexCliSupported = data.codex_cli_supported !== false;
      updateAgentStatusUI(data);
    }
  } catch(e) {}
}

async function loadSlots() {
  try {
    const qs = new URLSearchParams({
      model: currentModel(),
      session_id: sessionId,
    });
    const r = await fetch('/web/chat/slots?' + qs.toString());
    if (!r.ok) return;
    const data = await r.json();
    if (data.offline) return;
    if (data.session_id) {
      sessionId = data.session_id;
      _persistSessionId(sessionId);
      _setActiveSlotSession(sessionId);
    }
    activeSlot = data.active_slot || 1;
    const state = _loadSlotState();
    state.active_slot = activeSlot;
    if (data.session_id && sessionId && sessionId.startsWith('s-' + agentId + '-')) {
      state.slots[String(activeSlot)] = sessionId;
    }
    _saveSlotState(state);
    for (const s of (data.slots || [])) {
      const btn = document.getElementById('slot' + s.slot);
      if (!btn) continue;
      btn.className = '';
      if (s.slot === activeSlot) btn.classList.add('active');
      if (s.empty) {
        btn.classList.add('empty');
        btn.textContent = (['Chat A', 'Chat B', 'Chat C'][s.slot - 1] || ('Chat ' + s.slot));
      } else {
        btn.textContent = s.preview || (['Chat A', 'Chat B', 'Chat C'][s.slot - 1] || ('Chat ' + s.slot));
      }
    }
  } catch(e) {}
}

async function switchSlot(n) {
  if (n === activeSlot) return;
  if (sending) return;
  const targetSlot = (n === 1 || n === 2 || n === 3) ? n : 1;
  const previousState = _loadSlotState();
  const previousActiveSlot = activeSlot;
  const previousSessionId = sessionId;
  const targetSessionId = previousState.slots[String(targetSlot)] || _newSessionId();
  activeSlot = targetSlot;
  sessionId = targetSessionId;
  _highlightSlotButtons(targetSlot);
  document.getElementById('chat').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>';
  let switched = false;
  try {
    const r = await fetch('/web/chat/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        slot: targetSlot,
        model: currentModel(),
        session_id: previousSessionId,
      }),
    });
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      const confirmedSlot = (data.active_slot === 1 || data.active_slot === 2 || data.active_slot === 3)
        ? data.active_slot
        : targetSlot;
      const nextState = _loadSlotState();
      nextState.active_slot = confirmedSlot;
      if (!nextState.slots[String(targetSlot)]) nextState.slots[String(targetSlot)] = targetSessionId;
      if (!nextState.slots[String(confirmedSlot)]) {
        nextState.slots[String(confirmedSlot)] =
          (confirmedSlot === targetSlot) ? targetSessionId : _newSessionId();
      }
      _saveSlotState(nextState);
      activeSlot = confirmedSlot;
      sessionId = nextState.slots[String(activeSlot)];
      _persistSessionId(sessionId);
      _syncSlotButtons();
      switched = true;
    }
  } catch(e) {}
  if (!switched) {
    _saveSlotState(previousState);
    activeSlot = previousActiveSlot;
    sessionId = previousSessionId;
    _persistSessionId(sessionId);
    _syncSlotButtons();
  }
  document.getElementById('chat').innerHTML = '';
  await loadHistory();
}

async function loadHistory() {
  await loadSlots();
  try {
    const qs = new URLSearchParams({
      model: currentModel(),
      session_id: sessionId,
    });
    const r = await fetch('/web/chat/history?' + qs.toString());
    if (!r.ok) return;
    const data = await r.json();
    if (data.session_id) {
      sessionId = data.session_id;
      _persistSessionId(sessionId);
      _setActiveSlotSession(sessionId);
    }
    if (!data.messages || data.messages.length === 0) {
      showHintsIfEmpty();
      return;
    }
    hideHints();
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        addUserBubble(msg.content);
      } else if (msg.role === 'assistant') {
        const bubble = addAsstBubble();
        const thinking = bubble.querySelector('.thinking');
        if (thinking) thinking.remove();
        appendText(bubble, msg.content);
      }
    }
  } catch(e) {}
}

function showHintsIfEmpty() {
  if (document.getElementById('chat-hints')) return;
  document.getElementById('chat').innerHTML =
    '<div id="chat-hints">' +
    '<div class="hint-title">Unchained Chat</div>' +
    '<div class="hint-sub">Your AI agent controls a real browser on your machine.</div>' +
    '<div class="hint-examples">' +
    '<div class="hint-item" onclick="fillMsg(\'Research the top 3 noise-cancelling headphones on rtings.com and compare their ratings\')">Compare top headphones on rtings.com</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Find flights from SFO to Tokyo next month on Google Flights and summarize the cheapest options\')">Find cheap SFO to Tokyo flights</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Go to GitHub trending and give me a summary of the top repositories this week\')">Summarize GitHub trending repos</div>' +
    '</div></div>';
}

function removeClaudeUpgradeCard() {
  document.querySelectorAll('.upgrade-card').forEach(function(el) { el.remove(); });
}
function showClaudeUpgradeCard() {}

async function doNewChat() {
  if (sending) return;
  removeClaudeUpgradeCard();
  document.getElementById('chat').innerHTML = '';
  showHintsIfEmpty();
  try {
    const r = await fetch('/web/chat/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        model: currentModel(),
        session_id: sessionId,
      }),
    });
    if (r.ok) {
      const data = await r.json();
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
        _setActiveSlotSession(sessionId);
      }
    }
  } catch(e) {}
  await loadSlots();
}

async function openArchives() {
  const overlay = document.getElementById('archive-overlay');
  overlay.classList.add('open');
  const list = document.getElementById('archive-list');
  list.innerHTML = '<div class="archive-empty">Loading...</div>';
  try {
    const r = await fetch('/web/chat/archives?model=' + encodeURIComponent(currentModel()));
    if (!r.ok) { list.innerHTML = '<div class="archive-empty">Failed to load archives</div>'; return; }
    const data = await r.json();
    const archives = data.archives || [];
    if (archives.length === 0) {
      list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
      return;
    }
    list.innerHTML = '';
    for (const arc of archives) {
      const d = new Date(arc.archived_at * 1000);
      const dateStr = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      const div = document.createElement('div');
      div.className = 'archive-item';
      div.innerHTML =
        '<div class="archive-info">' +
          '<div class="archive-preview">' + esc(arc.preview || '(empty)') + '</div>' +
          '<div class="archive-meta">' + arc.message_count + ' messages &middot; ' + dateStr + '</div>' +
        '</div>' +
        '<div class="archive-actions">' +
          '<button class="restore-btn" data-id="' + esc(arc.id) + '">Restore</button>' +
          '<button class="delete-btn" data-id="' + esc(arc.id) + '">Delete</button>' +
        '</div>';
      div.querySelector('.restore-btn').onclick = () => restoreArchive(arc.id);
      div.querySelector('.delete-btn').onclick = () => deleteArchive(arc.id, div);
      list.appendChild(div);
    }
  } catch(e) {
    list.innerHTML = '<div class="archive-empty">Error loading archives</div>';
  }
}

function closeArchives() {
  document.getElementById('archive-overlay').classList.remove('open');
}

async function restoreArchive(id) {
  if (!confirm('Restore this archived conversation? It will replace your current chat.')) return;
  try {
    const r = await fetch('/web/chat/restore-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
        _setActiveSlotSession(sessionId);
      }
      closeArchives();
      location.reload();
    } else {
      const data = await r.json().catch(() => ({}));
      alert(data.error || 'Failed to restore archive');
    }
  } catch(e) { alert('Failed to restore archive'); }
}

async function deleteArchive(id, el) {
  if (!confirm('Delete this archived chat permanently?')) return;
  try {
    const r = await fetch('/web/chat/delete-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (r.ok) {
      el.remove();
      const list = document.getElementById('archive-list');
      if (!list.querySelector('.archive-item')) {
        list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
      }
    }
  } catch(e) {}
}

maybeShowDevLogin();
checkSession();
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    doSend();
  }
}

function scrollToBottom() {
  const chat = document.getElementById('chat');
  chat.scrollTop = chat.scrollHeight;
}

function fillMsg(text) {
  const input = document.getElementById('msginput');
  input.value = text;
  input.focus();
  autoGrow(input);
}

function hideHints() {
  const hints = document.getElementById('chat-hints');
  if (hints) hints.remove();
}

function addUserBubble(text) {
  hideHints();
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble user';
  div.textContent = text;
  chat.appendChild(div);
  scrollToBottom();
}

function addAsstBubble() {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'bubble asst';
  div.innerHTML = '<button class="copy-btn" onclick="copyBubbleText(this)">Copy</button><div class="thinking">thinking...</div>';
  chat.appendChild(div);
  scrollToBottom();
  return div;
}

const TOOL_META = {
  navigate:   {emoji:'🌐', label:'Navigate'},
  ddm:        {emoji:'👁', label:'Look'},
  click:      {emoji:'👆', label:'Click'},
  type:       {emoji:'⌨️', label:'Type'},
  intel:      {emoji:'🔬', label:'Analyze'},
  screenshot: {emoji:'📷', label:'Screenshot'},
  js:         {emoji:'⚡', label:'Script'},
  websearch:  {emoji:'🔍', label:'Search'},
  webfetch:   {emoji:'📥', label:'Fetch'},
  bash:       {emoji:'⚙️', label:'Command'},
  intervention: {emoji:'🛑', label:'Nudge'},
  intervention_screenshot: {emoji:'📷', label:'Nudge Screenshot'},
};

function toolFriendlyDesc(name, input) {
  if (!input) return '';
  const i = input.trim();
  if (name === 'navigate') {
    try { const u = new URL(i); return u.hostname + (u.pathname !== '/' ? u.pathname.slice(0,30) : ''); }
    catch { return i.slice(0,50); }
  }
  if (name === 'ddm') {
    if (i.includes('--at')) return 'inspect element';
    if (i.includes('--tabs')) return 'list tabs';
    if (i.includes('--new')) { const m=i.match(/https?:\/\/([^/\s]+)/); return m ? 'open '+m[1] : 'new tab'; }
    if (i.includes('--close')) return 'close tab';
    if (i.includes('--text')) { const m=i.match(/--find\s+"?([^"\s]+)/); return m ? 'find "'+m[1]+'"' : 'read text'; }
    if (i.includes('--llm-2pass')) return 'map layout';
    if (i.includes('--js')) return 'run js';
    if (i.includes('--api')) return 'find api';
    if (i.includes('--forms')) return 'find forms';
    return 'scan';
  }
  if (name === 'intel') {
    if (i.includes('--probe')) return 'probe page type';
    if (i.includes('--extract')) { const m=i.match(/--strategy\s+(\S+)/); return m ? 'extract ('+m[1]+')' : 'extract data'; }
    if (i.includes('--stores')) return 'find data stores';
    if (i.includes('--shape')) { const m=i.match(/--shape\s+(\S+)/); return m ? 'map '+m[1] : 'map data'; }
    if (i.includes('--find-paths')) return 'find paths';
    return 'analyze';
  }
  if (name === 'click') { const m=i.match(/(\d+)\s+(\d+)/); return m ? '('+m[1]+','+m[2]+')' : i.slice(0,30); }
  if (name === 'type') return '"' + i.replace(/^["']|["']$/g,'').slice(0,40) + '"';
  if (name === 'js') return i.slice(0,50).replace(/\n/g,' ');
  if (name === 'websearch') return '"' + i.slice(0,50) + '"';
  if (name === 'webfetch') { try { return new URL(i).hostname; } catch { return i.slice(0,40); } }
  return i.slice(0,50);
}

const BROWSER_TOOLS = new Set(['navigate','ddm','click','type','intel','screenshot','js','intervention','intervention_screenshot']);
let _currentGroup = null;
let _currentGroupSteps = 0;
let _currentGroupDot = null;
let _turnCount = 0;
let _navTrail = [];

function renderNavTrail() {
  const el = document.getElementById('nav-trail');
  if (el) el.innerHTML = _navTrail.map(s => '<span>' + esc(s) + '</span>').join('');
}

function _finalizeGroup() {
  if (!_currentGroup) return;
  if (_currentGroupDot) {
    _currentGroupDot.className = 'ag-dot done';
    _currentGroupDot.textContent = '✓';
  }
  const countEl = _currentGroup.querySelector('.ag-count');
  if (countEl && _currentGroupSteps > 0) {
    countEl.textContent = _currentGroupSteps + (_currentGroupSteps !== 1 ? ' steps' : ' step');
  }
  _currentGroup = null;
  _currentGroupDot = null;
  _currentGroupSteps = 0;
}

function addToolCall(bubble, name, input) {
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  const meta = TOOL_META[name] || {emoji:'⚙️', label:name};
  const desc = toolFriendlyDesc(name, input);

  if (name === 'navigate' && desc) {
    _navTrail.push(desc);
    if (_navTrail.length > 4) _navTrail.shift();
    renderNavTrail();
  }

  _turnCount++;
  const actionEl = document.getElementById('agent-action');
  if (actionEl) actionEl.textContent = meta.label + (desc ? '  ' + desc : '');
  const turnEl = document.getElementById('turn-ctr');
  if (turnEl) turnEl.textContent = 't' + _turnCount;
  document.getElementById('agent-bar').classList.add('active');

  if (BROWSER_TOOLS.has(name)) {
    // Determine site hostname for grouping
    let hostname = '';
    if (name === 'navigate') {
      try { hostname = new URL(input.trim()).hostname; } catch { hostname = input.trim().slice(0,30); }
    } else {
      hostname = _currentGroup ? _currentGroup.dataset.site : 'browser';
    }
    // Navigate to a new site starts a new group
    if (name === 'navigate' && (!_currentGroup || _currentGroup.dataset.site !== hostname)) {
      _finalizeGroup();
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = hostname;
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">🌐</span>' +
          '<span class="ag-site">' + esc(hostname) + '</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">●</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    } else if (!_currentGroup) {
      // No group yet — create a generic browser group
      const group = document.createElement('div');
      group.className = 'action-group';
      group.dataset.site = 'browser';
      group.innerHTML =
        '<div class="ag-header">' +
          '<span class="ag-emoji">🌐</span>' +
          '<span class="ag-site">browser</span>' +
          '<span class="ag-count"></span>' +
          '<span class="ag-dot running">●</span>' +
        '</div>' +
        '<div class="ag-steps"></div>';
      bubble.appendChild(group);
      _currentGroup = group;
      _currentGroupDot = group.querySelector('.ag-dot');
      _currentGroupSteps = 0;
    }
    // Add step row inside current group
    _currentGroupSteps++;
    const steps = _currentGroup.querySelector('.ag-steps');
    const step = document.createElement('div');
    step.className = 'action-step';
    step.innerHTML =
      '<span class="as-emoji">' + meta.emoji + '</span>' +
      '<span class="as-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="as-desc">' + esc(desc) + '</span>' : '') +
      '<span class="as-dot running">●</span>';
    steps.appendChild(step);
    scrollToBottom();
    return step;
  } else {
    // Standalone action (websearch, webfetch, bash)
    const sa = document.createElement('div');
    sa.className = 'action-standalone';
    sa.innerHTML =
      '<span class="standalone-emoji">' + meta.emoji + '</span>' +
      '<span class="standalone-label">' + esc(meta.label) + '</span>' +
      (desc ? '<span class="standalone-desc">' + esc(desc) + '</span>' : '') +
      '<span class="standalone-dot running">●</span>';
    bubble.appendChild(sa);
    scrollToBottom();
    return sa;
  }
}

function parseIntelBars(text) {
  const m = text.match(/strategy:\s*(\S+)\s*\((\d+)%\)(?:.*?runner-up:\s*(\S+)\s*\((\d+)%\))?/);
  if (!m) return null;
  const bars = [{label: m[1], pct: parseInt(m[2])}];
  if (m[3]) bars.push({label: m[3], pct: parseInt(m[4])});
  return bars;
}

function setToolResult(el, result, isScreenshot, visible) {
  const isStep = el.classList.contains('action-step');
  const dotCls = isStep ? 'as-dot' : 'standalone-dot';
  const dot = el.querySelector('.' + dotCls);
  if (dot) { dot.className = dotCls + ' done'; dot.textContent = '✓'; }

  if (isScreenshot && visible) {
    const imgDiv = document.createElement('div');
    imgDiv.className = 'as-screenshot';
    imgDiv.innerHTML = '<img src="data:image/png;base64,' + result + '">';
    el.after(imgDiv);
  } else if (!isScreenshot) {
    const bars = parseIntelBars(result);
    if (bars) {
      const barsDiv = document.createElement('div');
      barsDiv.className = 'intel-bars';
      barsDiv.innerHTML = bars.map(b =>
        '<div class="intel-bar">' +
          '<span class="intel-bar-label">' + esc(b.label) + '</span>' +
          '<span class="intel-bar-track"><span class="intel-bar-fill" style="width:' + b.pct + '%"></span></span>' +
          '<span class="intel-bar-pct">' + b.pct + '%</span>' +
        '</div>'
      ).join('');
      el.after(barsDiv);
    } else {
      // For navigate results, extract and show the page title in the site card header
      const titleM = result.match(/Title:\s*(.+)/);
      if (titleM && _currentGroup) {
        const siteEl = _currentGroup.querySelector('.ag-site');
        if (siteEl) {
          const title = titleM[1].trim().slice(0, 60);
          siteEl.title = title;  // tooltip with full title
          // Show truncated title next to hostname
          const host = _currentGroup.dataset.site;
          siteEl.textContent = host + (title ? '  —  ' + title : '');
        }
      }
    }
  }
  scrollToBottom();
}

// --- Markdown + KaTeX rendering ---
let _markedConfigured = false;
function ensureMarkedConfigured() {
  if (_markedConfigured || typeof marked === 'undefined') return;
  _markedConfigured = true;
  const blockMath = {
    name: 'blockMath',
    level: 'block',
    start(src) { return src.indexOf('$$'); },
    tokenizer(src) {
      const m = src.match(/^\$\$([\s\S]+?)\$\$/);
      if (m) return { type: 'blockMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<pre>' + esc(token.raw) + '</pre>';
      try { return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>'; }
      catch(e) { return '<pre>' + esc(token.raw) + '</pre>'; }
    }
  };
  const inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start(src) { return src.indexOf('$'); },
    tokenizer(src) {
      const m = src.match(/^\$([^\$\n]+?)\$/);
      if (m) return { type: 'inlineMath', raw: m[0], text: m[1].trim() };
    },
    renderer(token) {
      if (typeof katex === 'undefined') return '<code>' + esc(token.raw) + '</code>';
      const isDisplay = /\\begin\{/.test(token.text);
      try {
        if (isDisplay) return '<div class="math-display">' + katex.renderToString(token.text, {displayMode:true,throwOnError:false}) + '</div>';
        return katex.renderToString(token.text, {displayMode:false,throwOnError:false});
      }
      catch(e) { return '<code>' + esc(token.raw) + '</code>'; }
    }
  };
  marked.use({ extensions: [blockMath, inlineMath] });
}

function appendText(bubble, text) {
  // Remove thinking indicator if present
  const thinking = bubble.querySelector('.thinking');
  if (thinking) thinking.remove();

  if (!bubble._rawText) bubble._rawText = '';
  bubble._rawText += text;

  let span = bubble.querySelector('.text');
  if (!span) {
    span = document.createElement('div');
    span.className = 'text';
    bubble.appendChild(span);
  }

  ensureMarkedConfigured();
  if (typeof marked !== 'undefined') {
    try {
      span.innerHTML = marked.parse(bubble._rawText);
      span.classList.add('rendered');
    } catch(e) {
      span.textContent = bubble._rawText;
      span.classList.remove('rendered');
    }
  } else {
    span.textContent = bubble._rawText;
  }
  scrollToBottom();
}

function copyBubbleText(btn) {
  const bubble = btn.closest('.bubble.asst');
  if (!bubble) return;
  const text = bubble._rawText || (bubble.querySelector('.text') ? bubble.querySelector('.text').textContent : '');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

async function doCancel() {
  try {
    await fetch('/web/chat/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId}),
    });
  } catch(e) {}
  if (_cancelCtrl) _cancelCtrl.abort();
}

async function doSend() {
  if (sending) return;
  const input = document.getElementById('msginput');
  const msg = input.value.trim();
  if (!msg) return;
  removeClaudeUpgradeCard();
  input.value = '';
  input.style.height = 'auto';

  sending = true;
  document.getElementById('sendbtn').style.display = 'none';
  document.getElementById('cancelbtn').style.display = 'block';
  const slotbar = document.getElementById('slotbar');
  if (slotbar) slotbar.classList.add('locked');

  addUserBubble(msg);
  const bubble = addAsstBubble();

  let currentTool = null;
  _cancelCtrl = new AbortController();
  const model = currentModel();
  if (model.startsWith('codex-cli:') && !lastCodexCliSupported) {
    appendText(bubble, 'Error: Codex CLI needs an updated local agent. Please run the latest install/update command and restart your local agent.');
    sending = false;
    document.getElementById('sendbtn').style.display = 'block';
    document.getElementById('cancelbtn').style.display = 'none';
    const slotbar = document.getElementById('slotbar');
    if (slotbar) slotbar.classList.remove('locked');
    return;
  }

  try {
    const payload = {
      message: msg,
      agent_id: agentId,
      session_id: sessionId,
      model: model,
      slot: activeSlot,
    };
    const profilePath = currentProfilePath();
    if (profilePath) {
      payload.profile_path = profilePath;
    }
    const r = await fetch('/web/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
      signal: _cancelCtrl.signal,
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({error: 'Request failed'}));
      const thinking = bubble.querySelector('.thinking');
      if (thinking) thinking.remove();
      appendText(bubble, 'Error: ' + (err.error || r.statusText));
      if (r.status === 401 || r.status === 403) doDisconnect();
      return;
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});

      let nl;
      while ((nl = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, nl);
        buf = buf.slice(nl + 2);

        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6);
          let evt;
          try { evt = JSON.parse(raw); } catch { continue; }

          if (evt.type === 'tool_start') {
            currentTool = addToolCall(bubble, evt.name, evt.input);
          } else if (evt.type === 'tool_result') {
            if (currentTool) {
              setToolResult(currentTool, evt.data, evt.is_screenshot, evt.visible);
              currentTool = null;
            }
          } else if (evt.type === 'text') {
            appendText(bubble, evt.data);
          } else if (evt.type === 'cancelled') {
            appendText(bubble, '[Cancelled by user]');
          } else if (evt.type === 'error') {
            appendText(bubble, 'Error: ' + evt.data);
          } else if (evt.type === 'done') {
            _finalizeGroup();
            document.getElementById('agent-bar').classList.remove('active');
            _turnCount = 0;
            _navTrail = [];
            renderNavTrail();
            showClaudeUpgradeCard();
          }
        }
      }
    }
  } catch(e) {
    const thinking = bubble.querySelector('.thinking');
    if (thinking) thinking.remove();
    if (e.name === 'AbortError') {
      appendText(bubble, '[Cancelled by user]');
    } else if (!bubble.querySelector('.text') && !bubble.querySelector('.action-group') && !bubble.querySelector('.action-standalone')) {
      appendText(bubble, 'Connection error: ' + e.message);
    }
  } finally {
    _cancelCtrl = null;
    sending = false;
    _finalizeGroup();
    document.getElementById('sendbtn').style.display = 'block';
    document.getElementById('cancelbtn').style.display = 'none';
    const slotbar2 = document.getElementById('slotbar');
    if (slotbar2) slotbar2.classList.remove('locked');
    document.getElementById('agent-bar').classList.remove('active');
    _turnCount = 0;
    _navTrail = [];
    renderNavTrail();
  }
}

function _isWindows() { return navigator.userAgent.indexOf('Windows') !== -1; }

async function showBannerInstall() {
  await showInstallCmd();
}

function _normalizeLocalUrl(raw) {
  const s = String(raw || '');
  const isLocalHost = location.hostname === 'localhost' || location.hostname === '127.0.0.1';
  if (isLocalHost) return s.replace(/^https:\/\//i, 'http://');
  return s;
}

async function showInstallCmd() {
  const isWin = _isWindows();
  document.getElementById('install-modal-title').textContent = isWin ? 'Install Agent (PowerShell)' : 'Install Agent (curl)';
  document.getElementById('install-modal-desc').textContent = isWin ? 'Run this command in PowerShell:' : 'Run this command in your terminal:';
  document.getElementById('install-modal-note').textContent = isWin ? 'Links expire in 15 minutes. Requires Python 3.8+.' : 'Links expire in 15 minutes. Requires Python 3.8+ and curl.';
  document.getElementById('copy-btn').textContent = 'Copy Command';
  const modal = document.getElementById('install-modal');
  modal.style.display = 'flex';
  document.getElementById('install-cmd').textContent = 'Generating install command...';
  try {
    const r = await fetch('/web/install-token', {method: 'POST'});
    if (!r.ok) { document.getElementById('install-cmd').textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    const command = _normalizeLocalUrl(isWin ? (data.powershell_command || '') : (data.curl_command || ''));
    document.getElementById('install-cmd').textContent = command || 'No install command available.';
  } catch(e) {
    document.getElementById('install-cmd').textContent = 'Error: ' + e.message;
  }
}

function copyInstallCmd() {
  const cmd = document.getElementById('install-cmd').textContent;
  navigator.clipboard.writeText(cmd).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy Command', 2000);
  });
}

function closeInstallModal() {
  document.getElementById('install-modal').style.display = 'none';
}
</script>
</body>
</html>"""

# Shared modern skin for all chat-like routes so /trial, /chat-*, /first-look,
# and /local stay visually aligned.
_MODERN_CHAT_THEME_LINKS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">"""

_MODERN_CHAT_THEME_STYLE = """<style id="modern-chat-theme">
:root{
  color-scheme:dark;
  --bg:#0a0c0f;
  --surface:#11161d;
  --surface-elev:#171d26;
  --accent:#ff6b4a;
  --accent-strong:#ff8768;
  --accent-soft:rgba(255,107,74,0.16);
  --text:#edf2f7;
  --muted:#9da7b7;
  --line:#2a3341;
  --line-strong:#3a4555;
  --user-bg:#2f1f1b;
  --asst-bg:#171f29;
  --success:#5cd48a;
  --mono:'IBM Plex Mono','SF Mono','Menlo','Consolas',monospace;
  --sans:'Space Grotesk','Avenir Next','Segoe UI',sans-serif;
}
body{
  font-family:var(--sans)!important;
  background:
    radial-gradient(1200px 420px at 12% -8%, rgba(255,107,74,0.15), transparent 58%),
    radial-gradient(900px 340px at 95% 4%, rgba(64,136,124,0.14), transparent 62%),
    linear-gradient(180deg,#121720 0%,#0d1118 44%,#0a0c0f 100%)!important;
  color:var(--text)!important;
  letter-spacing:0.01em;
}
#login h1{
  font-size:34px!important;
  color:var(--text)!important;
  letter-spacing:0.06em!important;
  text-transform:uppercase;
  font-weight:700;
}
#login .sub{color:var(--muted)!important}
#login input{
  border:1px solid var(--line)!important;
  border-radius:12px!important;
  background:var(--surface)!important;
  color:var(--text)!important;
}
#login button{
  border-radius:12px!important;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
  color:#fff!important;
}
#login button:active{transform:translateY(1px)}
#login #dev-login-btn{
  border-radius:12px!important;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
}
#login a[href="/trial"]{color:var(--muted)!important}
#login a[href="/trial"]:hover{color:#c9d1df!important}
#login > div[style*="background:#0d2d1a"]{
  border-radius:999px!important;
  background:rgba(92,212,138,0.12)!important;
  border:1px solid rgba(92,212,138,0.35)!important;
  color:#9bf0bc!important;
}
#pending{
  align-items:center!important;
  justify-content:center!important;
  text-align:center!important;
}
#pending h1{color:var(--text)!important;letter-spacing:0.04em!important}
#pending p{color:var(--muted)!important}
#pending button{
  border-radius:12px!important;
  border:1px solid var(--line-strong)!important;
  background:rgba(255,255,255,0.02)!important;
  color:var(--text)!important;
}
#pending button:hover{
  border-color:var(--accent)!important;
  background:var(--accent-soft)!important;
}
#main{
  max-width:1160px;
  margin:0 auto;
  border-left:1px solid rgba(255,255,255,0.05);
  border-right:1px solid rgba(255,255,255,0.05);
  background:linear-gradient(180deg,rgba(17,22,29,0.9) 0%,rgba(11,14,19,0.93) 100%);
  box-shadow:0 22px 50px rgba(0,0,0,0.34);
}
#topbar{
  align-items:flex-start!important;
  gap:14px;
  padding:12px clamp(12px,2vw,24px)!important;
  background:rgba(17,22,29,0.88)!important;
  border-bottom:1px solid var(--line)!important;
  backdrop-filter:blur(8px);
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .status-stack{display:flex;flex-direction:column;gap:2px}
#topbar .agent{
  font-family:var(--mono)!important;
  font-size:12px!important;
  color:var(--accent-strong)!important;
  letter-spacing:0.04em;
  background:var(--accent-soft)!important;
  border:1px solid rgba(255,107,74,0.4)!important;
  padding:4px 9px;
  border-radius:999px;
}
#topbar .status{color:var(--muted)!important}
#topbar .status.online{color:var(--success)!important}
#topbar .status.warn{color:#f0d58b!important}
#topbar .nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
#topbar .nav a{
  font-family:var(--mono)!important;
  font-size:11px!important;
  color:var(--muted)!important;
  border:1px solid var(--line)!important;
  background:rgba(255,255,255,0.02)!important;
  padding:6px 10px!important;
  border-radius:999px!important;
}
#topbar .nav a:hover{
  border-color:var(--accent)!important;
  color:#ffd2c8!important;
  background:var(--accent-soft)!important;
}
#workspace{
  background:transparent!important;
  gap:10px;
  padding:10px clamp(10px,2vw,20px);
}
#chat-pane,#browser-pane{
  background:rgba(17,22,29,0.9)!important;
  border:1px solid var(--line)!important;
  border-radius:14px!important;
}
#chat{
  padding:18px clamp(12px,2vw,24px)!important;
  gap:12px!important;
}
#chat::-webkit-scrollbar{width:9px}
#chat::-webkit-scrollbar-thumb{background:#2b3341;border-radius:8px}
.bubble{
  max-width:min(86%,860px)!important;
  padding:11px 14px!important;
  border-radius:14px!important;
  line-height:1.55!important;
  border:1px solid var(--line)!important;
}
.bubble.user{
  background:linear-gradient(145deg,var(--user-bg),rgba(255,107,74,0.14))!important;
  border-color:rgba(255,107,74,0.28)!important;
  border-bottom-right-radius:6px!important;
}
.bubble.asst{
  background:linear-gradient(180deg,var(--asst-bg) 0%,rgba(23,31,41,0.94) 100%)!important;
  border-color:#303b4a!important;
  border-bottom-left-radius:6px!important;
}
.bubble.asst .copy-btn{
  border-radius:6px!important;
  border:1px solid rgba(255,255,255,0.06)!important;
  background:rgba(255,255,255,0.08)!important;
}
.bubble.asst .copy-btn:hover{background:var(--accent-soft)!important;color:#ffd7ce!important}
.bubble.asst .copy-btn.copied{color:var(--accent-strong)!important}
#agent-bar{
  padding:5px clamp(12px,2vw,24px)!important;
  background:rgba(14,19,25,0.78)!important;
  border-bottom:1px solid var(--line)!important;
}
#slotbar{
  gap:8px!important;
  padding:8px clamp(12px,2vw,24px)!important;
  background:rgba(14,19,25,0.82)!important;
  border-bottom:1px solid var(--line)!important;
}
#slotbar button{
  height:33px!important;
  border:1px solid var(--line)!important;
  border-radius:999px!important;
  background:rgba(255,255,255,0.02)!important;
  font-size:11px!important;
}
#slotbar button:hover{
  border-color:var(--accent)!important;
  color:#ffe0d8!important;
  background:var(--accent-soft)!important;
}
#slotbar button.active{
  border-color:var(--accent)!important;
  color:#ffd5cb!important;
  background:var(--accent-soft)!important;
}
#modelrow{
  padding:8px clamp(12px,2vw,24px) 0!important;
  gap:8px!important;
  flex-wrap:wrap;
}
#modelrow label{
  font-size:10px!important;
  letter-spacing:0.08em!important;
  text-transform:uppercase;
  font-family:var(--mono)!important;
  color:var(--muted)!important;
}
#modelsel,#profilesel{
  height:32px!important;
  border:1px solid var(--line)!important;
  border-radius:10px!important;
  background:var(--surface-elev)!important;
  color:var(--text)!important;
}
#inputbar{
  padding:10px clamp(12px,2vw,24px)!important;
  background:rgba(15,20,27,0.9)!important;
  border-top:1px solid var(--line)!important;
}
#msginput{
  border:1px solid var(--line)!important;
  border-radius:14px!important;
  background:var(--surface-elev)!important;
  color:var(--text)!important;
  font-family:var(--sans)!important;
}
#msginput::placeholder{color:#718097}
#sendbtn{
  border-radius:14px!important;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
}
#cancelbtn{border-radius:14px!important;background:#ef5c5c!important}
#download-banner{
  padding:9px clamp(12px,2vw,24px)!important;
  background:rgba(255,107,74,0.1)!important;
  border-bottom:1px solid rgba(255,107,74,0.3)!important;
}
#download-banner .detail{color:#b5bfce!important}
#download-banner a{
  color:#ffd8cf!important;
  border:1px solid rgba(255,107,74,0.5)!important;
  border-radius:999px!important;
  background:rgba(255,107,74,0.2)!important;
}
#download-banner a:hover{
  background:rgba(255,107,74,0.28)!important;
  border-color:var(--accent-strong)!important;
}
#chat-hints{padding-top:min(8vh,68px)!important}
.hint-title{
  font-size:26px!important;
  color:var(--text)!important;
}
.hint-sub{color:var(--muted)!important;font-size:14px!important}
.hint-examples{max-width:620px!important;gap:9px!important}
.hint-item{
  border:1px solid var(--line)!important;
  border-radius:12px!important;
  background:rgba(255,255,255,0.02)!important;
}
.hint-item:hover{
  border-color:var(--accent)!important;
  background:var(--accent-soft)!important;
}
#install-modal{
  background:rgba(0,0,0,0.7)!important;
}
#install-modal .modal-card,
#install-modal>div{
  background:var(--surface-elev)!important;
  border:1px solid var(--line-strong)!important;
  border-radius:14px!important;
}
#install-modal .modal-title{color:#ffd5cc!important}
#install-modal .modal-code,
#install-modal code[id="install-cmd"]{
  font-family:var(--mono)!important;
}
@media (max-width: 900px){
  #main{max-width:none;border:none;box-shadow:none}
  #profilesel{min-width:0;flex:1}
  .bubble{max-width:92%!important;font-size:13px!important}
}
@media (max-width: 640px){
  #topbar{
    flex-direction:column!important;
    align-items:flex-start!important;
    gap:10px;
    padding:10px!important;
  }
  #topbar .nav{
    width:100%;
    overflow:auto;
    flex-wrap:nowrap;
  }
  #slotbar{padding:8px 10px!important;gap:6px!important}
  #modelrow{padding:8px 10px 0!important}
  #chat{padding:12px 10px!important}
  #inputbar{padding:10px!important}
  #download-banner{flex-wrap:wrap;justify-content:flex-start}
  #download-banner .copy{width:100%}
}
</style>"""


def _apply_modern_chat_theme(html: str) -> str:
    if 'id="modern-chat-theme"' in html:
        return html
    if "</head>" not in html:
        return html
    parts: list[str] = []
    if "fonts.googleapis.com/css2?family=Space+Grotesk" not in html:
        parts.append(_MODERN_CHAT_THEME_LINKS)
    parts.append(_MODERN_CHAT_THEME_STYLE)
    return html.replace("</head>", "\n" + "\n".join(parts) + "\n</head>", 1)


TRIAL_CHAT_HTML = _apply_modern_chat_theme(TRIAL_CHAT_HTML)
CHAT_GEMINI_HTML = _apply_modern_chat_theme(CHAT_GEMINI_HTML)
CHAT_CLAUDE_SDK_HTML = _apply_modern_chat_theme(CHAT_CLAUDE_SDK_HTML)
CHAT_CODEX_HTML = _apply_modern_chat_theme(CHAT_CODEX_HTML)
HEADLESS_DEMO_HTML = _apply_modern_chat_theme(HEADLESS_DEMO_HTML)


# ---------------------------------------------------------------------------
# Sidebar injection — ChatGPT-style left sidebar for chat history
# ---------------------------------------------------------------------------

_SIDEBAR_STYLE = """<style id="sidebar-panel">
#app-shell{display:none;height:100dvh}
#app-shell #main{height:auto;flex:1;min-height:0;min-width:0}
#sidebar{width:260px;flex-shrink:0;background:var(--surface,#11161d);border-right:1px solid var(--line,#2a3341);display:flex;flex-direction:column;overflow:hidden}
.sidebar-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--line,#2a3341)}
.sidebar-title{font-size:15px;font-weight:600;color:var(--text,#edf2f7);letter-spacing:0.5px}
.sidebar-new{background:none;border:1px solid var(--line,#2a3341);color:var(--muted,#9da7b7);border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;white-space:nowrap}
.sidebar-new:hover{border-color:var(--accent,#ff6b4a);color:var(--accent,#ff6b4a)}
#sidebar-history{overflow-y:auto;flex:1;padding:8px}
.sidebar-item{display:flex;flex-direction:column;padding:10px 12px;border-radius:8px;cursor:pointer;border-left:3px solid transparent;margin-bottom:2px}
.sidebar-item:hover{background:var(--surface-elev,#171d26)}
.sidebar-item.active{border-left-color:var(--accent,#ff6b4a);background:var(--surface-elev,#171d26)}
.sidebar-item .sb-preview{font-size:13px;color:var(--text,#edf2f7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sidebar-item .sb-meta{font-size:11px;color:var(--muted,#9da7b7);margin-top:3px;display:flex;gap:8px}
.sidebar-empty{text-align:center;color:var(--muted,#9da7b7);padding:32px 16px;font-size:13px}
.sidebar-viewall{display:block;text-align:center;padding:10px 12px;font-size:12px;color:var(--muted,#9da7b7);cursor:pointer;border-top:1px solid var(--line,#2a3341);text-decoration:none}
.sidebar-viewall:hover{color:var(--accent,#ff6b4a)}
#sidebar-toggle{display:none;background:none;border:none;color:var(--muted,#9da7b7);font-size:20px;cursor:pointer;padding:4px 8px;margin-right:4px;line-height:1}
@media(max-width:1024px){
  #sidebar{position:fixed;left:-260px;top:0;bottom:0;z-index:999;transition:left 0.2s ease;box-shadow:4px 0 20px rgba(0,0,0,0.5)}
  body.sidebar-open #sidebar{left:0}
  body.sidebar-open::after{content:'';position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:998}
  #sidebar-toggle{display:block}
}
</style>"""

_SIDEBAR_BODY = """<div id="app-shell">
<aside id="sidebar">
  <div class="sidebar-head">
    <span class="sidebar-title">Unchained</span>
    <button class="sidebar-new" onclick="doNewChat()">+ New</button>
  </div>
  <div id="sidebar-history"></div>
</aside>
"""

_SIDEBAR_JS = """
function _relativeDate(ts) {
  var d = new Date(ts * 1000);
  var now = new Date();
  var days = Math.floor((now - d) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return d.toLocaleDateString(undefined, {weekday:'long'});
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

async function loadSidebarHistory() {
  var list = document.getElementById('sidebar-history');
  if (!list) return;
  try {
    var r = await fetch('/web/chat/archives?model=' + encodeURIComponent(currentModel()));
    if (!r.ok) { list.innerHTML = '<div class="sidebar-empty">Could not load history</div>'; return; }
    var data = await r.json();
    var allArchives = data.archives || [];
    var archives = allArchives.slice(0, 6);
    if (archives.length === 0) {
      list.innerHTML = '<div class="sidebar-empty">No chat history yet</div>';
      return;
    }
    list.innerHTML = '';
    for (var i = 0; i < archives.length; i++) {
      var arc = archives[i];
      var div = document.createElement('div');
      div.className = 'sidebar-item';
      div.setAttribute('data-id', arc.id);
      div.innerHTML =
        '<div class="sb-preview">' + esc(arc.preview || '(empty)') + '</div>' +
        '<div class="sb-meta"><span>' + _relativeDate(arc.archived_at) + '</span><span>' + arc.message_count + ' msgs</span></div>';
      div.onclick = (function(id) { return function() { restoreArchive(id); }; })(arc.id);
      list.appendChild(div);
    }
    var link = document.createElement('a');
    link.className = 'sidebar-viewall';
    link.href = '#';
    link.textContent = allArchives.length > 6 ? 'View all ' + allArchives.length + ' archives' : 'View all archives';
    link.onclick = function(e) { e.preventDefault(); openArchives(); };
    list.appendChild(link);
  } catch(e) {
    list.innerHTML = '<div class="sidebar-empty">Could not load history</div>';
  }
}

function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

document.addEventListener('click', function(e) {
  if (document.body.classList.contains('sidebar-open') && window.innerWidth <= 1024) {
    var sidebar = document.getElementById('sidebar');
    var toggle = document.getElementById('sidebar-toggle');
    if (sidebar && !sidebar.contains(e.target) && toggle && !toggle.contains(e.target)) {
      document.body.classList.remove('sidebar-open');
    }
  }
});
"""

_OLD_DELETE_ARCHIVE_JS = """async function deleteArchive(id, el) {
  if (!confirm('Delete this archived chat permanently?')) return;
  try {
    const r = await fetch('/web/chat/delete-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (r.ok) {
      el.remove();
      const list = document.getElementById('archive-list');
      if (!list.querySelector('.archive-item')) {
        list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
      }
    }
  } catch(e) {}
}"""

_NEW_DELETE_ARCHIVE_JS = """async function deleteArchive(id, el) {
  if (!confirm('Delete this archived chat permanently?')) return;
  try {
    const r = await fetch('/web/chat/delete-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (!r.ok) { try { alert((await r.json()).error || 'Delete failed'); } catch(_) { alert('Delete failed'); } return; }
    el.remove();
    loadSidebarHistory();
    const list = document.getElementById('archive-list');
    if (!list.querySelector('.archive-item')) {
      list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
    }
  } catch(e) {}
}"""


_ARCHIVE_INJECT_STYLE = """<style id="archive-inject">
#archive-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;justify-content:center;align-items:center}
#archive-overlay.open{display:flex}
#archive-panel{background:var(--surface,#11161d);border:1px solid var(--line,#2a3341);border-radius:12px;width:90%;max-width:480px;max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
#archive-panel .archive-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line,#2a3341)}
#archive-panel .archive-header h3{font-size:16px;font-weight:600;color:var(--text,#edf2f7)}
#archive-panel .archive-close{background:none;border:none;color:var(--muted,#9da7b7);font-size:20px;cursor:pointer;padding:4px 8px}
#archive-panel .archive-close:hover{color:var(--text,#edf2f7)}
#archive-list{overflow-y:auto;flex:1;padding:8px}
.archive-item{display:flex;align-items:flex-start;gap:12px;padding:12px;border-radius:8px;cursor:default}
.archive-item:hover{background:var(--surface-elev,#171d26)}
.archive-item .archive-info{flex:1;min-width:0}
.archive-item .archive-preview{font-size:13px;color:var(--text,#edf2f7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.archive-item .archive-meta{font-size:11px;color:var(--muted,#9da7b7);margin-top:4px}
.archive-item .archive-actions{display:flex;gap:6px;flex-shrink:0}
.archive-item .archive-actions button{background:none;border:1px solid var(--line,#2a3341);color:var(--muted,#9da7b7);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
.archive-item .archive-actions button:hover{color:var(--text,#edf2f7);border-color:var(--muted,#9da7b7)}
.archive-item .archive-actions .restore-btn:hover{border-color:var(--accent,#ff6b4a);color:var(--accent,#ff6b4a)}
.archive-item .archive-actions .delete-btn:hover{border-color:#e53e3e;color:#e53e3e}
.archive-empty{text-align:center;color:var(--muted,#9da7b7);padding:40px 20px;font-size:14px}
</style>"""

_ARCHIVE_INJECT_HTML = """<div id="archive-overlay" onclick="if(event.target===this)closeArchives()">
  <div id="archive-panel">
    <div class="archive-header">
      <h3>Archived Chats</h3>
      <button class="archive-close" onclick="closeArchives()">&times;</button>
    </div>
    <div id="archive-list"></div>
  </div>
</div>"""

_ARCHIVE_INJECT_JS = """
async function openArchives() {
  const overlay = document.getElementById('archive-overlay');
  overlay.classList.add('open');
  const list = document.getElementById('archive-list');
  list.innerHTML = '<div class="archive-empty">Loading...</div>';
  try {
    const r = await fetch('/web/chat/archives?model=' + encodeURIComponent(currentModel()));
    if (!r.ok) { list.innerHTML = '<div class="archive-empty">Failed to load archives</div>'; return; }
    const data = await r.json();
    const archives = data.archives || [];
    if (archives.length === 0) {
      list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
      return;
    }
    list.innerHTML = '';
    for (const arc of archives) {
      const d = new Date(arc.archived_at * 1000);
      const dateStr = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      const div = document.createElement('div');
      div.className = 'archive-item';
      div.innerHTML =
        '<div class="archive-info">' +
          '<div class="archive-preview">' + esc(arc.preview || '(empty)') + '</div>' +
          '<div class="archive-meta">' + arc.message_count + ' messages &middot; ' + dateStr + '</div>' +
        '</div>' +
        '<div class="archive-actions">' +
          '<button class="restore-btn" data-id="' + esc(arc.id) + '">Restore</button>' +
          '<button class="delete-btn" data-id="' + esc(arc.id) + '">Delete</button>' +
        '</div>';
      div.querySelector('.restore-btn').onclick = () => restoreArchive(arc.id);
      div.querySelector('.delete-btn').onclick = () => deleteArchive(arc.id, div);
      list.appendChild(div);
    }
  } catch(e) {
    list.innerHTML = '<div class="archive-empty">Error loading archives</div>';
  }
}

function closeArchives() {
  document.getElementById('archive-overlay').classList.remove('open');
}

async function restoreArchive(id) {
  if (!confirm('Restore this archived conversation? It will replace your current chat.')) return;
  try {
    const r = await fetch('/web/chat/restore-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      if (data.session_id) {
        sessionId = data.session_id;
        _persistSessionId(sessionId);
        if (typeof _setActiveSlotSession === 'function') _setActiveSlotSession(sessionId);
      }
      closeArchives();
      location.reload();
    } else {
      const data = await r.json().catch(() => ({}));
      alert(data.error || 'Failed to restore archive');
    }
  } catch(e) { alert('Failed to restore archive'); }
}

async function deleteArchive(id, el) {
  if (!confirm('Delete this archived chat permanently?')) return;
  try {
    const r = await fetch('/web/chat/delete-archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ archive_id: id, model: currentModel() }),
    });
    if (!r.ok) { try { alert((await r.json()).error || 'Delete failed'); } catch(_) { alert('Delete failed'); } return; }
    el.remove();
    if (typeof loadSidebarHistory === 'function') loadSidebarHistory();
    const list = document.getElementById('archive-list');
    if (!list.querySelector('.archive-item')) {
      list.innerHTML = '<div class="archive-empty">No archived chats yet.<br>Archives are created automatically when you start a new chat.</div>';
    }
  } catch(e) {}
}
"""


def _inject_sidebar(html: str) -> str:
    """Inject ChatGPT-style left sidebar for chat history."""
    if 'id="sidebar"' in html:
        return html

    # 0. If archive support is missing, inject it (needed for Gemini/Codex/Claude SDK)
    if 'id="archive-overlay"' not in html:
        html = html.replace("</head>", _ARCHIVE_INJECT_STYLE + "\n</head>", 1)
        html = html.replace("</body>", _ARCHIVE_INJECT_HTML + "\n</body>", 1)
        html = html.replace(
            "\ncheckSession();\n",
            _ARCHIVE_INJECT_JS + "\ncheckSession();\n",
            1,
        )

    # 1. Sidebar CSS before </head>
    html = html.replace("</head>", _SIDEBAR_STYLE + "\n</head>", 1)

    # 2. Wrap #main in app-shell with sidebar
    html = html.replace(
        "<!-- Main -->\n<div id=\"main\">",
        _SIDEBAR_BODY + "<!-- Main -->\n<div id=\"main\">",
    )

    # 3. Hamburger button in topbar
    html = html.replace(
        '<div class="left">',
        '<div class="left">\n      <button id="sidebar-toggle" onclick="toggleSidebar()" aria-label="Menu">&#9776;</button>',
        1,
    )

    # 4. Remove duplicate nav links (sidebar has + New and View all archives)
    html = html.replace(
        '      <a href="#" onclick="doNewChat();return false">New Chat</a>\n',
        '',
    )
    html = html.replace(
        '      <a href="#" onclick="openArchives();return false">Archives</a>\n',
        '',
    )

    # 5. Close app-shell wrapper after main closes, before <script>
    html = html.replace("</div>\n<script>", "</div>\n</div>\n<script>", 1)

    # 6. Show/hide app-shell alongside main
    html = html.replace(
        "document.getElementById('main').style.display = 'flex';",
        "document.getElementById('app-shell').style.display = 'flex';\n"
        "  document.getElementById('main').style.display = 'flex';",
    )
    # 6b. Load sidebar after model selector is initialized (loadHistory runs
    #     after model setup in showMain, so sidebar gets the correct model).
    html = html.replace(
        "  loadHistory();\n}",
        "  loadHistory();\n  loadSidebarHistory();\n}",
        1,
    )
    html = html.replace(
        "document.getElementById('main').style.display = 'none';",
        "document.getElementById('app-shell').style.display = 'none';\n"
        "  document.getElementById('main').style.display = 'none';",
    )

    # 7. Sidebar JS functions (before checkSession)
    html = html.replace(
        "\ncheckSession();\n",
        _SIDEBAR_JS + "\ncheckSession();\n",
        1,
    )

    # 8. doNewChat → reload sidebar after new chat
    for old_hook, new_hook in [
        (
            "  _syncSlotButtons();\n}",
            "  _syncSlotButtons();\n  loadSidebarHistory();\n}",
        ),
        (
            "  await loadSlots();\n}",
            "  await loadSlots();\n  loadSidebarHistory();\n}",
        ),
    ]:
        if old_hook in html:
            html = html.replace(old_hook, new_hook, 1)
            break

    # 9. Fix deleteArchive: show error on failure, refresh sidebar on success
    html = html.replace(_OLD_DELETE_ARCHIVE_JS, _NEW_DELETE_ARCHIVE_JS)

    return html


TRIAL_CHAT_HTML = _inject_sidebar(TRIAL_CHAT_HTML)
CLAUDE_CHAT_HTML = _inject_sidebar(CLAUDE_CHAT_HTML)
CHAT_GEMINI_HTML = _inject_sidebar(CHAT_GEMINI_HTML)
CHAT_CLAUDE_SDK_HTML = _inject_sidebar(CHAT_CLAUDE_SDK_HTML)
CHAT_CODEX_HTML = _inject_sidebar(CHAT_CODEX_HTML)

# Backward-compat alias used by older tests and tooling.
# Older tests assert an inline model expression in doSend().
_CHAT_HTML_MODEL_SENTINEL = "model: currentModel()"
if _CHAT_HTML_MODEL_SENTINEL not in CLAUDE_CHAT_HTML:
    raise RuntimeError(
        "CHAT_HTML compatibility shim out of sync: model sentinel missing"
    )
CHAT_HTML = CLAUDE_CHAT_HTML.replace(
    _CHAT_HTML_MODEL_SENTINEL,
    "model: document.getElementById('modelsel').value",
)


# ---------------------------------------------------------------------------
# Chat WebSocket bridge + SSE endpoint
# ---------------------------------------------------------------------------

INSTALL_CLAIM_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize Installer</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root{--bg:#0b0d10;--surface:#13171d;--line:#2a313b;--text:#e8edf3;--muted:#a7b0bc;--ok:#23c483;--warn:#f59e0b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{max-width:680px;margin:0 auto;padding:36px 18px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px}
h1{margin:0 0 8px;font-size:28px}
.sub{color:var(--muted);font-size:15px;line-height:1.5}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.btn{border:none;border-radius:9px;padding:10px 14px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,#23c483,#18a96f);color:#052117}
.btn-ghost{background:transparent;color:#d0daea;border:1px solid #39465a}
#state{margin-top:12px;font-size:13px}
#state.ok{color:var(--ok)}
#state.warn{color:var(--warn)}
#approve-panel{display:none}
#signin-panel{display:none}
</style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <h1>Authorize This Installer</h1>
      <div class="sub">Approve this device to continue setup. The installer will fetch a short-lived token and exchange it for your API key locally.</div>
      <div id="signin-panel" class="row">
        <a class="btn btn-primary" id="signin-link" href="/local">Sign In To Continue</a>
      </div>
      <div id="approve-panel">
        <div class="row">
          <button class="btn btn-primary" id="approve-btn" onclick="approveInstall()">Approve Device</button>
          <a class="btn btn-ghost" href="/install">Open Installer Page</a>
        </div>
      </div>
      <div id="state"></div>
    </div>
  </div>
<script>
const CLAIM_ID = "__CLAIM_ID__";

function setState(msg, mode) {
  const el = document.getElementById('state');
  el.textContent = msg || '';
  el.className = mode || '';
}

async function approveInstall() {
  const btn = document.getElementById('approve-btn');
  btn.disabled = true;
  btn.textContent = 'Approving...';
  try {
    const r = await fetch('/web/install/claim/approve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({claim_id: CLAIM_ID}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    setState('Approved. Return to the installer window; setup will continue automatically.', 'ok');
    btn.textContent = 'Approved';
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Approve Device';
    setState(`Approval failed: ${e.message}`, 'warn');
  }
}

async function initClaimPage() {
  const signInLink = document.getElementById('signin-link');
  signInLink.href = '/local?next=' + encodeURIComponent('/install/claim/' + CLAIM_ID);
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (!r.ok || !data.authenticated) {
      document.getElementById('signin-panel').style.display = 'flex';
      setState('Sign in to approve this installer.', 'warn');
      return;
    }
    document.getElementById('approve-panel').style.display = 'block';
    setState('Signed in. Click "Approve Device" to continue.', '');
  } catch (e) {
    document.getElementById('signin-panel').style.display = 'flex';
    setState(`Could not verify session: ${e.message}`, 'warn');
  }
}

initClaimPage();
</script>
</body>
</html>
"""


INSTALL_ONBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Install Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root{
  --bg:#0b0d10;--surface:#13171d;--line:#2a313b;--text:#e8edf3;--muted:#a7b0bc;
  --accent:#23c483;--accent-2:#0ea5e9;--warn:#f59e0b;
}
*{box-sizing:border-box}
body{
  margin:0;background:radial-gradient(1200px 700px at 70% -10%, #1d2c3a 0%, var(--bg) 60%);
  color:var(--text);font-family:ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.shell{max-width:860px;margin:0 auto;padding:40px 18px 80px}
.hero{margin-bottom:18px}
.kicker{font-size:12px;letter-spacing:1.2px;text-transform:uppercase;color:var(--accent-2)}
h1{margin:8px 0 10px;font-size:36px;line-height:1.15}
.sub{color:var(--muted);font-size:16px;line-height:1.6;max-width:720px}
.card{
  margin-top:20px;background:rgba(19,23,29,0.95);border:1px solid var(--line);
  border-radius:14px;padding:20px;
}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{
  display:inline-flex;align-items:center;border:1px solid #334155;border-radius:999px;
  padding:4px 10px;font-size:12px;color:#c6d0dc;background:#0d1218;
}
.pill.online{color:#90edba;border-color:#2f6f39;background:#112219}
.pill.warn{color:#f2d18a;border-color:#7a6326;background:#1d1607}
.safe{margin:14px 0 4px;padding-left:18px;color:#cbd5e1}
.safe li{margin:6px 0}
.agree{
  margin-top:12px;border:1px solid #32404f;background:#0e141b;border-radius:10px;padding:12px;
  font-size:14px;color:#dbe7f3
}
.agree input{vertical-align:middle;transform:translateY(-1px)}
.btn{
  border:none;border-radius:10px;padding:11px 14px;font-size:14px;font-weight:600;cursor:pointer;
}
.btn-primary{background:linear-gradient(135deg,#23c483,#18a96f);color:#062217}
.btn-primary:disabled{opacity:0.45;cursor:not-allowed}
.btn-ghost{background:transparent;color:#c8d4e1;border:1px solid #3a4759}
.cmd{
  margin-top:12px;padding:10px 12px;border:1px solid #2f3b4a;border-radius:8px;
  background:#0b1118;color:#c8f7df;font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space:pre-wrap;word-break:break-word;font-size:12px;
}
.note{margin-top:10px;color:#9aa6b3;font-size:12px}
.note.warn{color:#f9c56e}
.status{margin-top:10px;font-size:13px;color:#cde0f5}
.warn{color:#f9c56e}
.runtime-actions{display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap;margin-top:10px}
.install-client-update-btn{
  border-radius:999px;
  padding:9px 12px;
  transition:border-color 0.15s ease,background 0.15s ease,color 0.15s ease,box-shadow 0.15s ease;
}
.install-client-update-btn.warn{
  color:#f2d18a;
  border-color:#7a6326;
  background:#1d1607;
  animation:installClientUpdatePulse 1.8s ease-in-out infinite;
}
.install-client-update-btn.busy{
  color:#d8fff0;
  border-color:#1f7f5a;
  background:rgba(35,196,131,0.12);
  animation:none;
}
.install-client-update-note{margin-top:0;min-height:20px;line-height:1.4}
@keyframes installClientUpdatePulse{0%,100%{box-shadow:none}50%{box-shadow:0 0 14px rgba(245,158,11,0.16)}}
a{color:#93d5ff}
#auth-panel{display:none}
#ready-panel{display:none}
@media (max-width:680px){
  h1{font-size:30px}
  .shell{padding-top:30px}
  .pill{
    border:none;
    padding:0;
    border-radius:0;
    background:transparent;
  }
  .pill.online,
  .pill.warn{border-color:transparent;background:transparent}
}
</style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div class="kicker">Secure Installer</div>
      <h1>Install Unchained</h1>
      <div class="sub">One guided download from this website. No terminal required for normal onboarding.</div>
    </div>

    <div class="card" id="auth-panel">
      <div style="font-size:18px;font-weight:600;margin-bottom:6px">Sign in first</div>
      <div class="sub" style="font-size:14px">You need to be signed in before downloading your personalized installer.</div>
      <div class="row" style="margin-top:12px">
        <a class="btn btn-primary" style="text-decoration:none;display:inline-flex;align-items:center" href="/local?next=%2Finstall">Sign In</a>
      </div>
    </div>

    <div class="card" id="ready-panel">
      <div class="row">
        <span class="pill">Detected OS: <strong id="os-label" style="margin-left:6px">macOS</strong></span>
        <span class="pill">Signed Installer Flow</span>
      </div>
      <div class="row" style="margin-top:10px">
        <span class="pill" id="install-agentstatus">agent offline</span>
        <span class="pill" id="install-bridgestatus">bridge offline</span>
      </div>
      <div class="note" id="install-runtime-status">Checking local agent status...</div>
      <div class="runtime-actions">
        <button class="btn btn-ghost install-client-update-btn" id="install-client-update-btn" type="button" onclick="triggerInstallClientUpdate()">Client current</button>
        <span class="note install-client-update-note" id="install-client-update-note"></span>
      </div>

      <ul class="safe">
        <li>Installer download is issued from your authenticated account session.</li>
        <li>Downloads a native installer binary for your OS (.dmg/.pkg or .msi/.exe).</li>
        <li>Fallback shell installers are disabled in the one-click flow.</li>
      </ul>

      <div class="agree">
        <label>
          <input type="checkbox" id="agree-box" onchange="refreshInstallButton()">
          I agree to install the local Unchained agent on this device and allow it to automate my browser sessions.
        </label>
      </div>

      <div class="row" style="margin-top:14px">
        <button class="btn btn-primary" id="install-btn" onclick="startInstall()" disabled>Download Installer</button>
        <a class="btn btn-ghost" style="text-decoration:none;display:inline-flex;align-items:center" href="/web/download-agent">Download ZIP</a>
      </div>

      <div class="status" id="install-status"></div>
    </div>
  </div>

<script>
let _installOs = 'mac';
let _installStatusTimer = null;
let installClientUpdateInFlight = false;
let installClientUpdateSawDisconnect = false;
let installClientUpdateError = '';
const INSTALL_CLIENT_UPDATE_TIMEOUT_MS = 90000;
let installClientUpdateStartedAt = 0;
let lastInstallClientStatus = null;

function _detectInstallOs() {
  const src = `${navigator.platform || ''} ${navigator.userAgent || ''}`.toLowerCase();
  if (src.includes('win')) return 'windows';
  if (src.includes('mac')) return 'mac';
  return 'other';
}

function refreshInstallButton() {
  const cb = document.getElementById('agree-box');
  const btn = document.getElementById('install-btn');
  btn.disabled = !cb.checked || _installOs === 'other';
}

function _setStatus(msg, warn) {
  const el = document.getElementById('install-status');
  el.textContent = msg;
  el.className = warn ? 'status warn' : 'status';
}

function _setRuntimeStatus(msg, warn) {
  const el = document.getElementById('install-runtime-status');
  if (!el) return;
  el.textContent = msg;
  el.className = warn ? 'note warn' : 'note';
}

function _setInstallPill(id, text, mode) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'pill' + (mode ? ' ' + mode : '');
}

function updateInstallClientUpdateUI(data) {
  const btn = document.getElementById('install-client-update-btn');
  const note = document.getElementById('install-client-update-note');
  if (!btn || !note) return;
  const clientConnected = !!data.client_connected;
  const updateSupported = !!data.client_update_supported;
  const outdated = !!data.client_outdated;
  const required = !!data.client_update_required;
  const localVersion = String(data.client_version || '').trim();
  const serverVersion = String(data.server_version || '').trim();
  btn.className = 'btn btn-ghost install-client-update-btn';
  if (installClientUpdateInFlight) {
    btn.classList.add('busy');
    btn.disabled = true;
    btn.textContent = 'Updating...';
    note.textContent = 'Client will reconnect after restart.';
    return;
  }
  if (outdated) btn.classList.add('warn');
  btn.disabled = !clientConnected || !updateSupported || !outdated;
  if (!clientConnected) {
    btn.textContent = 'Client offline';
    note.textContent = localVersion ? ('Last seen v' + localVersion) : '';
  } else if (!updateSupported) {
    btn.textContent = 'Manual update';
    note.textContent = localVersion ? ('Client v' + localVersion) : 'Reconnect with the latest package once.';
  } else if (outdated) {
    btn.textContent = required ? 'Update required' : 'Update client';
    note.textContent = (localVersion && serverVersion) ? ('v' + localVersion + ' -> v' + serverVersion) : 'New client available';
  } else {
    btn.textContent = 'Client current';
    note.textContent = localVersion ? ('v' + localVersion) : '';
  }
  if (installClientUpdateError) note.textContent = installClientUpdateError;
}

async function triggerInstallClientUpdate() {
  if (installClientUpdateInFlight) return;
  installClientUpdateError = '';
  installClientUpdateInFlight = true;
  installClientUpdateSawDisconnect = false;
  installClientUpdateStartedAt = Date.now();
  updateInstallClientUpdateUI(lastInstallClientStatus || {});
  try {
    const r = await fetch('/web/chat/update-client', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    checkInstallAgentStatus();
  } catch(e) {
    installClientUpdateInFlight = false;
    installClientUpdateSawDisconnect = false;
    installClientUpdateStartedAt = 0;
    installClientUpdateError = e.message || 'Update failed';
    updateInstallClientUpdateUI(lastInstallClientStatus || {});
  }
}

function updateInstallAgentStatusUI(data) {
  const chatConnected = !!data.chat_connected;
  const bridgeConnected = !!data.bridge_connected;
  const mismatch = !!data.mismatch;
  if (installClientUpdateInFlight) {
    if (!data.client_connected) installClientUpdateSawDisconnect = true;
    else if (installClientUpdateSawDisconnect || !data.client_outdated) {
      installClientUpdateInFlight = false;
      installClientUpdateSawDisconnect = false;
      installClientUpdateStartedAt = 0;
      installClientUpdateError = '';
    } else if (installClientUpdateStartedAt && (Date.now() - installClientUpdateStartedAt) >= INSTALL_CLIENT_UPDATE_TIMEOUT_MS) {
      installClientUpdateInFlight = false;
      installClientUpdateSawDisconnect = false;
      installClientUpdateStartedAt = 0;
      installClientUpdateError = 'Update timed out. Check the local client logs and retry.';
    }
  }
  if (!installClientUpdateInFlight && !data.client_outdated) installClientUpdateError = '';
  lastInstallClientStatus = data;
  updateInstallClientUpdateUI(data);

  if (chatConnected) _setInstallPill('install-agentstatus', 'agent online', 'online');
  else if (mismatch) _setInstallPill('install-agentstatus', 'agent mismatch', 'warn');
  else _setInstallPill('install-agentstatus', 'agent offline', '');

  if (bridgeConnected) _setInstallPill('install-bridgestatus', 'bridge online', 'online');
  else _setInstallPill('install-bridgestatus', 'bridge offline', '');

  if (chatConnected && bridgeConnected) {
    _setRuntimeStatus('Agent and browser bridge are online on this machine.', false);
    return;
  }
  if (chatConnected && !bridgeConnected) {
    _setRuntimeStatus('Chat agent is online, but browser bridge is offline on this machine.', true);
    return;
  }
  if (!chatConnected && bridgeConnected) {
    _setRuntimeStatus('Browser bridge is online, but chat agent is offline on this machine.', true);
    return;
  }
  if (mismatch) {
    _setRuntimeStatus('A different machine currently owns the active chat agent for this account.', true);
    return;
  }
  _setRuntimeStatus('Local chat agent and browser bridge are offline on this machine.', true);
}

async function checkInstallAgentStatus() {
  try {
    const r = await fetch('/web/chat/status');
    if (!r.ok) return;
    const data = await r.json();
    updateInstallAgentStatusUI(data);
  } catch(e) {
    _setRuntimeStatus(`Could not check local status: ${e.message}`, true);
  }
}

async function startInstall() {
  const btn = document.getElementById('install-btn');
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = 'Preparing installer...';
  _setStatus('');
  try {
    const r = await fetch('/web/install-token', {method: 'POST'});
    if (r.status === 401) {
      window.location.href = '/local?next=%2Finstall';
      return;
    }
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);

    const downloadUrl = _installOs === 'windows' ? data.windows_installer_url : data.mac_installer_url;
    const native = data.native_available || {};
    const osNativeReady = _installOs === 'windows' ? native.windows : native.mac;

    if (!osNativeReady) {
      const zipUrl = data.zip_url || '/web/download-agent';
      _setStatus('Native installer is not available yet for this OS. Downloading ZIP package instead.', true);
      window.location.href = zipUrl;
      return;
    }
    if (!downloadUrl) throw new Error('Installer URL missing from server response.');
    _setStatus('Download started. Open the file from your Downloads folder when ready.');
    window.location.href = downloadUrl;
  } catch (e) {
    _setStatus(`Install failed: ${e.message}`, true);
  } finally {
    btn.textContent = _installOs === 'windows' ? 'Download Installer for Windows' : 'Download Installer for macOS';
    refreshInstallButton();
  }
}

async function initInstallPage() {
  _installOs = _detectInstallOs();
  const label = document.getElementById('os-label');
  if (_installOs === 'windows') label.textContent = 'Windows';
  else if (_installOs === 'mac') label.textContent = 'macOS';
  else label.textContent = 'Unsupported';

  if (_installOs === 'other') {
    _setStatus('Native one-click installer is currently available for macOS and Windows. Use Download ZIP for other OSes.', true);
  }

  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (!r.ok || !data.authenticated) {
      document.getElementById('auth-panel').style.display = 'block';
      return;
    }
    document.getElementById('ready-panel').style.display = 'block';
    refreshInstallButton();
    await checkInstallAgentStatus();
    if (!_installStatusTimer) _installStatusTimer = setInterval(checkInstallAgentStatus, 5000);
  } catch (e) {
    document.getElementById('auth-panel').style.display = 'block';
    _setStatus(`Auth check failed: ${e.message}`, true);
  }
}

initInstallPage();
</script>
</body>
</html>
"""
SETUP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Setup — Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#1a1a2e;--surface:#16213e;--accent:#e94560;
  --text:#eee;--muted:#888;
  --green:#4ade80;--red:#f87171;--yellow:#fbbf24;
  --mono:'SF Mono','Menlo','Monaco','Consolas',monospace;
}
body{
  font-family:-apple-system,system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  min-height:100dvh;
}

/* === Login === */
#login{
  display:none;flex-direction:column;align-items:center;
  justify-content:center;height:100dvh;padding:24px;gap:16px;
}
#login h1{font-size:28px;color:var(--accent);margin-bottom:8px;letter-spacing:1px}
#login .sub{color:var(--muted);font-size:13px;margin-bottom:8px}
#loginerr{color:#ff6b6b;font-size:14px;min-height:20px}

/* === Main === */
#main{display:none;flex-direction:column;min-height:100dvh}

#topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:8px 16px;padding-top:max(8px,env(safe-area-inset-top));
  background:var(--surface);border-bottom:1px solid #333;flex-shrink:0;
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .title{font-family:var(--mono);font-size:15px;color:var(--accent);font-weight:600;letter-spacing:0.5px}
#topbar .nav{display:flex;gap:8px;align-items:center}
#topbar .nav a{
  color:var(--muted);text-decoration:none;font-size:12px;
  border:1px solid #555;padding:4px 10px;border-radius:6px;
}
#topbar .nav a:hover{border-color:var(--accent);color:var(--accent)}
.status-stack{display:flex;flex-direction:column;line-height:1.2;gap:2px}
.status{font-size:11px;color:var(--muted)}
.status.online{color:var(--green)}
.status.warn{color:var(--yellow)}
.setup-client-update{display:flex;flex-direction:column;gap:3px;align-items:flex-start}
.setup-client-update-btn{
  border:1px solid #555;
  background:rgba(255,255,255,0.03);
  color:var(--muted);
  border-radius:999px;
  padding:5px 10px;
  font-size:11px;
  font-family:var(--mono,monospace);
  cursor:pointer;
  transition:border-color 0.15s ease,background 0.15s ease,color 0.15s ease,box-shadow 0.15s ease;
}
.setup-client-update-btn:hover{border-color:var(--accent);color:var(--accent);background:rgba(255,255,255,0.05)}
.setup-client-update-btn.warn{
  color:#f3c56a;
  border-color:#8d6722;
  background:rgba(243,197,106,0.12);
  animation:setupClientUpdatePulse 1.8s ease-in-out infinite;
}
.setup-client-update-btn.busy{
  color:#ffd2c7;
  border-color:rgba(233,69,96,0.45);
  background:rgba(233,69,96,0.12);
  animation:none;
}
.setup-client-update-btn:disabled{opacity:0.5;cursor:default;box-shadow:none}
.setup-client-update-note{
  min-height:12px;
  font-size:10px;
  color:var(--muted);
  font-family:var(--mono,monospace);
  line-height:1.1;
}
@keyframes setupClientUpdatePulse{0%,100%{box-shadow:none}50%{box-shadow:0 0 14px rgba(243,197,106,0.16)}}

/* Setup install banner */
#setup-download-banner{
  display:flex;align-items:center;justify-content:center;gap:10px;
  padding:8px 16px;background:#2a1a1a;border-bottom:1px solid #444;
  font-size:12px;color:var(--muted);flex-shrink:0;flex-wrap:wrap;
}
#setup-download-banner .copy{display:flex;flex-direction:column;gap:2px;min-width:0}
#setup-download-banner .detail{font-size:11px;color:#9aa0aa}
#setup-download-banner a{
  color:var(--accent);text-decoration:none;font-weight:600;
  border:1px solid var(--accent);padding:4px 10px;border-radius:6px;
}
#setup-download-banner a:hover{background:var(--accent);color:#fff}

/* === Content === */
.content{max-width:600px;margin:0 auto;padding:24px 16px}

.step{
  background:var(--surface);border:1px solid #333;border-radius:10px;
  padding:20px;margin-bottom:20px;
}
.step h2{font-size:14px;color:var(--accent);margin-bottom:14px;font-weight:600;letter-spacing:0.3px}

/* Profile list */
.profile-list{display:flex;flex-direction:column;gap:8px}
.profile-option{
  display:flex;align-items:center;gap:12px;
  padding:10px 14px;border:1px solid #333;border-radius:8px;
  cursor:pointer;transition:border-color 0.15s,background 0.15s;
}
.profile-option:hover{border-color:#555;background:rgba(255,255,255,0.02)}
.profile-option.selected{border-color:var(--accent);background:rgba(233,69,96,0.08)}
.profile-option input[type="radio"]{
  accent-color:var(--accent);width:16px;height:16px;cursor:pointer;flex-shrink:0;
}
.profile-info{display:flex;flex-direction:column;gap:2px;min-width:0}
.profile-name{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.profile-email{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.profile-hint{font-size:11px;color:var(--muted);font-style:italic}
.profiles-loading{color:var(--muted);font-size:13px;padding:12px 0}

/* Provision button */
.provision-btn{
  width:100%;height:44px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:14px;font-weight:600;
  cursor:pointer;font-family:var(--mono);transition:opacity 0.15s;
}
.provision-btn:hover{opacity:0.9}
.provision-btn:active{opacity:0.8}
.provision-btn:disabled{opacity:0.35;cursor:not-allowed}

.provision-status{
  margin-top:14px;font-size:12px;font-family:var(--mono);
  color:var(--muted);min-height:20px;line-height:1.5;
}
.provision-status.running{color:var(--yellow)}
.provision-status.done{color:var(--green)}
.provision-status.error{color:var(--red)}

.spinner{
  display:inline-block;width:12px;height:12px;
  border:2px solid var(--yellow);border-top-color:transparent;
  border-radius:50%;animation:spin 0.8s linear infinite;
  vertical-align:middle;margin-right:6px;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* Status section */
.key-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 0;border-bottom:1px solid #2a2a3e;
}
.key-row:last-child{border-bottom:none}
.key-info{display:flex;flex-direction:column;gap:3px}
.key-provider{font-size:13px;font-weight:500}
.key-preview{font-size:11px;font-family:var(--mono);color:var(--muted)}
.key-status{font-size:12px}
.key-status.provisioned{color:var(--green)}
.key-status.none{color:var(--muted)}
.revoke-btn{
  padding:4px 12px;border-radius:4px;font-size:11px;
  font-family:var(--mono);cursor:pointer;border:1px solid;
  font-weight:500;transition:opacity 0.15s;
  background:#2d0d0d;color:var(--red);border-color:#4a1a1a;
}
.revoke-btn:hover{opacity:0.8}
.revoke-btn:disabled{opacity:0.3;cursor:not-allowed}

/* Pending screen */
#pending{display:none;flex-direction:column;align-items:center;justify-content:center;height:100dvh;padding:24px;gap:16px;text-align:center}

/* Toast */
.toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;border-radius:6px;font-size:12px;font-family:var(--mono);z-index:999;animation:fadeIn 0.2s ease-out}
.toast.ok{background:#0d2d1a;color:var(--green);border:1px solid #1a4a2a}
.toast.err{background:#2d0d0d;color:var(--red);border:1px solid #4a1a1a}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

/* Consent modal */
#consent-modal{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.7);
  display:none;align-items:center;justify-content:center;padding:24px}
#consent-modal .modal-card{background:var(--surface);border:1px solid #333;
  border-radius:12px;padding:28px;max-width:480px;width:100%}
#consent-modal h3{color:var(--accent);font-size:17px;margin-bottom:12px}
#consent-modal p{color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:14px}
#consent-modal ul{list-style:none;padding:0;margin:0 0 20px}
#consent-modal li{color:#bbb;font-size:13px;line-height:1.8;padding-left:18px;position:relative}
#consent-modal li::before{content:"\2022";position:absolute;left:0;color:var(--green)}
#consent-modal .modal-btns{display:flex;gap:10px}
#consent-modal .btn-agree{
  flex:1;padding:10px;border:none;border-radius:8px;
  background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
#consent-modal .btn-agree:hover{opacity:0.9}
#consent-modal .btn-cancel{
  padding:10px 20px;border:1px solid #555;border-radius:8px;
  background:transparent;color:var(--muted);font-size:14px;cursor:pointer}
#consent-modal .btn-cancel:hover{border-color:#888;color:var(--text)}

/* Agent status */
.agent-status{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.agent-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex-shrink:0}
.agent-dot.online{background:var(--green)}
.agent-label{font-size:13px;color:var(--muted)}
.agent-label.online{color:var(--green)}

/* Install command */
.install-cmd{background:#0d0d1a;border:1px solid #333;border-radius:6px;
  padding:12px;font-family:var(--mono);font-size:12px;color:var(--text);
  word-break:break-all;margin:10px 0;position:relative}
.copy-btn{padding:6px 14px;border:1px solid #555;border-radius:6px;
  background:transparent;color:var(--muted);font-size:12px;cursor:pointer}
.copy-btn:hover{border-color:var(--accent);color:var(--accent)}

/* Consent denied message */
.consent-denied{text-align:center;padding:40px 20px;color:var(--muted);font-size:14px}

/* Install modal */
#setup-install-modal{
  display:none;position:fixed;inset:0;z-index:110;background:rgba(0,0,0,0.7);
  align-items:center;justify-content:center;padding:20px;
}
#setup-install-modal .card{
  background:var(--surface);border:1px solid #444;border-radius:12px;
  padding:20px;max-width:560px;width:92%;position:relative;
}
#setup-install-modal h3{color:var(--accent);margin-bottom:8px;font-size:16px}
#setup-install-modal p{color:var(--muted);font-size:12px;line-height:1.5}
#setup-install-modal .cmd{
  background:#0d0d1a;border:1px solid #333;border-radius:8px;padding:10px;
  font-family:var(--mono);font-size:12px;word-break:break-all;margin-top:10px;color:var(--text);
}
#setup-install-modal .x{
  position:absolute;top:10px;right:12px;background:none;border:none;color:var(--muted);
  font-size:18px;cursor:pointer;
}
</style>
</head>
<body>

<!-- Consent Modal -->
<div id="consent-modal">
  <div class="modal-card">
    <h3>Browser Access Consent</h3>
    <p>Here's exactly what the automation will do:</p>
    <ul>
      <li>Open a new tab in your Chrome and navigate to aistudio.google.com/apikey</li>
      <li>Click "Create API key" to generate a key in your Google account</li>
      <li>Capture the key from the page response and store it encrypted on our server</li>
      <li>If Google asks you to accept Terms of Service, you'll need to do that yourself in Chrome</li>
      <li>You can view or revoke the key any time from this page</li>
    </ul>
    <div class="modal-btns">
      <button class="btn-agree" onclick="acceptConsent()">I Agree &mdash; Continue</button>
      <button class="btn-cancel" onclick="declineConsent()">Cancel</button>
    </div>
  </div>
</div>

<!-- Login -->
<div id="login">
  <h1>Unchained</h1>
  <div class="sub">Setup &amp; Provisioning</div>
  <div id="google-auth">
    <div id="g_id_onload"
         data-client_id="__GOOGLE_CLIENT_ID__"
         data-callback="handleGoogleCredential"
         data-auto_prompt="false"
         data-context="signin"
         data-ux_mode="popup"></div>
    <div class="g_id_signin"
         data-type="standard"
         data-shape="rectangular"
         data-theme="outline"
         data-text="signin_with"
         data-size="large"
         data-logo_alignment="center"
         data-width="320"></div>
  </div>
  <div id="dev-auth" style="display:none">
    <button onclick="devLogin()" style="width:320px;height:48px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:16px;font-weight:600;cursor:pointer">Dev Login</button>
    <div style="color:var(--muted);font-size:11px;margin-top:6px">No Google OAuth configured — using local dev auth</div>
  </div>
  <div id="loginerr"></div>
</div>

<!-- Pending -->
<div id="pending">
  <h1 style="font-size:28px;color:var(--accent);letter-spacing:1px">Request Being Reviewed</h1>
  <p style="color:var(--muted);font-size:15px;max-width:400px">Your sign-up request is being reviewed. We'll send you an email once your account is approved.</p>
  <button onclick="checkApproval()" style="margin-top:16px;padding:10px 24px;border:1px solid var(--accent);border-radius:8px;background:transparent;color:var(--accent);font-size:14px;cursor:pointer">Check Again</button>
  <button onclick="backToLogin()" style="margin-top:8px;padding:10px 24px;border:1px solid #444;border-radius:8px;background:transparent;color:var(--muted);font-size:14px;cursor:pointer">Use a Different Account</button>
  <div id="pendingmsg" style="color:var(--muted);font-size:13px;min-height:20px"></div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <div class="left">
      <span class="title">Setup</span>
      <div class="status-stack">
        <span class="status" id="setup-agentstatus">agent offline</span>
        <span class="status" id="setup-bridgestatus">bridge offline</span>
      </div>
      <div class="setup-client-update">
        <button class="setup-client-update-btn" id="setup-client-update-btn" type="button" onclick="triggerSetupClientUpdate()">Client current</button>
        <span class="setup-client-update-note" id="setup-client-update-note"></span>
      </div>
    </div>
    <div class="nav">
      <a href="/local">Chat</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doLogout();return false">Logout</a>
    </div>
  </div>

  <div id="setup-download-banner" style="display:none">
    <div class="copy">
      <span id="setup-banner-msg">Your local chat agent is offline.</span>
      <span class="detail" id="setup-banner-detail">Start the installer to enable chat and browser control.</span>
    </div>
    <a href="#" onclick="showSetupInstallCmd();return false" id="setup-banner-curl">Install (curl)</a>
    <a href="/install" id="setup-banner-connect">Download Agent Installer</a>
  </div>

  <div id="setup-install-modal">
    <div class="card">
      <button class="x" onclick="closeSetupInstallModal()">&times;</button>
      <h3>Install Agent (curl)</h3>
      <p>Run this command in your terminal:</p>
      <div class="cmd" id="setup-install-curl-cmd">Generating install command...</div>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button class="copy-btn" onclick="copySetupInstallCmd(this)">Copy</button>
      </div>
    </div>
  </div>

  <div class="content">
    <!-- Consent denied message (production only) -->
    <div id="consent-denied" class="consent-denied" style="display:none">
      Setup requires browser access consent to proceed.
      <br><a href="#" onclick="location.reload();return false" style="color:var(--accent);margin-top:8px;display:inline-block">Try Again</a>
    </div>

    <!-- Step: Connect Your Browser (production only) -->
    <div class="step" id="step-connect" style="display:none">
      <h2>Step 1: Connect Your Browser</h2>
      <div class="agent-status">
        <span class="agent-dot" id="agent-dot"></span>
        <span class="agent-label" id="agent-label">Agent Offline</span>
      </div>
      <p id="setup-connect-desc" style="color:var(--muted);font-size:13px;margin-bottom:10px">Run this in your terminal:</p>
      <div class="install-cmd" id="setup-install-cmd">Loading...</div>
      <button class="copy-btn" onclick="copySetupCmd(this)">Copy</button>
      <p id="setup-connect-note" style="color:var(--muted);font-size:11px;margin-top:12px">Requires Python 3 and curl. Link expires in 15 minutes.</p>
    </div>

    <!-- Step: Choose Chrome Profile -->
    <div class="step" id="step-profiles">
      <h2 id="profile-step-title">Step 1: Choose Chrome Profile</h2>
      <div id="profile-list" class="profile-list">
        <div class="profiles-loading">Loading profiles...</div>
      </div>
    </div>

    <!-- Step: Provision Provider Key -->
    <div class="step" id="step-provision">
      <h2 id="provision-step-title">Step 2: Provision Gemini API Key</h2>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <label for="provider-select" style="font-size:13px;color:var(--muted)">Provider:</label>
        <select id="provider-select" onchange="onProviderChange(this.value)" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--fg);font-size:13px">
          <option value="gemini">Gemini API</option>
          <option value="claude-sdk">Claude API</option>
          <option value="codex-sdk">Codex API</option>
        </select>
      </div>
      <div id="local-mode-hint" style="display:none;color:var(--muted);font-size:13px;margin-bottom:10px">
        Chrome will open visibly &mdash; you can watch the entire process.
      </div>
      <div id="relay-mode-hint" style="display:none;color:var(--muted);font-size:13px;margin-bottom:10px">
        Prefer full control? Access this page from your computer at <code style="background:rgba(255,255,255,0.1);padding:1px 4px;border-radius:3px">localhost:8080/setup</code> to use visible Chrome mode.
      </div>
      <button class="provision-btn" id="provision-btn" onclick="startProvision()" disabled>
        Provision Gemini API Key
      </button>
      <div class="provision-status" id="provision-status"></div>
      <div id="manual-key-section" style="display:none;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
        <div style="color:var(--muted);font-size:13px;margin-bottom:8px">Or paste a key manually:</div>
        <div style="display:flex;gap:8px;align-items:center">
          <input type="text" id="manual-key-input" placeholder="AIzaSy..." style="flex:1;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--fg);font-size:14px;font-family:monospace">
          <button onclick="saveManualKey()" style="padding:8px 14px;border:none;border-radius:6px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer;white-space:nowrap">Save Key</button>
        </div>
      </div>
    </div>

    <!-- Status -->
    <div class="step">
      <h2>Status</h2>
      <div id="status-list">
        <div class="profiles-loading">Loading...</div>
      </div>
    </div>
  </div>
</div>

<script>
function _isWindows() { return navigator.userAgent.indexOf('Windows') !== -1; }

/* --- Auth --- */
async function handleGoogleCredential(response) {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential}),
    });
    const data = await r.json();
    if (data.pending) { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) { showMain(); return; }
    if (data.pending) { showPending(); return; }
  } catch(e) {}
  document.getElementById('login').style.display = 'flex';
}

async function checkApproval() {
  const msg = document.getElementById('pendingmsg');
  msg.textContent = 'Checking...';
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) { showMain(); return; }
    if (data.pending) { msg.textContent = 'Still under review. Check back soon!'; return; }
    msg.textContent = 'Still under review.';
  } catch(e) { msg.textContent = 'Could not check status.'; }
}

async function doLogout() {
  await fetch('/auth/logout', {method: 'POST'});
  document.getElementById('login').style.display = 'flex';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
}

async function backToLogin() {
  await fetch('/auth/logout', {method: 'POST'});
  document.getElementById('pending').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

function showPending() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = 'none';
  document.getElementById('pending').style.display = 'flex';
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  if (!isLocal && !sessionStorage.getItem('uc_setup_consent')) {
    document.getElementById('consent-modal').style.display = 'flex';
  } else {
    init();
  }
}

function acceptConsent() {
  sessionStorage.setItem('uc_setup_consent', '1');
  document.getElementById('consent-modal').style.display = 'none';
  init();
}

function declineConsent() {
  document.getElementById('consent-modal').style.display = 'none';
  document.getElementById('step-connect').style.display = 'none';
  document.getElementById('step-profiles').style.display = 'none';
  document.getElementById('step-provision').style.display = 'none';
  document.getElementById('consent-denied').style.display = 'block';
}

/* --- Toast --- */
function showToast(msg, isErr) {
  const t = document.createElement('div');
  t.className = 'toast ' + (isErr ? 'err' : 'ok');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

/* --- Mode detection --- */
const isLocal = ['localhost','127.0.0.1'].includes(window.location.hostname);
let selectedProfile = undefined; // undefined = nothing selected, null = clean profile
let selectedProvider = 'gemini';
let agentConnected = false;
let statusPollTimer = null;
let setupClientUpdateInFlight = false;
let setupClientUpdateSawDisconnect = false;
let setupClientUpdateError = '';
const SETUP_CLIENT_UPDATE_TIMEOUT_MS = 90000;
let setupClientUpdateStartedAt = 0;
let lastSetupClientStatus = null;

function _normalizeLocalUrl(raw) {
  const s = String(raw || '');
  const h = (window.location.hostname || '').toLowerCase();
  if (h === 'localhost' || h === '127.0.0.1' || h.startsWith('192.168.')) {
    return s.replace(/^https:\/\//i, 'http://');
  }
  return s;
}

function updateSetupStatusPill(el, text, mode) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status' + (mode ? ' ' + mode : '');
}

function updateSetupClientUpdateUI(data) {
  const btn = document.getElementById('setup-client-update-btn');
  const note = document.getElementById('setup-client-update-note');
  if (!btn || !note) return;
  const clientConnected = !!data.client_connected;
  const updateSupported = !!data.client_update_supported;
  const outdated = !!data.client_outdated;
  const required = !!data.client_update_required;
  const localVersion = String(data.client_version || '').trim();
  const serverVersion = String(data.server_version || '').trim();
  btn.className = 'setup-client-update-btn';
  if (setupClientUpdateInFlight) {
    btn.classList.add('busy');
    btn.disabled = true;
    btn.textContent = 'Updating...';
    note.textContent = 'Client will reconnect after restart.';
    return;
  }
  if (outdated) btn.classList.add('warn');
  btn.disabled = !clientConnected || !updateSupported || !outdated;
  if (!clientConnected) {
    btn.textContent = 'Client offline';
    note.textContent = localVersion ? ('Last seen v' + localVersion) : '';
  } else if (!updateSupported) {
    btn.textContent = 'Manual update';
    note.textContent = localVersion ? ('Client v' + localVersion) : 'Reconnect with the latest package once.';
  } else if (outdated) {
    btn.textContent = required ? 'Update required' : 'Update client';
    note.textContent = (localVersion && serverVersion) ? ('v' + localVersion + ' -> v' + serverVersion) : 'New client available';
  } else {
    btn.textContent = 'Client current';
    note.textContent = localVersion ? ('v' + localVersion) : '';
  }
  if (setupClientUpdateError) note.textContent = setupClientUpdateError;
}

async function triggerSetupClientUpdate() {
  if (setupClientUpdateInFlight) return;
  setupClientUpdateError = '';
  setupClientUpdateInFlight = true;
  setupClientUpdateSawDisconnect = false;
  setupClientUpdateStartedAt = Date.now();
  updateSetupClientUpdateUI(lastSetupClientStatus || {});
  try {
    const r = await fetch('/web/chat/update-client', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    checkAgentStatus();
  } catch(e) {
    setupClientUpdateInFlight = false;
    setupClientUpdateSawDisconnect = false;
    setupClientUpdateStartedAt = 0;
    setupClientUpdateError = e.message || 'Update failed';
    updateSetupClientUpdateUI(lastSetupClientStatus || {});
  }
}

function updateSetupAgentStatusUI(data) {
  const chatEl = document.getElementById('setup-agentstatus');
  const bridgeEl = document.getElementById('setup-bridgestatus');
  const banner = document.getElementById('setup-download-banner');
  const bannerMsg = document.getElementById('setup-banner-msg');
  const bannerDetail = document.getElementById('setup-banner-detail');
  const bannerConnect = document.getElementById('setup-banner-connect');
  const bannerCurl = document.getElementById('setup-banner-curl');

  const chatConnected = !!data.chat_connected;
  const bridgeConnected = !!data.bridge_connected;
  const mismatch = !!data.mismatch;
  if (setupClientUpdateInFlight) {
    if (!data.client_connected) setupClientUpdateSawDisconnect = true;
    else if (setupClientUpdateSawDisconnect || !data.client_outdated) {
      setupClientUpdateInFlight = false;
      setupClientUpdateSawDisconnect = false;
      setupClientUpdateStartedAt = 0;
      setupClientUpdateError = '';
    } else if (setupClientUpdateStartedAt && (Date.now() - setupClientUpdateStartedAt) >= SETUP_CLIENT_UPDATE_TIMEOUT_MS) {
      setupClientUpdateInFlight = false;
      setupClientUpdateSawDisconnect = false;
      setupClientUpdateStartedAt = 0;
      setupClientUpdateError = 'Update timed out. Check the local client logs and retry.';
    }
  }
  if (!setupClientUpdateInFlight && !data.client_outdated) setupClientUpdateError = '';
  lastSetupClientStatus = data;
  updateSetupClientUpdateUI(data);

  if (bridgeConnected) updateSetupStatusPill(bridgeEl, 'bridge online', 'online');
  else updateSetupStatusPill(bridgeEl, 'bridge offline', '');

  if (chatConnected) updateSetupStatusPill(chatEl, 'agent online', 'online');
  else if (mismatch) updateSetupStatusPill(chatEl, 'agent mismatch', 'warn');
  else updateSetupStatusPill(chatEl, 'agent offline', '');

  if (!banner) return;
  if (bannerMsg) bannerMsg.textContent = 'Your local chat agent is offline.';
  if (bannerDetail) bannerDetail.textContent = 'Start the full local agent package to enable chat and browser control.';
  if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
  if (bannerCurl) bannerCurl.textContent = (typeof _isWindows==='function'&&_isWindows()) ? 'Install (PowerShell)' : 'Install (curl)';

  if (chatConnected && bridgeConnected) {
    banner.style.display = 'none';
    return;
  }
  if (chatConnected && !bridgeConnected) {
    if (bannerMsg) bannerMsg.textContent = 'Your browser bridge is offline.';
    if (bannerDetail) bannerDetail.textContent = 'Chat is connected, but setup profile detection on this machine still needs the browser bridge.';
    banner.style.display = 'flex';
    return;
  }
  if (mismatch) {
    if (bannerMsg) bannerMsg.textContent = 'A different local chat agent is connected for this account.';
    if (bannerDetail) bannerDetail.textContent = 'Reinstall only if this machine should own the active chat agent.';
    if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
    if (bannerCurl) bannerCurl.textContent = (typeof _isWindows==='function'&&_isWindows()) ? 'Reinstall (PowerShell)' : 'Reinstall (curl)';
    banner.style.display = 'flex';
    return;
  }
  if (bridgeConnected && bannerDetail) {
    bannerDetail.textContent = 'Browser bridge is online, so setup profile detection can still work on this machine.';
  }
  banner.style.display = 'flex';
}

function providerLabel(provider) {
  if (provider === 'claude-sdk') return 'Claude API';
  if (provider === 'codex-sdk') return 'Codex API';
  if (provider === 'codex-cli') return 'Codex CLI';
  if (provider === 'gemini') return 'Gemini API';
  if (!provider) return 'Provider';
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

function providerChatUrl(provider) {
  if (provider === 'claude-sdk') return '/chat-claude';
  if (provider === 'codex-cli') return '/chat-codex?model=codex-cli:gpt-5.1-codex-mini';
  return provider === 'gemini' ? '/chat-gemini' : '/chat-codex';
}

function updateProvisionUiTexts() {
  const p = selectedProvider;
  const label = providerLabel(p);
  const title = document.getElementById('provision-step-title');
  const btn = document.getElementById('provision-btn');
  const input = document.getElementById('manual-key-input');
  const manual = document.getElementById('manual-key-section');
  if (!title || !btn || !input) return;
  const localOnly = (p === 'codex-cli');
  title.textContent = (isLocal ? 'Step 2: Provision ' : 'Step 3: Provision ') + label + ' Key';
  if (localOnly) btn.textContent = 'Use Local Codex CLI';
  else btn.textContent = 'Provision ' + label + ' Key';
  if (p === 'gemini') input.placeholder = 'AIzaSy...';
  else if (p === 'claude-sdk') input.placeholder = 'sk-ant-...';
  else input.placeholder = 'sk-...';
  if (manual) {
    if (!isLocal) manual.style.display = 'block';
    else manual.style.display = 'none';
  }
  if (localOnly) btn.disabled = false;
  else btn.disabled = (selectedProfile === undefined);
}

function onProviderChange(provider) {
  selectedProvider = provider || 'gemini';
  updateProvisionUiTexts();
}

function _providerFromQuery() {
  try {
    const p = (new URLSearchParams(window.location.search).get('provider') || '').trim().toLowerCase();
    if (p === 'gemini' || p === 'claude-sdk' || p === 'codex-sdk' || p === 'codex-cli') return p;
  } catch(e) {}
  return '';
}

async function init() {
  const sel = document.getElementById('provider-select');
  selectedProvider = sel ? sel.value : 'gemini';
  const qProvider = _providerFromQuery();
  if (qProvider) selectedProvider = qProvider;
  if (sel) sel.value = selectedProvider;
  if (isLocal) {
    document.getElementById('step-connect').style.display = 'none';
    document.getElementById('step-profiles').style.display = 'block';
    document.getElementById('local-mode-hint').style.display = 'block';
    document.getElementById('manual-key-section').style.display = 'none';
    updateProvisionUiTexts();
    await Promise.all([loadProfiles(), checkAgentStatus(), loadProvisionStatus()]);
  } else {
    document.getElementById('step-profiles').style.display = 'none';
    document.getElementById('step-connect').style.display = 'block';
    document.getElementById('local-mode-hint').style.display = 'none';
    document.getElementById('relay-mode-hint').style.display = 'block';
    document.getElementById('manual-key-section').style.display = 'block';
    updateProvisionUiTexts();
    await Promise.all([loadInstallCmd(), checkAgentStatus(), loadProvisionStatus()]);
  }
  if (!statusPollTimer) statusPollTimer = setInterval(checkAgentStatus, 5000);
}

/* --- Connect step (production) --- */
async function loadInstallCmd() {
  const cmdEl = document.getElementById('setup-install-cmd');
  const isWin = _isWindows();
  const descEl = document.getElementById('setup-connect-desc');
  const noteEl = document.getElementById('setup-connect-note');
  if (descEl) descEl.textContent = isWin ? 'Run this in PowerShell:' : 'Run this in your terminal:';
  if (noteEl) noteEl.textContent = isWin ? 'Requires Python 3.8+. Link expires in 15 minutes.' : 'Requires Python 3.8+ and curl. Link expires in 15 minutes.';
  cmdEl.textContent = 'Generating link...';
  try {
    const r = await fetch('/trial/token', {method: 'POST'});
    if (!r.ok) { cmdEl.textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    cmdEl.textContent = isWin ? data.powershell_command : data.curl_command;
  } catch(e) {
    cmdEl.textContent = 'Error: ' + e.message;
  }
}

async function showSetupInstallCmd() {
  const isWin = _isWindows();
  const modal = document.getElementById('setup-install-modal');
  const cmdEl = document.getElementById('setup-install-curl-cmd');
  modal.style.display = 'flex';
  cmdEl.textContent = 'Generating install command...';
  try {
    const r = await fetch('/web/install-token', {method: 'POST'});
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      cmdEl.textContent = 'Error: ' + (err.error || 'Failed to generate install command');
      return;
    }
    const data = await r.json();
    cmdEl.textContent = _normalizeLocalUrl(isWin ? (data.powershell_command || '') : (data.curl_command || '')) || 'No install command available.';
  } catch(e) {
    cmdEl.textContent = 'Error: ' + e.message;
  }
}

function closeSetupInstallModal() {
  document.getElementById('setup-install-modal').style.display = 'none';
}

function copySetupInstallCmd(btn) {
  const cmd = document.getElementById('setup-install-curl-cmd').textContent || '';
  const done = () => {
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 2000);
  };
  navigator.clipboard.writeText(cmd).then(done).catch(() => {
    try {
      const ta = document.createElement('textarea');
      ta.value = cmd;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    } catch(e) {}
  });
}

let profilesLoaded = false;
async function checkAgentStatus() {
  try {
    const r = await fetch('/web/chat/status');
    if (!r.ok) return;
    const data = await r.json();
    updateSetupAgentStatusUI(data);
    const wasConnected = agentConnected;
    agentConnected = data.connected;
    const dot = document.getElementById('agent-dot');
    const label = document.getElementById('agent-label');
    if (agentConnected) {
      if (dot) dot.classList.add('online');
      if (label) {
        label.classList.add('online');
        label.textContent = 'Agent Online';
      }
      // Once agent connects, load profiles from user's machine via relay
      if (!isLocal && !profilesLoaded) {
        profilesLoaded = true;
        document.getElementById('step-profiles').style.display = 'block';
        document.getElementById('profile-step-title').textContent = 'Step 2: Choose Chrome Profile';
        updateProvisionUiTexts();
        await loadProfiles();
      }
      // Enable provision button based on selected provider requirements.
      document.getElementById('provision-btn').disabled = (
        selectedProvider === 'codex-cli' ? false : (selectedProfile === undefined)
      );
    } else {
      if (dot) dot.classList.remove('online');
      if (label) {
        label.classList.remove('online');
        label.textContent = 'Agent Offline';
      }
      if (!isLocal && selectedProvider !== 'codex-cli') {
        document.getElementById('provision-btn').disabled = true;
      }
    }
  } catch(e) {}
}

function copySetupCmd(btn) {
  const cmd = document.getElementById('setup-install-cmd').textContent;
  try {
    const ta = document.createElement('textarea');
    ta.value = cmd;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 2000);
  } catch(e) {
    navigator.clipboard.writeText(cmd).then(() => {
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = 'Copy', 2000);
    });
  }
}

/* --- Step 1: Profiles --- */
async function loadProfiles() {
  const container = document.getElementById('profile-list');
  try {
    const r = await fetch('/web/provision/profiles');
    if (!r.ok) { container.innerHTML = '<div class="profiles-loading" style="color:var(--red)">Failed to load profiles</div>'; return; }
    const {profiles} = await r.json();
    renderProfiles(profiles);
  } catch(e) {
    container.innerHTML = '<div class="profiles-loading" style="color:var(--red)">Failed to load profiles</div>';
  }
}

function renderProfiles(profiles) {
  const container = document.getElementById('profile-list');
  let html = '';

  profiles.forEach((p, i) => {
    const name = p.full_name || p.name || 'Profile';
    const email = p.email || '';
    const path = p.profile_path || p.path || '';
    html += '<label class="profile-option" data-path="' + escHtml(path) + '" onclick="selectProfile(this)">';
    html += '<input type="radio" name="profile" value="' + escHtml(path) + '">';
    html += '<div class="profile-info">';
    html += '<span class="profile-name">' + escHtml(name) + '</span>';
    if (email) html += '<span class="profile-email">' + escHtml(email) + '</span>';
    html += '</div></label>';
  });

  // Clean profile option
  html += '<label class="profile-option" data-path="" onclick="selectProfile(this)">';
  html += '<input type="radio" name="profile" value="">';
  html += '<div class="profile-info">';
  html += '<span class="profile-name">Use clean profile</span>';
  html += '<span class="profile-hint">(will need Google sign-in)</span>';
  html += '</div></label>';

  container.innerHTML = html;
}

function selectProfile(el) {
  document.querySelectorAll('.profile-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  el.querySelector('input[type="radio"]').checked = true;
  const path = el.dataset.path;
  selectedProfile = path || null;
  document.getElementById('provision-btn').disabled = false;
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* --- Step 2: Provision --- */
async function startProvision() {
  const btn = document.getElementById('provision-btn');
  const statusEl = document.getElementById('provision-status');
  const pLabel = providerLabel(selectedProvider);
  if (selectedProvider === 'codex-cli') {
    statusEl.className = 'provision-status done';
    statusEl.innerHTML = 'Codex CLI is local-only. Redirecting to local chat...';
    setTimeout(() => { window.location.href = providerChatUrl('codex-cli'); }, 400);
    return;
  }
  btn.disabled = true;
  statusEl.className = 'provision-status running';
  statusEl.innerHTML = '<span class="spinner"></span>Chrome is opening... Accept the Terms of Service if prompted.';

  try {
    const body = {provider: selectedProvider};
    if (selectedProfile) body.profile_path = selectedProfile;
    if (!isLocal) {
      body.use_relay = true;
    }

    const r = await fetch('/web/provision/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();

    if (data.status === 'success' && data.key_preview) {
      statusEl.className = 'provision-status done';
      statusEl.innerHTML = 'Key provisioned: <code style="background:rgba(255,255,255,0.1);padding:2px 6px;border-radius:4px">' + escHtml(data.key_preview) + '</code><br>' +
        'Store this key to power your ' + escHtml(pLabel) + ' chat sessions?' +
        '<div style="margin-top:10px;display:flex;gap:8px">' +
        '<button onclick="confirmStoreKey()" style="padding:8px 16px;border:none;border-radius:6px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer">Store Key</button>' +
        '<button onclick="discardKey()" style="padding:8px 16px;border:none;border-radius:6px;background:var(--surface);color:var(--muted);cursor:pointer">Discard</button>' +
        '</div>';
      showToast('Key ready — confirm to store', false);
    } else if (data.status === 'success' || data.status === 'already_exists') {
      const resolvedProvider = data.provider || selectedProvider;
      const chatUrl = data.chat_url || providerChatUrl(resolvedProvider);
      const chatLabel = providerLabel(resolvedProvider);
      statusEl.className = 'provision-status done';
      statusEl.innerHTML = (data.message || 'Key already exists.') +
        ' <a href="' + escHtml(chatUrl) + '" style="color:var(--accent)">Start chatting with ' + escHtml(chatLabel) + ' &rarr;</a>';
      showToast('Key provisioned', false);
    } else if (data.status === 'not_signed_in') {
      statusEl.className = 'provision-status error';
      statusEl.textContent = data.message || ('Not signed in for ' + pLabel + ' in that profile. Try a different profile.');
      showToast('Not signed in — try another profile', true);
    } else if (data.status === 'tos_required') {
      statusEl.className = 'provision-status error';
      statusEl.textContent = data.message || 'Please accept the Terms of Service in Chrome, then try again.';
      showToast('ToS required', true);
    } else {
      statusEl.className = 'provision-status error';
      statusEl.textContent = data.message || 'Provisioning failed.';
      showToast(data.message || 'Provisioning failed', true);
    }
  } catch(e) {
    statusEl.className = 'provision-status error';
    statusEl.textContent = 'Network error: ' + e.message;
    showToast('Network error', true);
  }

  btn.disabled = (selectedProvider === 'codex-cli') ? false : (selectedProfile === undefined);
  await loadProvisionStatus();
}

async function confirmStoreKey() {
  const statusEl = document.getElementById('provision-status');
  statusEl.innerHTML = '<span class="spinner"></span>Storing key...';
  try {
    const r = await fetch('/web/provision/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    const data = await r.json();
    if (data.status === 'success') {
      const resolvedProvider = data.provider || selectedProvider;
      const chatUrl = data.chat_url || providerChatUrl(resolvedProvider);
      const chatLabel = providerLabel(resolvedProvider);
      statusEl.className = 'provision-status done';
      statusEl.innerHTML = (data.message || 'Key stored.') +
        ' <a href="' + escHtml(chatUrl) + '" style="color:var(--accent)">Start chatting with ' + escHtml(chatLabel) + ' &rarr;</a>';
      showToast('Key stored', false);
    } else {
      statusEl.className = 'provision-status error';
      statusEl.textContent = data.error || 'Failed to store key.';
      showToast(data.error || 'Store failed', true);
    }
  } catch(e) {
    statusEl.className = 'provision-status error';
    statusEl.textContent = 'Network error: ' + e.message;
    showToast('Network error', true);
  }
  await loadProvisionStatus();
}

async function saveManualKey() {
  const input = document.getElementById('manual-key-input');
  const key = input.value.trim();
  if (!key) { showToast('Please paste a key first', true); return; }
  const pLabel = providerLabel(selectedProvider);
  const statusEl = document.getElementById('provision-status');
  statusEl.className = 'provision-status running';
  statusEl.innerHTML = '<span class="spinner"></span>Saving key...';
  try {
    const r = await fetch('/web/provision/save-manual', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: selectedProvider, api_key: key}),
    });
    const data = await r.json();
    if (data.status === 'success') {
      const chatUrl = data.chat_url || providerChatUrl(selectedProvider);
      statusEl.className = 'provision-status done';
      statusEl.innerHTML = (data.message || 'Key saved.') +
        ' <a href="' + escHtml(chatUrl) + '" style="color:var(--accent)">Start chatting with ' + escHtml(pLabel) + ' &rarr;</a>';
      showToast('Key saved', false);
      input.value = '';
    } else {
      statusEl.className = 'provision-status error';
      statusEl.textContent = data.error || 'Failed to save key.';
      showToast(data.error || 'Save failed', true);
    }
  } catch(e) {
    statusEl.className = 'provision-status error';
    statusEl.textContent = 'Network error: ' + e.message;
    showToast('Network error', true);
  }
  await loadProvisionStatus();
}

function discardKey() {
  const statusEl = document.getElementById('provision-status');
  const btn = document.getElementById('provision-btn');
  statusEl.className = 'provision-status';
  statusEl.textContent = 'Key discarded. You can provision again or paste a key manually.';
  btn.disabled = (selectedProvider === 'codex-cli') ? false : (selectedProfile === undefined);
}

/* --- Status --- */
async function loadProvisionStatus() {
  const container = document.getElementById('status-list');
  try {
    const r = await fetch('/web/provision/status');
    if (!r.ok) { container.innerHTML = '<div class="profiles-loading" style="color:var(--red)">Failed to load status</div>'; return; }
    const {providers} = await r.json();
    renderStatus(providers);
  } catch(e) {
    container.innerHTML = '<div class="profiles-loading" style="color:var(--red)">Failed to load status</div>';
  }
}

function renderStatus(providers) {
  const container = document.getElementById('status-list');
  if (!providers || providers.length === 0) {
    container.innerHTML = '<div style="color:var(--muted);font-size:13px">No providers configured.</div>';
    return;
  }

  let html = '';
  providers.forEach(p => {
    html += '<div class="key-row">';
    html += '<div class="key-info">';
    html += '<span class="key-provider">' + escHtml(providerLabel(p.name)) + '</span>';
    if (p.provisioned && p.key_preview) {
      html += '<span class="key-preview">' + escHtml(p.key_preview) + '</span>';
    }
    html += '</div>';
    if (p.provisioned) {
      html += '<div style="display:flex;align-items:center;gap:10px">';
      html += '<span class="key-status provisioned">Provisioned</span>';
      html += '<button class="revoke-btn" onclick="reprovisionKey(\'' + escHtml(p.name) + '\')">Change Profile</button>';
      html += '<button class="revoke-btn" onclick="revokeKey(\'' + escHtml(p.name) + '\')" style="opacity:0.6">Revoke</button>';
      html += '</div>';
    } else {
      html += '<span class="key-status none">Not provisioned</span>';
    }
    html += '</div>';
  });
  container.innerHTML = html;
}

/* --- Revoke --- */
async function revokeKey(provider) {
  if (!confirm('Revoke ' + provider + ' API key? This cannot be undone.')) return;
  await _doRevoke(provider);
}

async function reprovisionKey(provider) {
  if (!confirm('Revoke current ' + provider + ' key and provision a new one with a different Chrome profile?')) return;
  await _doRevoke(provider);
  selectedProvider = provider || 'gemini';
  const sel = document.getElementById('provider-select');
  if (sel) sel.value = selectedProvider;
  updateProvisionUiTexts();
  // Reset provision UI — show profile picker for both local and relay modes
  selectedProfile = undefined;
  document.getElementById('provision-btn').disabled = (selectedProvider === 'codex-cli') ? false : true;
  document.getElementById('provision-status').innerHTML = '';
  document.getElementById('provision-status').className = 'provision-status';
  document.getElementById('step-profiles').style.display = 'block';
  await loadProfiles();
  await loadProvisionStatus();
  showToast('Select a Chrome profile and provision again', false);
}

async function _doRevoke(provider) {
  try {
    const r = await fetch('/web/provision/revoke', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: provider}),
    });
    const data = await r.json();
    if (data.revoked) {
      showToast(provider + ' key revoked', false);
    } else {
      showToast('No key to revoke', true);
    }
  } catch(e) {
    showToast('Network error', true);
  }
  await loadProvisionStatus();
}

/* --- Dev auth --- */
const hasGoogleOAuth = !!'__GOOGLE_CLIENT_ID__';
if (!hasGoogleOAuth) {
  document.getElementById('google-auth').style.display = 'none';
  document.getElementById('dev-auth').style.display = 'block';
}

async function devLogin() {
  const errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    const r = await fetch('/auth/dev', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: 'dev@localhost', name: 'Dev User'}),
    });
    const data = await r.json();
    if (!r.ok) { errEl.textContent = data.error || 'Dev login failed'; return; }
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

checkSession();
</script>
</body>
</html>
"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0a;--surface:#111;--border:#222;--text:#e8e8e8;--muted:#666;--accent:#60a5fa;--green:#4ade80;--red:#f87171;--yellow:#fbbf24;--mono:'JetBrains Mono','Fira Code',monospace}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;min-height:100dvh}
header{display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--border);background:var(--surface)}
header h1{font-size:15px;font-weight:600;letter-spacing:0.03em}
header .subtitle{color:var(--muted);font-size:11px}
header .spacer{flex:1}
header .badge{background:#1a1a1a;border:1px solid var(--border);padding:3px 10px;border-radius:4px;font-size:11px;color:var(--muted)}
.tabs{display:flex;gap:0;padding:12px 24px 0;border-bottom:1px solid var(--border)}
.tab{padding:6px 16px;font-size:12px;font-family:var(--mono);cursor:pointer;border:none;background:none;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab:hover:not(.active){color:var(--text)}
.count-badge{display:inline-block;background:#1e1e1e;border-radius:10px;padding:1px 6px;font-size:10px;margin-left:4px;color:var(--muted)}
.tab.active .count-badge{color:var(--accent)}
.content{padding:16px 24px}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{text-align:left;padding:8px 12px;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);white-space:nowrap}
tbody tr{border-bottom:1px solid #181818;transition:background 0.1s}
tbody tr:hover{background:#111}
tbody td{padding:10px 12px;vertical-align:middle}
.avatar{width:28px;height:28px;border-radius:50%;object-fit:cover;background:#222}
.avatar-placeholder{width:28px;height:28px;border-radius:50%;background:#2a2a2a;display:inline-flex;align-items:center;justify-content:center;font-size:12px;color:var(--muted)}
.status-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:500}
.status-pill.pending{background:#2d2000;color:var(--yellow);border:1px solid #443300}
.status-pill.approved{background:#0d2d1a;color:var(--green);border:1px solid #1a4a2a}
.status-pill.rejected{background:#2d0d0d;color:var(--red);border:1px solid #4a1a1a}
.actions{display:flex;gap:6px}
.btn{padding:4px 12px;border-radius:4px;font-size:11px;font-family:var(--mono);cursor:pointer;border:1px solid;font-weight:500;transition:opacity 0.15s}
.btn:hover{opacity:0.8}
.btn:disabled{opacity:0.3;cursor:not-allowed}
.btn-approve{background:#0d2d1a;color:var(--green);border-color:#1a4a2a}
.btn-reject{background:#2d0d0d;color:var(--red);border-color:#4a1a1a}
.email{color:var(--text)}
.name{color:var(--muted);font-size:11px}
.ts{color:var(--muted);font-size:11px;white-space:nowrap}
.empty{padding:48px;text-align:center;color:var(--muted)}
.refresh{display:flex;align-items:center;gap:8px;padding:8px 0 12px;font-size:11px;color:var(--muted)}
.refresh-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;border-radius:6px;font-size:12px;font-family:var(--mono);z-index:999;animation:fadeIn 0.2s ease-out}
.toast.ok{background:#0d2d1a;color:var(--green);border:1px solid #1a4a2a}
.toast.err{background:#2d0d0d;color:var(--red);border:1px solid #4a1a1a}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
#error-msg{padding:40px;text-align:center;color:var(--red)}
</style>
</head>
<body>
<header>
  <h1>⚡ Admin Panel</h1>
  <span class="subtitle">User management</span>
  <span class="spacer"></span>
  <span class="badge" id="admin-email">loading…</span>
</header>
<div class="tabs">
  <button class="tab active" data-filter="all">All <span class="count-badge" id="cnt-all">0</span></button>
  <button class="tab" data-filter="pending">Pending <span class="count-badge" id="cnt-pending">0</span></button>
  <button class="tab" data-filter="approved">Approved <span class="count-badge" id="cnt-approved">0</span></button>
  <button class="tab" data-filter="rejected">Rejected <span class="count-badge" id="cnt-rejected">0</span></button>
</div>
<div class="content">
  <div class="refresh"><span class="refresh-dot"></span><span id="refresh-status">Loading…</span></div>
  <div id="error-msg" style="display:none"></div>
  <table id="users-table" style="display:none">
    <thead><tr>
      <th></th><th>Email</th><th>Name</th><th>Type</th><th>Status</th><th>Signed Up</th><th>Last Login</th><th>OR Spend</th><th>OR Remaining</th><th>Actions</th>
    </tr></thead>
    <tbody id="users-body"></tbody>
  </table>
  <div id="empty-msg" class="empty" style="display:none">No users in this view.</div>
</div>
<script>
let _allUsers = [];
let _filter = 'all';
let _adminEmail = '';

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = (now - d.getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  if (diff < 86400*7) return Math.floor(diff/86400) + 'd ago';
  return d.toLocaleDateString();
}

function showToast(msg, isErr) {
  const t = document.createElement('div');
  t.className = 'toast ' + (isErr ? 'err' : 'ok');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

async function doAction(email, action) {
  const url = '/admin/' + action;
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({email})
    });
    const data = await r.json();
    if (!r.ok) { showToast(data.error || 'Error', true); return; }
    showToast(action === 'approve' ? '✓ Approved ' + email : '✓ Rejected ' + email, false);
    await loadUsers();
  } catch(e) { showToast('Network error', true); }
}

function renderTable() {
  const filtered = _filter === 'all' ? _allUsers : _allUsers.filter(u => u.status === _filter);
  const tbody = document.getElementById('users-body');
  const table = document.getElementById('users-table');
  const empty = document.getElementById('empty-msg');

  // Update counts
  document.getElementById('cnt-all').textContent = _allUsers.length;
  document.getElementById('cnt-pending').textContent = _allUsers.filter(u=>u.status==='pending').length;
  document.getElementById('cnt-approved').textContent = _allUsers.filter(u=>u.status==='approved').length;
  document.getElementById('cnt-rejected').textContent = _allUsers.filter(u=>u.status==='rejected').length;

  if (filtered.length === 0) {
    table.style.display = 'none';
    empty.style.display = 'block';
    return;
  }
  table.style.display = 'table';
  empty.style.display = 'none';

  tbody.innerHTML = filtered.map(u => {
    const pillCls = 'status-pill ' + (u.status || 'approved');
    const pillLabel = u.status === 'pending' ? '● pending' : u.status === 'approved' ? '✓ approved' : '✗ rejected';
    const typePill = u.user_type === 'trial'
      ? '<span class="status-pill" style="background:#1a1a2d;color:#a78bfa;border:1px solid #2d2d4a">trial</span>'
      : '<span class="status-pill" style="background:#0d2d1a;color:#4ade80;border:1px solid #1a4a2a">claude</span>';
    const avatarHtml = u.picture
      ? '<img class="avatar" src="' + esc(u.picture) + '" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">'
      : '<span class="avatar-placeholder">' + esc((u.name||u.email||'?')[0].toUpperCase()) + '</span>';
    const spendUsd = Number(u.openrouter_spend_usd || 0);
    const budgetUsd = Number(u.openrouter_budget_usd || 0);
    const spendLabel = budgetUsd > 0
      ? ('$' + spendUsd.toFixed(4) + ' / $' + budgetUsd.toFixed(2))
      : ('$' + spendUsd.toFixed(4));
    const remainingUsd = Math.max(0, budgetUsd - spendUsd);
    const remainingLabel = budgetUsd > 0
      ? ('$' + remainingUsd.toFixed(4))
      : '—';
    const canApprove = u.status !== 'approved';
    const canReject = u.status !== 'rejected';
    const approveBtn = canApprove
      ? '<button class="btn btn-approve" onclick="doAction(\'' + esc(u.email) + '\',\'approve\')">Approve</button>'
      : '<button class="btn btn-approve" disabled>Approve</button>';
    const rejectBtn = canReject
      ? '<button class="btn btn-reject" onclick="doAction(\'' + esc(u.email) + '\',\'reject\')">Reject</button>'
      : '<button class="btn btn-reject" disabled>Reject</button>';
    return '<tr>' +
      '<td>' + avatarHtml + '</td>' +
      '<td class="email">' + esc(u.email) + '</td>' +
      '<td class="name">' + esc(u.name || '—') + '</td>' +
      '<td>' + typePill + '</td>' +
      '<td><span class="' + pillCls + '">' + pillLabel + '</span></td>' +
      '<td class="ts">' + fmtTs(u.created_at) + '</td>' +
      '<td class="ts">' + fmtTs(u.last_login_at) + '</td>' +
      '<td class="ts">' + spendLabel + '</td>' +
      '<td class="ts">' + remainingLabel + '</td>' +
      '<td><div class="actions">' + approveBtn + rejectBtn + '</div></td>' +
      '</tr>';
  }).join('');
}

async function loadUsers() {
  try {
    const r = await fetch('/admin/users');
    if (r.status === 403) {
      document.getElementById('error-msg').style.display = 'block';
      document.getElementById('error-msg').textContent = 'Access denied. You must be an admin to view this page.';
      document.getElementById('refresh-status').textContent = 'Access denied';
      return;
    }
    const data = await r.json();
    _allUsers = data.users || [];
    renderTable();
    document.getElementById('refresh-status').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('refresh-status').textContent = 'Error: ' + e.message;
  }
}

async function loadAdminEmail() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.email) {
      _adminEmail = data.email;
      document.getElementById('admin-email').textContent = data.email;
    }
  } catch(e) {}
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    _filter = tab.dataset.filter;
    renderTable();
  });
});

// Auto-refresh every 30s
loadAdminEmail();
loadUsers();
setInterval(loadUsers, 30000);
</script>
</body>
</html>"""


SCHEDULER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scheduled Tasks — Unchained</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#1a1a2e;--surface:#16213e;--accent:#e94560;
  --text:#eee;--muted:#888;--border:#333;
  --green:#4caf50;--red:#ef5350;--warn:#fbbf24;
  --radius:12px;
}
body{background:var(--bg);color:var(--text);font-family:-apple-system,system-ui,sans-serif;font-size:14px;min-height:100dvh}

/* Topbar */
#topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .title{font-size:16px;font-weight:600}
#topbar .badge{font-size:11px;color:var(--muted);border:1px solid var(--border);padding:3px 8px;border-radius:999px}
#topbar .nav{display:flex;gap:8px}
#topbar .nav a{color:var(--muted);text-decoration:none;font-size:12px;border:1px solid #555;padding:4px 10px;border-radius:6px}
#topbar .nav a:hover{border-color:var(--accent);color:var(--accent)}

/* Main container */
main{max-width:680px;margin:0 auto;padding:20px 16px}

/* Header bar */
.header-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.header-bar .count{font-size:13px;color:var(--muted)}
.btn{border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity 0.15s}
.btn:active{opacity:0.8}
.btn-accent{background:var(--accent);color:#fff}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--muted);padding:6px 12px;font-size:12px}
.btn-ghost:hover{color:var(--text);border-color:#555}
.btn-danger{background:transparent;border:1px solid var(--red);color:var(--red);padding:6px 12px;font-size:12px}
.btn-danger:hover{background:rgba(239,83,80,0.1)}

/* Status toast */
#toast{position:fixed;top:16px;right:16px;padding:10px 18px;border-radius:8px;font-size:13px;z-index:1100;opacity:0;transition:opacity 0.3s;pointer-events:none}
#toast.show{opacity:1}
#toast.ok{background:#1b4332;color:#95d5b2;border:1px solid #2d6a4f}
#toast.err{background:#4a1c1c;color:#fca5a5;border:1px solid #7f1d1d}

/* Job cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:12px;transition:border-color 0.15s}
.card:hover{border-color:#555}
.card-top{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.card-top .name{font-weight:600;font-size:15px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-top .actions{display:flex;gap:6px;flex-shrink:0}
.card-top .actions button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:4px 8px;border-radius:4px}
.card-top .actions button:hover{color:var(--text);background:rgba(255,255,255,0.06)}
.card-top .actions .del-btn:hover{color:var(--red)}
.card-prompt{color:var(--muted);font-size:13px;line-height:1.4;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-schedule{font-size:12px;color:var(--accent);margin-bottom:6px}
.card-meta{display:flex;gap:12px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.card-meta .status-ok{color:var(--green)}
.card-meta .status-fail{color:var(--red)}
.card-output{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);font-size:12px;color:#cfd5e6;line-height:1.5;white-space:pre-wrap}
.card-output .label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.04em}
.card.disabled{opacity:0.5}

/* Toggle switch */
.toggle{position:relative;width:36px;height:20px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle .slider{position:absolute;inset:0;background:#444;border-radius:10px;cursor:pointer;transition:background 0.2s}
.toggle .slider::before{content:'';position:absolute;width:16px;height:16px;left:2px;top:2px;background:#fff;border-radius:50%;transition:transform 0.2s}
.toggle input:checked+.slider{background:var(--green)}
.toggle input:checked+.slider::before{transform:translateX(16px)}

/* Empty state */
.empty{text-align:center;padding:48px 16px;color:var(--muted)}
.empty .icon{font-size:40px;margin-bottom:12px;opacity:0.4}
.empty p{margin-bottom:16px;font-size:14px;line-height:1.5}

/* Advanced section */
.advanced{margin-top:24px;border-top:1px solid var(--border);padding-top:16px}
.advanced summary{color:var(--muted);font-size:12px;cursor:pointer;user-select:none}
.advanced summary:hover{color:var(--text)}
.advanced .adv-btns{display:flex;gap:8px;margin-top:12px}

/* Modal overlay */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;align-items:center;justify-content:center;padding:16px}
.modal-overlay.open{display:flex}
.modal{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);width:100%;max-width:520px;max-height:90dvh;overflow-y:auto;padding:24px}
.modal h2{font-size:18px;font-weight:600;margin-bottom:20px}
.field{margin-bottom:16px}
.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;font-weight:500}
.field input[type="text"],.field input[type="number"],.field textarea,.field input[type="datetime-local"],.field select{
  width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);
  border-radius:8px;padding:10px 12px;font-size:14px;font-family:inherit;
}
.field input:focus,.field textarea:focus,.field select:focus{outline:none;border-color:var(--accent)}
.field textarea{min-height:80px;resize:vertical;line-height:1.5}
.field .hint{font-size:11px;color:var(--muted);margin-top:4px}

/* Radio schedule options */
.schedule-options{display:flex;flex-direction:column;gap:10px}
.sched-option{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;cursor:pointer}
.sched-option:has(input:checked){border-color:var(--accent);background:rgba(233,69,96,0.08)}
.sched-option input[type="radio"]{accent-color:var(--accent);margin:0}
.sched-option .sched-label{font-size:13px;flex:1}
.sched-option .sched-inputs{display:flex;align-items:center;gap:4px}
.sched-option .sched-inputs input{width:60px;padding:6px 8px;font-size:13px;text-align:center;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px}
.sched-option .sched-inputs span{color:var(--muted);font-size:12px}

/* Advanced settings in modal */
.adv-fields summary{color:var(--muted);font-size:12px;cursor:pointer;margin-bottom:10px}
.adv-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.adv-grid .field{margin-bottom:0}
.check-field{display:flex;align-items:center;gap:8px;padding:8px 0}
.check-field input[type="checkbox"]{accent-color:var(--accent);width:16px;height:16px}
.check-field label{font-size:13px;color:var(--text);margin-bottom:0}

/* Modal footer */
.modal-footer{display:flex;justify-content:flex-end;gap:8px;margin-top:20px;padding-top:16px;border-top:1px solid var(--border)}

/* Import modal */
.import-area textarea{width:100%;min-height:160px;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px 12px;font-family:'SF Mono','Menlo',monospace;font-size:12px;resize:vertical;line-height:1.5}
.history-list{display:flex;flex-direction:column;gap:10px}
.history-item{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px}
.history-item summary{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;list-style:none}
.history-item summary::-webkit-details-marker{display:none}
.history-left{display:flex;align-items:center;gap:8px;min-width:0}
.history-status{display:inline-flex;align-items:center;border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600}
.history-status.ok{background:rgba(76,175,80,0.14);color:#95d5b2}
.history-status.err{background:rgba(239,83,80,0.14);color:#fca5a5}
.history-ts{font-size:12px;color:var(--muted)}
.history-len{font-size:11px;color:var(--muted);flex-shrink:0}
.history-detail{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);font-size:12px;line-height:1.5;color:#dbe3f4;white-space:pre-wrap;word-break:break-word}
.history-empty{font-size:13px;color:var(--muted);padding:12px 0}

@media (max-width:480px){
  .sched-option{flex-wrap:wrap}
  .sched-option .sched-inputs{width:100%;margin-top:4px}
  .adv-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>

<div id="topbar">
  <div class="left">
    <span class="title">Scheduled Tasks</span>
    <span class="badge" id="whoami">checking session…</span>
  </div>
  <nav class="nav">
    <a href="/app">Chat</a>
    <a href="/setup">Setup</a>
  </nav>
</div>

<main>
  <div class="header-bar">
    <span class="count" id="job-count">0 tasks</span>
    <button class="btn btn-accent" onclick="openAddModal()">+ New Task</button>
  </div>

  <div id="job-list"></div>

  <details class="advanced">
    <summary>Advanced</summary>
    <div class="adv-btns">
      <button class="btn-ghost" onclick="downloadJobs()">Download JSON</button>
      <button class="btn-ghost" onclick="openImportModal()">Import JSON</button>
    </div>
  </details>
</main>

<div id="toast"></div>

<!-- Add/Edit Modal -->
<div class="modal-overlay" id="job-modal">
  <div class="modal">
    <h2 id="modal-title">New Task</h2>

    <div class="field">
      <label>Task Name</label>
      <input type="text" id="f-name" placeholder="e.g. Daily Summary">
    </div>

    <div class="field">
      <label>Prompt</label>
      <textarea id="f-prompt" placeholder="What should the agent do?"></textarea>
    </div>

    <div class="field">
      <label>Schedule</label>
      <div class="schedule-options">
        <label class="sched-option">
          <input type="radio" name="sched-type" value="daily" checked>
          <span class="sched-label">Daily at</span>
          <div class="sched-inputs">
            <input type="number" id="f-hour" min="0" max="23" value="9" placeholder="HH">
            <span>:</span>
            <input type="number" id="f-min" min="0" max="59" value="0" placeholder="MM">
            <span>UTC</span>
          </div>
        </label>
        <label class="sched-option">
          <input type="radio" name="sched-type" value="minutes">
          <span class="sched-label">Every</span>
          <div class="sched-inputs">
            <input type="number" id="f-interval" min="1" max="1440" value="30">
            <span>minutes</span>
          </div>
        </label>
        <label class="sched-option">
          <input type="radio" name="sched-type" value="once">
          <span class="sched-label">One time at</span>
          <div class="sched-inputs">
            <input type="datetime-local" id="f-once" style="width:auto">
          </div>
        </label>
      </div>
    </div>

    <details class="adv-fields">
      <summary>Advanced Settings</summary>
      <div class="field" style="margin-top:10px">
        <label for="f-model">Model</label>
        <select id="f-model" onchange="syncSchedulerModelField()">
          <option value="">Default: Claude CLI Opus</option>
          <option value="claude-sonnet-4-6">Claude CLI: Sonnet 4.6</option>
          <option value="claude-opus-4-6">Claude CLI: Opus 4.6</option>
          <option value="claude-haiku-4-5-20251001">Claude CLI: Haiku 4.5</option>
          <option value="claude-sdk:claude-sonnet-4-6">Claude API: Sonnet 4.6</option>
          <option value="claude-sdk:claude-opus-4-6">Claude API: Opus 4.6</option>
          <option value="claude-sdk:claude-haiku-4-5-20251001">Claude API: Haiku 4.5</option>
          <option value="codex-cli:gpt-5.1-codex-mini">Codex CLI: GPT-5.1 Codex Mini</option>
          <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
          <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
          <option value="arcee-ai/trinity-large-preview:free">OpenRouter: Trinity Fast</option>
          <option value="stepfun/step-3.5-flash:free">OpenRouter: StepFun 3.5 Flash</option>
          <option value="__custom__">Custom model ID</option>
        </select>
        <span class="hint">Leave on default to use your normal local Claude CLI agent. Choose Custom for any raw model ID.</span>
      </div>
      <div class="field" id="f-model-custom-wrap" style="display:none">
        <label for="f-model-custom">Custom Model ID</label>
        <input type="text" id="f-model-custom" placeholder="e.g. anthropic/claude-sonnet-4.5 or codex-cli:gpt-5.1-codex">
      </div>
      <div class="field" style="margin-top:10px">
        <label for="f-profile">Chrome Profile</label>
        <select id="f-profile" title="Optional profile copy to avoid re-sign-in prompts">
          <option value="">Current browser (no profile copy)</option>
        </select>
        <span class="hint">Optional. Reuse a signed-in Chrome profile from your local chat agent.</span>
      </div>
      <div class="adv-grid" style="margin-top:10px">
        <div class="field">
          <label>Timeout (seconds)</label>
          <input type="number" id="f-timeout" min="10" max="3600" value="180">
        </div>
        <div class="field">
          <label>Retry after (seconds)</label>
          <input type="number" id="f-retry" min="0" max="3600" value="0">
          <span class="hint">0 = no retry</span>
        </div>
      </div>
      <div class="check-field">
        <input type="checkbox" id="f-headless">
        <label for="f-headless">Use headless browser</label>
      </div>
      <div class="check-field">
        <input type="checkbox" id="f-session">
        <label for="f-session">Keep same session</label>
      </div>
    </details>

    <div class="modal-footer">
      <button class="btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveModal()">Save Task</button>
    </div>
  </div>
</div>

<!-- Import JSON Modal -->
<div class="modal-overlay" id="import-modal">
  <div class="modal">
    <h2>Import JSON</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:12px">Paste a scheduled_jobs.json file. This will replace all current tasks.</p>
    <div class="import-area">
      <textarea id="import-text" placeholder='{"jobs": [...]}'></textarea>
    </div>
    <div class="modal-footer">
      <button class="btn-ghost" onclick="closeImportModal()">Cancel</button>
      <button class="btn btn-accent" onclick="doImport()">Import</button>
    </div>
  </div>
</div>

<!-- History Modal -->
<div class="modal-overlay" id="history-modal">
  <div class="modal">
    <h2 id="history-title">Run History</h2>
    <div id="history-list" class="history-list">
      <div class="history-empty">Loading…</div>
    </div>
    <div class="modal-footer">
      <button class="btn-ghost" onclick="closeHistoryModal()">Close</button>
    </div>
  </div>
</div>

<script>
// ── State ──
let jobs = [];
let preview = {};
let editingIndex = -1;
let historyJobId = '';
const schedulerProfileLabels = {};

// ── Helpers ──
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function toast(msg, kind='ok'){
  const el=document.getElementById('toast');
  el.textContent=msg; el.className=kind+' show';
  clearTimeout(el._t);
  el._t=setTimeout(()=>el.classList.remove('show'),3000);
}

function scheduleToText(s){
  if(!s) return 'No schedule';
  if(s.daily_at){
    const [h,m]=s.daily_at.split(':').map(Number);
    const hh=h%12||12, ap=h<12?'AM':'PM';
    return 'Every day at '+hh+':'+(m<10?'0':'')+m+' '+ap+' UTC';
  }
  if(s.every_minutes) return 'Every '+s.every_minutes+' minute'+(s.every_minutes==1?'':'s');
  if(s.every_seconds){
    const m=Math.round(s.every_seconds/60);
    if(m>=1) return 'Every '+m+' minute'+(m==1?'':'s');
    return 'Every '+s.every_seconds+' second'+(s.every_seconds==1?'':'s');
  }
  if(s.at){
    try{return 'Once at '+new Date(s.at).toLocaleString()}catch(e){}
    return 'Once at '+s.at;
  }
  return 'Custom schedule';
}

function timeAgo(iso){
  if(!iso) return null;
  const d=new Date(iso), now=new Date(), sec=Math.floor((now-d)/1000);
  if(sec<60) return sec+'s ago';
  if(sec<3600) return Math.floor(sec/60)+'m ago';
  if(sec<86400) return Math.floor(sec/3600)+'h ago';
  return Math.floor(sec/86400)+'d ago';
}

function getPreview(id){
  return preview[id] || {};
}

function formatSchedulerModel(model){
  const value=String(model||'').trim();
  if(!value) return 'Default: Claude CLI Opus';
  const labels={
    'claude-sonnet-4-6':'Claude CLI: Sonnet 4.6',
    'claude-opus-4-6':'Claude CLI: Opus 4.6',
    'claude-haiku-4-5-20251001':'Claude CLI: Haiku 4.5',
    'claude-sdk:claude-sonnet-4-6':'Claude API: Sonnet 4.6',
    'claude-sdk:claude-opus-4-6':'Claude API: Opus 4.6',
    'claude-sdk:claude-haiku-4-5-20251001':'Claude API: Haiku 4.5',
    'codex-cli:gpt-5.1-codex-mini':'Codex CLI: GPT-5.1 Codex Mini',
    'gemini-2.5-flash':'Gemini 2.5 Flash',
    'gemini-2.5-pro':'Gemini 2.5 Pro',
    'arcee-ai/trinity-large-preview:free':'OpenRouter: Trinity Fast',
    'stepfun/step-3.5-flash:free':'OpenRouter: StepFun 3.5 Flash'
  };
  return labels[value]||value;
}

function syncSchedulerModelField(){
  const select=document.getElementById('f-model');
  const customWrap=document.getElementById('f-model-custom-wrap');
  if(!select||!customWrap) return;
  customWrap.style.display=select.value==='__custom__'?'block':'none';
}

function setSchedulerModelValue(value){
  const model=String(value||'').trim();
  const select=document.getElementById('f-model');
  const custom=document.getElementById('f-model-custom');
  if(!select||!custom) return;
  const known=[...select.options].some(opt=>opt.value===model);
  if(!model){
    select.value='';
    custom.value='';
  }else if(known){
    select.value=model;
    custom.value='';
  }else{
    select.value='__custom__';
    custom.value=model;
  }
  syncSchedulerModelField();
}

function getSchedulerModelValue(){
  const select=document.getElementById('f-model');
  const custom=document.getElementById('f-model-custom');
  if(!select||!custom) return '';
  if(select.value==='__custom__') return custom.value.trim();
  return select.value.trim();
}

function formatSchedulerProfile(value){
  const path=String(value||'').trim();
  if(!path) return '';
  if(schedulerProfileLabels[path]) return 'Profile: '+schedulerProfileLabels[path];
  const fallback=path.split('/').filter(Boolean).pop()||path;
  return 'Profile: '+fallback;
}

function setSchedulerProfileValue(value){
  const path=String(value||'').trim();
  const select=document.getElementById('f-profile');
  if(!select) return;
  if(path && ![...select.options].some(opt=>opt.value===path)){
    const opt=document.createElement('option');
    opt.value=path;
    opt.textContent='Unavailable profile ('+path+')';
    select.appendChild(opt);
  }
  select.value=path;
  if(select.value!==path) select.value='';
}

function getSchedulerProfileValue(){
  const select=document.getElementById('f-profile');
  if(!select) return '';
  return String(select.value||'').trim();
}

async function loadSchedulerProfiles(){
  const select=document.getElementById('f-profile');
  if(!select) return;
  const currentValue=getSchedulerProfileValue();
  select.innerHTML='';
  Object.keys(schedulerProfileLabels).forEach(key=>delete schedulerProfileLabels[key]);

  const defaultOpt=document.createElement('option');
  defaultOpt.value='';
  defaultOpt.textContent='Current browser (no profile copy)';
  select.appendChild(defaultOpt);

  try{
    const r=await fetch('/web/provision/profiles');
    if(r.ok){
      const data=await r.json();
      for(const p of (data.profiles||[])){
        const path=String(p.profile_path||p.path||'').trim();
        if(!path||schedulerProfileLabels[path]) continue;
        const label=String(p.name||p.dir_name||'Profile').trim()||'Profile';
        const email=String(p.email||'').trim();
        const text=email?(label+' ('+email+')'):label;
        schedulerProfileLabels[path]=text;
        const opt=document.createElement('option');
        opt.value=path;
        opt.textContent=text;
        select.appendChild(opt);
      }
    }
  }catch(e){}

  setSchedulerProfileValue(currentValue);
}

// ── Render ──
function render(){
  const list=document.getElementById('job-list');
  const countEl=document.getElementById('job-count');
  countEl.textContent=jobs.length+' task'+(jobs.length===1?'':'s');

  if(!jobs.length){
    list.innerHTML='<div class="empty"><div class="icon">&#128197;</div><p>No scheduled tasks yet.<br>Create one to automate your workflows.</p><button class="btn btn-accent" onclick="openAddModal()">Create your first task</button></div>';
    return;
  }

  list.innerHTML=jobs.map((j,i)=>{
    const p=getPreview(j.id);
    const en=j.enabled!==false;
    const lastAgo=timeAgo(p.last_run_at);
    const nextAgo=p.next_run_at?new Date(p.next_run_at).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):'—';
    const statusCls=p.last_status==='success'?'status-ok':p.last_status==='error'?'status-fail':'';
    const lastOutput=p.last_output?'<div class="card-output"><span class="label">Last output</span>'+esc(p.last_output)+'</div>':'';
    const modelMeta='<span>'+esc(formatSchedulerModel(j.model||''))+'</span>';
    const profileMeta=j.profile_path?'<span>'+esc(formatSchedulerProfile(j.profile_path))+'</span>':'';
    return '<div class="card'+(en?'':' disabled')+'">' +
      '<div class="card-top">' +
        '<label class="toggle"><input type="checkbox" '+(en?'checked':'')+' onchange="toggleJob('+i+',this.checked)"><span class="slider"></span></label>' +
        '<span class="name">'+esc(j.id||'Untitled')+'</span>' +
        '<span class="actions">' +
          '<button title="History" onclick="openHistoryModal('+i+')">History</button>' +
          '<button title="Edit" onclick="openEditModal('+i+')">Edit</button>' +
          '<button class="del-btn" title="Delete" onclick="deleteJob('+i+')">&#10005;</button>' +
        '</span>' +
      '</div>' +
      '<div class="card-prompt">'+esc((j.prompt||'').substring(0,120))+'</div>' +
      '<div class="card-schedule">'+esc(scheduleToText(j.schedule))+'</div>' +
      '<div class="card-meta">' +
        modelMeta +
        profileMeta +
        (lastAgo?'<span>Last: '+esc(lastAgo)+'</span>':'') +
        (p.last_status?'<span class="'+statusCls+'">'+esc(p.last_status)+'</span>':'') +
        (p.run_count?'<span>'+p.run_count+' run'+(p.run_count==1?'':'s')+'</span>':'') +
        '<span>Next: '+esc(nextAgo)+'</span>' +
      '</div>' +
      lastOutput +
    '</div>';
  }).join('');
}

// ── API ──
async function checkSession(){
  const r=await fetch('/auth/me');
  const data=await r.json();
  if(!data.authenticated){location.href='/app';return false;}
  document.getElementById('whoami').textContent=data.email||'signed in';
  return true;
}

async function loadJobs(){
  try{
    const r=await fetch('/web/scheduler/jobs');
    const data=await r.json();
    if(!r.ok) throw new Error(data.error||'load failed');
    jobs=data.jobs||[];
    preview={};
    (data.preview||[]).forEach(p=>{preview[p.id]=p;});
    render();
  }catch(e){toast('Load failed: '+e.message,'err');}
}

async function persistJobs(){
  try{
    const r=await fetch('/web/scheduler/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jobs})});
    const data=await r.json();
    if(!r.ok) throw new Error(data.error||'save failed');
    jobs=data.jobs||[];
    preview={};
    (data.preview||[]).forEach(p=>{preview[p.id]=p;});
    render();
    return true;
  }catch(e){toast('Save failed: '+e.message,'err');return false;}
}

// ── Card actions ──
async function toggleJob(i,checked){
  jobs[i].enabled=checked;
  if(await persistJobs()) toast(jobs[i].id+(checked?' enabled':' disabled'));
}

async function deleteJob(i){
  const name=jobs[i].id||'this task';
  if(!confirm('Delete "'+name+'"?')) return;
  jobs.splice(i,1);
  if(await persistJobs()) toast('Deleted','ok');
}

// ── Modal ──
function openModal(){
  document.getElementById('job-modal').classList.add('open');
}
function closeModal(){
  document.getElementById('job-modal').classList.remove('open');
  editingIndex=-1;
}

function openAddModal(){
  editingIndex=-1;
  document.getElementById('modal-title').textContent='New Task';
  document.getElementById('f-name').value='';
  document.getElementById('f-prompt').value='';
  document.querySelector('input[name="sched-type"][value="daily"]').checked=true;
  document.getElementById('f-hour').value='9';
  document.getElementById('f-min').value='0';
  document.getElementById('f-interval').value='30';
  document.getElementById('f-once').value='';
  document.getElementById('f-timeout').value='180';
  document.getElementById('f-retry').value='0';
  setSchedulerModelValue('');
  setSchedulerProfileValue('');
  document.getElementById('f-headless').checked=false;
  document.getElementById('f-session').checked=false;
  openModal();
}

function openEditModal(i){
  editingIndex=i;
  const j=jobs[i];
  document.getElementById('modal-title').textContent='Edit Task';
  document.getElementById('f-name').value=j.id||'';
  document.getElementById('f-prompt').value=j.prompt||'';

  // Schedule
  const s=j.schedule||{};
  if(s.daily_at){
    document.querySelector('input[name="sched-type"][value="daily"]').checked=true;
    const [h,m]=(s.daily_at||'9:00').split(':');
    document.getElementById('f-hour').value=parseInt(h)||0;
    document.getElementById('f-min').value=parseInt(m)||0;
  }else if(s.every_minutes||s.every_seconds){
    document.querySelector('input[name="sched-type"][value="minutes"]').checked=true;
    document.getElementById('f-interval').value=s.every_minutes||(Math.round((s.every_seconds||60)/60));
  }else if(s.at){
    document.querySelector('input[name="sched-type"][value="once"]').checked=true;
    try{document.getElementById('f-once').value=new Date(s.at).toISOString().slice(0,16);}catch(e){}
  }else{
    document.querySelector('input[name="sched-type"][value="daily"]').checked=true;
  }

  // Advanced
  document.getElementById('f-timeout').value=j.timeout_seconds||180;
  document.getElementById('f-retry').value=j.retry_after_seconds||j.retry_seconds||0;
  setSchedulerModelValue(j.model||'');
  setSchedulerProfileValue(j.profile_path||'');
  document.getElementById('f-headless').checked=!!j.headless;
  document.getElementById('f-session').checked=!!(j.keep_session||j.use_stable_session);

  openModal();
}

async function saveModal(){
  const name=document.getElementById('f-name').value.trim();
  const prompt=document.getElementById('f-prompt').value.trim();
  if(!name){toast('Task name is required','err');return;}
  if(!prompt){toast('Prompt is required','err');return;}

  const schedType=document.querySelector('input[name="sched-type"]:checked').value;
  let schedule={};
  if(schedType==='daily'){
    const h=parseInt(document.getElementById('f-hour').value)||0;
    const m=parseInt(document.getElementById('f-min').value)||0;
    schedule={daily_at:(h<10?'0':'')+h+':'+(m<10?'0':'')+m};
  }else if(schedType==='minutes'){
    const n=parseInt(document.getElementById('f-interval').value)||30;
    schedule={every_minutes:n};
  }else if(schedType==='once'){
    const dt=document.getElementById('f-once').value;
    if(!dt){toast('Pick a date/time','err');return;}
    schedule={at:new Date(dt).toISOString()};
  }

  const timeout=parseInt(document.getElementById('f-timeout').value)||180;
  const retry=parseInt(document.getElementById('f-retry').value)||0;
  const model=getSchedulerModelValue();
  const profilePath=getSchedulerProfileValue();
  const headless=document.getElementById('f-headless').checked;
  const keepSession=document.getElementById('f-session').checked;

  if(headless&&profilePath){
    toast('Profile selection is not supported with headless mode','err');
    return;
  }

  const job={id:name,prompt:prompt,schedule:schedule,enabled:true,timeout_seconds:timeout};
  if(model) job.model=model;
  if(profilePath) job.profile_path=profilePath;
  if(retry>0) job.retry_seconds=retry;
  if(headless) job.headless=true;
  if(keepSession) job.use_stable_session=true;

  if(editingIndex>=0){
    // Preserve enabled state from existing job
    job.enabled=jobs[editingIndex].enabled!==false;
    jobs[editingIndex]=job;
  }else{
    jobs.push(job);
  }

  if(await persistJobs()){
    toast(editingIndex>=0?'Task updated':'Task created','ok');
    closeModal();
  }
}

// ── Import/Export ──
function downloadJobs(){
  const blob=new Blob([JSON.stringify({jobs},null,2)+'\n'],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='scheduled_jobs.json';
  document.body.appendChild(a);a.click();a.remove();
  toast('Downloaded scheduled_jobs.json','ok');
  setTimeout(()=>URL.revokeObjectURL(a.href),800);
}

function openImportModal(){
  document.getElementById('import-text').value='';
  document.getElementById('import-modal').classList.add('open');
}
function closeImportModal(){document.getElementById('import-modal').classList.remove('open');}

async function doImport(){
  const txt=document.getElementById('import-text').value.trim();
  if(!txt){toast('Paste JSON first','err');return;}
  let parsed;
  try{parsed=JSON.parse(txt);}catch(e){toast('Invalid JSON: '+e.message,'err');return;}
  if(!parsed.jobs||!Array.isArray(parsed.jobs)){toast('JSON must have a "jobs" array','err');return;}
  jobs=parsed.jobs;
  if(await persistJobs()){
    toast('Imported '+jobs.length+' task(s)','ok');
    closeImportModal();
  }
}

function closeHistoryModal(){
  historyJobId='';
  document.getElementById('history-title').textContent='Run History';
  document.getElementById('history-list').innerHTML='<div class="history-empty">Loading…</div>';
  document.getElementById('history-modal').classList.remove('open');
}

function renderHistory(records){
  const list=document.getElementById('history-list');
  if(!records.length){
    list.innerHTML='<div class="history-empty">No runs recorded yet.</div>';
    return;
  }
  list.innerHTML=records.map((rec,idx)=>{
    const ok=!!rec.ok;
    const status=ok?'Success':'Error';
    const ts=rec.ts ? new Date(rec.ts).toLocaleString() : 'Unknown time';
    const detail=String(rec.detail||'').trim();
    const length=Number.isFinite(Number(rec.len)) ? Number(rec.len) : detail.length;
    const detailHtml=detail ? esc(detail) : '<span class="history-empty">No detail</span>';
    return '<details class="history-item"'+(idx===0?' open':'')+'>' +
      '<summary>' +
        '<span class="history-left">' +
          '<span class="history-status '+(ok?'ok':'err')+'">'+status+'</span>' +
          '<span class="history-ts">'+esc(ts)+'</span>' +
        '</span>' +
        '<span class="history-len">'+esc(length+' chars')+'</span>' +
      '</summary>' +
      '<div class="history-detail">'+detailHtml+'</div>' +
    '</details>';
  }).join('');
}

async function loadHistory(){
  if(!historyJobId) return;
  const list=document.getElementById('history-list');
  list.innerHTML='<div class="history-empty">Loading…</div>';
  try{
    const r=await fetch('/web/scheduler/history?job_id='+encodeURIComponent(historyJobId)+'&limit=20');
    const data=await r.json();
    if(!r.ok) throw new Error(data.error||'load failed');
    renderHistory(data.records||[]);
  }catch(e){
    list.innerHTML='<div class="history-empty">Load failed: '+esc(e.message)+'</div>';
  }
}

async function openHistoryModal(i){
  const job=jobs[i];
  if(!job) return;
  historyJobId=job.id||'';
  document.getElementById('history-title').textContent='Run History: '+historyJobId;
  document.getElementById('history-modal').classList.add('open');
  await loadHistory();
}

// Close modals on overlay click
document.querySelectorAll('.modal-overlay').forEach(el=>{
  el.addEventListener('click',e=>{
    if(e.target!==el) return;
    if(el.id==='job-modal') closeModal();
    else if(el.id==='import-modal') closeImportModal();
    else if(el.id==='history-modal') closeHistoryModal();
  });
});

// Close modals on Escape
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    closeModal();
    closeImportModal();
    closeHistoryModal();
  }
});

// ── Init ──
(async function init(){
  try{
    if(await checkSession()){
      await loadSchedulerProfiles();
      await loadJobs();
    }
  }
  catch(e){toast('Session check failed: '+e.message,'err');}
})();
</script>
</body>
</html>"""

# Scheduler page visual alignment with the modern chat/setup skin.
_MODERN_SCHEDULER_THEME_STYLE = """<style id="modern-scheduler-theme">
:root{
  color-scheme:dark;
  --bg:#0a0c0f;
  --surface:#11161d;
  --surface-elev:#171d26;
  --accent:#ff6b4a;
  --accent-strong:#ff8768;
  --accent-soft:rgba(255,107,74,0.16);
  --text:#edf2f7;
  --muted:#9da7b7;
  --line:#2a3341;
  --line-strong:#3a4555;
  --green:#5cd48a;
  --red:#ef7c7c;
  --warn:#f0c46d;
  --radius:14px;
  --border:var(--line);
  --sans:'Space Grotesk','Avenir Next','Segoe UI',sans-serif;
  --mono:'IBM Plex Mono','SF Mono','Menlo','Consolas',monospace;
}
body{
  font-family:var(--sans)!important;
  background:
    radial-gradient(1200px 420px at 12% -8%, rgba(255,107,74,0.15), transparent 58%),
    radial-gradient(900px 340px at 95% 4%, rgba(64,136,124,0.14), transparent 62%),
    linear-gradient(180deg,#121720 0%,#0d1118 44%,#0a0c0f 100%)!important;
  color:var(--text)!important;
  letter-spacing:0.01em;
}
#topbar{
  align-items:flex-start!important;
  gap:14px;
  padding:12px clamp(12px,2vw,24px)!important;
  background:rgba(17,22,29,0.88)!important;
  border-bottom:1px solid var(--line)!important;
  backdrop-filter:blur(8px);
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .title{
  font-family:var(--mono)!important;
  font-size:12px!important;
  color:var(--accent-strong)!important;
  letter-spacing:0.04em;
  text-transform:uppercase;
  background:var(--accent-soft)!important;
  border:1px solid rgba(255,107,74,0.4)!important;
  border-radius:999px;
  padding:4px 10px;
}
#topbar .badge{
  font-family:var(--mono)!important;
  font-size:11px!important;
  color:var(--muted)!important;
  border:1px solid var(--line)!important;
  background:rgba(255,255,255,0.02)!important;
}
#topbar .nav a{
  font-family:var(--mono)!important;
  font-size:11px!important;
  color:var(--muted)!important;
  border:1px solid var(--line)!important;
  border-radius:999px!important;
  background:rgba(255,255,255,0.02)!important;
  padding:6px 10px!important;
}
#topbar .nav a:hover{
  border-color:var(--accent)!important;
  color:#ffd2c8!important;
  background:var(--accent-soft)!important;
}
main{
  max-width:860px!important;
  padding:24px clamp(12px,2vw,24px)!important;
}
.header-bar .count{
  font-family:var(--mono)!important;
  color:var(--muted)!important;
}
.btn{
  border-radius:12px!important;
  font-family:var(--sans)!important;
}
.btn-accent{
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
  color:#fff!important;
}
.btn-ghost{
  border:1px solid var(--line)!important;
  color:var(--muted)!important;
  background:rgba(255,255,255,0.02)!important;
}
.btn-ghost:hover{
  color:#ffd2c8!important;
  border-color:var(--accent)!important;
  background:var(--accent-soft)!important;
}
.btn-danger{
  border:1px solid rgba(239,124,124,0.55)!important;
  color:#ffb8b8!important;
  background:rgba(239,124,124,0.1)!important;
}
#toast{
  border-radius:12px!important;
  font-family:var(--mono)!important;
}
#toast.ok{
  background:rgba(92,212,138,0.13)!important;
  border:1px solid rgba(92,212,138,0.45)!important;
  color:#a7efc4!important;
}
#toast.err{
  background:rgba(239,124,124,0.13)!important;
  border:1px solid rgba(239,124,124,0.45)!important;
  color:#ffc2c2!important;
}
.card{
  background:rgba(17,22,29,0.9)!important;
  border:1px solid var(--line)!important;
  border-radius:14px!important;
  padding:16px!important;
  transition:border-color 0.16s ease,transform 0.16s ease,box-shadow 0.16s ease;
}
.card:hover{
  border-color:rgba(255,107,74,0.45)!important;
  transform:translateY(-1px);
  box-shadow:0 16px 30px rgba(0,0,0,0.28);
}
.card-top .name{font-size:15px!important}
.card-top .actions button{
  border:1px solid transparent;
  border-radius:8px!important;
}
.card-top .actions button:hover{
  color:#ffd2c8!important;
  border-color:var(--line)!important;
  background:var(--accent-soft)!important;
}
.card-top .actions .del-btn:hover{
  color:#ffb8b8!important;
  border-color:rgba(239,124,124,0.45)!important;
  background:rgba(239,124,124,0.1)!important;
}
.card-prompt{color:var(--muted)!important}
.card-schedule{
  color:#ffd2c8!important;
  font-family:var(--mono)!important;
  font-size:11px!important;
  letter-spacing:0.03em;
}
.card-meta{
  color:var(--muted)!important;
  font-family:var(--mono)!important;
}
.card-meta .status-ok{color:var(--green)!important}
.card-meta .status-fail{color:#ffb8b8!important}
.card-output{
  border-top:1px solid var(--line)!important;
  color:#dbe3f4!important;
}
.card-output .label{
  color:var(--muted)!important;
  font-family:var(--mono)!important;
}
.toggle .slider{
  background:#3b4557!important;
  border:1px solid var(--line)!important;
}
.toggle input:checked + .slider{
  background:linear-gradient(135deg,#5cd48a,#45bf77)!important;
}
.empty{color:var(--muted)!important}
.empty .icon{opacity:0.5}
.advanced{
  border-top:1px solid var(--line)!important;
}
.advanced summary{
  color:var(--muted)!important;
  font-family:var(--mono)!important;
  letter-spacing:0.02em;
}
.advanced summary:hover{color:#ffd2c8!important}
.modal-overlay{
  background:rgba(0,0,0,0.72)!important;
}
.modal{
  background:var(--surface-elev)!important;
  border:1px solid var(--line-strong)!important;
  border-radius:16px!important;
}
.modal h2{
  color:#ffd4cb!important;
  letter-spacing:0.01em;
}
.field label{
  color:var(--muted)!important;
  font-family:var(--mono)!important;
  font-size:11px!important;
  letter-spacing:0.06em;
  text-transform:uppercase;
}
.field input[type="text"],.field input[type="number"],.field textarea,.field input[type="datetime-local"],.field select{
  background:rgba(255,255,255,0.03)!important;
  border:1px solid var(--line)!important;
  border-radius:10px!important;
  color:var(--text)!important;
}
.field input:focus,.field textarea:focus,.field select:focus{
  border-color:var(--accent)!important;
  box-shadow:0 0 0 3px rgba(255,107,74,0.15);
}
.field .hint{color:var(--muted)!important}
.sched-option{
  background:rgba(255,255,255,0.02)!important;
  border:1px solid var(--line)!important;
  border-radius:10px!important;
}
.sched-option:has(input:checked){
  border-color:var(--accent)!important;
  background:var(--accent-soft)!important;
}
.sched-option .sched-inputs input{
  background:#10151d!important;
  border:1px solid var(--line)!important;
  border-radius:8px!important;
}
.sched-option .sched-inputs span{color:var(--muted)!important}
.adv-fields summary{
  color:var(--muted)!important;
  font-family:var(--mono)!important;
}
.check-field label{color:var(--text)!important}
.modal-footer{
  border-top:1px solid var(--line)!important;
}
.import-area textarea{
  background:#10151d!important;
  border:1px solid var(--line)!important;
  border-radius:10px!important;
  font-family:var(--mono)!important;
}
.history-item{
  background:rgba(255,255,255,0.02)!important;
  border:1px solid var(--line)!important;
  border-radius:12px!important;
}
.history-status.ok{
  background:rgba(92,212,138,0.14)!important;
  color:#a7efc4!important;
}
.history-status.err{
  background:rgba(239,124,124,0.14)!important;
  color:#ffc2c2!important;
}
.history-ts,.history-len{color:var(--muted)!important}
.history-detail{
  border-top:1px solid var(--line)!important;
  color:#dbe3f4!important;
}
.history-empty{color:var(--muted)!important}
@media (max-width: 640px){
  #topbar{
    flex-direction:column!important;
    align-items:flex-start!important;
    gap:10px;
  }
  #topbar .nav{
    width:100%;
    overflow:auto;
    flex-wrap:nowrap;
  }
  main{padding:14px 10px!important}
}
</style>"""


def _apply_modern_scheduler_theme(html: str) -> str:
    if 'id="modern-scheduler-theme"' in html:
        return html
    if "</head>" not in html:
        return html
    parts: list[str] = []
    if "fonts.googleapis.com/css2?family=Space+Grotesk" not in html:
        parts.append(_MODERN_CHAT_THEME_LINKS)
    parts.append(_MODERN_SCHEDULER_THEME_STYLE)
    return html.replace("</head>", "\n" + "\n".join(parts) + "\n</head>", 1)


SCHEDULER_HTML = _apply_modern_scheduler_theme(SCHEDULER_HTML)

# Setup page visual alignment with the modern chat skin.
_MODERN_SETUP_THEME_STYLE = """<style id="modern-setup-theme">
:root{
  color-scheme:dark;
  --bg:#0a0c0f;
  --surface:#11161d;
  --surface-elev:#171d26;
  --accent:#ff6b4a;
  --accent-strong:#ff8768;
  --accent-soft:rgba(255,107,74,0.16);
  --text:#edf2f7;
  --muted:#9da7b7;
  --line:#2a3341;
  --line-strong:#3a4555;
  --green:#5cd48a;
  --red:#ef7c7c;
  --yellow:#f0c46d;
  --border:var(--line);
  --fg:var(--text);
  --mono:'IBM Plex Mono','SF Mono','Menlo','Consolas',monospace;
  --sans:'Space Grotesk','Avenir Next','Segoe UI',sans-serif;
}
body{
  font-family:var(--sans)!important;
  background:
    radial-gradient(1200px 420px at 12% -8%, rgba(255,107,74,0.15), transparent 58%),
    radial-gradient(900px 340px at 95% 4%, rgba(64,136,124,0.14), transparent 62%),
    linear-gradient(180deg,#121720 0%,#0d1118 44%,#0a0c0f 100%)!important;
  color:var(--text)!important;
}
a{color:var(--accent-strong)}
#login h1{
  font-size:34px!important;
  color:var(--text)!important;
  letter-spacing:0.06em!important;
  text-transform:uppercase;
  font-weight:700;
}
#login .sub{color:var(--muted)!important}
#login #dev-auth button{
  border-radius:12px!important;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
}
#topbar{
  align-items:flex-start!important;
  gap:14px;
  padding:12px clamp(12px,2vw,24px)!important;
  background:rgba(17,22,29,0.88)!important;
  border-bottom:1px solid var(--line)!important;
  backdrop-filter:blur(8px);
}
#topbar .left{display:flex;align-items:center;gap:12px}
#topbar .title{
  font-family:var(--mono)!important;
  font-size:12px!important;
  color:var(--accent-strong)!important;
  letter-spacing:0.04em;
  background:var(--accent-soft)!important;
  border:1px solid rgba(255,107,74,0.4)!important;
  padding:4px 10px;
  border-radius:999px;
}
#topbar .status-stack{display:flex;flex-direction:column;gap:2px}
#topbar .status{color:var(--muted)!important}
#topbar .status.online{color:var(--green)!important}
#topbar .status.warn{color:var(--yellow)!important}
#topbar .nav a{
  font-family:var(--mono)!important;
  font-size:11px!important;
  color:var(--muted)!important;
  border:1px solid var(--line)!important;
  background:rgba(255,255,255,0.02)!important;
  padding:6px 10px!important;
  border-radius:999px!important;
}
#topbar .nav a:hover{
  border-color:var(--accent)!important;
  color:#ffd2c8!important;
  background:var(--accent-soft)!important;
}
#main{
  max-width:1160px;
  margin:0 auto;
  border-left:1px solid rgba(255,255,255,0.05);
  border-right:1px solid rgba(255,255,255,0.05);
  background:linear-gradient(180deg,rgba(17,22,29,0.9) 0%,rgba(11,14,19,0.93) 100%);
  box-shadow:0 22px 50px rgba(0,0,0,0.34);
}
#setup-download-banner{
  padding:9px clamp(12px,2vw,24px)!important;
  background:rgba(255,107,74,0.1)!important;
  border-bottom:1px solid rgba(255,107,74,0.3)!important;
}
#setup-download-banner .detail{color:#b5bfce!important}
#setup-download-banner a{
  color:#ffd8cf!important;
  border:1px solid rgba(255,107,74,0.5)!important;
  border-radius:999px!important;
  background:rgba(255,107,74,0.2)!important;
}
#setup-download-banner a:hover{
  background:rgba(255,107,74,0.28)!important;
  border-color:var(--accent-strong)!important;
}
.content{
  max-width:760px!important;
  padding:24px clamp(12px,2vw,24px)!important;
}
.step{
  background:rgba(17,22,29,0.9)!important;
  border:1px solid var(--line)!important;
  border-radius:14px!important;
  padding:18px!important;
}
.step h2{
  color:#ffd4cb!important;
  font-family:var(--mono)!important;
  font-size:12px!important;
  letter-spacing:0.08em;
  text-transform:uppercase;
}
.profile-option{
  border:1px solid var(--line)!important;
  border-radius:12px!important;
  background:rgba(255,255,255,0.01)!important;
}
.profile-option:hover{border-color:var(--accent)!important;background:var(--accent-soft)!important}
.profile-option.selected{
  border-color:var(--accent)!important;
  background:var(--accent-soft)!important;
}
.profiles-loading,.profile-email,.profile-hint{color:var(--muted)!important}
.provision-btn{
  border-radius:12px!important;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
  font-family:var(--sans)!important;
}
.provision-status{font-family:var(--mono)!important}
.provision-status.running{color:var(--yellow)!important}
.provision-status.done{color:var(--green)!important}
.provision-status.error{color:var(--red)!important}
.key-row{border-bottom:1px solid var(--line)!important}
.key-preview{color:var(--muted)!important}
.revoke-btn{
  border-radius:8px!important;
  border:1px solid #5a2626!important;
  background:#351818!important;
  color:#ffb8b8!important;
}
.agent-label{color:var(--muted)!important}
.agent-label.online{color:var(--green)!important}
.install-cmd,#setup-install-modal .cmd{
  border:1px solid var(--line)!important;
  border-radius:10px!important;
  background:#10151d!important;
}
.copy-btn{
  border:1px solid var(--line)!important;
  border-radius:10px!important;
  background:rgba(255,255,255,0.02)!important;
  color:var(--muted)!important;
}
.copy-btn:hover{
  border-color:var(--accent)!important;
  color:#ffd2c8!important;
  background:var(--accent-soft)!important;
}
#consent-modal{
  background:rgba(0,0,0,0.72)!important;
}
#consent-modal .modal-card{
  background:var(--surface-elev)!important;
  border:1px solid var(--line-strong)!important;
  border-radius:14px!important;
}
#consent-modal h3{color:#ffd5cc!important}
#consent-modal p{color:var(--muted)!important}
#consent-modal li{color:#c7cfda!important}
#consent-modal .btn-agree{
  border-radius:10px!important;
  background:linear-gradient(135deg,var(--accent),var(--accent-strong))!important;
}
#consent-modal .btn-cancel{
  border-radius:10px!important;
  border:1px solid var(--line)!important;
  color:var(--muted)!important;
}
#setup-install-modal{
  background:rgba(0,0,0,0.72)!important;
}
#setup-install-modal .card{
  background:var(--surface-elev)!important;
  border:1px solid var(--line-strong)!important;
  border-radius:14px!important;
}
#setup-install-modal h3{color:#ffd5cc!important}
#setup-install-modal p{color:var(--muted)!important}
#setup-install-modal .x{color:var(--muted)!important}
#pending{
  align-items:center!important;
  justify-content:center!important;
  text-align:center!important;
}
#pending h1{
  color:var(--text)!important;
  letter-spacing:0.04em!important;
}
#pending p{color:var(--muted)!important}
#pending button{
  border-radius:12px!important;
  border:1px solid var(--line-strong)!important;
  background:rgba(255,255,255,0.02)!important;
  color:var(--text)!important;
}
#pending button:hover{
  border-color:var(--accent)!important;
  background:var(--accent-soft)!important;
}
#pendingmsg{color:var(--muted)!important}
.toast{
  border-radius:10px!important;
  font-family:var(--mono)!important;
}
.toast.ok{
  background:rgba(92,212,138,0.12)!important;
  color:#9bf0bc!important;
  border:1px solid rgba(92,212,138,0.35)!important;
}
.toast.err{
  background:rgba(239,124,124,0.12)!important;
  color:#ffb8b8!important;
  border:1px solid rgba(239,124,124,0.35)!important;
}
@media (max-width: 900px){
  #main{max-width:none;border:none;box-shadow:none}
}
@media (max-width: 640px){
  #topbar{
    flex-direction:column!important;
    align-items:flex-start!important;
    gap:10px;
    padding:10px!important;
  }
  #topbar .nav{
    width:100%;
    overflow:auto;
    display:flex;
    flex-wrap:nowrap;
  }
  .content{padding:14px 10px!important}
}
</style>"""


def _apply_modern_setup_theme(html: str) -> str:
    if 'id="modern-setup-theme"' in html:
        return html
    if "</head>" not in html:
        return html
    parts: list[str] = []
    if "fonts.googleapis.com/css2?family=Space+Grotesk" not in html:
        parts.append(_MODERN_CHAT_THEME_LINKS)
    parts.append(_MODERN_SETUP_THEME_STYLE)
    return html.replace("</head>", "\n" + "\n".join(parts) + "\n</head>", 1)


SETUP_HTML = _apply_modern_setup_theme(SETUP_HTML)


# ---------------------------------------------------------------------------
# MCP Install Page — /mcp
# ---------------------------------------------------------------------------

MCP_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>MCP Setup | Unchained</title>
  <script src="https://accounts.google.com/gsi/client" async defer></script>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{
      font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      background:#0a0a0f;color:#e8e8ec;line-height:1.6;
    }
    a{color:#ff8398;text-decoration:none}
    a:hover{text-decoration:underline}
    .wrap{max-width:960px;margin:0 auto;padding:28px 18px 56px}
    .nav{display:flex;flex-wrap:wrap;align-items:center;gap:10px;justify-content:space-between;margin-bottom:24px}
    .brand{font-size:12px;letter-spacing:1.4px;text-transform:uppercase;color:#8e8ea0}
    .nav-links{display:flex;gap:8px}
    .nav-btn{
      display:inline-flex;align-items:center;justify-content:center;
      border:1px solid #3a3a44;border-radius:10px;padding:8px 14px;
      color:#e8e8ec;text-decoration:none;font-size:13px;background:#141420;
    }
    .nav-btn:hover{border-color:#e94560;text-decoration:none}
    .hero{
      border:1px solid #252532;border-radius:16px;padding:32px 24px;
      background:linear-gradient(180deg,#141420 0%,#101018 100%);
      margin-bottom:28px;text-align:center;
    }
    .hero h1{font-size:clamp(26px,3.5vw,38px);margin-bottom:8px}
    .hero h1 span{color:#e94560}
    .hero p{color:#a6a6b5;font-size:16px;max-width:600px;margin:0 auto}
    .steps{display:flex;flex-direction:column;gap:20px;margin-bottom:32px}
    .step{
      border:1px solid #252532;border-radius:14px;background:#0e0e15;
      padding:22px 20px;position:relative;
    }
    .step-num{
      display:inline-flex;align-items:center;justify-content:center;
      width:28px;height:28px;border-radius:50%;background:#e94560;
      color:#fff;font-size:13px;font-weight:700;margin-right:10px;
    }
    .step h2{display:inline;font-size:18px;vertical-align:middle}
    .step-body{margin-top:14px}
    .agent-status{
      display:flex;align-items:center;gap:8px;padding:10px 14px;
      border-radius:10px;background:#111118;border:1px solid #2a2a34;
      font-size:13px;margin-bottom:10px;
    }
    .dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .dot.green{background:#34d399}
    .dot.yellow{background:#fbbf24}
    .dot.red{background:#f87171}
    .tab-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
    .tab-btn{
      border:1px solid #2a2a34;border-radius:999px;padding:7px 14px;
      background:#12121b;color:#aeb0c0;cursor:pointer;font-size:13px;
      font-family:inherit;
    }
    .tab-btn.active{border-color:#e94560;background:#23141a;color:#fff}
    .code-wrap{position:relative;margin-bottom:8px}
    .code-block{
      overflow-x:auto;padding:14px 16px;border-radius:10px;
      background:#111118;border:1px solid #2b2b36;
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-all;
    }
    .copy-btn{
      position:absolute;top:8px;right:8px;padding:5px 10px;
      border:1px solid #3a3a44;border-radius:8px;background:#1a1a26;
      color:#aeb0c0;cursor:pointer;font-size:12px;font-family:inherit;
    }
    .copy-btn:hover{border-color:#e94560;color:#fff}
    .copy-btn.copied{background:#23141a;color:#e94560;border-color:#e94560}
    .signin-prompt{
      display:flex;align-items:center;gap:10px;padding:12px 16px;
      border-radius:10px;background:rgba(233,69,96,0.08);
      border:1px solid rgba(233,69,96,0.25);font-size:13px;margin-bottom:12px;
    }
    .signin-btn{
      padding:7px 16px;border-radius:8px;background:#e94560;
      color:#fff;font-size:13px;font-weight:600;cursor:pointer;
      border:none;text-decoration:none;
    }
    .signin-btn:hover{background:#d63b55;text-decoration:none}
    .tools-section{
      border:1px solid #252532;border-radius:14px;background:#0e0e15;
      padding:22px 20px;margin-bottom:28px;
    }
    .tools-toggle{
      display:flex;align-items:center;justify-content:space-between;
      cursor:pointer;user-select:none;
    }
    .tools-toggle h2{font-size:18px}
    .tools-toggle .arrow{font-size:14px;color:#8e8ea0;transition:transform .2s}
    .tools-toggle .arrow.open{transform:rotate(180deg)}
    .tools-table{
      width:100%;border-collapse:collapse;margin-top:14px;
      font-size:13px;display:none;
    }
    .tools-table.show{display:table}
    .tools-table th{text-align:left;padding:8px 10px;border-bottom:1px solid #252532;color:#8e8ea0;font-weight:500}
    .tools-table td{padding:8px 10px;border-bottom:1px solid #1a1a24}
    .tools-table tr:last-child td{border-bottom:none}
    .tools-table code{
      background:#171722;border:1px solid #2f2f3c;border-radius:4px;
      padding:1px 5px;font-size:12px;
    }
    .tools-group td{
      padding:12px 10px 6px;color:#c5c5d2;font-weight:600;font-size:13px;
      border-bottom:1px solid #252532;letter-spacing:.3px;
    }
    .tools-pipeline{
      font-size:13px;color:#8e8ea0;margin-top:14px;margin-bottom:4px;
      line-height:1.5;display:none;
    }
    .tools-pipeline.show{display:block}
    .tools-pipeline code{
      background:#171722;border:1px solid #2f2f3c;border-radius:4px;
      padding:1px 5px;font-size:12px;
    }
    .footer{text-align:center;color:#6e6e80;font-size:13px;padding:20px 0}
    .footer a{color:#8e8ea0}
    @media(max-width:640px){
      .wrap{padding:16px 12px 40px}
      .step{padding:16px 14px}
      .hero{padding:24px 16px}
    }
  </style>
</head>
<body>
  <div class="wrap">
    <nav class="nav">
      <div class="brand">Unchained MCP</div>
      <div class="nav-links">
        <a class="nav-btn" href="/">Home</a>
        <a class="nav-btn" href="/local">Chat</a>
        <a class="nav-btn" href="/mcp-guide">Docs</a>
      </div>
    </nav>

    <section class="hero">
      <h1>Your browser, <span>one command</span> away</h1>
      <p>Use your real Chrome through any MCP client. No Playwright. No headless. Raw CDP over your existing agent.</p>
    </section>

    <div class="steps">
      <div class="step">
        <span class="step-num">1</span>
        <h2>Connect Your Browser</h2>
        <div class="step-body">
          <div class="agent-status" id="agent-status">
            <div class="dot yellow" id="agent-dot"></div>
            <span id="agent-status-text">Checking agent status...</span>
          </div>
          <p style="color:#a6a6b5;font-size:13px;margin-bottom:10px">
            Install the Unchained agent on your Mac to bridge your local Chrome:
          </p>
          <div class="code-wrap" id="installer-wrap">
            <pre class="code-block" id="installer-cmd">curl -fsSL https://api.unchainedsky.com/install.sh | bash</pre>
            <button class="copy-btn" onclick="copyCode('installer-cmd',this)">Copy</button>
          </div>
        </div>
      </div>

      <div class="step">
        <span class="step-num">2</span>
        <h2>Add MCP Server</h2>
        <div class="step-body">
          <div id="signin-section" class="signin-prompt" style="display:none">
            <span>Sign in to auto-fill your API key</span>
            <div id="mcp-gsi-btn"></div>
          </div>
          <div class="tab-bar">
            <button class="tab-btn active" onclick="switchTab('claude-code',this)">Claude Code</button>
            <button class="tab-btn" onclick="switchTab('claude-desktop',this)">Claude Desktop</button>
            <button class="tab-btn" onclick="switchTab('other',this)">Other</button>
          </div>
          <div id="tab-claude-code">
            <div class="code-wrap">
              <pre class="code-block" id="snippet-claude-code">claude mcp add unchainedsky \
  https://api.unchainedsky.com/mcp \
  -t http \
  -H "Authorization: Bearer <span id="key-cc">YOUR_API_KEY</span>"</pre>
              <p style="color:#a6a6b5;font-size:12px;margin-top:8px">
                Restart Claude Code after adding (<code>/mcp</code> to verify tools are loaded).
              </p>
              <button class="copy-btn" onclick="copySnippet('claude-code',this)">Copy</button>
            </div>
          </div>
          <div id="tab-claude-desktop" style="display:none">
            <div class="code-wrap">
              <pre class="code-block" id="snippet-claude-desktop">{
  "mcpServers": {
    "unchainedsky": {
      "url": "https://api.unchainedsky.com/mcp",
      "headers": {
        "Authorization": "Bearer <span id="key-cd">YOUR_API_KEY</span>"
      }
    }
  }
}</pre>
              <button class="copy-btn" onclick="copySnippet('claude-desktop',this)">Copy</button>
            </div>
          </div>
          <div id="tab-other" style="display:none">
            <p style="color:#a6a6b5;font-size:13px;margin-bottom:10px">
              Use any MCP client that supports HTTP transport. Set the endpoint and Authorization header:
            </p>
            <div class="code-wrap">
              <pre class="code-block" id="snippet-other">Endpoint: https://api.unchainedsky.com/mcp
Header:   Authorization: Bearer <span id="key-ot">YOUR_API_KEY</span></pre>
              <button class="copy-btn" onclick="copySnippet('other',this)">Copy</button>
            </div>
          </div>
        </div>
      </div>

      <div class="step">
        <span class="step-num">3</span>
        <h2>Verify</h2>
        <div class="step-body">
          <p style="color:#a6a6b5;font-size:13px;margin-bottom:10px">
            Ask your MCP client to run a quick DDM extraction (agent_id is auto-detected from your API key):
          </p>
          <div class="code-wrap">
            <pre class="code-block" id="snippet-verify">ddm url=https://example.com</pre>
            <button class="copy-btn" onclick="copySnippet('verify',this)">Copy</button>
          </div>
          <p style="color:#a6a6b5;font-size:13px;margin-top:10px">
            To check your agent connection status:
          </p>
          <div class="code-wrap">
            <pre class="code-block" id="snippet-agent-lookup">curl -s -H "Authorization: Bearer YOUR_API_KEY" https://api.unchainedsky.com/api/agents | python3 -m json.tool</pre>
            <button class="copy-btn" onclick="copyCode('snippet-agent-lookup',this)">Copy</button>
          </div>
        </div>
      </div>
    </div>

    <div class="tools-section">
      <div class="tools-toggle" onclick="toggleTools()">
        <h2>Available MCP Tools</h2>
        <span class="arrow" id="tools-arrow">&#9660;</span>
      </div>
      <div class="tools-pipeline" id="tools-pipeline">
        Extraction pipeline: <code>ddm</code> orients the page (~500 tokens) &#x2192; <code>intel_probe</code> classifies the best extraction strategy &#x2192; <code>intel_extract</code> pulls structured data. For JS-heavy SPAs, probe may rank <code>intel_stores</code> &#x2192; <code>intel_shape</code> / <code>intel_find_paths</code> &#x2192; <code>js_eval</code> higher.
      </div>
      <table class="tools-table" id="tools-table">
        <thead>
          <tr><th>Tool</th><th>Description</th></tr>
        </thead>
        <tbody>
          <tr class="tools-group"><td colspan="2">Page Understanding</td></tr>
          <tr><td><code>ddm</code></td><td>DOM Density Map &mdash; structural page layout + interactive elements (~500 tokens). Use <code>--text</code> for page text, <code>--at x,y</code> for element details, <code>--find "keyword"</code> to search.</td></tr>
          <tr><td><code>intel_probe</code></td><td>Page intelligence probe &mdash; DOM fingerprint + Bayesian strategy ranking (~100 tokens). Run on first visit to any SPA.</td></tr>
          <tr><td><code>intel_extract</code></td><td>Extract structured data using auto-selected or forced strategy (host_attrs, react_fiber, data_testid, etc.)</td></tr>
          <tr class="tools-group"><td colspan="2">Data Store Extraction</td></tr>
          <tr><td><code>intel_stores</code></td><td>List JavaScript data stores on page (globals &gt;10KB). Use on Nuxt/Next/YouTube sites.</td></tr>
          <tr><td><code>intel_shape</code></td><td>Map the shape of a JS global object (e.g. <code>__NUXT__</code>, <code>ytInitialData</code>).</td></tr>
          <tr><td><code>intel_find_paths</code></td><td>Find paths to a key pattern inside a JS global.</td></tr>
          <tr class="tools-group"><td colspan="2">Browser Interaction</td></tr>
          <tr><td><code>cdp_navigate</code></td><td>Navigate to a URL. Returns page title and DDM layout.</td></tr>
          <tr><td><code>cdp_click</code></td><td>Click at pixel coordinates (from DDM output). Returns updated DDM layout.</td></tr>
          <tr><td><code>cdp_type</code></td><td>Type text into the focused element. Click an input first to focus it.</td></tr>
          <tr><td><code>js_eval</code></td><td>Execute JavaScript and return the result. Use for SPA widgets, querySelectorAll, data stores.</td></tr>
          <tr><td><code>cdp_screenshot</code></td><td>Take a screenshot (~2100 tokens). Prefer DDM. Use for CAPTCHAs/visual verification only.</td></tr>
          <tr class="tools-group"><td colspan="2">File &amp; Agent Management</td></tr>
          <tr><td><code>cdp_set_file</code></td><td>Set a file on <code>&lt;input type="file"&gt;</code> without the OS picker.</td></tr>
          <tr><td><code>list_connected_agents</code></td><td>List connected browser agents and Chrome profiles.</td></tr>
          <tr class="tools-group"><td colspan="2">Chrome Profile Provisioning</td></tr>
          <tr><td><code>cdp_provision_launch</code></td><td>Launch Chrome with a user profile for OAuth/authenticated browsing.</td></tr>
          <tr><td><code>cdp_provision_cleanup</code></td><td>Clean up provisioned Chrome instances.</td></tr>
          <tr><td><code>list_provisioned_tabs</code></td><td>List tabs in provisioned Chrome (discover OAuth popups).</td></tr>
        </tbody>
      </table>
    </div>

    <footer class="footer">
      <a href="/mcp-guide">MCP Docs</a> &middot;
      <a href="/local">Open Chat</a> &middot;
      <a href="/install">Install Agent</a> &middot;
      <a href="/">Home</a>
    </footer>
  </div>

  <script>
    let apiKey = '';
    let agentId = '';

    async function handleMcpGoogleCredential(response) {
      try {
        var res = await fetch('/auth/google', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({credential: response.credential}),
          credentials: 'include'
        });
        if (res.ok) { location.reload(); }
      } catch(e) {}
    }

    window.addEventListener('load', function() {
      var gcid = '__GOOGLE_CLIENT_ID__';
      if (gcid && gcid !== '__GOOGLE_' + 'CLIENT_ID__' && window.google && google.accounts) {
        google.accounts.id.initialize({
          client_id: gcid,
          callback: handleMcpGoogleCredential,
          auto_prompt: false
        });
        var el = document.getElementById('mcp-gsi-btn');
        if (el) {
          google.accounts.id.renderButton(el, {
            theme: 'filled_black', size: 'large', text: 'signin_with', shape: 'rectangular', width: 260
          });
        }
      }
    });

    function switchTab(tab, btn) {
      document.querySelectorAll('[id^="tab-"]').forEach(function(el) {
        if (el.id.startsWith('tab-claude') || el.id === 'tab-other') {
          el.style.display = 'none';
        }
      });
      document.getElementById('tab-' + tab).style.display = '';
      document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
    }

    function copyCode(id, btn) {
      var el = id ? document.getElementById(id) : btn.previousElementSibling;
      if (!el) return;
      navigator.clipboard.writeText(el.textContent).then(function() {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
      });
    }

    function copySnippet(tab, btn) {
      var el = document.getElementById('snippet-' + tab);
      if (!el) return;
      var text = el.textContent;
      if (apiKey) text = text.replace(/YOUR_API_KEY/g, apiKey);
      if (agentId) text = text.replace(/YOUR_AGENT_ID/g, agentId);
      navigator.clipboard.writeText(text).then(function() {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
      });
    }

    function toggleTools() {
      var table = document.getElementById('tools-table');
      var pipeline = document.getElementById('tools-pipeline');
      var arrow = document.getElementById('tools-arrow');
      table.classList.toggle('show');
      pipeline.classList.toggle('show');
      arrow.classList.toggle('open');
    }

    function fillKey(key) {
      apiKey = key;
      ['key-cc','key-cd','key-ot'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) { el.textContent = key; el.style.color = '#34d399'; }
      });
    }

    function fillAgentId(aid) {
      agentId = aid;
    }

    (async function init() {
      try {
        var meResp = await fetch('/auth/me', { credentials: 'include' });
        if (meResp.ok) {
          var me = await meResp.json();
          if (me.authenticated && me.api_key) {
            fillKey(me.api_key);
            document.getElementById('signin-section').style.display = 'none';
            try {
              var agentsResp = await fetch('/api/agents', {
                headers: { 'Authorization': 'Bearer ' + me.api_key }
              });
              if (agentsResp.ok) {
                var agents = await agentsResp.json();
                var list = agents.agents || [];
                if (list.length > 0) {
                  var aid = list[0].agent_id || list[0].id || '';
                  if (aid) fillAgentId(aid);
                  document.getElementById('agent-dot').className = 'dot green';
                  document.getElementById('agent-status-text').textContent =
                    'Agent connected' + (aid ? ' (' + aid + ')' : '') + ' \u2014 skip to step 2';
                  document.getElementById('installer-wrap').style.display = 'none';
                } else {
                  document.getElementById('agent-dot').className = 'dot red';
                  document.getElementById('agent-status-text').textContent =
                    'No agent connected \u2014 install below';
                }
              }
            } catch(e) {}
          } else {
            document.getElementById('signin-section').style.display = '';
            document.getElementById('agent-dot').className = 'dot yellow';
            document.getElementById('agent-status-text').textContent =
              'Sign in to check agent status';
          }
        } else {
          document.getElementById('signin-section').style.display = '';
          document.getElementById('agent-dot').className = 'dot yellow';
          document.getElementById('agent-status-text').textContent =
            'Sign in to check agent status';
        }
      } catch(e) {
        document.getElementById('agent-dot').className = 'dot yellow';
        document.getElementById('agent-status-text').textContent =
          'Could not check status';
      }
    })();
  </script>
</body>
</html>"""
