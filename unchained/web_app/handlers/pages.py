"""Page handlers extracted from web.py."""

from __future__ import annotations

from aiohttp import web


from web_app.core import get_core as _core


async def handle_install_page(request: web.Request) -> web.Response:
    """Serve the one-click installer onboarding page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.INSTALL_ONBOARD_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_mcp_page(request: web.Request) -> web.Response:
    """Serve the MCP install/setup page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.MCP_PAGE_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_mcp_guide_page(request: web.Request) -> web.Response:
    """Serve markdown-rendered MCP setup + route-plan docs."""
    core = _core()
    core._track_page_view(request)
    return web.Response(text=core._build_mcp_guide_html(), content_type="text/html")


def _build_research_desk_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Research Desk | Unchained</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <style>
    :root{--bg:#08111b;--panel:#10202b;--panel2:#132733;--line:rgba(183,205,214,0.16);--text:#edf4f5;--muted:#9ab0b7;--accent:#7be0b8;--accent2:#f4c55c}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--text);font-family:"Iowan Old Style","Palatino Linotype","Book Antiqua",serif;background:radial-gradient(circle at top left, rgba(123,224,184,0.12), transparent 32%),radial-gradient(circle at top right, rgba(244,197,92,0.10), transparent 24%),linear-gradient(180deg,#071018 0%,#08111b 55%,#050a0f 100%)}
    .shell{max-width:1160px;margin:0 auto;padding:32px 18px 64px}.hero,.grid{display:grid;gap:18px}.hero{grid-template-columns:1.15fr 0.85fr;margin-bottom:18px}.grid{grid-template-columns:0.92fr 1.08fr}
    .panel{border:1px solid var(--line);border-radius:24px;background:linear-gradient(180deg, rgba(19,39,51,0.94), rgba(10,20,27,0.94));padding:22px;box-shadow:0 20px 70px rgba(0,0,0,0.28);backdrop-filter:blur(10px)}
    .eyebrow{display:inline-flex;align-items:center;gap:8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:var(--accent);margin-bottom:10px}.eyebrow::before{content:"";width:10px;height:10px;border-radius:999px;background:var(--accent);box-shadow:0 0 18px rgba(123,224,184,0.55)}
    h1{margin:0 0 10px;font-size:clamp(32px,5vw,56px);line-height:0.96;letter-spacing:-0.04em}h2{margin:0 0 10px;font-size:28px;line-height:1.05;letter-spacing:-0.03em}p{margin:0;color:var(--muted);line-height:1.6}
    .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.btn,.pill{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;text-decoration:none}
    .btn{padding:12px 18px;border:1px solid var(--line);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase}.btn.primary{background:linear-gradient(90deg,var(--accent),#a8f7d9);color:#062018;border:none}.btn.secondary{background:rgba(255,255,255,0.04);color:var(--text)}.btn.disabled{opacity:0.45;pointer-events:none;cursor:default}
    .chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.pill{padding:8px 12px;border:1px solid var(--line);background:rgba(255,255,255,0.03);color:var(--muted);font-size:12px}
    .status-shell,.capsule-list{display:grid;gap:12px}.status-card,.capsule-card,.watch-card{border:1px solid var(--line);border-radius:18px;padding:14px 16px;background:rgba(255,255,255,0.02)}
    .status-card strong{display:block;margin-bottom:4px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent2)}
    .capsule-card h3,.watch-card h3{margin:0 0 6px;font-size:20px}.capsule-meta,.watch-meta{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.status-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.muted-note{margin-top:12px;font-size:13px;color:var(--muted)}code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:rgba(255,255,255,0.06);padding:2px 6px;border-radius:6px}
    .watch-card{display:none}.watch-card.visible{display:block}
    @media (max-width:900px){.hero,.grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="panel">
        <div class="eyebrow">Research Desk</div>
        <h1>From first look to a local dataframe and notebook.</h1>
        <p>Research Desk is the local product for turning a live web question into a shaped object, then opening <code>Lab Notes</code> with the right dataframe already loaded. This page only checks whether your local desk is running and what it has already shaped.</p>
        <div class="chips">
          <span class="pill">local-first</span>
          <span class="pill">browser to dataframe</span>
          <span class="pill">pyreplab backed</span>
        </div>
        <div class="actions">
          <a id="open-local-desk" class="btn primary disabled" href="http://127.0.0.1:8766/" target="_blank" rel="noreferrer" aria-disabled="true" tabindex="-1">Open Local Desk</a>
          <button id="connect-local-desk" class="btn secondary disabled" type="button" aria-disabled="true">Connect to Local Desk</button>
          <button id="create-local-mission" class="btn secondary disabled" type="button" aria-disabled="true">Create Mission in Local Desk</button>
          <a class="btn secondary" href="/first-look">Back to First Look</a>
          <a class="btn secondary" href="/mcp-guide">MCP Guide</a>
        </div>
        <p id="connect-note" class="muted-note">Hosted connection is available after the local desk is detected.</p>
      </div>
      <div class="panel">
        <div class="eyebrow">Local Setup</div>
        <div class="status-shell">
          <div class="status-card"><strong>1. Start the browser bridge</strong><p>Run <code>uv run unchained-pyreplab bridge-start</code> on this machine.</p></div>
          <div class="status-card"><strong>2. Start Research Desk</strong><p>Run <code>./scripts/serve_with_codex.sh</code> or <code>uv run unchained-pyreplab serve --open --reload</code>.</p></div>
          <div class="status-card"><strong>3. Return here</strong><p>This page probes <code>127.0.0.1:8766</code> for the local status and recent mission summaries.</p></div>
        </div>
      </div>
    </section>
    <section class="grid">
      <div class="panel">
        <div class="eyebrow">Local Status</div>
        <h2 id="status-title">Checking for a running desk...</h2>
        <p id="status-copy">Trying <code>http://127.0.0.1:8766/web/research-desk/status</code>.</p>
        <div id="status-chips" class="chips"></div>
        <div class="status-actions">
          <button id="retry-local-desk" class="btn secondary" type="button">Check Again</button>
        </div>
        <p id="status-note" class="muted-note">This page will keep checking for a local desk for about one minute.</p>
      </div>
      <div class="panel">
        <div class="eyebrow">Recent Missions</div>
        <h2>Your local desk stays local.</h2>
        <p style="margin-bottom:16px">This hosted page only reads the local summary surface. It does not execute notebook cells or mutate capsules.</p>
        <div id="mission-watch" class="watch-card">
          <strong style="display:block;margin-bottom:6px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent)">Current handoff</strong>
          <h3 id="mission-watch-title">No hosted mission yet.</h3>
          <p id="mission-watch-copy" class="muted">Create a local Mission from the current first-look prompt to watch it progress here.</p>
          <div id="mission-watch-meta" class="watch-meta"></div>
          <div class="actions" style="margin-top:12px">
            <a id="mission-watch-mission-link" class="btn secondary disabled" href="http://127.0.0.1:8766/" target="_blank" rel="noreferrer" aria-disabled="true" tabindex="-1">Open Mission</a>
            <a id="mission-watch-lab-link" class="btn secondary disabled" href="http://127.0.0.1:8766/" target="_blank" rel="noreferrer" aria-disabled="true" tabindex="-1">Open Lab Notes</a>
            <button id="run-local-next-step" class="btn secondary disabled" type="button" aria-disabled="true">Run Next Step</button>
          </div>
        </div>
        <div id="capsule-list" class="capsule-list"><div class="capsule-card"><p>Waiting for local Research Desk...</p></div></div>
      </div>
    </section>
  </main>
  <script>
    const FALLBACK_LOCAL_URL = 'http://127.0.0.1:8766/';
    const STATUS_URL = FALLBACK_LOCAL_URL + 'web/research-desk/status';
    const CAPSULES_URL = FALLBACK_LOCAL_URL + 'web/research-desk/capsules?limit=8';
    const POLL_INTERVAL_MS = 3000;
    const MAX_AUTO_POLL_ATTEMPTS = 20;
    const HANDSHAKE_POLL_INTERVAL_MS = 1500;
    const MAX_HANDSHAKE_POLL_ATTEMPTS = 40;
    const MISSION_WATCH_POLL_INTERVAL_MS = 2500;
    const MAX_MISSION_WATCH_ATTEMPTS = 20;
    const MAX_IDENTICAL_MISSION_STATES = 6;
    const MAX_TRANSIENT_HANDSHAKE_NOT_READY = 5;
    const FIRST_LOOK_PROMPT_KEY = 'unchained_first_look_last_prompt';
    const FIRST_LOOK_SESSION_KEY = 'unchained_first_look_last_session';
    const HANDSHAKE_TOKEN_KEY = 'research-desk-handshake-token';
    const HANDSHAKE_TOKEN_EXPIRES_KEY = 'research-desk-handshake-token-expires-at';
    const PENDING_HANDSHAKE_KEY = 'research-desk-pending-handshake';
    const MISSION_WATCH_URL_KEY = 'research-desk-mission-watch-url';
    const MISSION_WATCH_STATE_KEY = 'research-desk-mission-watch-state';
    const MAX_HANDOFF_PROMPT_CHARS = 4000;
    const MAX_URL_PROMPT_CHARS = 500;
    const SESSION_ID_RE = /^[A-Za-z0-9._:-]{1,120}$/;
    let deskPollAttempts = 0;
    let deskPollTimer = null;
    let deskLoadInFlight = false;
    let latestDeskRequestId = 0;
    let deskWasDetected = false;
    let latestDeskStatus = null;
    let handshakePollTimer = null;
    let approvedHandshakeToken = '';
    let handshakeInFlight = false;
    let missionWatchTimer = null;
    let missionWatchUrl = '';
    let missionWatchAttempts = 0;
    let missionWatchStableCount = 0;
    let missionWatchLastSignature = '';
    let missionWatchVisible = false;
    let missionCanAdvance = false;
    let pendingHandshakeState = null;
    let missionWatchSnapshot = null;
    let handshakeNotReadyCount = 0;
    let sessionRestored = false;
    function chip(text){const span=document.createElement('span');span.className='pill';span.textContent=text;return span;}
    function safeLocalUrl(value){try{const url=new URL(String(value||FALLBACK_LOCAL_URL));if(url.protocol!=='http:'&&url.protocol!=='https:') return FALLBACK_LOCAL_URL;const host=url.hostname.toLowerCase();/* any localhost port is accepted intentionally */if(host!=='127.0.0.1'&&host!=='localhost') return FALLBACK_LOCAL_URL;return url.toString();}catch(_err){return FALLBACK_LOCAL_URL;}}
    function safeOptionalLocalUrl(value){if(value===undefined||value===null||String(value).trim()==='') return '';try{const url=new URL(String(value));if(url.protocol!=='http:'&&url.protocol!=='https:') return '';const host=url.hostname.toLowerCase();/* any localhost port is accepted intentionally */if(host!=='127.0.0.1'&&host!=='localhost') return '';return url.toString();}catch(_err){return '';}}
    function sanitizeBearerToken(value){return String(value||'').replace(/[\\r\\n]+/g,'').replace(/[^\\x21-\\x7E]/g,'').trim();}
    function normalizeHandoffPrompt(value){return String(value||'').replace(/[\\u0000-\\u001f\\u007f]+/g,' ').trim().slice(0,MAX_HANDOFF_PROMPT_CHARS);}
    function normalizeSourceSessionId(value){const text=String(value||'').trim();return SESSION_ID_RE.test(text)?text:'';}
    /* Reconnect recovery keeps the short-lived hosted handshake token in sessionStorage so reloads can resume locally. This accepts same-origin script access in exchange for tab-scoped recovery. */
    function sessionValue(key){try{return window.sessionStorage.getItem(key)||'';}catch(_err){return '';}}
    function setSessionValue(key, value){try{if(value===undefined||value===null||String(value)===''){window.sessionStorage.removeItem(key);}else{window.sessionStorage.setItem(key, String(value));}}catch(_err){}}
    function sessionJson(key){try{const raw=window.sessionStorage.getItem(key);return raw ? JSON.parse(raw) : null;}catch(_err){return null;}}
    function setSessionJson(key, value){try{if(!value){window.sessionStorage.removeItem(key);}else{window.sessionStorage.setItem(key, JSON.stringify(value));}}catch(_err){}}
    function currentPromptFromContext(){try{const qs=new URLSearchParams(window.location.search);const prompt=normalizeHandoffPrompt(qs.get('prompt')||'');if(prompt){try{const next=new URL(window.location.href);next.searchParams.delete('prompt');next.searchParams.delete('session_id');window.history.replaceState({},'',next.pathname+(next.searchParams.toString()?('?'+next.searchParams.toString()):''));}catch(_err){}return prompt;}return normalizeHandoffPrompt(window.localStorage.getItem(FIRST_LOOK_PROMPT_KEY)||'');}catch(_err){return '';}}
    function currentSourceSessionId(){try{const qs=new URLSearchParams(window.location.search);const value=normalizeSourceSessionId(qs.get('session_id')||'');if(value) return value;return normalizeSourceSessionId(window.localStorage.getItem(FIRST_LOOK_SESSION_KEY)||'');}catch(_err){return '';}}
    function computeMissionAdvanceReady(){return Boolean(approvedHandshakeToken)&&Boolean(missionWatchVisible)&&Boolean(missionCanAdvance)&&Boolean(safeOptionalLocalUrl(latestDeskStatus?.handshake?.actions?.mission_advance_url));}
    function clearApprovedHandshake(){approvedHandshakeToken='';setSessionValue(HANDSHAKE_TOKEN_KEY,'');setSessionValue(HANDSHAKE_TOKEN_EXPIRES_KEY,'');}
    function persistApprovedHandshake(token, expiresAtEpoch){const safeToken=sanitizeBearerToken(token);const expiresAt=Number(expiresAtEpoch||0);if(!safeToken||!Number.isFinite(expiresAt)||expiresAt<=0){clearApprovedHandshake();return '';}approvedHandshakeToken=safeToken;setSessionValue(HANDSHAKE_TOKEN_KEY,safeToken);setSessionValue(HANDSHAKE_TOKEN_EXPIRES_KEY,String(expiresAt));return safeToken;}
    function restoreApprovedHandshake(){const safeToken=sanitizeBearerToken(sessionValue(HANDSHAKE_TOKEN_KEY));const expiresAt=Number(sessionValue(HANDSHAKE_TOKEN_EXPIRES_KEY)||0);if(!safeToken||!Number.isFinite(expiresAt)||expiresAt<=Date.now()/1000){clearApprovedHandshake();return '';}approvedHandshakeToken=safeToken;return safeToken;}
    function clearPendingHandshake(){handshakeInFlight=false;pendingHandshakeState=null;setSessionJson(PENDING_HANDSHAKE_KEY,null);}
    function persistPendingHandshake(statusUrl, requestId){const safeStatusUrl=safeOptionalLocalUrl(statusUrl);const safeRequestId=String(requestId||'').trim();if(!safeStatusUrl||!safeRequestId){clearPendingHandshake();return;}pendingHandshakeState={statusUrl:safeStatusUrl,requestId:safeRequestId,startAttempt:0};setSessionJson(PENDING_HANDSHAKE_KEY,{status_url:safeStatusUrl,request_id:safeRequestId,stored_at_epoch:Date.now()/1000});}
    function restorePendingHandshake(){const saved=sessionJson(PENDING_HANDSHAKE_KEY);const safeStatusUrl=safeOptionalLocalUrl(saved?.status_url||'');const safeRequestId=String(saved?.request_id||'').trim();const storedAtEpoch=Number(saved?.stored_at_epoch||0);if(!safeStatusUrl||!safeRequestId){clearPendingHandshake();return null;}handshakeInFlight=true;const elapsedSeconds=Number.isFinite(storedAtEpoch)&&storedAtEpoch>0 ? Math.max(0, (Date.now()/1000)-storedAtEpoch) : 0;const startAttempt=Math.min(Math.floor(elapsedSeconds/(HANDSHAKE_POLL_INTERVAL_MS/1000)), MAX_HANDSHAKE_POLL_ATTEMPTS);pendingHandshakeState={statusUrl:safeStatusUrl,requestId:safeRequestId,startAttempt};return pendingHandshakeState;}
    function clearMissionWatchState(resetView){setSessionValue(MISSION_WATCH_URL_KEY,'');setSessionJson(MISSION_WATCH_STATE_KEY,null);missionWatchUrl='';missionWatchSnapshot=null;missionWatchAttempts=0;missionWatchStableCount=0;missionWatchLastSignature='';if(resetView) renderMissionWatch(null);}
    function persistMissionWatchState(url, data){const safeUrl=safeOptionalLocalUrl(url);if(!safeUrl){clearMissionWatchState(false);return;}const previousUrl=missionWatchUrl;missionWatchUrl=safeUrl;setSessionValue(MISSION_WATCH_URL_KEY,safeUrl);/* Preserve the existing snapshot while the watch URL stays the same; URL changes intentionally drop the old snapshot. */if(previousUrl!==safeUrl){missionWatchSnapshot=null;setSessionJson(MISSION_WATCH_STATE_KEY,null);}if(data&&data.ok){missionWatchSnapshot=data;setSessionJson(MISSION_WATCH_STATE_KEY,{url:safeUrl,data});}}
    function restoreMissionWatchState(){const safeUrl=safeOptionalLocalUrl(sessionValue(MISSION_WATCH_URL_KEY));const saved=sessionJson(MISSION_WATCH_STATE_KEY);if(!safeUrl){clearMissionWatchState(false);return false;}missionWatchUrl=safeUrl;missionWatchSnapshot=saved&&saved.data&&saved.data.ok ? saved.data : null;if(missionWatchSnapshot){renderMissionWatch(missionWatchSnapshot);}return true;}
    function restoreStoredDeskSession(){const pending=restorePendingHandshake();const restoredToken=restoreApprovedHandshake();const restoredWatch=restoreMissionWatchState();sessionRestored=Boolean(pending||restoredToken||restoredWatch);if(pending){setConnectNote('Resuming the pending local approval check from your last hosted session...');return;}if(restoredToken&&restoredWatch){setConnectNote('Restored the last local mission snapshot while reconnecting to Research Desk.');return;}if(restoredToken){setConnectNote('Restored the approved local connection. Rechecking Research Desk now.');return;}if(restoredWatch){setConnectNote('Restored the last local mission snapshot while reconnecting to Research Desk.');}}
    function setMissionWatchLink(link, href, label, enabled){if(!link) return;link.textContent=label;link.href=enabled ? href : '#';link.classList.toggle('disabled', !enabled);link.setAttribute('aria-disabled', enabled ? 'false' : 'true');if(enabled){link.removeAttribute('tabindex');}else{link.setAttribute('tabindex','-1');}}
    function renderMissionWatch(data){
      const card=document.getElementById('mission-watch');
      const title=document.getElementById('mission-watch-title');
      const copy=document.getElementById('mission-watch-copy');
      const meta=document.getElementById('mission-watch-meta');
      const missionLink=document.getElementById('mission-watch-mission-link');
      const labLink=document.getElementById('mission-watch-lab-link');
      const nextButton=document.getElementById('run-local-next-step');
      if(!card||!title||!copy||!meta||!missionLink||!labLink||!nextButton) return;
      if(!data||!data.ok){
        missionWatchVisible=false;
        missionCanAdvance=false;
        card.classList.remove('visible');
        missionWatchUrl='';
        missionWatchSnapshot=null;
        missionWatchAttempts=0;
        missionWatchStableCount=0;
        missionWatchLastSignature='';
        setMissionWatchLink(missionLink, '#', 'Open Mission', false);
        setMissionWatchLink(labLink, '#', 'Open Lab Notes', false);
        nextButton.classList.add('disabled');
        nextButton.setAttribute('aria-disabled','true');
        nextButton.disabled=true;
        if(missionWatchTimer){window.clearTimeout(missionWatchTimer);missionWatchTimer=null;}
        return;
      }
      missionWatchVisible=true;
      missionCanAdvance=Boolean(data.can_advance);
      card.classList.add('visible');
      title.textContent=String(data.capsule_name||'mission');
      copy.textContent=String(data.blocked_reason||(data.advance_busy ? ('Running '+String(data.active_action_kind||data.autopilot_next_label||'next step')+' in the local desk.') : '')||data.autopilot_next_label||data.next_step||'Mission is progressing in the local desk.');
      meta.innerHTML='';
      const badges=[
        String(data.stage||'planning'),
        String(data.readiness_status||'planned'),
        String(data.primary_object_name||'object pending'),
        String(data.advance_busy ? 'running' : 'idle'),
        String((Number(data.primary_row_count||0))+' rows'),
        String(data.reviewed_page_count ? ('pages '+Number(data.reviewed_page_count||0)) : 'pages pending'),
        String(data.accepted_like_fraction ? ('qa '+Math.round(Number(data.accepted_like_fraction||0)*100)+'%') : 'qa pending'),
      ];
      badges.forEach((value)=>{const badge=document.createElement('span');badge.className='pill';badge.textContent=value;meta.appendChild(badge);});
      const missionUrl=safeOptionalLocalUrl(data.mission_url_abs||data.mission_url||'');
      const labUrl=safeOptionalLocalUrl(data.lab_url_abs||data.capsule_url_abs||data.capsule_url||'');
      setMissionWatchLink(missionLink, missionUrl||'#', 'Open Mission', Boolean(missionUrl));
      setMissionWatchLink(labLink, labUrl||'#', 'Open Lab Notes', Boolean(labUrl)&&Boolean(data.lab_ready));
      const nextReady=computeMissionAdvanceReady();
      nextButton.classList.toggle('disabled',!nextReady);
      nextButton.setAttribute('aria-disabled',nextReady?'false':'true');
      nextButton.disabled=!nextReady;
      nextButton.textContent=String(data.autopilot_next_label||'Run Next Step');
    }
    function scheduleMissionWatch(url){if(missionWatchTimer) window.clearTimeout(missionWatchTimer);const safeUrl=safeOptionalLocalUrl(url);if(!safeUrl){clearMissionWatchState(false);return;}if(missionWatchUrl!==safeUrl){missionWatchAttempts=0;missionWatchStableCount=0;missionWatchLastSignature='';}persistMissionWatchState(safeUrl, null);if(missionWatchAttempts>=MAX_MISSION_WATCH_ATTEMPTS){setConnectNote('Mission watch timed out. Open the local desk to continue from there.');return;}missionWatchTimer=window.setTimeout(async()=>{missionWatchTimer=null;missionWatchAttempts+=1;try{const resp=await fetch(safeUrl,{mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(5000)});if(!resp.ok){if(resp.status>=500&&missionWatchUrl===safeUrl&&missionWatchAttempts<MAX_MISSION_WATCH_ATTEMPTS){scheduleMissionWatch(safeUrl);return;}setConnectNote('Mission watch stopped after repeated local status failures. Open the local desk to continue.');return;}const data=await resp.json();renderMissionWatch(data);if(!data.ok){clearMissionWatchState(false);return;}persistMissionWatchState(safeUrl, data);const readiness=String(data.readiness_status||'');const signature=[String(data.stage||''),readiness,String(data.primary_row_count||0),String(data.next_step||''),String(data.autopilot_next_label||''),String(data.advance_busy||false)].join('|');if(data.advance_busy){setConnectNote('The local desk is still running the current step. This page will keep watching for the result.');scheduleMissionWatch(safeUrl);return;}if(signature===missionWatchLastSignature){missionWatchStableCount+=1;}else{missionWatchLastSignature=signature;missionWatchStableCount=0;}if(['final_ready','blocked'].includes(readiness)) return;if(missionWatchStableCount>=MAX_IDENTICAL_MISSION_STATES){setConnectNote('Mission watch paused because the local state stopped changing. Open the local desk to continue.');return;}scheduleMissionWatch(safeUrl);}catch(_err){if(missionWatchUrl===safeUrl&&missionWatchAttempts<MAX_MISSION_WATCH_ATTEMPTS){scheduleMissionWatch(safeUrl);return;}setConnectNote('Mission watch stopped after repeated local status failures. Open the local desk to continue.');}},MISSION_WATCH_POLL_INTERVAL_MS);}
    function setLaunchReady(ready, href){const link=document.getElementById('open-local-desk');if(!link) return;link.href=safeLocalUrl(href);link.classList.toggle('disabled', !ready);link.setAttribute('aria-disabled', ready ? 'false' : 'true');if(ready){link.removeAttribute('tabindex');}else{link.setAttribute('tabindex','-1');}}
    function setConnectReady(ready){const button=document.getElementById('connect-local-desk');if(!button) return;const enabled=Boolean(ready)&&!handshakeInFlight;button.classList.toggle('disabled', !enabled);button.setAttribute('aria-disabled', enabled ? 'false' : 'true');button.disabled=!enabled;}
    function setMissionCreateReady(ready){const button=document.getElementById('create-local-mission');if(!button) return;button.classList.toggle('disabled', !ready);button.setAttribute('aria-disabled', ready ? 'false' : 'true');button.disabled=!ready;}
    function setMissionAdvanceReady(ready){const button=document.getElementById('run-local-next-step');if(!button) return;button.classList.toggle('disabled', !ready);button.setAttribute('aria-disabled', ready ? 'false' : 'true');button.disabled=!ready;}
    function setConnectNote(text){const el=document.getElementById('connect-note');if(el) el.textContent=text;}
    function setStatusNote(text){const el=document.getElementById('status-note');if(el) el.textContent=text;}
    function resetCapsulesWaiting(text){const root=document.getElementById('capsule-list');if(!root) return;root.innerHTML='';const card=document.createElement('div');card.className='capsule-card';const body=document.createElement('p');body.textContent=text;card.appendChild(body);root.appendChild(card);}
    function scheduleDeskProbe(){if(deskPollTimer||deskPollAttempts>=MAX_AUTO_POLL_ATTEMPTS||document.hidden) return;deskPollTimer=window.setTimeout(()=>{deskPollTimer=null;deskPollAttempts+=1;loadDeskState({silent:true});},POLL_INTERVAL_MS);}
    function clearDeskProbe(){if(deskPollTimer){window.clearTimeout(deskPollTimer);deskPollTimer=null;}}
    function renderMissingDeskState(){latestDeskStatus=null;missionWatchVisible=false;missionCanAdvance=false;const title=document.getElementById('status-title');const copy=document.getElementById('status-copy');const chips=document.getElementById('status-chips');const hasRecoveryState=Boolean(approvedHandshakeToken||handshakeInFlight||missionWatchUrl||missionWatchSnapshot);title.textContent='Local Research Desk not detected yet.';copy.innerHTML='Start the local bridge and local desk on this machine, then click <strong>Check Again</strong> or wait for auto-detect.';chips.innerHTML='';chips.appendChild(chip('1. uv run unchained-pyreplab bridge-start'));chips.appendChild(chip('2. ./scripts/serve_with_codex.sh'));chips.appendChild(chip('3. keep this page open'));setLaunchReady(false, FALLBACK_LOCAL_URL);setConnectReady(false);setMissionCreateReady(false);setMissionAdvanceReady(false);if(handshakePollTimer){window.clearTimeout(handshakePollTimer);handshakePollTimer=null;}if(hasRecoveryState){setConnectNote('Local desk looks offline right now. The last mission snapshot is still shown below while this page keeps retrying.');}else{clearApprovedHandshake();clearPendingHandshake();clearMissionWatchState(true);setConnectNote('Hosted connection becomes available after the local desk is detected and ready.');}setStatusNote(deskPollAttempts >= MAX_AUTO_POLL_ATTEMPTS ? 'Automatic checking paused. Start the local desk, then click Check Again.' : 'Still checking every few seconds for a local desk on this machine.');resetCapsulesWaiting('Waiting for a running local Research Desk before showing recent missions.');scheduleDeskProbe();}
    function renderStatus(data){latestDeskStatus=data;deskWasDetected=true;clearDeskProbe();const title=document.getElementById('status-title');const copy=document.getElementById('status-copy');const chips=document.getElementById('status-chips');const launchUrl=safeLocalUrl(data.local_urls?.home);const configuredProvider=String(data.provider?.configured_provider||'unknown');const browserClient=String(data.provider?.browser_client||'');const trialStatus=String(data.trial?.status||'unknown');const launchReady=('launch_ready' in (data||{})) ? Boolean(data.launch_ready) : true;const handshakeStartUrl=safeOptionalLocalUrl(data.handshake?.start_url);const handshakeStatusUrl=safeOptionalLocalUrl(data.handshake?.status_url);const missionCreateUrl=safeOptionalLocalUrl(data.handshake?.actions?.mission_create_url);const handshakeReady=Boolean(data.handshake?.supported)&&launchReady&&Boolean(handshakeStartUrl)&&Boolean(handshakeStatusUrl);if(handshakeReady){handshakeNotReadyCount=0;}else{handshakeNotReadyCount+=1;if(handshakePollTimer){window.clearTimeout(handshakePollTimer);handshakePollTimer=null;}if(handshakeNotReadyCount>=MAX_TRANSIENT_HANDSHAKE_NOT_READY){clearApprovedHandshake();clearPendingHandshake();}}chips.innerHTML='';title.textContent=launchReady ? 'Local Research Desk detected.' : 'Local Research Desk found, but setup is incomplete.';copy.textContent='Hosted Unchained can see the local status surface. Use the local app for the full workflow and local notebook execution.';chips.appendChild(chip('provider: '+configuredProvider));chips.appendChild(chip('agent mode: '+String(data.provider?.agent_mode||'unknown')));if(browserClient) chips.appendChild(chip('browser client: '+browserClient));chips.appendChild(chip('trial: '+trialStatus));chips.appendChild(chip('bridge key: '+(data.bridge?.api_key_present?'present':'missing')));chips.appendChild(chip('agent: '+String(data.bridge?.agent_id||'not found')));chips.appendChild(chip('pyreplab: '+(data.pyreplab?.available?'ready':'missing')));chips.appendChild(chip('capsules: '+String(data.capsules?.count||0)));if(Array.isArray(data.missing) && data.missing.length) chips.appendChild(chip('missing: '+data.missing.join(', ')));setLaunchReady(launchReady, launchUrl);setConnectReady(handshakeReady);setMissionCreateReady(Boolean(approvedHandshakeToken)&&handshakeReady&&Boolean(missionCreateUrl));setMissionAdvanceReady(computeMissionAdvanceReady());if(handshakeInFlight){setConnectNote('Resuming the pending local approval check from your last hosted session...');}else if(approvedHandshakeToken){setConnectNote('Hosted connection approved. You can now create or advance a local Mission from here.');}else{setConnectNote(handshakeReady ? 'You can now request a trusted hosted connection to this local desk.' : 'Finish local setup before requesting a hosted connection.');}setStatusNote(launchReady ? 'Local Research Desk is ready. Open it directly or stay here to review local mission summaries.' : 'Local Research Desk responded, but setup is still incomplete. Fix the missing items in the local app, then try again.');if(handshakeReady&&handshakeInFlight&&!handshakePollTimer&&pendingHandshakeState){scheduleHandshakePoll(pendingHandshakeState.statusUrl, pendingHandshakeState.requestId, pendingHandshakeState.startAttempt||0);}if(missionWatchUrl&&!missionWatchTimer){scheduleMissionWatch(missionWatchUrl);}}
    function renderCapsules(data){const root=document.getElementById('capsule-list');root.innerHTML='';const rows=Array.isArray(data.capsules)?data.capsules:[];if(!rows.length){const emptyCard=document.createElement('div');emptyCard.className='capsule-card';const emptyText=document.createElement('p');emptyText.textContent='No local missions yet.';emptyCard.appendChild(emptyText);root.appendChild(emptyCard);return;}rows.forEach((row)=>{const el=document.createElement('article');el.className='capsule-card';const title=document.createElement('h3');title.textContent=String(row.capsule_name||'mission');const task=document.createElement('p');task.textContent=String(row.task||'').trim()||'No task summary yet.';const meta=document.createElement('div');meta.className='capsule-meta';const objectName=String(row.primary_object_name||'not shaped');const rowCount=Number(row.primary_row_count||0);const readiness=String(row.readiness_status||'planned');[objectName, `${rowCount} rows`, readiness].forEach((value)=>{const badge=document.createElement('span');badge.className='pill';badge.textContent=value;meta.appendChild(badge);});const next=document.createElement('p');next.textContent=String(row.next_step||'').trim()||'Open the local desk to continue.';el.appendChild(title);el.appendChild(task);el.appendChild(meta);el.appendChild(next);root.appendChild(el);});}
    function scheduleHandshakePoll(statusUrl, requestId, attempt){if(handshakePollTimer) window.clearTimeout(handshakePollTimer);const tries=Number(attempt||0);if(tries>=MAX_HANDSHAKE_POLL_ATTEMPTS){clearPendingHandshake();setConnectReady(Boolean(latestDeskStatus?.handshake?.supported)&&Boolean(('launch_ready' in (latestDeskStatus||{})) ? latestDeskStatus.launch_ready : true));setConnectNote('Timed out waiting for local approval. Try connecting again.');setMissionCreateReady(false);setMissionAdvanceReady(false);return;}handshakePollTimer=window.setTimeout(async()=>{handshakePollTimer=null;try{const pollUrl=new URL(statusUrl);pollUrl.searchParams.set('request_id', requestId);const resp=await fetch(pollUrl.toString(),{mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',signal:AbortSignal.timeout(5000)});if(!resp.ok){clearPendingHandshake();setConnectReady(Boolean(latestDeskStatus?.handshake?.supported)&&Boolean(('launch_ready' in (latestDeskStatus||{})) ? latestDeskStatus.launch_ready : true));setMissionCreateReady(false);setMissionAdvanceReady(false);setConnectNote('Local approval check failed. Try connecting again.');return;}const data=await resp.json();if(data.status==='approved'){clearPendingHandshake();persistApprovedHandshake(data.session_token, data.token_expires_at_epoch);setConnectReady(Boolean(latestDeskStatus?.handshake?.supported)&&Boolean(('launch_ready' in (latestDeskStatus||{})) ? latestDeskStatus.launch_ready : true));setMissionCreateReady(Boolean(approvedHandshakeToken)&&Boolean(safeOptionalLocalUrl(latestDeskStatus?.handshake?.actions?.mission_create_url)));setMissionAdvanceReady(computeMissionAdvanceReady());setConnectNote('Hosted connection approved. Local session token is ready for the next action.');return;}if(data.status==='denied'){clearPendingHandshake();clearApprovedHandshake();setConnectReady(Boolean(latestDeskStatus?.handshake?.supported)&&Boolean(('launch_ready' in (latestDeskStatus||{})) ? latestDeskStatus.launch_ready : true));setMissionCreateReady(false);setMissionAdvanceReady(false);setConnectNote('Hosted connection was denied in the local desk.');return;}const remainingSeconds=Math.max(0, Math.ceil(((MAX_HANDSHAKE_POLL_ATTEMPTS-(tries+1))*HANDSHAKE_POLL_INTERVAL_MS)/1000));setConnectNote('Waiting for local approval... about '+remainingSeconds+'s left.');scheduleHandshakePoll(statusUrl, requestId, tries+1);}catch(_err){const remainingSeconds=Math.max(0, Math.ceil(((MAX_HANDSHAKE_POLL_ATTEMPTS-(tries+1))*HANDSHAKE_POLL_INTERVAL_MS)/1000));setConnectNote('Waiting for local approval... about '+remainingSeconds+'s left.');scheduleHandshakePoll(statusUrl, requestId, tries+1);}},HANDSHAKE_POLL_INTERVAL_MS);}
    async function loadDeskState(options){const opts=options||{};if(deskLoadInFlight) return;deskLoadInFlight=true;const requestId=++latestDeskRequestId;try{const [statusResp,capsulesResp]=await Promise.all([fetch(STATUS_URL,{mode:'cors'}),fetch(CAPSULES_URL,{mode:'cors'})]);if(requestId!==latestDeskRequestId) return;if(!statusResp.ok) throw new Error('local status unavailable');renderStatus(await statusResp.json());if(capsulesResp.ok) renderCapsules(await capsulesResp.json());}catch(err){if(requestId!==latestDeskRequestId) return;if(!opts.silent) deskPollAttempts=0;if(opts.silent&&deskWasDetected){setStatusNote('Local Research Desk was previously detected. Recheck failed, but the current connected view is preserved.');return;}renderMissingDeskState();}finally{deskLoadInFlight=false;}}
    document.getElementById('retry-local-desk')?.addEventListener('click', ()=>{if(deskLoadInFlight) return;deskPollAttempts=0;clearDeskProbe();setStatusNote('Checking again for the local desk...');loadDeskState();});
    document.getElementById('connect-local-desk')?.addEventListener('click', async()=>{if(handshakeInFlight) return;const startUrl=safeOptionalLocalUrl(latestDeskStatus?.handshake?.start_url);const statusUrl=safeOptionalLocalUrl(latestDeskStatus?.handshake?.status_url);if(!startUrl||!statusUrl) return;handshakeInFlight=true;setConnectReady(false);clearApprovedHandshake();setMissionCreateReady(false);setMissionAdvanceReady(false);setConnectNote('Requesting local approval...');try{const body=new URLSearchParams({client_label:'Unchained First Look',requested_scope:'mission:create mission:advance'});const resp=await fetch(startUrl,{method:'POST',mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',headers:{'Content-Type':'application/x-www-form-urlencoded'},body,signal:AbortSignal.timeout(5000)});if(!resp.ok) throw new Error('handshake start failed');const data=await resp.json();const approvalUrl=safeOptionalLocalUrl(data.approval_url);if(approvalUrl){const approvalWindow=window.open(approvalUrl,'_blank','noopener');if(approvalWindow){setConnectNote('Approve the request in the local desk tab that just opened.');}else{setConnectNote('Approval tab could not be opened. Open the local desk manually and approve the pending request there.');}}else{setConnectNote('Approval tab could not be opened. Open the local desk manually and approve the pending request there.');}if(data.request_id){persistPendingHandshake(statusUrl,String(data.request_id));scheduleHandshakePoll(statusUrl,String(data.request_id),0);}else{clearPendingHandshake();setConnectReady(true);setConnectNote('Local desk did not return a request ID. Try connecting again.');}}catch(_err){clearPendingHandshake();setConnectReady(true);setConnectNote('Could not start the local approval flow. Make sure Research Desk is still running, then try again.');}});
    document.getElementById('create-local-mission')?.addEventListener('click', async()=>{const missionCreateUrl=safeOptionalLocalUrl(latestDeskStatus?.handshake?.actions?.mission_create_url);const safeToken=sanitizeBearerToken(approvedHandshakeToken);if(!safeToken||!missionCreateUrl) return;const handoffPrompt=currentPromptFromContext()||'Continue this First Look in Research Desk';setConnectNote('Creating a local Mission from hosted Unchained...');try{const body=new URLSearchParams({mission_prompt:handoffPrompt,source_route:'/first-look',source_session_id:currentSourceSessionId()});const resp=await fetch(missionCreateUrl,{method:'POST',mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',headers:{'Content-Type':'application/x-www-form-urlencoded','Authorization':'Bearer '+safeToken},body,signal:AbortSignal.timeout(5000)});const data=await resp.json();if(!resp.ok||!data.ok) throw new Error('mission create failed');if(data.mission_url){const missionUrl=safeLocalUrl(new URL(String(data.mission_url), FALLBACK_LOCAL_URL).toString());window.open(missionUrl,'_blank','noopener');}const statusUrl=safeOptionalLocalUrl(data.mission_status_url ? new URL(String(data.mission_status_url), FALLBACK_LOCAL_URL).toString() : '');if(statusUrl){persistMissionWatchState(statusUrl, null);scheduleMissionWatch(statusUrl);}setConnectNote('Local Mission created successfully. Research Desk opened it in a new tab.');}catch(_err){setConnectNote('Could not create the local Mission. Try reconnecting to the local desk first.');}});
    document.getElementById('run-local-next-step')?.addEventListener('click', async()=>{const missionAdvanceUrl=safeOptionalLocalUrl(latestDeskStatus?.handshake?.actions?.mission_advance_url);const safeToken=sanitizeBearerToken(approvedHandshakeToken);const capsuleName=String(document.getElementById('mission-watch-title')?.textContent||'').trim();if(!safeToken||!missionAdvanceUrl||!capsuleName) return;setConnectNote('Running the next local Mission step...');setMissionAdvanceReady(false);try{const body=new URLSearchParams({capsule_name:capsuleName});const resp=await fetch(missionAdvanceUrl,{method:'POST',mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',headers:{'Content-Type':'application/x-www-form-urlencoded','Authorization':'Bearer '+safeToken},body,signal:AbortSignal.timeout(5000)});const data=await resp.json();if(resp.status===429&&data.error==='advance_busy'){setConnectNote('The local desk is still running the current step. This page will keep watching for the result.');if(missionWatchUrl) scheduleMissionWatch(missionWatchUrl);setMissionAdvanceReady(false);return;}if(!resp.ok||!data.ok) throw new Error('mission advance failed');if(data.mission) renderMissionWatch(data.mission);const statusUrl=safeOptionalLocalUrl(data.mission?.mission_status_url||missionWatchUrl);if(statusUrl) scheduleMissionWatch(statusUrl);setConnectNote(String(data.message||'Local Mission step completed.'));}catch(_err){setConnectNote('Could not run the next local Mission step. Open the local desk to continue or reconnect first.');setMissionAdvanceReady(computeMissionAdvanceReady());}});
    document.addEventListener('visibilitychange', ()=>{if(document.hidden){clearDeskProbe();return;}if(!deskLoadInFlight) loadDeskState({silent:true});});
    window.addEventListener('beforeunload', ()=>{clearDeskProbe();if(handshakePollTimer){window.clearTimeout(handshakePollTimer);handshakePollTimer=null;}if(missionWatchTimer){window.clearTimeout(missionWatchTimer);missionWatchTimer=null;}});
    restoreStoredDeskSession();
    const existingPrompt=currentPromptFromContext();if(existingPrompt&&!sessionRestored){setConnectNote('Ready to hand off the current first-look prompt into your local desk once the connection is approved.');}
    loadDeskState();
  </script>
</body>
</html>"""


async def handle_research_desk_page(request: web.Request) -> web.Response:
    """Serve the hosted launch page for the local Research Desk app."""
    core = _core()
    core._track_page_view(request)
    return web.Response(text=_build_research_desk_html(), content_type="text/html")


async def handle_tab_page(request: web.Request) -> web.Response:
    """Serve the lightweight branded default tab page."""
    del request
    core = _core()
    return web.Response(
        text=core.BRANDED_TAB_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def handle_trial_page(request: web.Request) -> web.Response:
    """Serve the trial chat HTML page (OpenRouter models)."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.TRIAL_CHAT_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_gemini_page(request: web.Request) -> web.Response:
    """Serve the Gemini SDK chat HTML page (per-user provisioned key)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_GEMINI_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_codex_page(request: web.Request) -> web.Response:
    """Serve the Codex chat HTML page (per-user provisioned key)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_CODEX_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_claude_page(request: web.Request) -> web.Response:
    """Serve the Claude SDK chat HTML page (per-user provisioned key)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CHAT_CLAUDE_SDK_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_case_study_zillow(request: web.Request) -> web.Response:
    """Serve the Zillow rental relisting case study page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.CASE_STUDY_ZILLOW_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_privacy_page(request: web.Request) -> web.Response:
    """Serve public privacy policy page (required for OAuth provider submissions)."""
    core = _core()
    core._track_page_view(request)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Privacy Policy | Unchained</title>
</head>
<body style="margin:0;padding:32px 18px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b1020;color:#e6ecff;line-height:1.6">
  <main style="max-width:880px;margin:0 auto">
    <h1 style="margin:0 0 6px;font-size:32px">Privacy Policy</h1>
    <p style="margin:0 0 20px;color:#a8b3cf">Last updated: March 6, 2026</p>
    <p>Unchained provides browser automation and chat tooling. This policy describes how we collect, use, and protect information when you use our services.</p>
    <h2>Information We Collect</h2>
    <ul>
      <li>Account information you provide during sign-in (name, email, avatar from Google/Facebook when authorized).</li>
      <li>Session and usage data needed to operate chat, authentication, and rate limits.</li>
      <li>Operational logs and analytics events for reliability, abuse prevention, and product improvement.</li>
    </ul>
    <h2>How We Use Information</h2>
    <ul>
      <li>Authenticate users and secure accounts.</li>
      <li>Provide and improve product functionality.</li>
      <li>Detect abuse, enforce limits, and maintain service reliability.</li>
    </ul>
    <h2>Data Sharing</h2>
    <p>We do not sell personal data. We share data only with service providers required to run the platform (for example hosting, email delivery, and model providers), subject to contractual safeguards.</p>
    <h2>Data Retention</h2>
    <p>We retain account and operational data for as long as needed to provide the service, meet legal requirements, resolve disputes, and enforce agreements.</p>
    <h2>Your Rights</h2>
    <p>You can request account/data deletion via <a href="/data-deletion" style="color:#7dd3fc">/data-deletion</a>.</p>
    <h2>Contact</h2>
    <p>Questions about this policy: <a href="mailto:{core.CONTACT_EMAIL}" style="color:#7dd3fc">{core.CONTACT_EMAIL}</a></p>
  </main>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_data_deletion_page(request: web.Request) -> web.Response:
    """Serve public user-data deletion instructions page for OAuth compliance."""
    core = _core()
    core._track_page_view(request)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>User Data Deletion | Unchained</title>
</head>
<body style="margin:0;padding:32px 18px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b1020;color:#e6ecff;line-height:1.6">
  <main style="max-width:880px;margin:0 auto">
    <h1 style="margin:0 0 6px;font-size:32px">User Data Deletion</h1>
    <p style="margin:0 0 20px;color:#a8b3cf">Last updated: March 6, 2026</p>
    <p>If you want your Unchained account and related personal data deleted, send a request from your account email to:</p>
    <p><a href="mailto:{core.CONTACT_EMAIL}" style="color:#7dd3fc">{core.CONTACT_EMAIL}</a></p>
    <h2>What to Include</h2>
    <ul>
      <li>Your account email address.</li>
      <li>Subject line: <code>Data Deletion Request</code>.</li>
    </ul>
    <h2>What Happens Next</h2>
    <ul>
      <li>We verify account ownership.</li>
      <li>We delete or anonymize eligible personal data from active systems.</li>
      <li>We may retain limited records where required for legal/security obligations.</li>
    </ul>
    <p>For policy details, see <a href="/privacy" style="color:#7dd3fc">/privacy</a>.</p>
  </main>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_first_look_page(request: web.Request) -> web.Response:
    """Serve the headless first-look chat HTML page."""
    core = _core()
    ref = request.query.get('ref', '')[:64]
    core._track_page_view(request, meta={'ref': ref} if ref else None)
    html = core.inject_google_client_id(core.HEADLESS_DEMO_HTML, core.GOOGLE_CLIENT_ID)
    resp = web.Response(text=html, content_type="text/html")
    _, guest_id, quota_count = core._first_look_guest_auth(request)
    core._attach_first_look_guest_cookies(resp, request, guest_id, quota_count=quota_count)
    return resp


async def handle_demo_page(request: web.Request) -> web.Response:
    """Redirect /demo to /first-look for backward compatibility."""
    core = _core()
    core._track_redirect(request, "/first-look", reason="legacy_route_alias")
    raise web.HTTPFound("/first-look")


async def handle_local_page(request: web.Request) -> web.Response:
    """Serve the local agent chat HTML page (Claude CLI + Codex CLI)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.CLAUDE_CHAT_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_claude_page(request: web.Request) -> web.Response:
    """Redirect /app to /local for backward compatibility."""
    core = _core()
    core._track_page_view(request)
    core._track_redirect(request, "/local", reason="legacy_route_alias")
    raise web.HTTPFound("/local")


async def handle_chat_redirect(request: web.Request) -> web.Response:
    """Redirect /chat to /local for backward compatibility."""
    core = _core()
    core._track_redirect(request, "/local", reason="legacy_route_alias")
    raise web.HTTPFound("/local")
