"""Shared helpers for HTML template rendering."""

from __future__ import annotations


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


def inject_google_client_id(template_html: str, google_client_id: str) -> str:
    """Replace Google client placeholder and inject lightweight analytics client."""
    html = template_html.replace("__GOOGLE_CLIENT_ID__", google_client_id)
    if "data-uc-analytics-client" in html:
        return html
    if "</body>" in html:
        return html.replace("</body>", _ANALYTICS_CLIENT_SNIPPET + "\n</body>")
    return html + _ANALYTICS_CLIENT_SNIPPET
