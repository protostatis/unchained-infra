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
    .status-shell,.capsule-list{display:grid;gap:12px}.status-card,.capsule-card{border:1px solid var(--line);border-radius:18px;padding:14px 16px;background:rgba(255,255,255,0.02)}
    .status-card strong{display:block;margin-bottom:4px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent2)}
    .capsule-card h3{margin:0 0 6px;font-size:20px}.capsule-meta{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.status-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.muted-note{margin-top:12px;font-size:13px;color:var(--muted)}code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:rgba(255,255,255,0.06);padding:2px 6px;border-radius:6px}
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
          <a class="btn secondary" href="/first-look">Back to First Look</a>
          <a class="btn secondary" href="/mcp-guide">MCP Guide</a>
        </div>
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
    let deskPollAttempts = 0;
    let deskPollTimer = null;
    let deskLoadInFlight = false;
    function chip(text){const span=document.createElement('span');span.className='pill';span.textContent=text;return span;}
    function safeLocalUrl(value){try{const url=new URL(String(value||FALLBACK_LOCAL_URL));if(url.protocol!=='http:'&&url.protocol!=='https:') return FALLBACK_LOCAL_URL;const host=url.hostname.toLowerCase();/* any localhost port is accepted intentionally */if(host!=='127.0.0.1'&&host!=='localhost') return FALLBACK_LOCAL_URL;return url.toString();}catch(_err){return FALLBACK_LOCAL_URL;}}
    function setLaunchReady(ready, href){const link=document.getElementById('open-local-desk');if(!link) return;link.href=safeLocalUrl(href);link.classList.toggle('disabled', !ready);link.setAttribute('aria-disabled', ready ? 'false' : 'true');if(ready){link.removeAttribute('tabindex');}else{link.setAttribute('tabindex','-1');}}
    function setStatusNote(text){const el=document.getElementById('status-note');if(el) el.textContent=text;}
    function resetCapsulesWaiting(text){const root=document.getElementById('capsule-list');if(!root) return;root.innerHTML='';const card=document.createElement('div');card.className='capsule-card';const body=document.createElement('p');body.textContent=text;card.appendChild(body);root.appendChild(card);}
    function scheduleDeskProbe(){if(deskPollTimer||deskPollAttempts>=MAX_AUTO_POLL_ATTEMPTS||document.hidden) return;deskPollTimer=window.setTimeout(()=>{deskPollTimer=null;deskPollAttempts+=1;loadDeskState({silent:true});},POLL_INTERVAL_MS);}
    function clearDeskProbe(){if(deskPollTimer){window.clearTimeout(deskPollTimer);deskPollTimer=null;}}
    function renderMissingDeskState(){const title=document.getElementById('status-title');const copy=document.getElementById('status-copy');const chips=document.getElementById('status-chips');title.textContent='Local Research Desk not detected yet.';copy.innerHTML='Start the local bridge and local desk on this machine, then click <strong>Check Again</strong> or wait for auto-detect.';chips.innerHTML='';chips.appendChild(chip('1. uv run unchained-pyreplab bridge-start'));chips.appendChild(chip('2. ./scripts/serve_with_codex.sh'));chips.appendChild(chip('3. keep this page open'));setLaunchReady(false, FALLBACK_LOCAL_URL);setStatusNote(deskPollAttempts >= MAX_AUTO_POLL_ATTEMPTS ? 'Automatic checking paused. Start the local desk, then click Check Again.' : 'Still checking every few seconds for a local desk on this machine.');resetCapsulesWaiting('Waiting for a running local Research Desk before showing recent missions.');scheduleDeskProbe();}
    function renderStatus(data){clearDeskProbe();const title=document.getElementById('status-title');const copy=document.getElementById('status-copy');const chips=document.getElementById('status-chips');const launchUrl=safeLocalUrl(data.local_urls?.home);const configuredProvider=String(data.provider?.configured_provider||'unknown');const browserClient=String(data.provider?.browser_client||'');const trialStatus=String(data.trial?.status||'unknown');const launchReady=('launch_ready' in (data||{})) ? Boolean(data.launch_ready) : true;chips.innerHTML='';title.textContent=launchReady ? 'Local Research Desk detected.' : 'Local Research Desk found, but setup is incomplete.';copy.textContent='Hosted Unchained can see the local status surface. Use the local app for the full workflow and local notebook execution.';chips.appendChild(chip('provider: '+configuredProvider));chips.appendChild(chip('agent mode: '+String(data.provider?.agent_mode||'unknown')));if(browserClient) chips.appendChild(chip('browser client: '+browserClient));chips.appendChild(chip('trial: '+trialStatus));chips.appendChild(chip('bridge key: '+(data.bridge?.api_key_present?'present':'missing')));chips.appendChild(chip('agent: '+String(data.bridge?.agent_id||'not found')));chips.appendChild(chip('pyreplab: '+(data.pyreplab?.available?'ready':'missing')));chips.appendChild(chip('capsules: '+String(data.capsules?.count||0)));if(Array.isArray(data.missing) && data.missing.length) chips.appendChild(chip('missing: '+data.missing.join(', ')));setLaunchReady(launchReady, launchUrl);setStatusNote(launchReady ? 'Local Research Desk is ready. Open it directly or stay here to review local mission summaries.' : 'Local Research Desk responded, but setup is still incomplete. Fix the missing items in the local app, then try again.');}
    function renderCapsules(data){const root=document.getElementById('capsule-list');root.innerHTML='';const rows=Array.isArray(data.capsules)?data.capsules:[];if(!rows.length){const emptyCard=document.createElement('div');emptyCard.className='capsule-card';const emptyText=document.createElement('p');emptyText.textContent='No local missions yet.';emptyCard.appendChild(emptyText);root.appendChild(emptyCard);return;}rows.forEach((row)=>{const el=document.createElement('article');el.className='capsule-card';const title=document.createElement('h3');title.textContent=String(row.capsule_name||'mission');const task=document.createElement('p');task.textContent=String(row.task||'').trim()||'No task summary yet.';const meta=document.createElement('div');meta.className='capsule-meta';const objectName=String(row.primary_object_name||'not shaped');const rowCount=Number(row.primary_row_count||0);const readiness=String(row.readiness_status||'planned');[objectName, `${rowCount} rows`, readiness].forEach((value)=>{const badge=document.createElement('span');badge.className='pill';badge.textContent=value;meta.appendChild(badge);});const next=document.createElement('p');next.textContent=String(row.next_step||'').trim()||'Open the local desk to continue.';el.appendChild(title);el.appendChild(task);el.appendChild(meta);el.appendChild(next);root.appendChild(el);});}
    async function loadDeskState(options){const opts=options||{};if(deskLoadInFlight) return;deskLoadInFlight=true;try{const [statusResp,capsulesResp]=await Promise.all([fetch(STATUS_URL,{mode:'cors'}),fetch(CAPSULES_URL,{mode:'cors'})]);if(!statusResp.ok) throw new Error('local status unavailable');renderStatus(await statusResp.json());if(capsulesResp.ok) renderCapsules(await capsulesResp.json());}catch(err){if(!opts.silent) deskPollAttempts=0;renderMissingDeskState();}finally{deskLoadInFlight=false;}}
    document.getElementById('retry-local-desk')?.addEventListener('click', ()=>{if(deskLoadInFlight) return;deskPollAttempts=0;clearDeskProbe();setStatusNote('Checking again for the local desk...');loadDeskState();});
    document.addEventListener('visibilitychange', ()=>{if(document.hidden){clearDeskProbe();return;}if(!deskLoadInFlight) loadDeskState({silent:true});});
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
    core._track_page_view(request)
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
