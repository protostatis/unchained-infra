"""Page handlers extracted from web.py."""

from __future__ import annotations

import json
from html import escape

from aiohttp import web


from web_app.core import get_core as _core
from web_app.research_desk_page import build_research_desk_html as _build_research_desk_html
from web_app.templates import CHROME_TAX_HTML, FIRST_LOOK_PREVIEW_HTML, UNBROWSER_PAGE_HTML


_LEGAL_LAST_UPDATED = "July 12, 2026"


def _build_legal_page(
    *,
    title: str,
    description: str,
    canonical_path: str,
    current_path: str,
    content: str,
    contact_email: str,
) -> str:
    """Wrap legal copy in the shared, dependency-free public site shell."""
    safe_contact = escape(contact_email, quote=True)
    nav_links = []
    for href, label in (
        ("/", "Home"),
        ("/demo", "Demo"),
        ("/privacy", "Privacy"),
        ("/data-deletion", "Data deletion"),
    ):
        current = ' aria-current="page"' if href == current_path else ""
        nav_links.append(f'<li><a href="{href}"{current}>{label}</a></li>')
    nav = "".join(nav_links)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{escape(description, quote=True)}">
  <meta name="theme-color" content="#0b0a10">
  <link rel="canonical" href="https://unchainedsky.com{canonical_path}">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <title>{escape(title)} | Unchained</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b0a10;
      --surface: #15121d;
      --line: #2c2738;
      --text: #f1ede2;
      --muted: #aaa397;
      --accent: #ff6a3d;
      --signal: #b6f25c;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-width: 320px;
      background:
        radial-gradient(circle at 12% 0%, rgba(182, 242, 92, .07), transparent 30rem),
        radial-gradient(circle at 88% 12%, rgba(255, 106, 61, .07), transparent 28rem),
        var(--bg);
      color: var(--text);
      font-family: "Avenir Next", "Helvetica Neue", sans-serif;
      line-height: 1.7;
    }}
    body::before {{
      position: fixed;
      inset: 0;
      z-index: -1;
      background-image:
        linear-gradient(rgba(255, 255, 255, .025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255, 255, 255, .025) 1px, transparent 1px);
      background-size: 48px 48px;
      content: "";
      mask-image: linear-gradient(to bottom, black, transparent 72%);
    }}
    a {{ color: var(--signal); text-underline-offset: .2em; }}
    a:hover {{ color: #d5ff97; }}
    a:focus-visible {{ outline: 3px solid var(--accent); outline-offset: 4px; border-radius: 2px; }}
    .skip-link {{
      position: fixed;
      top: .75rem;
      left: .75rem;
      z-index: 10;
      padding: .65rem .9rem;
      background: var(--text);
      color: var(--bg);
      font-weight: 700;
      transform: translateY(-150%);
    }}
    .skip-link:focus {{ transform: none; }}
    .site-header {{ border-bottom: 1px solid rgba(255, 255, 255, .08); }}
    .header-inner, .footer-inner {{
      width: min(70rem, calc(100% - 2.5rem));
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 2rem;
    }}
    .header-inner {{ min-height: 4.5rem; }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: .65rem;
      color: var(--text);
      font-size: .82rem;
      font-weight: 800;
      letter-spacing: .18em;
      text-decoration: none;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .brand img {{ width: 1.7rem; height: 1.7rem; }}
    .brand span {{ color: var(--accent); }}
    nav ul {{ display: flex; align-items: center; gap: 1.25rem; margin: 0; padding: 0; list-style: none; }}
    nav a {{ color: var(--muted); font-size: .87rem; font-weight: 600; text-decoration: none; }}
    nav a:hover, nav a[aria-current="page"] {{ color: var(--text); }}
    nav a[aria-current="page"] {{ text-decoration: underline; text-decoration-color: var(--accent); text-decoration-thickness: 2px; }}
    .legal-main {{ width: min(70rem, calc(100% - 2.5rem)); margin: 0 auto; padding: clamp(3.5rem, 8vw, 7rem) 0; }}
    .legal-document {{ max-width: 46rem; }}
    .eyebrow {{
      margin: 0 0 1rem;
      color: var(--accent);
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: .72rem;
      font-weight: 700;
      letter-spacing: .18em;
      text-transform: uppercase;
    }}
    h1, h2 {{ font-family: Georgia, "Times New Roman", serif; font-weight: 400; }}
    h1 {{ margin: 0; font-size: clamp(2.8rem, 8vw, 5.4rem); letter-spacing: -.045em; line-height: .98; }}
    .updated {{ margin: 1.25rem 0 3.25rem; color: var(--muted); font-size: .9rem; }}
    .legal-copy {{ padding-top: 2rem; border-top: 1px solid var(--line); font-size: clamp(1rem, 2vw, 1.08rem); }}
    .legal-copy h2 {{ margin: 2.5rem 0 .65rem; font-size: clamp(1.45rem, 4vw, 1.85rem); line-height: 1.25; }}
    .legal-copy p {{ margin: 0 0 1rem; }}
    .legal-copy ul {{ margin: .5rem 0 1.2rem; padding-left: 1.35rem; }}
    .legal-copy li {{ margin: .4rem 0; padding-left: .25rem; }}
    code {{ padding: .15rem .35rem; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--text); }}
    .contact-card {{ margin-top: 1rem; padding: 1.1rem 1.25rem; border: 1px solid var(--line); border-left: 3px solid var(--signal); background: var(--surface); }}
    .site-footer {{ padding: 2rem 0 2.5rem; border-top: 1px solid rgba(255, 255, 255, .08); color: var(--muted); }}
    .footer-inner {{ align-items: flex-start; }}
    .footer-note {{ margin: 0; font-size: .78rem; letter-spacing: .08em; text-transform: uppercase; }}
    .footer-contact {{ font-size: .85rem; }}
    @media (max-width: 680px) {{
      .header-inner {{ align-items: flex-start; flex-direction: column; gap: .65rem; padding: 1rem 0; }}
      nav {{ width: 100%; overflow-x: auto; padding-bottom: .2rem; }}
      nav ul {{ width: max-content; gap: 1.1rem; }}
      nav a {{ display: inline-block; min-height: 2.25rem; padding: .35rem 0; }}
      .legal-main {{ padding: 3.5rem 0 4.5rem; }}
      .updated {{ margin-bottom: 2.5rem; }}
      .footer-inner {{ flex-direction: column; gap: .75rem; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <div class="header-inner">
      <a class="brand" href="/" aria-label="Unchained home"><img src="/favicon.svg" alt="" width="27" height="27">UN<span>CHAIN</span>ED</a>
      <nav aria-label="Primary navigation"><ul>{nav}</ul></nav>
    </div>
  </header>
  <main class="legal-main" id="main-content">
    <article class="legal-document" aria-labelledby="page-title">
      <p class="eyebrow">Policy &amp; trust</p>
      <h1 id="page-title">{escape(title)}</h1>
      <p class="updated">Last updated: <time datetime="2026-07-12">{_LEGAL_LAST_UPDATED}</time></p>
      <div class="legal-copy">{content}</div>
    </article>
  </main>
  <footer class="site-footer">
    <div class="footer-inner">
      <p class="footer-note">Unchained &mdash; browser work from a profile you control</p>
      <a class="footer-contact" href="mailto:{safe_contact}">Contact {safe_contact}</a>
    </div>
  </footer>
</body>
</html>"""


async def handle_install_page(request: web.Request) -> web.Response:
    """Serve the one-click installer onboarding page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.INSTALL_ONBOARD_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_cli_page(request: web.Request) -> web.Response:
    """Serve the CLI install guide page."""
    core = _core()
    core._track_page_view(request)
    from web_app.templates import CLI_INSTALL_HTML
    return web.Response(
        text=CLI_INSTALL_HTML,
        content_type="text/html",
        charset="utf-8",
        headers={
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )


async def handle_mcp_page(request: web.Request) -> web.Response:
    """Serve the MCP install/setup page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.MCP_PAGE_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_unbrowser_page(request: web.Request) -> web.Response:
    """Serve the public unbrowser landing page."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(UNBROWSER_PAGE_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chrome_tax_page(request: web.Request) -> web.Response:
    """Serve the Chrome Tax Calculator — value-first artifact for the breadcrumb trail."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(CHROME_TAX_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_mcp_guide_page(request: web.Request) -> web.Response:
    """Serve markdown-rendered MCP setup + route-plan docs."""
    core = _core()
    core._track_page_view(request)
    return web.Response(text=core._build_mcp_guide_html(), content_type="text/html")


def _build_first_look_preview_html(*, prompt_limit: int, remaining: int) -> str:
    html = FIRST_LOOK_PREVIEW_HTML
    html = html.replace("__FIRST_LOOK_GUEST_LIMIT__", str(max(1, int(prompt_limit))))
    html = html.replace("__FIRST_LOOK_GUEST_REMAINING__", str(max(0, int(remaining))))
    return html


async def handle_research_desk_page(request: web.Request) -> web.Response:
    """Serve the hosted launch page for the local Research Desk app."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    return web.Response(
        text=_build_research_desk_html(authenticated=bool(auth_info)),
        content_type="text/html",
    )


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
    """Redirect legacy Codex CLI chat route to the shared local page."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    model = request.query.get("model", "").strip().lower()
    target = "/local?provider=codex-cli" if model.startswith("codex-cli:") else "/local?provider=codex-sdk"
    core._track_redirect(request, target, reason="legacy_route_alias")
    raise web.HTTPFound(target)


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


async def handle_use_case_apartment(request: web.Request) -> web.Response:
    """Serve apartment hunting use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_APARTMENT_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_flights(request: web.Request) -> web.Response:
    """Serve flight comparison use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_FLIGHTS_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_competitor(request: web.Request) -> web.Response:
    """Serve competitor monitoring use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_COMPETITOR_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_use_case_price_tracking(request: web.Request) -> web.Response:
    """Serve price tracking use-case page (public, no auth)."""
    core = _core()
    core._track_page_view(request)
    html = core.USE_CASE_PRICE_TRACKING_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_published_result(request: web.Request) -> web.Response:
    """Serve a published result page (public, no auth)."""
    import re as _re
    import time as _time
    from html import escape
    from published_results import get_result
    core = _core()
    slug = request.match_info.get("slug", "")
    # Validate slug format
    if not slug or not _re.fullmatch(r"[a-z0-9-]{1,120}", slug):
        raise web.HTTPNotFound()
    result = get_result(slug)
    if not result:
        raise web.HTTPNotFound()
    core._track_page_view(request)
    query = result["query"]
    result_text = result["result_text"]
    # Escape for HTML meta attributes
    title_text = escape(query[:80])
    desc_html = escape(result_text[:200]).replace("\n", " ")
    # Escape for JSON-LD (must be valid JSON string content + escape </ for script safety)
    title_json = json.dumps(query[:80])[1:-1].replace("</", r"<\/")
    desc_json = json.dumps(result_text[:200].replace("\n", " "))[1:-1].replace("</", r"<\/")
    created = _time.strftime("%Y-%m-%d", _time.gmtime(result["created_at"]))
    # Single-pass substitution to prevent double-replacement if result_html
    # contains placeholder strings like __RESULT_TITLE__
    import re as _re
    _subs = {
        "__RESULT_TITLE__": title_text,
        "__RESULT_TITLE_JSON__": title_json,
        "__RESULT_DESC__": desc_html,
        "__RESULT_DESC_JSON__": desc_json,
        "__RESULT_SLUG__": slug,
        "__RESULT_HTML__": result["result_html"],
        "__RESULT_DATE__": created,
        "__RESULT_VIEWS__": str(result["view_count"]),
        "__CONTACT_EMAIL__": core.CONTACT_EMAIL,
    }
    _pattern = _re.compile("|".join(_re.escape(k) for k in _subs))
    html = _pattern.sub(lambda m: _subs[m.group(0)], core.PUBLISHED_RESULT_HTML)
    html = core.inject_google_client_id(html, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_publish_result(request: web.Request) -> web.Response:
    """POST /web/publish-result — publish a session as a shareable page."""
    import re as _re
    from published_results import publish_result
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    body = await request.json()
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)
    # Validate session_id format to prevent path traversal
    if not _re.fullmatch(r"[a-zA-Z0-9_.-]{8,80}", session_id):
        return web.json_response({"error": "Invalid session_id"}, status=400)
    # Load session data
    session_path = core._trial_session_path(session_id)
    try:
        with open(session_path) as f:
            session_data = json.load(f)
    except FileNotFoundError:
        return web.json_response({"error": "Session not found"}, status=404)
    user_id = auth_info.get("email", auth_info.get("key_hash", ""))
    # Offload to thread — publish_result does sync SQLite + sync HTTP (PII guard)
    import asyncio
    slug = await asyncio.to_thread(
        publish_result, session_data, user_id=user_id, session_id=session_id
    )
    if not slug:
        return web.json_response(
            {"error": "No publishable content in session"}, status=400
        )
    return web.json_response({
        "slug": slug,
        "url": f"https://unchainedsky.com/r/{slug}",
        "status": "pending",
    })


async def handle_pending_results(request: web.Request) -> web.Response:
    """GET /web/publish/pending — list results awaiting approval."""
    from published_results import list_pending
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    results = list_pending(limit=100)
    return web.json_response({"pending": results})


async def handle_approve_result(request: web.Request) -> web.Response:
    """POST /web/publish/approve — approve one or more pending results."""
    from published_results import approve_result, bulk_approve
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    body = await request.json()
    slugs = body.get("slugs", [])
    slug = body.get("slug", "")
    if slug:
        ok = approve_result(slug)
        return web.json_response({"approved": ok, "slug": slug})
    if slugs:
        count = bulk_approve(slugs)
        return web.json_response({"approved_count": count, "slugs": slugs})
    return web.json_response({"error": "slug or slugs required"}, status=400)


async def handle_reject_result(request: web.Request) -> web.Response:
    """POST /web/publish/reject — reject one or more pending results."""
    from published_results import reject_result, bulk_reject
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Authentication required"}, status=401)
    body = await request.json()
    reason = body.get("reason", "")
    slugs = body.get("slugs", [])
    slug = body.get("slug", "")
    if slug:
        ok = reject_result(slug, reason)
        return web.json_response({"rejected": ok, "slug": slug})
    if slugs:
        count = bulk_reject(slugs, reason)
        return web.json_response({"rejected_count": count, "slugs": slugs})
    return web.json_response({"error": "slug or slugs required"}, status=400)


async def handle_privacy_page(request: web.Request) -> web.Response:
    """Serve public privacy policy page (required for OAuth provider submissions)."""
    core = _core()
    core._track_page_view(request)
    safe_contact = escape(core.CONTACT_EMAIL, quote=True)
    content = f"""
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
    <p>You can request account/data deletion via <a href="/data-deletion">/data-deletion</a>.</p>
    <h2>Contact</h2>
    <p class="contact-card">Questions about this policy: <a href="mailto:{safe_contact}">{safe_contact}</a></p>"""
    html = _build_legal_page(
        title="Privacy Policy",
        description="How Unchained collects, uses, and protects information.",
        canonical_path="/privacy",
        current_path="/privacy",
        content=content,
        contact_email=core.CONTACT_EMAIL,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_data_deletion_page(request: web.Request) -> web.Response:
    """Serve public user-data deletion instructions page for OAuth compliance."""
    core = _core()
    core._track_page_view(request)
    safe_contact = escape(core.CONTACT_EMAIL, quote=True)
    content = f"""
    <p>If you want your Unchained account and related personal data deleted, send a request from your account email to:</p>
    <p class="contact-card"><a href="mailto:{safe_contact}">{safe_contact}</a></p>
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
    <p>For policy details, see <a href="/privacy">/privacy</a>.</p>"""
    html = _build_legal_page(
        title="User Data Deletion",
        description="How to request deletion of your Unchained account and related personal data.",
        canonical_path="/data-deletion",
        current_path="/data-deletion",
        content=content,
        contact_email=core.CONTACT_EMAIL,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_first_look_page(request: web.Request) -> web.Response:
    """Serve the first-look page (now uses the preview template)."""
    core = _core()
    ref = request.query.get('ref', '')[:64]
    core._track_page_view(request, meta={'ref': ref} if ref else None)
    _, guest_id, quota_count = core._first_look_guest_auth(request)
    html = _build_first_look_preview_html(
        prompt_limit=core._FIRST_LOOK_GUEST_PROMPT_LIMIT,
        remaining=max(0, core._FIRST_LOOK_GUEST_PROMPT_LIMIT - quota_count),
    )
    resp = web.Response(text=html, content_type="text/html")
    core._attach_first_look_guest_cookies(resp, request, guest_id, quota_count=quota_count)
    return resp


async def handle_first_look_preview_page(request: web.Request) -> web.Response:
    """Serve the guest-safe preview route for first-look review."""
    core = _core()
    ref = request.query.get("ref", "")[:64]
    meta = {"route": "first-look-preview"}
    if ref:
        meta["ref"] = ref
    core._track_page_view(request, meta=meta)
    _, guest_id, quota_count = core._first_look_guest_auth(request)
    html = _build_first_look_preview_html(
        prompt_limit=core._FIRST_LOOK_GUEST_PROMPT_LIMIT,
        remaining=max(0, core._FIRST_LOOK_GUEST_PROMPT_LIMIT - quota_count),
    )
    resp = web.Response(text=html, content_type="text/html")
    core._attach_first_look_guest_cookies(resp, request, guest_id, quota_count=quota_count)
    return resp


async def handle_demo_page(request: web.Request) -> web.Response:
    """Redirect /demo to /first-look for backward compatibility."""
    core = _core()
    core._track_redirect(request, "/first-look", reason="legacy_route_alias")
    raise web.HTTPFound("/first-look")


async def handle_local_page(request: web.Request) -> web.Response:
    """Serve the local agent chat HTML page (Claude, Codex, and OpenCode CLI)."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    provider = request.query.get("provider", "").strip().lower()
    html_source = core.CHAT_CODEX_HTML if provider in {"codex-cli", "codex-sdk"} else core.CLAUDE_CHAT_HTML
    html = core.inject_google_client_id(html_source, core.GOOGLE_CLIENT_ID)
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
