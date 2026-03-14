"""Shared helpers for HTML template rendering."""

from __future__ import annotations

import json
import os


_ANALYTICS_CLIENT_SNIPPET = r"""<script data-uc-analytics-client>
(function(){
  var ROUTE = window.location.pathname || '';
  var STORAGE_KEY = 'uc_analytics_session_id';
  var SESSION_HEADER = 'X-Unchained-Analytics-Session';
  var PAGE_VIEW_HEADER = 'X-Unchained-Analytics-Page-View';
  var ROUTE_HEADER = 'X-Unchained-Analytics-Route';
  var GATE_HEADER = 'X-Unchained-Analytics-Gate-Type';
  var EVENT_SEQ = 0;
  var queue = [];
  var flushTimer = null;
  var warned = {};
  var gsiClicked = false;
  var gateShown = false;
  var gsiIframeSeen = false;

  function randHex(len){
    var out = '';
    var chars = '0123456789abcdef';
    for(var i = 0; i < len; i++){
      out += chars[Math.floor(Math.random() * chars.length)];
    }
    return out;
  }

  function getSessionId(){
    var sid = '';
    try{ sid = sessionStorage.getItem(STORAGE_KEY) || ''; }catch(_e){}
    if(!sid){
      sid = 's-' + Date.now().toString(36) + '-' + randHex(8);
      try{ sessionStorage.setItem(STORAGE_KEY, sid); }catch(_e2){}
    }
    return sid;
  }

  var sessionId = getSessionId();
  var pageViewId = 'pv-' + Date.now().toString(36) + '-' + randHex(8);
  window.__ucAnalytics = {
    sessionId: sessionId,
    pageViewId: pageViewId,
    route: ROUTE
  };

  function inferGateType(){
    var login = document.getElementById('login');
    if(login){
      var st = window.getComputedStyle(login);
      var shown = st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || '1') !== 0;
      if(shown){
        if(document.querySelector('.g_id_signin')) return 'inline_gsi';
        return 'inline_gate';
      }
    }
    var signinLink = document.querySelector('a[href*="/local?next="]');
    if(signinLink) return 'link_signin';
    return 'none';
  }

  function detectCta(ev){
    var t = ev && ev.target;
    if(!t) return '';
    var node = t.closest ? t.closest('a,button,[role=button],input[type=button],input[type=submit]') : t;
    if(!node) return '';
    if(node.dataset && node.dataset.analyticsCta) return String(node.dataset.analyticsCta || '').trim();

    var id = (node.id || '').trim();
    if(id){
      if(id === 'install-btn') return 'install_download_native';
      if(id === 'banner-connect') return 'banner_connect';
      if(id === 'setup-banner-connect') return 'setup_banner_connect';
      if(id === 'banner-curl') return 'banner_install_curl';
      if(id === 'setup-banner-curl') return 'setup_banner_install_curl';
    }

    var href = '';
    try{ href = (node.getAttribute && node.getAttribute('href')) || ''; }catch(_e){}
    href = String(href || '');
    if(href.indexOf('/local?next=') !== -1){
      if(ROUTE === '/install') return 'install_signin_link';
      return 'signin_link';
    }
    if(href.indexOf('/web/download-installer') !== -1) return 'download_installer';
    if(href.indexOf('/web/download-agent') !== -1) return 'download_agent_zip';
    if(href === '/install') return 'open_install_page';
    if(href === '/trial') return 'open_trial_page';
    return id ? ('id_' + id) : '';
  }

  function analyticsHeaders(existing){
    var headers = new Headers(existing || {});
    headers.set(SESSION_HEADER, sessionId);
    headers.set(PAGE_VIEW_HEADER, pageViewId);
    headers.set(ROUTE_HEADER, ROUTE);
    headers.set(GATE_HEADER, inferGateType());
    return headers;
  }

  function normalizeFetchUrl(input){
    try{
      if(typeof Request !== 'undefined' && input instanceof Request){
        return new URL(String(input.url || ''), window.location.origin);
      }
      return new URL(String(input || ''), window.location.origin);
    }catch(_e){
      return null;
    }
  }

  function shouldAttachAnalytics(urlObj){
    if(!urlObj) return false;
    if(urlObj.origin !== window.location.origin) return false;
    return urlObj.pathname.indexOf('/web/') === 0 || urlObj.pathname.indexOf('/auth/') === 0;
  }

  if(window.fetch){
    var originalFetch = window.fetch.bind(window);
    window.fetch = function(input, init){
      var urlObj = normalizeFetchUrl(input);
      if(!shouldAttachAnalytics(urlObj)){
        return originalFetch(input, init);
      }
      if(typeof Request !== 'undefined' && input instanceof Request){
        var requestInit = init ? Object.assign({}, init) : {};
        requestInit.headers = analyticsHeaders(requestInit.headers || input.headers);
        return originalFetch(new Request(input, requestInit));
      }
      var nextInit = init ? Object.assign({}, init) : {};
      nextInit.headers = analyticsHeaders(nextInit.headers);
      return originalFetch(input, nextInit);
    };
  }

  function makeEvent(eventName, opts){
    if(!eventName) return null;
    var options = (opts && typeof opts === 'object') ? opts : {};
    EVENT_SEQ += 1;
    var meta = (options.meta && typeof options.meta === 'object') ? options.meta : {};
    return {
      event: eventName,
      event_id: 'ev-' + Date.now().toString(36) + '-' + EVENT_SEQ + '-' + randHex(5),
      session_id: sessionId,
      page_view_id: options.page_view_id || meta.page_view_id || pageViewId,
      route: options.route || ROUTE,
      route_intended: options.route_intended || ROUTE,
      route_effective: options.route_effective || ROUTE,
      gate_type: options.gate_type || meta.gate_type || '',
      cta_id: options.cta_id || meta.cta_id || '',
      error_code: options.error_code || meta.error_code || '',
      latency_ms: Number(options.latency_ms || meta.latency_ms || 0) || 0,
      source: options.source || 'web',
      meta: meta
    };
  }

  function flushQueue(forceSingle){
    if(!queue.length) return;
    var batch = queue.splice(0, Math.min(queue.length, 25));
    var body = {events: batch};
    try{
      fetch('/web/analytics/events', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        keepalive: true,
        body: JSON.stringify(body)
      }).catch(function(){
        if(!forceSingle && batch[0]){
          fetch('/web/analytics/event', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'same-origin',
            keepalive: true,
            body: JSON.stringify(batch[0])
          }).catch(function(){});
        }
      });
    }catch(_e){
      if(!forceSingle && batch[0]){
        try{
          fetch('/web/analytics/event', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'same-origin',
            keepalive: true,
            body: JSON.stringify(batch[0])
          });
        }catch(_e2){}
      }
    }
  }

  function scheduleFlush(){
    if(flushTimer) return;
    flushTimer = setTimeout(function(){
      flushTimer = null;
      flushQueue(false);
    }, 600);
  }

  function postEvent(eventName, opts){
    var payload = makeEvent(eventName, opts);
    if(!payload) return;
    queue.push(payload);
    scheduleFlush();
  }

  function isVisible(el){
    if(!el) return false;
    var st = window.getComputedStyle(el);
    if(st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || '1') === 0) return false;
    var rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function maybeTrackGate(){
    var gateType = inferGateType();
    if(gateType === 'none') return false;
    if(gateShown) return true;
    gateShown = true;
    postEvent('gate_shown', {gate_type: gateType, meta: {gate_type: gateType}});
    if(gateType === 'inline_gsi' || gateType === 'inline_gate'){
      postEvent('login_gate_visible', {gate_type: gateType, meta: {gate_type: gateType}});
    }
    return true;
  }

  function maybeTrackGsiIframe(){
    if(gsiIframeSeen) return true;
    var iframes = document.querySelectorAll('iframe');
    for(var i = 0; i < iframes.length; i++){
      var src = (iframes[i].getAttribute('src') || '').toLowerCase();
      if(src.indexOf('accounts.google.com') !== -1 || src.indexOf('gsi') !== -1){
        gsiIframeSeen = true;
        postEvent('gsi_iframe_loaded', {gate_type: 'inline_gsi'});
        return true;
      }
    }
    return false;
  }

  function startAnalyticsObservers(){
    var observer = null;
    var polls = 0;
    var timer = setInterval(function(){
      polls += 1;
      maybeTrackGate();
      maybeTrackGsiIframe();
      if((gateShown && gsiIframeSeen) || polls >= 30){
        clearInterval(timer);
        if(observer) observer.disconnect();
      }
    }, 400);

    if(window.MutationObserver && document.documentElement){
      observer = new MutationObserver(function(){
        maybeTrackGate();
        maybeTrackGsiIframe();
        watchWarnings();
        if(gateShown && gsiIframeSeen){
          observer.disconnect();
        }
      });
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['style', 'class', 'hidden']
      });
    }
  }

  function watchWarnings(){
    var nodes = document.querySelectorAll(
      '#loginerr,[id*="error"],[id*="warn"],[class*="error"],[class*="warn"],.status,.notice'
    );
    for(var i = 0; i < nodes.length; i++){
      var node = nodes[i];
      if(!isVisible(node)) continue;
      var txt = String(node.textContent || '').trim();
      if(!txt || txt.length < 3) continue;
      var key = (node.id || node.className || 'anon') + '|' + txt.slice(0, 120);
      if(warned[key]) continue;
      warned[key] = 1;
      postEvent('warning_shown', {
        cta_id: node.id || '',
        error_code: txt.slice(0, 64).toLowerCase().replace(/\s+/g, '_'),
        meta: {message: txt.slice(0, 240)}
      });
    }
  }

  function bindClicks(){
    document.addEventListener('click', function(ev){
      var t = ev && ev.target;
      if(!t) return;

      var gsiHit = false;
      if(t.closest && t.closest('.g_id_signin')){
        gsiHit = true;
      }else if(t.tagName === 'IFRAME'){
        var src = (t.getAttribute('src') || '').toLowerCase();
        gsiHit = src.indexOf('accounts.google.com') !== -1 || src.indexOf('gsi') !== -1;
      }

      if(gsiHit && !gsiClicked){
        gsiClicked = true;
        postEvent('gsi_click', {gate_type: 'inline_gsi', cta_id: 'gsi_button'});
        postEvent('google_signin_click', {gate_type: 'inline_gsi', cta_id: 'gsi_button'});
      }

      var ctaId = detectCta(ev);
      if(ctaId){
        postEvent('cta_click', {cta_id: ctaId, gate_type: inferGateType(), meta: {cta_id: ctaId}});
        if(ctaId === 'install_signin_link'){
          postEvent('install_signin_click', {cta_id: ctaId, gate_type: 'link_signin'});
        }
      }
    }, true);
  }

  function init(){
    setTimeout(maybeTrackGate, 300);
    setTimeout(maybeTrackGate, 1200);
    startAnalyticsObservers();
    bindClicks();
    setInterval(watchWarnings, 1800);
    setTimeout(watchWarnings, 1400);

    window.addEventListener('pagehide', function(){
      flushQueue(true);
    });
    document.addEventListener('visibilitychange', function(){
      if(document.visibilityState === 'hidden'){
        flushQueue(true);
      }
    });
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', init, {once: true});
  }else{
    init();
  }
})();
</script>"""

_FACEBOOK_LOGIN_SNIPPET_TEMPLATE = r"""<script data-uc-facebook-login>
(function(){
  var FB_APP_ID = __UC_FACEBOOK_APP_ID__;
  var GH_CLIENT_ID = __UC_GITHUB_CLIENT_ID__;
  if(!FB_APP_ID && !GH_CLIENT_ID) return;

  function inferSource(){
    var p = window.location.pathname || '';
    if(p === '/trial' || p === '/first-look' || p === '/demo' || p.indexOf('/trial/') === 0){
      return 'trial';
    }
    return 'claude';
  }

  function currentNextPath(){
    return (window.location.pathname || '/local') + (window.location.search || '');
  }

  function authErrorMessage(code){
    if(code === 'facebook_email_required') return 'Facebook account email is required. Please share your email to continue.';
    if(code === 'pending_review') return 'Your sign-up request is still being reviewed.';
    if(code === 'rejected') return 'Your sign-up request was not approved.';
    if(code === 'facebook_denied') return 'Facebook sign-in was canceled.';
    if(code === 'facebook_state_invalid') return 'Facebook sign-in expired. Please try again.';
    if(code === 'facebook_exchange_failed') return 'Facebook sign-in failed during token exchange.';
    if(code === 'facebook_profile_failed') return 'Facebook sign-in failed while reading your profile.';
    if(code === 'facebook_not_configured') return 'Facebook sign-in is not configured yet.';
    if(code === 'github_email_required') return 'GitHub account email is required. Add a public/verified email and try again.';
    if(code === 'github_denied') return 'GitHub sign-in was canceled.';
    if(code === 'github_state_invalid') return 'GitHub sign-in expired. Please try again.';
    if(code === 'github_exchange_failed') return 'GitHub sign-in failed during token exchange.';
    if(code === 'github_profile_failed') return 'GitHub sign-in failed while reading your profile.';
    if(code === 'github_not_configured') return 'GitHub sign-in is not configured yet.';
    return 'Social sign-in failed. Please try again.';
  }

  function renderQueryError(){
    var errEl = document.getElementById('loginerr');
    if(!errEl) return;
    try{
      var params = new URLSearchParams(window.location.search || '');
      var code = String(params.get('auth_error') || '').trim();
      if(!code) return;
      errEl.textContent = authErrorMessage(code);
      params.delete('auth_error');
      var cleanQs = params.toString();
      var cleanUrl = window.location.pathname + (cleanQs ? ('?' + cleanQs) : '') + (window.location.hash || '');
      window.history.replaceState({}, document.title, cleanUrl);
    }catch(_err){}
  }

  function injectAuthButtonStyles(){
    if(document.getElementById('uc-auth-btn-style')) return;
    var style = document.createElement('style');
    style.id = 'uc-auth-btn-style';
    style.textContent = [
      '#login #fb-login-btn,#login #gh-login-btn{width:min(100%,360px) !important;height:48px !important;border-radius:12px !important;}'
    ].join('');
    if(document.head){
      document.head.appendChild(style);
    }
  }

  function insertFacebookButton(){
    if(!FB_APP_ID) return;
    var login = document.getElementById('login');
    if(!login) return;
    if(document.getElementById('fb-login-btn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'fb-login-btn';
    btn.textContent = 'Continue with Facebook';
    btn.style.cssText = 'display:block;width:320px;height:44px;border:none;border-radius:8px;background:#1877f2;color:#fff;font-size:15px;font-weight:600;cursor:pointer;margin-top:10px';
    btn.addEventListener('click', function(){
      var source = inferSource();
      var next = currentNextPath();
      var url = '/auth/facebook/start?source=' + encodeURIComponent(source) + '&next=' + encodeURIComponent(next);
      window.location.href = url;
    });

    var gsi = login.querySelector('.g_id_signin');
    if(gsi && gsi.parentNode){
      gsi.insertAdjacentElement('afterend', btn);
      return;
    }
    var loginErr = login.querySelector('#loginerr');
    if(loginErr && loginErr.parentNode){
      loginErr.insertAdjacentElement('beforebegin', btn);
      return;
    }
    login.appendChild(btn);
  }

  function insertGithubButton(){
    if(!GH_CLIENT_ID) return;
    var login = document.getElementById('login');
    if(!login) return;
    if(document.getElementById('gh-login-btn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'gh-login-btn';
    btn.textContent = 'Continue with GitHub';
    btn.style.cssText = 'display:block;width:320px;height:44px;border:1px solid #30363d;border-radius:8px;background:#0d1117;color:#f0f6fc;font-size:15px;font-weight:600;cursor:pointer;margin-top:10px';
    btn.addEventListener('click', function(){
      var source = inferSource();
      var next = currentNextPath();
      var url = '/auth/github/start?source=' + encodeURIComponent(source) + '&next=' + encodeURIComponent(next);
      window.location.href = url;
    });

    var fb = login.querySelector('#fb-login-btn');
    if(fb && fb.parentNode){
      fb.insertAdjacentElement('afterend', btn);
      return;
    }
    var gsi = login.querySelector('.g_id_signin');
    if(gsi && gsi.parentNode){
      gsi.insertAdjacentElement('afterend', btn);
      return;
    }
    var loginErr = login.querySelector('#loginerr');
    if(loginErr && loginErr.parentNode){
      loginErr.insertAdjacentElement('beforebegin', btn);
      return;
    }
    login.appendChild(btn);
  }

  function init(){
    injectAuthButtonStyles();
    insertFacebookButton();
    insertGithubButton();
    renderQueryError();
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', init, {once: true});
  }else{
    init();
  }
})();
</script>"""


_GSI_SAFARI_FIX_SNIPPET = r"""<script data-uc-gsi-safari-fix>
(function(){
  if(document.getElementById('uc-gsi-safari-fix')) return;
  var style = document.createElement('style');
  style.id = 'uc-gsi-safari-fix';
  style.textContent = '#login .g_id_signin{display:flex;justify-content:center;width:min(100%,320px);max-width:320px;margin:0 auto;}';
  if(document.head) document.head.appendChild(style);
})();
</script>"""


def _inject_before_body(html: str, snippet: str) -> str:
    if "</body>" in html:
        return html.replace("</body>", snippet + "\n</body>")
    return html + snippet


def inject_google_client_id(template_html: str, google_client_id: str) -> str:
    """Replace Google client placeholder and inject lightweight analytics client."""
    html = template_html.replace("__GOOGLE_CLIENT_ID__", google_client_id)
    facebook_app_id = os.environ.get("FACEBOOK_APP_ID", "").strip()
    facebook_app_secret = os.environ.get("FACEBOOK_APP_SECRET", "").strip()
    facebook_ui_enabled = os.environ.get("FACEBOOK_LOGIN_UI_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    facebook_enabled = bool(facebook_app_id and facebook_app_secret and facebook_ui_enabled)
    github_client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    github_client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    github_enabled = bool(github_client_id and github_client_secret)
    html = html.replace("__FACEBOOK_APP_ID__", facebook_app_id if facebook_enabled else "")
    if "data-uc-gsi-safari-fix" not in html:
        html = _inject_before_body(html, _GSI_SAFARI_FIX_SNIPPET)
    if (facebook_enabled or github_enabled) and "data-uc-facebook-login" not in html:
        fb_snippet = _FACEBOOK_LOGIN_SNIPPET_TEMPLATE.replace(
            "__UC_FACEBOOK_APP_ID__", json.dumps(facebook_app_id if facebook_enabled else "")
        ).replace("__UC_GITHUB_CLIENT_ID__", json.dumps(github_client_id if github_enabled else ""))
        html = _inject_before_body(html, fb_snippet)
    if "data-uc-analytics-client" in html:
        return html
    return _inject_before_body(html, _ANALYTICS_CLIENT_SNIPPET)
