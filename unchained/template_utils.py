"""Shared helpers for HTML template rendering."""

from __future__ import annotations

import json
import os


_ANALYTICS_CLIENT_SNIPPET = r"""<script data-uc-analytics-client>
(function(){
  var ROUTE = window.location.pathname || '';
  var STORAGE_KEY = 'uc_analytics_session_id';
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
    var node = t.closest ? t.closest('a,button,[role=button],div,input[type=button],input[type=submit]') : t;
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
    if(gateShown) return;
    var gateType = inferGateType();
    if(gateType === 'none') return;
    gateShown = true;
    postEvent('gate_shown', {gate_type: gateType, meta: {gate_type: gateType}});
    if(gateType === 'inline_gsi' || gateType === 'inline_gate'){
      postEvent('login_gate_visible', {gate_type: gateType, meta: {gate_type: gateType}});
    }
  }

  function maybeTrackGsiIframe(){
    if(gsiIframeSeen) return;
    var iframes = document.querySelectorAll('iframe');
    for(var i = 0; i < iframes.length; i++){
      var src = (iframes[i].getAttribute('src') || '').toLowerCase();
      if(src.indexOf('accounts.google.com') !== -1 || src.indexOf('gsi') !== -1){
        gsiIframeSeen = true;
        postEvent('gsi_iframe_loaded', {gate_type: 'inline_gsi'});
        return;
      }
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
    var gsiPoll = 0;
    var gsiTimer = setInterval(function(){
      gsiPoll += 1;
      maybeTrackGsiIframe();
      if(gsiIframeSeen || gsiPoll >= 12) clearInterval(gsiTimer);
    }, 350);

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
  var GOOGLE_REDIRECT_ENABLED = __UC_GOOGLE_REDIRECT_ENABLED__;
  if(!FB_APP_ID && !GH_CLIENT_ID && !GOOGLE_REDIRECT_ENABLED) return;

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
    if(code === 'google_denied') return 'Google sign-in was canceled.';
    if(code === 'google_state_invalid') return 'Google sign-in expired. Please try again.';
    if(code === 'google_exchange_failed') return 'Google sign-in failed during token exchange.';
    if(code === 'google_email_required') return 'Google account email is required to continue.';
    if(code === 'google_not_configured') return 'Google sign-in is not configured yet.';
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
    var parts = [
      '#login #google-login-btn,#login #fb-login-btn,#login #gh-login-btn{width:min(100%,360px) !important;height:48px !important;border-radius:12px !important;}'
    ];
    if(GOOGLE_REDIRECT_ENABLED){
      parts.push('#login .g_id_signin{display:none !important;}');
      parts.push('#login #google-login-btn{display:flex;align-items:center;justify-content:center;gap:10px;border:1px solid #30363d;background:#0d1117;color:#f0f6fc;font-size:15px;font-weight:600;cursor:pointer;margin:0 auto;}');
      parts.push('#login #google-login-btn .icon{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;flex:0 0 18px;}');
      parts.push('#login #google-login-btn .label{display:inline-block;line-height:1;}');
    }else{
      parts.push('#login .g_id_signin{display:flex;justify-content:center;width:min(100%,360px);margin:0 auto;}');
      parts.push('#login .g_id_signin iframe{border:0 !important;box-shadow:none !important;}');
      parts.push('#login .g_id_signin [role=\"button\"]{border-radius:12px !important;overflow:hidden !important;}');
    }
    style.textContent = parts.join('');
    if(document.head){
      document.head.appendChild(style);
    }
  }

  function insertGoogleButton(){
    if(!GOOGLE_REDIRECT_ENABLED) return;
    var login = document.getElementById('login');
    if(!login) return;
    if(document.getElementById('google-login-btn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'google-login-btn';
    btn.setAttribute('aria-label', 'Continue with Google');
    var icon = document.createElement('span');
    icon.className = 'icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg viewBox=\"0 0 24 24\" width=\"18\" height=\"18\"><path fill=\"#4285F4\" d=\"M23.49 12.27c0-.79-.07-1.54-.2-2.27H12v4.3h6.45a5.52 5.52 0 0 1-2.4 3.62v3h3.88c2.26-2.08 3.56-5.14 3.56-8.65z\"></path><path fill=\"#34A853\" d=\"M12 24c3.24 0 5.96-1.07 7.95-2.9l-3.88-3a7.2 7.2 0 0 1-10.72-3.78H1.34v3.08A12 12 0 0 0 12 24z\"></path><path fill=\"#FBBC05\" d=\"M5.35 14.32A7.2 7.2 0 0 1 4.95 12c0-.8.14-1.58.4-2.32V6.6H1.34A12 12 0 0 0 0 12c0 1.93.46 3.76 1.34 5.4l4.01-3.08z\"></path><path fill=\"#EA4335\" d=\"M12 4.8c1.76 0 3.33.6 4.57 1.8l3.43-3.43A11.95 11.95 0 0 0 12 0 12 12 0 0 0 1.34 6.6l4.01 3.08A7.2 7.2 0 0 1 12 4.8z\"></path></svg>';
    var label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'Continue with Google';
    btn.appendChild(icon);
    btn.appendChild(label);
    btn.addEventListener('click', function(){
      var source = inferSource();
      var next = currentNextPath();
      var url = '/auth/google/start?source=' + encodeURIComponent(source) + '&next=' + encodeURIComponent(next);
      window.location.href = url;
    });

    var gsi = login.querySelector('.g_id_signin');
    if(gsi && gsi.parentNode){
      gsi.insertAdjacentElement('beforebegin', btn);
      return;
    }
    var loginErr = login.querySelector('#loginerr');
    if(loginErr && loginErr.parentNode){
      loginErr.insertAdjacentElement('beforebegin', btn);
      return;
    }
    login.appendChild(btn);
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

    var googleBtn = login.querySelector('#google-login-btn');
    if(googleBtn && googleBtn.parentNode){
      googleBtn.insertAdjacentElement('afterend', btn);
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
    var googleBtn = login.querySelector('#google-login-btn');
    if(googleBtn && googleBtn.parentNode){
      googleBtn.insertAdjacentElement('afterend', btn);
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
    insertGoogleButton();
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
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    google_redirect_enabled = bool(google_client_id and google_client_secret)
    html = html.replace("__FACEBOOK_APP_ID__", facebook_app_id if facebook_enabled else "")
    if (facebook_enabled or github_enabled or google_redirect_enabled) and "data-uc-facebook-login" not in html:
        fb_snippet = _FACEBOOK_LOGIN_SNIPPET_TEMPLATE.replace(
            "__UC_FACEBOOK_APP_ID__", json.dumps(facebook_app_id if facebook_enabled else "")
        ).replace("__UC_GITHUB_CLIENT_ID__", json.dumps(github_client_id if github_enabled else "")).replace(
            "__UC_GOOGLE_REDIRECT_ENABLED__",
            "true" if google_redirect_enabled else "false",
        )
        html = _inject_before_body(html, fb_snippet)
    if "data-uc-analytics-client" in html:
        return html
    return _inject_before_body(html, _ANALYTICS_CLIENT_SNIPPET)
