"""Shared helpers for HTML template rendering."""

from __future__ import annotations


_ANALYTICS_CLIENT_SNIPPET = r"""<script data-uc-analytics-client>
(function(){
  function postEvent(eventName, meta){
    if(!eventName) return;
    var payload = {
      event: eventName,
      route: window.location.pathname || '',
      source: 'web',
      meta: (meta && typeof meta === 'object') ? meta : {}
    };
    try{
      fetch('/web/analytics/event', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        keepalive: true,
        body: JSON.stringify(payload)
      });
    }catch(_e){}
  }

  function init(){
    // Login panel is hidden for authenticated users; this captures only visible auth gates.
    setTimeout(function(){
      var login = document.getElementById('login');
      if(!login) return;
      var st = window.getComputedStyle(login);
      if(st.display === 'none' || st.visibility === 'hidden') return;
      postEvent('login_gate_visible');
    }, 1200);

    var clicked = false;
    document.addEventListener('click', function(ev){
      if(clicked) return;
      var t = ev && ev.target;
      if(!t) return;
      var hit = false;
      if (t.closest && t.closest('.g_id_signin')) {
        hit = true;
      } else if (t.tagName === 'IFRAME') {
        var src = t.getAttribute('src') || '';
        hit = src.indexOf('accounts.google.com') !== -1 || src.indexOf('gsi') !== -1;
      }
      if(!hit) return;
      clicked = true;
      postEvent('google_signin_click');
    }, true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, {once:true});
  } else {
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
