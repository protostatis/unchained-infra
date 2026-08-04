"""Web UI — mobile-first browser control panel for unchained agents.

Serves a single-page HTML app and a /web/cmd API that dispatches
to cloud_tools functions. Designed for phone-based demo control.

Usage:
    python web.py --host 0.0.0.0
"""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import smtplib
import subprocess
import tempfile
import time
import threading
import uuid
from email.mime.text import MIMEText
from urllib.parse import quote, urlparse

import httpx
import jwt
from aiohttp import web

from analytics import AnalyticsStore, _safe_event_name
from auth import Auth
import provision_helpers
from template_utils import inject_google_client_id
from web_state import ChatRuntimeState
from web_app.cmd_dispatch import (
    CmdInputError,
    UnknownCmdActionError,
    is_chrome_unavailable_error,
    run_cmd_action,
)
from web_app.routes import ROUTE_SPECS, register_route_specs
from web_app.templates import (
    ADMIN_HTML,
    BRANDED_TAB_HTML,
    CASE_STUDY_ZILLOW_HTML,
    USE_CASE_APARTMENT_HTML,
    USE_CASE_FLIGHTS_HTML,
    USE_CASE_COMPETITOR_HTML,
    USE_CASE_PRICE_TRACKING_HTML,
    PUBLISHED_RESULT_HTML,
    CHAT_CLAUDE_SDK_HTML,
    CHAT_CODEX_HTML,
    CHAT_GEMINI_HTML,
    CHAT_HTML,
    CLAUDE_CHAT_HTML,
    FIRST_LOOK_PREVIEW_HTML,
    HEADLESS_DEMO_HTML,
    HTML,
    INSTALL_CLAIM_HTML,
    INSTALL_ONBOARD_HTML,
    LANDING_HTML,
    LANDING_V2_HTML,
    LANDING_V3_HTML,
    LANDING_V4_HTML,
    MCP_PAGE_HTML,
    PUBLIC_404_HTML,
    SCHEDULER_HTML,
    SETUP_HTML,
    TRIAL_CHAT_HTML,
    UNBROWSER_PAGE_HTML,
)

log = logging.getLogger(__name__)

_auth = Auth()

# Financial workspace control plane (feature-flagged, default off). Eagerly
# initialized in ``create_app()`` when FIN_WORKSPACE_ENABLED=true; the
# workspace-control slice serves browser handoff/auth/callback + S2S routes.
_fin_workspace = None


def _resolve_analytics_db_path(auth_db_path: str) -> str:
    """Use dedicated analytics.db; auth.db analytics tables are legacy only."""
    configured = os.environ.get("UNCHAINED_ANALYTICS_DB_PATH", "").strip()
    if configured:
        return configured
    auth_path = Path(auth_db_path).expanduser()
    return str(auth_path.with_name("analytics.db"))


_analytics = AnalyticsStore(db_path=_resolve_analytics_db_path(_auth.db_path))
try:
    _activation_backfill_count = _auth.backfill_install_bootstrap_markers(
        _analytics.db_path
    )
    log.info(
        "[growth] durable install activation backfill matched %d account(s)",
        _activation_backfill_count,
    )
    _activation_backfill_ready = True
except Exception as exc:
    # Startup remains available, but lifecycle outreach must stay disabled
    # until a later backfill succeeds and is verified.
    _activation_backfill_count = 0
    _activation_backfill_ready = False
    log.warning("[growth] durable install activation backfill failed: %s", exc)

# Google OAuth config (from env)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET env var is required. Refusing to start with an insecure default."
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24 * 7  # 1 week
ALLOWED_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
    if e.strip()
)

# SMTP config for sign-up notifications
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@unchainedsky.com")

# Trial agent: handles OpenRouter model requests (model IDs containing '/')
# Uses a deployment-provided service key to bypass DB auth.
def _key_hash(key: str) -> str:
    """Return 8-char SHA256 hash of a key — stable user identifier."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _agent_id(prefix: str, key: str) -> str:
    """Build a typed agent ID: ``{prefix}-{hash}``."""
    return f"{prefix}-{_key_hash(key)}"


TRIAL_AGENT_KEY = os.environ.get("TRIAL_AGENT_KEY", "").strip()
# Mandatory, scoped service token for hosted-worker callbacks. It must remain
# separate from the trial-agent WebSocket key.
HOSTED_AGENT_SERVICE_TOKEN = os.environ.get("HOSTED_AGENT_SERVICE_TOKEN", "").strip()
TRIAL_AGENT_ID = os.environ.get("TRIAL_AGENT_ID", "").strip()
if not TRIAL_AGENT_ID and TRIAL_AGENT_KEY:
    TRIAL_AGENT_ID = _agent_id("trial", TRIAL_AGENT_KEY)
# Headless bridge agent_id — when set, trial/demo chat messages route CDP tools
# to this agent instead of the user's personal agent.
# Can be set explicitly, or derived from HEADLESS_API_KEY (the key the bridge uses).
HEADLESS_AGENT_ID = os.environ.get("HEADLESS_AGENT_ID", "").strip()
if not HEADLESS_AGENT_ID:
    _headless_key = os.environ.get("HEADLESS_API_KEY", "").strip()
    if _headless_key:
        HEADLESS_AGENT_ID = _agent_id("headless", _headless_key)
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
]
FIN_TERMINAL_ALLOWED_EMAILS = set(ADMIN_EMAILS)
FIN_TERMINAL_ALLOWED_EMAILS.update(
    e.strip().lower()
    for e in os.environ.get("FIN_TERMINAL_ALLOWED_EMAILS", "").split(",")
    if e.strip()
)


def _resolve_contact_email() -> str:
    """Return the public project contact without exposing an admin account."""
    return os.environ.get("CONTACT_EMAIL", "").strip() or "hello@unchainedsky.com"


CONTACT_EMAIL = _resolve_contact_email()
if not TRIAL_AGENT_KEY:
    log.warning("[chat] TRIAL_AGENT_KEY unset; trial-agent auth bypass disabled.")
if not TRIAL_AGENT_ID:
    log.warning("[chat] TRIAL_AGENT_ID unresolved; OpenRouter trial routing disabled.")
if TRIAL_AGENT_ID and not HOSTED_AGENT_SERVICE_TOKEN:
    log.warning(
        "[chat] HOSTED_AGENT_SERVICE_TOKEN unset; hosted inference callbacks fail closed."
    )

# Demo prompt quota — number of headless demo interactions before requiring trial install
_DEMO_PROMPT_LIMIT = 20

# First-look guest flow — anonymous browsing before sign-up
_FIRST_LOOK_GUEST_PROMPT_LIMIT = max(
    1,
    int(os.environ.get("FIRST_LOOK_GUEST_PROMPT_LIMIT", "20")),
)
_FIRST_LOOK_GUEST_COOKIE_MAX_AGE = 60 * 24 * 3600
_FIRST_LOOK_GUEST_ID_COOKIE = "uc_fl_guest"
_FIRST_LOOK_GUEST_QUOTA_COOKIE = "uc_fl_quota"
_SIGNED_INT_RE = re.compile(r"^[0-9]{1,4}$")
_SIGNED_GUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")

# Turn-based rate limiting for free-tier users
_FREE_DAILY_TURN_LIMIT = 25    # ~25 turns covers 10-20 min of real browsing
_FREE_WINDOW_TURN_LIMIT = 5    # per 5-min window cap (prevents rapid-fire)
_FREE_WINDOW_SECONDS = 300     # 5-minute window
_OPENROUTER_TRIAL_BUDGET_USD = max(
    0.0,
    float(os.environ.get("OPENROUTER_TRIAL_BUDGET_USD", "1.0")),
)
_OPENROUTER_TRIAL_DEFAULT_MODEL = (
    os.environ.get("OPENROUTER_TRIAL_DEFAULT_MODEL", "google/gemini-3.1-flash-lite").strip()
    or "google/gemini-3.1-flash-lite"
)
_OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS = tuple(
    m.strip()
    for m in os.environ.get(
        "OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS",
        (
            "nvidia/nemotron-3-super-120b-a12b:free,"
            "nvidia/nemotron-3-nano-30b-a3b:free,"
            "poolside/laguna-xs-2.1:free"
        ),
    ).split(",")
    if m.strip()
)
if not _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS:
    _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS = (
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "poolside/laguna-xs-2.1:free",
    )
_OPENROUTER_TRIAL_FALLBACK_MODEL = (
    os.environ.get("OPENROUTER_TRIAL_FALLBACK_MODEL", _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS[0]).strip()
    or _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS[0]
)
if _OPENROUTER_TRIAL_FALLBACK_MODEL not in _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS:
    _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS = (
        (_OPENROUTER_TRIAL_FALLBACK_MODEL,) + _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS
    )


def _is_demo_unlimited(user: dict | None) -> bool:
    """Return True if user is approved with an API key (bypasses demo quota)."""
    if not user:
        return False
    return user.get("status") == "approved" and bool(user.get("api_key"))


def _is_rate_limited_user(user: dict | None) -> bool:
    """Return True for free-tier demo/trial users (no approved API key)."""
    if not user:
        return True
    # Trial accounts remain rate-limited even though onboarding currently
    # issues them an approved connector key. Approval controls connector
    # access; it is not a paid-plan entitlement.
    if user.get("user_type") == "trial":
        return True
    # Approved users with API keys bypass rate limiting
    if user.get("status") == "approved" and bool(user.get("api_key")):
        return False
    return True


def send_email(to: str, subject: str, body_html: str) -> bool:
    """Return whether the SMTP server accepted the message for delivery."""
    if not SMTP_HOST:
        log.warning("[email] SMTP_HOST not configured; skipped subject: %s", subject)
        return False
    try:
        msg = MIMEText(body_html, "html")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = to
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            refused = server.sendmail(FROM_EMAIL, [to], msg.as_string())
        if refused:
            log.error("[email] SMTP refused recipient for subject: %s", subject)
            return False
        log.info("[email] SMTP accepted subject: %s", subject)
        return True
    except Exception as e:
        log.error("[email] SMTP send failed for subject %s: %s", subject, e)
        return False


def _request_id(request: web.Request) -> str:
    rid = request.headers.get("X-Request-ID", "").strip()
    return rid or uuid.uuid4().hex[:12]


def _trace(event: str, **fields):
    parts = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    suffix = " " + " ".join(parts) if parts else ""
    log.info("[trace] %s%s", event, suffix)


# ---------------------------------------------------------------------------
# Analytics infrastructure
# ---------------------------------------------------------------------------

_ANALYTICS_PAGE_VIEW_ROUTES = {
    "/",
    "/browserbase-alternative",
    "/browser-mcp-alternative",
    "/unbrowser",
    "/chrome-tax",
    "/mcp-guide",
    "/mcp",
    "/privacy",
    "/privacy-policy",
    "/data-deletion",
    "/case-study/zillow-rental",
    "/use/apartment-hunting",
    "/use/flight-comparison",
    "/use/competitor-monitoring",
    "/use/price-tracking",
    "/trial",
    "/workspace",
    "/local",
    "/setup",
    "/install",
    "/first-look",
    "/demo",
    "/chat-gemini",
    "/chat-codex",
    "/chat-claude",
    "/app",
    "/scheduler",
    "/labs/research-desk",
    "/cli",
    "/admin",
}
_ANALYTICS_INLINE_GSI_ROUTES = {
    "/trial",
    "/local",
    "/setup",
    "/chat-gemini",
    "/chat-codex",
    "/chat-claude",
}
_ANALYTICS_LINK_GATE_ROUTES = {
    "/install",
}
_ANALYTICS_SESSION_HEADER = "X-Unchained-Analytics-Session"
_ANALYTICS_PAGE_VIEW_HEADER = "X-Unchained-Analytics-Page-View"
_ANALYTICS_ROUTE_HEADER = "X-Unchained-Analytics-Route"
_ANALYTICS_GATE_TYPE_HEADER = "X-Unchained-Analytics-Gate-Type"
_ANALYTICS_ACQUISITION_QUERY_FIELDS = {
    "ref": 64,
    "task": 64,
    "utm_source": 64,
    "utm_medium": 64,
    "utm_campaign": 96,
}
_ANALYTICS_ACQUISITION_TASKS = frozenset(
    {"apartment", "flight", "research", "search-result"}
)
_ANALYTICS_LANDING_VARIANT_META_KEY = "landing_variant"
_ANALYTICS_LANDING_VARIANTS = frozenset(
    {"landing_value_prop_v1", "landing_legacy_v2", "landing_legacy_v3"}
)
_analytics_last_cleanup_ts = 0.0


def _analytics_maybe_cleanup():
    global _analytics_last_cleanup_ts
    now = time.time()
    if now - _analytics_last_cleanup_ts < 3600:
        return
    _analytics_last_cleanup_ts = now
    try:
        _analytics.cleanup_old_events()
    except Exception as e:
        log.warning("[analytics] cleanup failed: %s", e)


def _track_event(
    request: web.Request,
    event: str,
    *,
    event_id: str = "",
    session_id: str = "",
    page_view_id: str = "",
    route: str = "",
    route_intended: str = "",
    route_effective: str = "",
    gate_type: str = "",
    cta_id: str = "",
    error_code: str = "",
    user_id: str = "",
    user_type: str = "",
    source: str = "",
    status_code: int = 0,
    latency_ms: int = 0,
    meta: dict | None = None,
    dedupe_ttl_s: float = 0.0,
):
    try:
        header_session_id = _analytics_session_id_from_request(request)
        header_page_view_id = _analytics_page_view_id_from_request(request)
        header_route = _analytics_route_from_request(request)
        header_gate_type = _analytics_gate_type_from_request(request)
        _analytics_maybe_cleanup()
        return _analytics.track(
            event,
            request=request,
            event_id=event_id,
            session_id=session_id or header_session_id,
            page_view_id=page_view_id or header_page_view_id,
            route=route or header_route,
            route_intended=route_intended or route or header_route,
            route_effective=route_effective or route or header_route,
            gate_type=gate_type or header_gate_type,
            cta_id=cta_id,
            error_code=error_code,
            user_id=user_id,
            user_type=user_type,
            source=source,
            status_code=status_code,
            latency_ms=latency_ms,
            meta=meta,
            dedupe_ttl_s=dedupe_ttl_s,
        )
    except Exception as e:
        log.warning("[analytics] event=%s failed: %s", event, e)
        return False


def _analytics_header_value(request: web.Request | None, name: str, limit: int) -> str:
    if request is None:
        return ""
    try:
        value = str(request.headers.get(name, "")).strip()
    except Exception:
        return ""
    return value[:limit]


def _analytics_session_id_from_request(request: web.Request | None) -> str:
    return _analytics_header_value(request, _ANALYTICS_SESSION_HEADER, 80)


def _analytics_page_view_id_from_request(request: web.Request | None) -> str:
    return _analytics_header_value(request, _ANALYTICS_PAGE_VIEW_HEADER, 80)


def _analytics_route_from_request(request: web.Request | None) -> str:
    return _analytics_header_value(request, _ANALYTICS_ROUTE_HEADER, 200)


def _analytics_gate_type_from_request(request: web.Request | None) -> str:
    return _analytics_header_value(request, _ANALYTICS_GATE_TYPE_HEADER, 32)


def _analytics_acquisition_meta(request: web.Request | None) -> dict[str, str]:
    """Return only bounded campaign tokens; never persist arbitrary query data."""
    if request is None:
        return {}
    try:
        query = request.query
    except Exception:
        return {}
    meta: dict[str, str] = {}
    for field, limit in _ANALYTICS_ACQUISITION_QUERY_FIELDS.items():
        try:
            raw = str(query.get(field, "") or "").strip()
        except Exception:
            continue
        if not raw or len(raw) > limit or not re.fullmatch(r"[A-Za-z0-9._:-]+", raw):
            continue
        if field == "task" and raw not in _ANALYTICS_ACQUISITION_TASKS:
            continue
        meta[field] = raw
    return meta


def _track_page_view(
    request: web.Request,
    auth_info: dict | None = None,
    meta: dict | None = None,
    *,
    landing_variant: str = "",
):
    if str(getattr(request, "method", "GET") or "GET").upper() != "GET":
        return False
    route = request.path
    if route not in _ANALYTICS_PAGE_VIEW_ROUTES:
        return False
    if auth_info is None:
        auth_info = _authenticate(request)
    gate_type = ""
    if route in _ANALYTICS_INLINE_GSI_ROUTES:
        gate_type = "inline_gsi"
    elif route in _ANALYTICS_LINK_GATE_ROUTES:
        gate_type = "link_signin"
    event_meta = dict(meta or {})
    # The landing marker is server-owned. Never let generic event metadata or
    # request query fields manufacture an arbitrary experiment dimension.
    event_meta.pop(_ANALYTICS_LANDING_VARIANT_META_KEY, None)
    if landing_variant in _ANALYTICS_LANDING_VARIANTS:
        event_meta[_ANALYTICS_LANDING_VARIANT_META_KEY] = landing_variant
    for field, value in _analytics_acquisition_meta(request).items():
        event_meta.setdefault(field, value)
    return _track_event(
        request,
        "page_view",
        route=route,
        route_intended=route,
        route_effective=route,
        gate_type=gate_type,
        user_id=(auth_info or {}).get("user_id", ""),
        user_type=(auth_info or {}).get("user_type", ""),
        source="web",
        status_code=200,
        dedupe_ttl_s=5.0,
        meta=event_meta,
    )


def _track_redirect(
    request: web.Request,
    location: str,
    *,
    reason: str = "",
    auth_info: dict | None = None,
):
    _track_event(
        request,
        "route_redirect",
        route=request.path,
        route_intended=request.path,
        route_effective=location,
        gate_type="inline_gsi" if location in _ANALYTICS_INLINE_GSI_ROUTES else "",
        user_id=(auth_info or {}).get("user_id", ""),
        user_type=(auth_info or {}).get("user_type", ""),
        source="web",
        status_code=302,
        meta={"to": location, "reason": reason or "redirect"},
        dedupe_ttl_s=0.5,
    )


_analytics_ingest_buckets: dict[str, list[float]] = {}  # ip -> timestamps
_ANALYTICS_SERVER_ONLY_EVENTS = frozenset(
    {
        "first_look_run_accepted",
        "first_look_run_rejected",
        "first_look_run_terminal",
        "unbrowser_demo_run_accepted",
        "unbrowser_demo_run_rejected",
        "unbrowser_demo_run_terminal",
        "unbrowser_outbound_click",
    }
)


def _analytics_ingest_allow(
    request: web.Request, *, units: int = 1, window: float = 60.0, limit: int = 200
) -> tuple[bool, int]:
    """Per-IP rate limiter for analytics ingest endpoints.

    Returns (allowed, retry_after_seconds).
    """
    ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
    now = time.time()
    bucket = _analytics_ingest_buckets.setdefault(ip, [])
    cutoff = now - window
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) + units > limit:
        retry_after = max(1, int(bucket[0] + window - now)) if bucket else int(window)
        return False, retry_after
    bucket.extend([now] * units)
    return True, 0


def _coerce_analytics_event_payload(raw: dict, request: web.Request) -> tuple[dict | None, str]:
    if not isinstance(raw, dict):
        return None, "event payload must be a JSON object"
    event = str(raw.get("event", "")).strip()
    if not event:
        return None, "event required"
    if _safe_event_name(event) in _ANALYTICS_SERVER_ONLY_EVENTS:
        return None, "event reserved for server use"
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    route = str(raw.get("route", "")).strip() or request.path
    route_intended = str(raw.get("route_intended", "")).strip() or route
    route_effective = str(raw.get("route_effective", "")).strip() or route
    source = str(raw.get("source", "web")).strip() or "web"
    gate_type = str(raw.get("gate_type", meta.get("gate_type", ""))).strip()
    cta_id = str(raw.get("cta_id", meta.get("cta_id", ""))).strip()
    error_code = str(raw.get("error_code", meta.get("reason", ""))).strip()
    try:
        latency_ms = max(0, int(raw.get("latency_ms", meta.get("latency_ms", 0)) or 0))
    except Exception:
        latency_ms = 0
    return {
        "event": event,
        "event_id": str(raw.get("event_id", meta.get("event_id", ""))).strip(),
        "session_id": str(raw.get("session_id", meta.get("session_id", ""))).strip(),
        "page_view_id": str(raw.get("page_view_id", meta.get("page_view_id", ""))).strip(),
        "route": route,
        "route_intended": route_intended,
        "route_effective": route_effective,
        "gate_type": gate_type,
        "cta_id": cta_id,
        "error_code": error_code,
        "source": source,
        "latency_ms": latency_ms,
        "meta": meta,
    }, ""


def _cookie_secure(request: web.Request) -> bool:
    """Honor TLS termination when deciding cookie Secure flag."""
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.scheme == "https"


def _cookie_domain(request: web.Request) -> str | None:
    """Share auth cookies across unchainedsky.com subdomains in production."""
    forwarded_host = request.headers.get("X-Forwarded-Host", "")
    host = (forwarded_host.split(",")[0].strip() if forwarded_host else (request.host or "")).lower()
    host = host.split(":", 1)[0]
    if host == "unchainedsky.com" or host.endswith(".unchainedsky.com"):
        return ".unchainedsky.com"
    return None


def _set_session_cookie(resp: web.Response, token: str, request: web.Request):
    kwargs = {
        "max_age": JWT_EXPIRY_HOURS * 3600,
        "httponly": True,
        "secure": _cookie_secure(request),
        "samesite": "Lax",
        "path": "/",
    }
    domain = _cookie_domain(request)
    if domain:
        kwargs["domain"] = domain
    resp.set_cookie("uc_session", token, **kwargs)


def _clear_session_cookie(resp: web.Response, request: web.Request):
    resp.del_cookie("uc_session", path="/")
    domain = _cookie_domain(request)
    if domain:
        resp.del_cookie("uc_session", path="/", domain=domain)


# ---------------------------------------------------------------------------
# Signed cookie infrastructure (first-look guest flow)
# ---------------------------------------------------------------------------

def _sign_cookie_value(name: str, value: str) -> str:
    payload = f"{name}|{value}"
    sig = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{value}.{sig}"


def _verify_cookie_value(name: str, token: str) -> str:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return ""
    value, sig = raw.rsplit(".", 1)
    payload = f"{name}|{value}"
    expected = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected):
        return ""
    return value


def _set_signed_cookie(
    resp: web.Response,
    request: web.Request,
    name: str,
    value: str,
    *,
    max_age: int = _FIRST_LOOK_GUEST_COOKIE_MAX_AGE,
    http_only: bool = True,
):
    kwargs = {
        "max_age": max_age,
        "httponly": http_only,
        "secure": _cookie_secure(request),
        "samesite": "Lax",
        "path": "/",
    }
    domain = _cookie_domain(request)
    if domain:
        kwargs["domain"] = domain
    resp.set_cookie(name, _sign_cookie_value(name, value), **kwargs)


def _get_signed_cookie(request: web.Request, name: str) -> str:
    token = request.cookies.get(name, "")
    if token and len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    return _verify_cookie_value(name, token)


# ---------------------------------------------------------------------------
# First-look guest primitives
# ---------------------------------------------------------------------------

def _first_look_guest_id(request: web.Request) -> tuple[str, bool]:
    guest_id = _get_signed_cookie(request, _FIRST_LOOK_GUEST_ID_COOKIE)
    if guest_id and _SIGNED_GUEST_ID_RE.fullmatch(guest_id):
        return guest_id, False
    return uuid.uuid4().hex, True


def _first_look_guest_quota_count(request: web.Request) -> int:
    raw = _get_signed_cookie(request, _FIRST_LOOK_GUEST_QUOTA_COOKIE)
    if not raw or not _SIGNED_INT_RE.fullmatch(raw):
        return 0
    try:
        return max(0, int(raw))
    except Exception:
        return 0


def _attach_first_look_guest_cookies(
    resp: web.Response,
    request: web.Request,
    guest_id: str,
    *,
    quota_count: int | None = None,
):
    _set_signed_cookie(resp, request, _FIRST_LOOK_GUEST_ID_COOKIE, guest_id)
    if quota_count is not None:
        _set_signed_cookie(resp, request, _FIRST_LOOK_GUEST_QUOTA_COOKIE, str(max(0, quota_count)))


def _build_first_look_guest_auth(guest_id: str) -> dict:
    key = f"first-look-guest:{guest_id}"
    key_hash = hashlib.sha256(f"{key}|{JWT_SECRET}".encode()).hexdigest()[:8]
    return {
        "user_id": "",
        "key": key,
        "key_hash": key_hash,
        "agent_id": f"guest-{key_hash}",
        "email": "",
        "status": "guest",
        "user_type": "guest",
    }


def _first_look_guest_auth(request: web.Request) -> tuple[dict, str, int]:
    guest_id, _is_new = _first_look_guest_id(request)
    quota_count = _first_look_guest_quota_count(request)
    return _build_first_look_guest_auth(guest_id), guest_id, quota_count


def _session_cookie_candidates(request: web.Request) -> list[str]:
    """Return all uc_session cookie candidates (handles duplicate cookie names)."""
    tokens: list[str] = []
    cookie_header = request.headers.get("Cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            kv = part.strip()
            if not kv or "=" not in kv:
                continue
            name, value = kv.split("=", 1)
            if name.strip() != "uc_session":
                continue
            token = value.strip()
            if len(token) >= 2 and token[0] == token[-1] == '"':
                token = token[1:-1]
            if token and token not in tokens:
                tokens.append(token)
    parsed = request.cookies.get("uc_session")
    if parsed and parsed not in tokens:
        tokens.append(parsed)
    return tokens


# Cache for Google's JWKS public keys
_google_jwks = None
_google_jwks_expiry: float = 0

# Chat/runtime state (incrementally extracted from web.py globals).
_state = ChatRuntimeState()

# Backward-compatible aliases used across the existing module body.
_chat_agents = _state.chat_agents  # agent_id -> ws
# Optional capabilities sent by local agents at WS auth time.
# Example: {"codex_cli": true}
_chat_agent_caps: dict[str, dict] = {}  # agent_id -> capabilities
_chat_agent_users: dict[str, str] = {}  # agent_id -> user_id
_response_queues = _state.response_queues  # session_id -> event queue
_response_req_ids = _state.response_req_ids  # session_id -> expected req_id
_chat_turns = _state.chat_turns  # signed-in session_id -> refresh-safe turn journal
_session_agents = _state.session_agents  # session_id -> agent_id that handled it
_agent_req_queues = _state.agent_req_queues  # req_id -> one-shot response queue
_session_tabs = _state.session_tabs  # session_id -> Chrome tab_id
_session_allowed_tabs = _state.session_allowed_tabs  # session_id -> server-authorized Chrome tabs
_session_profile_paths = _state.session_profile_paths  # session_id -> selected Chrome profile path
_expired_profile_sessions = _state.expired_profile_sessions  # session_id -> expiry tombstone timestamp
_session_profile_locks = _state.session_profile_locks  # session_id -> profile recovery lock
_session_last_active = _state.session_last_active  # session_id -> timestamp
_session_agent_map = _state.session_agent_map  # session_id -> agent_id for CDP routing
_source_operation_locks = _state.source_operation_locks  # (agent_id, tab_id) -> asyncio.Lock
_chat_preview_generations = _state.chat_preview_generations  # session_id -> latest preview generation
_STALE_TAB_SECONDS = 5 * 60  # 5 minutes — first-look guest sessions (headless, limited RAM)
_STALE_TAB_SECONDS_AGENT = 60 * 60  # 60 minutes — logged-in agent sessions (Chrome on user's machine)
_stale_tab_task: asyncio.Task | None = None
_tabs_pending_close = _state.tabs_pending_close  # tab_id -> (agent_id, retry_count)
_tabs_pending_close_caller_tags = _state.tabs_pending_close_caller_tags  # tab_id -> owner tag
_MAX_CLOSE_RETRIES = 3
_MAX_TABS_PER_AGENT = 10
_gemini_procs = _state.gemini_procs  # agent_id -> subprocess
_gemini_log_fhs = _state.gemini_log_fhs  # agent_id -> log file handle
_gemini_last_active = _state.gemini_last_active  # agent_id -> last msg timestamp
_gemini_spawn_lock = _state.gemini_spawn_lock  # prevents duplicate spawn race
_GEMINI_IDLE_TIMEOUT = 600
_gemini_cleanup_task: asyncio.Task | None = None
_headless_watchdog_task: asyncio.Task | None = None
_scheduler_turn_grants = _state.scheduler_turn_grants  # grant_id -> {user_id, session_id, expires_at}
# Keep /schedule authorization valid through a hosted turn's absolute deadline.
# Terminal turns revoke their grant sooner; the extra minute covers setup and
# dispatch time before the deadline watchdog starts.
_HOSTED_TURN_DEADLINE_S = max(
    30, int(os.environ.get("HOSTED_TURN_DEADLINE_SECONDS", "600"))
)
_SCHEDULER_TURN_GRANT_TTL = max(
    5 * 60,
    _HOSTED_TURN_DEADLINE_S + 60,
)

# Overlay copilot v2 — single state object per session
_overlay_sessions = _state.overlay_sessions  # session_id -> OverlaySessionState

# Per-user Codex agent process management (sdk + cli modes)
_codex_sdk_procs: dict[str, subprocess.Popen] = {}   # agent_id → subprocess
_codex_sdk_log_fhs: dict[str, object] = {}           # agent_id → log file handle
_codex_sdk_last_active: dict[str, float] = {}        # agent_id → last msg timestamp
_codex_cli_procs: dict[str, subprocess.Popen] = {}   # agent_id → subprocess
_codex_cli_log_fhs: dict[str, object] = {}           # agent_id → log file handle
_codex_cli_last_active: dict[str, float] = {}        # agent_id → last msg timestamp
_codex_sdk_spawn_lock = __import__("threading").Lock()   # prevents duplicate spawn race
_codex_cli_spawn_lock = __import__("threading").Lock()   # prevents duplicate spawn race
_CODEX_IDLE_TIMEOUT = 600  # kill after 10 min idle

# Per-user Claude SDK agent process management
_claude_sdk_procs: dict[str, subprocess.Popen] = {}   # agent_id → subprocess
_claude_sdk_log_fhs: dict[str, object] = {}           # agent_id → log file handle
_claude_sdk_last_active: dict[str, float] = {}        # agent_id → last msg timestamp
_claude_sdk_spawn_lock = __import__("threading").Lock()  # prevents duplicate spawn race
_CLAUDE_SDK_IDLE_TIMEOUT = 600  # kill after 10 min idle

# Pending provision keys awaiting user confirmation
# user_id → (provider, api_key, timestamp)
_pending_provision = _state.pending_provision  # user_id -> (provider, api_key, timestamp)
_PENDING_PROVISION_TTL = 300
_provision_cooldowns = _state.provision_cooldowns  # user_id -> last provision timestamp
_PROVISION_COOLDOWN_SECS = 30

# Scheduler UI storage (per-user jobs + state snapshots).
_SCHEDULER_STORE_DIR = Path(os.environ.get("UNCHAINED_SCHEDULER_DIR", "/data/scheduler_jobs"))
_INSTALLER_ASSETS_DIR = Path(
    os.environ.get(
        "UNCHAINED_INSTALLER_ASSETS_DIR",
        str(Path(os.path.dirname(os.path.abspath(__file__))) / "installers"),
    )
)
_DEFAULT_MAC_INSTALLER_FILES = ("unchained-installer-mac.dmg", "unchained-installer-mac.pkg")
_DEFAULT_WINDOWS_INSTALLER_FILES = ("unchained-installer-windows.msi", "unchained-installer-windows.exe")


def _parse_installer_filename_list(raw: str, default_files: tuple[str, ...]) -> list[str]:
    out = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if out:
        return out
    return list(default_files)


_MAC_INSTALLER_FILES = _parse_installer_filename_list(
    os.environ.get("UNCHAINED_MAC_INSTALLER_FILES", "").strip(),
    (
        os.environ.get("UNCHAINED_MAC_INSTALLER_FILE", "").strip() or _DEFAULT_MAC_INSTALLER_FILES[0],
        _DEFAULT_MAC_INSTALLER_FILES[1],
    ),
)
_WINDOWS_INSTALLER_FILES = _parse_installer_filename_list(
    os.environ.get("UNCHAINED_WINDOWS_INSTALLER_FILES", "").strip(),
    (
        os.environ.get("UNCHAINED_WINDOWS_INSTALLER_FILE", "").strip() or _DEFAULT_WINDOWS_INSTALLER_FILES[0],
        _DEFAULT_WINDOWS_INSTALLER_FILES[1],
    ),
)
_ALLOW_SCRIPT_INSTALLER_FALLBACK = (
    os.environ.get("UNCHAINED_ALLOW_SCRIPT_INSTALLER", "0").strip().lower() in {"1", "true", "yes", "on"}
)
_INSTALL_CLAIM_TTL = int(os.environ.get("UNCHAINED_INSTALL_CLAIM_TTL", "600"))
_INSTALL_CLAIM_MAX_PENDING = max(1, int(os.environ.get("UNCHAINED_INSTALL_CLAIM_MAX_PENDING", "4096")))
_INSTALL_CLAIM_START_WINDOW = max(1, int(os.environ.get("UNCHAINED_INSTALL_CLAIM_START_WINDOW", "60")))
_INSTALL_CLAIM_START_MAX_PER_IP = max(1, int(os.environ.get("UNCHAINED_INSTALL_CLAIM_START_MAX_PER_IP", "30")))
_PUBLIC_BASE_URL = (os.environ.get("UNCHAINED_PUBLIC_BASE_URL", "https://api.unchainedsky.com").strip() or "https://api.unchainedsky.com").rstrip("/")
_install_claims: dict[str, dict] = {}  # claim_id -> {secret, expires_at, install_token?}
_install_claims_lock = threading.Lock()
_install_claim_start_hits: dict[str, list[float]] = {}  # source_ip -> recent claim timestamps


def _prune_scheduler_turn_grants(now: float | None = None) -> None:
    now_ts = time.time() if now is None else float(now)
    for grant_id, meta in list(_scheduler_turn_grants.items()):
        expires_at = 0.0
        if isinstance(meta, dict):
            try:
                expires_at = float(meta.get("expires_at", 0) or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
        if expires_at <= now_ts:
            _scheduler_turn_grants.pop(grant_id, None)


def _mint_scheduler_turn_grant(user_id: str, session_id: str, *, now: float | None = None) -> str:
    now_ts = time.time() if now is None else float(now)
    _prune_scheduler_turn_grants(now_ts)
    grant_id = f"sg-{uuid.uuid4().hex}"
    _scheduler_turn_grants[grant_id] = {
        "user_id": str(user_id or ""),
        "session_id": str(session_id or ""),
        "expires_at": now_ts + _SCHEDULER_TURN_GRANT_TTL,
    }
    return grant_id


def _validate_scheduler_turn_grant(user_id: str, session_id: str, grant_id: str, *, now: float | None = None) -> bool:
    if not user_id or not session_id or not grant_id:
        return False
    _prune_scheduler_turn_grants(now)
    meta = _scheduler_turn_grants.get(str(grant_id))
    if not isinstance(meta, dict):
        return False
    return (
        hmac.compare_digest(str(meta.get("user_id", "")), str(user_id))
        and hmac.compare_digest(str(meta.get("session_id", "")), str(session_id))
    )


def _parse_relay() -> tuple[str, int]:
    """Parse RELAY_INTERNAL_URL into (host, port)."""
    url = os.environ.get("RELAY_INTERNAL_URL", "ws://relay:8765")
    parsed = urlparse(url)
    return parsed.hostname or "relay", parsed.port or 8765


def _relay_shared_token() -> str:
    return (os.environ.get("RELAY_SHARED_TOKEN") or os.environ.get("PRIVATE_CORE_TOKEN", "")).strip()


def _relay_auth_headers() -> dict[str, str]:
    token = _relay_shared_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _relay_cdp_url(agent_id: str, tab_id: str = "auto") -> str:
    relay_host, relay_port = _parse_relay()
    scheme = "wss" if relay_port == 443 else "ws"
    port_part = "" if relay_port in (443, 80) else f":{relay_port}"
    url = f"{scheme}://{relay_host}{port_part}/cdp/{agent_id}/{tab_id}"
    token = _relay_shared_token()
    if token:
        url += f"?relay_token={quote(token, safe='')}"
    return url


def _scheduler_slug(user_id: str) -> str:
    """Stable, path-safe scheduler namespace for one user."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:24]


def _scheduler_jobs_path(user_id: str) -> Path:
    return _SCHEDULER_STORE_DIR / f"{_scheduler_slug(user_id)}.jobs.json"


def _scheduler_state_path(user_id: str) -> Path:
    return _SCHEDULER_STORE_DIR / f"{_scheduler_slug(user_id)}.state.json"


def _normalize_installer_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p in {"mac", "macos", "darwin", "osx"}:
        return "mac"
    if p in {"windows", "win", "win32"}:
        return "windows"
    return ""


def _native_installer_candidates(platform: str) -> list[str]:
    p = _normalize_installer_platform(platform)
    if p == "mac":
        files = _MAC_INSTALLER_FILES
    elif p == "windows":
        files = _WINDOWS_INSTALLER_FILES
    else:
        return []
    ordered: list[str] = []
    seen = set()
    for name in files:
        if not name or name in seen:
            continue
        ordered.append(name)
        seen.add(name)
    return ordered


def _native_installer_path(platform: str) -> Path | None:
    p = _normalize_installer_platform(platform)
    candidates = _native_installer_candidates(p)
    if not candidates:
        return None
    root = _INSTALLER_ASSETS_DIR.resolve()
    existing: list[tuple[Path, int, int]] = []
    for idx, name in enumerate(candidates):
        candidate = (root / name).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            try:
                mtime_ns = candidate.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            existing.append((candidate, mtime_ns, idx))
    if not existing:
        return None
    # Prefer the freshest artifact to avoid stale .msi/.dmg shadowing newly built .exe/.pkg.
    # Tie-break by configured candidate order.
    existing.sort(key=lambda item: (item[1], -item[2]), reverse=True)
    return existing[0][0]


def _cleanup_install_claims(now: float | None = None):
    ts = now or time.time()
    stale = []
    for claim_id, info in _install_claims.items():
        if info.get("expires_at", 0) <= ts:
            stale.append(claim_id)
    for claim_id in stale:
        _install_claims.pop(claim_id, None)


def _cleanup_install_claim_start_hits(now: float | None = None):
    ts = now or time.time()
    cutoff = ts - _INSTALL_CLAIM_START_WINDOW
    for source, hits in list(_install_claim_start_hits.items()):
        keep = [t for t in hits if t >= cutoff]
        if keep:
            _install_claim_start_hits[source] = keep
        else:
            _install_claim_start_hits.pop(source, None)


def _is_valid_claim_id(claim_id: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", claim_id or ""))


def _request_source_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        source = forwarded.split(",")[0].strip()
        if source:
            return source
    return (request.remote or "unknown").strip() or "unknown"


def _host_from_request(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-Host", "").strip()
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return (request.host or "").strip()


def _hostname_from_host(candidate: str) -> str:
    raw = (candidate or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(f"//{raw}")
    except Exception:
        return ""
    return (parsed.hostname or "").strip().lower()


def _is_local_hostname(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local")
    return ip.is_loopback or ip.is_private


def _is_public_unchained_hostname(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    return host == "unchainedsky.com" or host.endswith(".unchainedsky.com")


def _public_base_url(request: web.Request) -> str:
    requested_host = _host_from_request(request)
    requested_hostname = _hostname_from_host(requested_host)
    if requested_host and requested_hostname:
        if _is_local_hostname(requested_hostname):
            if not GOOGLE_CLIENT_ID:
                return f"http://{requested_host}"
        elif _is_public_unchained_hostname(requested_hostname):
            scheme = "https" if _cookie_secure(request) else "http"
            return f"{scheme}://{requested_host}"
    return _PUBLIC_BASE_URL


def _public_relay_url(request: web.Request) -> str:
    parsed = urlparse(_public_base_url(request))
    host = parsed.hostname or "api.unchainedsky.com"
    if parsed.scheme == "http":
        return f"ws://{host}:8765/tunnel"
    return f"wss://{parsed.netloc}/tunnel"


def _request_install_token(request: web.Request) -> str:
    header_token = request.headers.get("X-Install-Token", "").strip()
    if header_token:
        return header_token
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
        if bearer:
            return bearer
    return request.query.get("install_token", "").strip()


_SCHEDULER_DEFAULT_JOBS = {
    "jobs": [
        {
            "id": "daily-summary",
            "prompt": "Open my dashboard and summarize anything important since yesterday.",
            "schedule": {"daily_at": "14:00"},
            "use_stable_session": True,
            "timeout_seconds": 240,
            "enabled": False,
        },
        {
            "id": "quick-health-check",
            "prompt": "Check if the status page shows incidents and report back.",
            "schedule": {"every_minutes": 30},
            "retry_seconds": 120,
            "enabled": False,
        },
        {
            "id": "one-time-export",
            "prompt": "Export this week's report as CSV and confirm where it was downloaded.",
            "schedule": {"at": "2026-03-01T18:00:00Z"},
            "enabled": False,
        },
    ]
}


def _scheduler_read_jobs_payload(user_id: str) -> dict:
    path = _scheduler_jobs_path(user_id)
    if not path.exists():
        return _SCHEDULER_DEFAULT_JOBS
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {"jobs": []}
    except Exception:
        return {"jobs": []}


def _scheduler_write_jobs_payload(user_id: str, payload: dict) -> None:
    _SCHEDULER_STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = _scheduler_jobs_path(user_id)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(_SCHEDULER_STORE_DIR)) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def _scheduler_preview_rows(user_id: str, jobs: list) -> list[dict]:
    import scheduled_tasks as st

    state_path = _scheduler_state_path(user_id)
    state = st.load_state(state_path)
    preview = st.preview_jobs(jobs, state=state)
    for row in preview:
        last_output = st.latest_success_output(state_path, row["id"], limit=20)
        if last_output:
            one_line = " ".join(last_output.split())
            if len(one_line) > 120:
                one_line = one_line[:117].rstrip() + "..."
            row["last_output_preview"] = one_line
    return preview


_LOCAL_AGENT_MODEL_PREFIXES = (
    "claude-sdk:",
    "codex-cli:",
    "codex-sdk:",
    "opencode-cli:",
)


def _is_openrouter_model(model: str) -> bool:
    """Return whether a model ID routes through the OpenRouter trial agent."""
    value = (model or "").strip()
    return "/" in value and not value.startswith(_LOCAL_AGENT_MODEL_PREFIXES)


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _openrouter_budget_state_for_user(user_id: str) -> dict:
    return _auth.get_or_init_openrouter_budget(
        user_id,
        min_budget_usd=_OPENROUTER_TRIAL_BUDGET_USD,
        max_budget_usd=_OPENROUTER_TRIAL_BUDGET_USD,
    )


def _track_openrouter_usage_for_user(
    user_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost_usd: float,
) -> dict:
    return _auth.add_openrouter_usage(
        user_id=user_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        min_budget_usd=_OPENROUTER_TRIAL_BUDGET_USD,
        max_budget_usd=_OPENROUTER_TRIAL_BUDGET_USD,
    )


def _is_openrouter_post_cap_allowed_model(model: str) -> bool:
    m = (model or "").strip()
    return m in _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS


def _is_codex_sdk_model(model: str) -> bool:
    return (model or "").startswith("codex-sdk:")


def _is_codex_cli_model(model: str) -> bool:
    return (model or "").startswith("codex-cli:")


def _is_opencode_cli_model(model: str) -> bool:
    return (model or "").startswith("opencode-cli:")


def _is_claude_sdk_model(model: str) -> bool:
    return (model or "").startswith("claude-sdk:")


from web_app.runtime_agents import (
    cleanup_idle_gemini_agents as _cleanup_idle_gemini_agents,
    spawn_claude_sdk_agent as _spawn_claude_sdk_agent,
    spawn_codex_agent as _spawn_codex_agent,
    spawn_codex_cli_agent as _spawn_codex_cli_agent,
    spawn_codex_sdk_agent as _spawn_codex_sdk_agent,
    spawn_gemini_agent as _spawn_gemini_agent,
)
from web_app.runtime_tabs import (
    close_session_tab as _close_session_tab,
    create_session_tab as _create_session_tab,
    ensure_session_tab as _ensure_session_tab,
    headless_agent_watchdog_loop as _headless_agent_watchdog_loop,
    session_cdp_url as _session_cdp_url,
    stale_tab_cleanup_loop as _stale_tab_cleanup_loop,
)


def _resolve_trial_session_id(agent_id: str, requested: str) -> str:
    """Allow only session IDs scoped to the authenticated agent."""
    prefix = f"s-{agent_id}"
    sid = (requested or "").strip()
    if not sid:
        return prefix
    if sid.startswith(prefix):
        return sid
    log.warning("[chat] rejected cross-agent session_id=%s for agent=%s", sid, agent_id)
    return prefix


def _trial_session_path(session_id: str) -> str:
    safe_id = session_id.replace("/", "_").replace("..", "").replace(" ", "_")
    sessions_dir = os.environ.get("UNCHAINED_SESSIONS_DIR", "/data/sessions")
    return os.path.join(sessions_dir, f"{safe_id}.json")


def _looks_like_tool_payload(text: str) -> bool:
    """Detect raw tool-call JSON that should not be shown to users."""
    s = (text or "").strip()
    if not s:
        return False
    # {"name": "ddm", "arguments": {...}}
    if re.search(
        r'^\s*\{\s*"?name"?\s*:\s*[^,\n]+,\s*"?arguments"?\s*:\s*\{',
        s, flags=re.IGNORECASE,
    ):
        return True
    if "<tool_call" in s.lower() or "</tool_call>" in s.lower():
        return True
    return False


def _strip_tool_payloads(text: str) -> str:
    """Remove inline tool-call JSON from text, keep human-readable parts."""
    cleaned = re.sub(
        r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
        "", text, flags=re.DOTALL,
    )
    cleaned = re.sub(r"(?is)<tool_call\b.*?</tool_call>", "", cleaned)
    # Collapse whitespace left behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _read_trial_history(session_id: str) -> tuple[list[dict], bool]:
    """Read trial chat history from disk. Returns (messages, found)."""
    session_path = _trial_session_path(session_id)
    try:
        with open(session_path) as f:
            data = json.load(f)
        raw = data.get("messages", [])
        # Filter to visible chat messages only.
        msgs: list[dict] = []
        for m in raw:
            role = m.get("role")
            if role == "user":
                msgs.append({"role": "user", "content": m.get("content", "")})
            elif role == "assistant":
                # Skip messages that are purely tool calls (no user-facing text)
                if m.get("tool_calls") and not m.get("content"):
                    continue
                content = m.get("content") or ""
                if not content:
                    continue
                # Strip leaked tool-call JSON from content
                if _looks_like_tool_payload(content):
                    content = _strip_tool_payloads(content)
                if content:
                    msgs.append({"role": "assistant", "content": content})
        return msgs, True
    except FileNotFoundError:
        return [], False
    except Exception as e:
        log.warning("Failed to read trial session %s: %s", session_path, e)
        return [], False


def _delete_trial_session(session_id: str):
    """Remove a persisted trial session file if it exists."""
    session_path = _trial_session_path(session_id)
    try:
        os.remove(session_path)
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning("Failed to delete trial session %s: %s", session_path, e)


# ---------------------------------------------------------------------------
# Google OAuth + Session helpers
# ---------------------------------------------------------------------------

async def _get_google_jwks():
    """Fetch and cache Google's public keys for ID token verification."""
    global _google_jwks, _google_jwks_expiry
    now = time.time()
    if _google_jwks and now < _google_jwks_expiry:
        return _google_jwks
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://www.googleapis.com/oauth2/v3/certs")
        resp.raise_for_status()
    _google_jwks = jwt.PyJWKSet.from_dict(resp.json())
    _google_jwks_expiry = now + 3600
    return _google_jwks


async def verify_google_token(id_token: str) -> dict | None:
    """Verify a Google ID token. Returns payload {email, name, picture} or None."""
    try:
        jwks = await _get_google_jwks()
        header = jwt.get_unverified_header(id_token)
        key = next(k for k in jwks.keys if k.key_id == header["kid"])
        payload = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=GOOGLE_CLIENT_ID,
            options={"verify_iss": True},
            issuer=["https://accounts.google.com", "accounts.google.com"],
        )
        return payload
    except Exception as e:
        print(f"[auth] Google token verification failed: {e}")
        return None


def create_session_token(user_id: str, email: str) -> str:
    """Create a signed JWT session token."""
    return jwt.encode(
        {"user_id": user_id, "email": email,
         "iat": int(time.time()),
         "exp": int(time.time()) + JWT_EXPIRY_HOURS * 3600},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )


def verify_session_token(token: str) -> dict | None:
    """Verify a session JWT. Returns {user_id, email} or None."""
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {
            "user_id": p["user_id"],
            "email": p["email"],
            "iat": int(p.get("iat", 0)),
        }
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def _authenticate(request: web.Request) -> dict | None:
    """Authenticate via session cookie OR Bearer token.

    Returns {user_id, key, agent_id, email, status, user_type} or None.
    """
    # 1. Session cookie (web UI)
    sessions: list[dict] = []
    for token in _session_cookie_candidates(request):
        session = verify_session_token(token)
        if session:
            sessions.append(session)
    sessions.sort(key=lambda s: int(s.get("iat", 0)), reverse=True)
    for session in sessions:
        # Validate by user_id (from JWT), not just by email — a recycled
        # email or stale JWT must not bypass user status checks.
        user = _auth.find_user_by_id(session["user_id"])
        if user is None:
            continue
        # Ensure the email in the JWT matches the current user row
        if user.get("email", "").lower() != session.get("email", "").lower():
            continue
        if user and user.get("api_key"):
            status = user.get("status", "approved")
            # Rejected accounts cannot continue using existing sessions/keys
            if status == "rejected":
                continue
            api_key = user["api_key"]
            key_hash = _key_hash(api_key)
            agent_id = f"claude-{key_hash}"
            return {
                "user_id": session["user_id"], "key": api_key,
                "agent_id": agent_id, "key_hash": key_hash,
                "email": session["email"],
                "status": status,
                "user_type": user.get("user_type", "claude"),
            }

    # 2. Bearer token (local scripts, API clients)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:]
        info = _auth.validate_key(key)
        if info:
            key_hash = _key_hash(key)
            agent_id = f"claude-{key_hash}"
            result = {"user_id": info["user_id"], "key": key,
                      "agent_id": agent_id, "key_hash": key_hash}
            # Hydrate by validated user_id (NOT by api_key) so secondary
            # keys cannot bypass rejected/pending status.
            user = _auth.find_user_by_id(info["user_id"])
            if user:
                status = user.get("status", "approved")
                if status == "rejected":
                    return None
                result["email"] = user.get("email", "") or ""
                result["name"] = user.get("name", "") or ""
                result["picture"] = user.get("picture", "") or ""
                result["status"] = status
                result["user_type"] = user.get("user_type", "claude")
            else:
                # Fallback: service accounts may have keys without a users row
                result["status"] = "approved"
                result["user_type"] = "claude"
            return result

    return None


def _is_pending_user(auth_info: dict | None) -> bool:
    if not auth_info:
        return False
    return auth_info.get("status") == "pending"


def _is_pending_trial_user(auth_info: dict | None) -> bool:
    """Backward-compatible helper for legacy call sites."""
    return _is_pending_user(auth_info) and auth_info.get("user_type") == "trial"


def _pending_limited_response() -> web.Response:
    return web.json_response(
        {
            "error": "pending_account_limited",
            "message": "Account review is pending. Use /trial or /demo for now.",
        },
        status=403,
    )


def _pending_trial_limited_response() -> web.Response:
    """Backward-compatible helper for legacy call sites."""
    return _pending_limited_response()


# ---------------------------------------------------------------------------
# Public handlers (compat exports while web.py is split into web_app modules)
# ---------------------------------------------------------------------------

_FAVICON_SVG = None


def _load_favicon() -> str:
    global _FAVICON_SVG
    if _FAVICON_SVG is None:
        favicon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.svg")
        try:
            with open(favicon_path) as f:
                _FAVICON_SVG = f.read()
        except FileNotFoundError:
            _FAVICON_SVG = ""
    return _FAVICON_SVG


async def handle_favicon(request: web.Request) -> web.Response:
    """GET /favicon.svg — serve the site icon."""
    del request
    svg = _load_favicon()
    if not svg:
        return web.Response(status=404)
    return web.Response(
        text=svg,
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


_WASMBROWSER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "web_app", "static", "wasmbrowser",
)
_WASMBROWSER_ALLOWED = {
    "engine.js", "dom.js", "bridge.js", "shims.js", "loader.js", "intel.js",
}
_SIGNED_CHAT_RECONNECT_ASSET = Path(__file__).with_name("web_app") / "static" / "signed-chat-reconnect.js"


async def handle_signed_chat_reconnect_asset(request: web.Request) -> web.Response:
    """Serve the fixed signed-chat reconnect runtime."""
    del request
    try:
        body = _SIGNED_CHAT_RECONNECT_ASSET.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(status=404)
    return web.Response(
        text=body,
        content_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def handle_wasmbrowser_asset(request: web.Request) -> web.Response:
    """GET /web/wasmbrowser/{filename} — serve QuickJS-WASM browser assets.

    These are the 6 JS files extracted from sky-search/client/wasm/ that
    power the live WASM browser embedded in the marketing landing page.
    Allowlisted filenames only — do not let path traversal escape the dir.
    """
    raw = request.match_info.get("filename", "")
    # Defense-in-depth: strip any path components even though the allowlist
    # below only contains bare filenames. If the route ever broadens, this
    # keeps "../../etc/passwd" from reaching open().
    filename = os.path.basename(raw)
    if filename not in _WASMBROWSER_ALLOWED:
        return web.Response(status=404)
    path = os.path.join(_WASMBROWSER_DIR, filename)
    try:
        with open(path) as f:
            body = f.read()
    except FileNotFoundError:
        return web.Response(status=404)
    return web.Response(
        text=body,
        content_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=3600",
            # Prevent MIME-type confusion attacks: browser must honor the
            # declared application/javascript and not sniff the body.
            "X-Content-Type-Options": "nosniff",
        },
    )


_og_image_cache: bytes | None = None


async def handle_og_image(request: web.Request) -> web.Response:
    """GET /og-image.png — branded Open Graph image for social sharing."""
    global _og_image_cache
    del request
    if _og_image_cache is None:
        og_path = Path(__file__).with_name("og-image.png")
        if og_path.exists():
            _og_image_cache = og_path.read_bytes()
        else:
            _og_image_cache = b""
            log.warning("og-image.png not found at %s", og_path)
    if not _og_image_cache:
        return web.Response(status=404)
    return web.Response(
        body=_og_image_cache,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


async def handle_robots_txt(request: web.Request) -> web.Response:
    """GET /robots.txt — search engine crawl directives."""
    del request
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /web/\n"
        "Disallow: /auth/\n"
        "Disallow: /chat/ws\n"
        "Disallow: /install/\n"
        "Disallow: /trial/\n"
        "\n"
        "Sitemap: https://unchainedsky.com/sitemap.xml\n"
    )
    return web.Response(
        text=body,
        content_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_sitemap_xml(request: web.Request) -> web.Response:
    """GET /sitemap.xml — list public pages for search engines."""
    del request
    # Include published results dynamically
    try:
        from published_results import list_results
        published = list_results(limit=500)
    except Exception:
        published = []
    pages = [
        ("https://unchainedsky.com/", "1.0", "weekly"),
        ("https://unchainedsky.com/unbrowser", "0.9", "weekly"),
        ("https://unchainedsky.com/first-look", "0.9", "weekly"),
        ("https://unchainedsky.com/browserbase-alternative", "0.8", "monthly"),
        ("https://unchainedsky.com/browser-mcp-alternative", "0.8", "monthly"),
        ("https://unchainedsky.com/chrome-tax", "0.8", "monthly"),
        ("https://unchainedsky.com/use/apartment-hunting", "0.8", "monthly"),
        ("https://unchainedsky.com/use/flight-comparison", "0.8", "monthly"),
        ("https://unchainedsky.com/use/competitor-monitoring", "0.8", "monthly"),
        ("https://unchainedsky.com/use/price-tracking", "0.8", "monthly"),
        ("https://unchainedsky.com/mcp", "0.7", "monthly"),
        ("https://unchainedsky.com/mcp-guide", "0.7", "monthly"),
        ("https://unchainedsky.com/case-study/zillow-rental", "0.6", "monthly"),
        ("https://unchainedsky.com/privacy", "0.3", "yearly"),
    ]
    # Append published result pages
    for r in published:
        pages.append((
            f"https://unchainedsky.com/r/{r['slug']}",
            "0.5",
            "monthly",
        ))
    urls = "\n".join(
        f"  <url>\n"
        f"    <loc>{loc}</loc>\n"
        f"    <priority>{pri}</priority>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"  </url>"
        for loc, pri, freq in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return web.Response(
        text=xml,
        content_type="application/xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_google_verification(request: web.Request) -> web.Response:
    """GET /google83c650022d8db556.html — Google Search Console verification."""
    del request
    return web.Response(
        text="google-site-verification: google83c650022d8db556.html",
        content_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_google_verification_current(request: web.Request) -> web.Response:
    """Serve the current Google Search Console HTML-file verification."""
    del request
    return web.Response(
        text="google-site-verification: google333e7a6c98af8946.html",
        content_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_index(request: web.Request) -> web.Response:
    # When the financial workspace control plane is enabled, "/" must never
    # render the marketing index: /fin-terminal/ is served by the dedicated
    # /workspace-terminal leg, and a stray direct hit on "/" of the control
    # plane (Docker-internal only) fails closed instead of falsely routing a
    # workspace visitor to marketing.
    try:
        from financial_workspace import is_fin_workspace_enabled
        if is_fin_workspace_enabled():
            return web.Response(
                status=404,
                text="Not found",
                content_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
    except Exception:
        pass
    # Explicit per-variant routing so a flip of LANDING_HTML can't accidentally
    # break the v2/v3 escape hatches. Default falls back to LANDING_HTML. Old
    # preview cookies are ignored on plain "/" visits so V4 is truly default.
    query_variant = request.query.get("ui")
    preview_cookie = request.cookies.get("ui")
    variant = query_variant or ""
    if variant == "v3":
        template = LANDING_V3_HTML
        landing_variant = "landing_legacy_v3"
    elif variant == "v2":
        template = LANDING_V2_HTML
        landing_variant = "landing_legacy_v2"
    elif variant in {"v4", "default"}:
        template = LANDING_V4_HTML
        landing_variant = "landing_value_prop_v1"
    else:
        template = LANDING_HTML
        landing_variant = "landing_value_prop_v1"
    _track_page_view(request, landing_variant=landing_variant)
    html = inject_google_client_id(
        template.replace("__CONTACT_EMAIL__", CONTACT_EMAIL),
        GOOGLE_CLIENT_ID,
    )
    response = web.Response(text=html, content_type="text/html")
    if request.query.get("ui") in {"v2", "v3"}:
        # Persist the choice for ~1 day so deep-links inside the site keep the
        # same variant when the visitor returns to "/".
        response.set_cookie("ui", request.query["ui"], max_age=86400, path="/")
    elif request.query.get("ui") in {"v4", "default"}:
        # V4 is the default landing page. Clear older preview cookies so users
        # can explicitly return from v2/v3 without pinning another default.
        response.del_cookie("ui", path="/")
    elif query_variant is None and preview_cookie in {"v2", "v3"}:
        # Clear stale preview cookies when users return to the canonical route.
        response.del_cookie("ui", path="/")
    return response


async def handle_test(request: web.Request) -> web.Response:
    auth_info = _authenticate(request)
    if not auth_info:
        raise web.HTTPFound("/")  # redirect to landing page
    html = inject_google_client_id(HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_internal_health(request: web.Request) -> web.Response:
    """Return a minimal readiness response for Docker-internal health checks."""
    del request
    return web.json_response({"status": "ok"})


async def handle_agent_version(request: web.Request) -> web.Response:
    """GET /web/agent/version — return current agent version info."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    from agent_package import VERSION, MIN_VERSION
    return web.json_response({"version": VERSION, "min_version": MIN_VERSION})


async def handle_agent_files(request: web.Request) -> web.Response:
    """GET /web/agent/files — download code-only update ZIP."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    from agent_package import build_update_zip
    zip_bytes = build_update_zip()
    return web.Response(
        body=zip_bytes,
        content_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=unchained-update.zip"},
    )


async def handle_research_desk_files(request: web.Request) -> web.Response:
    """GET /web/research-desk/files — download an installable Research Desk ZIP."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    from agent_package import build_research_desk_zip

    zip_bytes = build_research_desk_zip()
    return web.Response(
        body=zip_bytes,
        content_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=unchained-pyreplab.zip"},
    )


from web_app.handlers.auth_admin import (
    handle_admin_approve,
    handle_admin_page,
    handle_admin_pending,
    handle_admin_reject,
    handle_admin_users,
    handle_auth_me,
    handle_dev_auth,
    handle_google_auth,
    handle_logout,
    handle_request_claude_access,
    handle_scheduler_agent_delete,
    handle_scheduler_agent_list,
    handle_scheduler_agent_preview,
    handle_scheduler_agent_upsert,
    handle_scheduler_history,
    handle_scheduler_jobs,
    handle_scheduler_page,
    handle_scheduler_preview,
    handle_setup_page,
    is_admin as _is_admin,
)
from web_app.handlers.chat_flow import (
    agent_request as _agent_request,
    check_relay_agent as _check_relay_agent,
    handle_chat_history,
    handle_chat_new,
    handle_chat_slots,
    handle_chat_status,
    handle_chat_switch,
    list_relay_agents_for_auth as _list_relay_agents_for_auth,
    resolve_bridge_agent as _resolve_bridge_agent,
    resolve_chat_agent_id as _resolve_chat_agent_id,
    trial_new_chat_status_cleanup_context,
)
from web_app.handlers.chat_stream import handle_chat_cancel, handle_chat_msg, handle_chat_ws
from web_app.handlers.install_flow import (
    handle_download_agent,
    handle_download_installer,
    handle_install_bootstrap,
    handle_install_claim_approve,
    handle_install_claim_page,
    handle_install_claim_poll,
    handle_install_claim_start,
    handle_install_script,
    handle_install_script_windows,
    handle_install_token,
    handle_trial_connector,
    handle_trial_script,
    handle_trial_script_windows,
    handle_trial_token,
)
from web_app.handlers.pages import (
    handle_case_study_zillow,
    handle_chat_claude_page,
    handle_chat_codex_page,
    handle_chat_gemini_page,
    handle_chat_redirect,
    handle_claude_page,
    handle_demo_page,
    handle_install_page,
    handle_local_page,
    handle_trial_page,
)
from web_app.handlers.provision import (
    handle_provision_confirm,
    handle_provision_profiles,
    handle_provision_revoke,
    handle_provision_save_manual,
    handle_provision_start,
    handle_provision_status,
    spawn_provider_agent as _spawn_provider_agent,
    terminate_provider_agent as _terminate_provider_agent,
)


# ---------------------------------------------------------------------------
# /web/cmd — Direct CDP command dispatch
# ---------------------------------------------------------------------------


def _chat_session_owned_by_auth(session_id: str, auth_info: dict) -> bool:
    """Return whether an authenticated key owns a chat session id."""
    key_hash = str(auth_info.get("key_hash") or "").strip()
    parts = session_id.split("-")
    return bool(
        key_hash
        and len(parts) >= 4
        and parts[0] == "s"
        and parts[2] == key_hash
    )


def _canonical_session_tab(raw_tab_id: str, active_tab_id: str) -> str:
    """Keep newly-created tabs in the active provision slot, when present."""
    raw = str(raw_tab_id or "").strip()
    active = str(active_tab_id or "").strip()
    if not raw or raw == "auto" or raw.startswith("prov-"):
        return raw
    if active.startswith("prov-"):
        parts = active.split("-", 2)
        if len(parts) == 3 and parts[1]:
            return f"prov-{parts[1]}-{raw}"
    return raw


def _resolve_authorized_session_tab(requested_tab: str, allowed_tabs: set[str]) -> str | None:
    """Resolve one exact server-authorized tab from an agent-facing prefix."""
    requested = str(requested_tab or "").strip()
    if not requested or requested == "auto":
        return None
    matches = [
        candidate
        for candidate in allowed_tabs
        if candidate == requested or candidate.startswith(requested)
    ]
    return matches[0] if len(matches) == 1 else None


async def handle_cmd(request: web.Request) -> web.Response:
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    req_id = _request_id(request)
    action = body.get("action")
    session_id = str(body.get("session_id") or body.get("chat_session_id") or "").strip()
    if session_id and not _chat_session_owned_by_auth(session_id, auth_info):
        return web.json_response({"error": "chat session not owned by authenticated user"}, status=403)
    requested_bridge_agent = str(body.get("bridge_agent_id") or "").strip()
    agent_id = auth_info.get("agent_id")
    agent_source = "auth"
    mapped_agent_id = _session_agent_map.get(session_id) if session_id else ""
    if mapped_agent_id:
        agent_id = mapped_agent_id
        agent_source = "session_map"
    else:
        try:
            bridge_info = await _resolve_bridge_agent(
                auth_info,
                preferred_agent_id=requested_bridge_agent,
            )
            resolved_agent_id = bridge_info.get("bridge_agent_id")
            if resolved_agent_id:
                agent_id = resolved_agent_id
                agent_source = "bridge"
        except Exception as e:
            _trace(
                "cmd.bridge_resolution_error",
                req_id=req_id,
                user_id=auth_info.get("user_id", ""),
                requested_bridge_agent=requested_bridge_agent or "-",
                error=str(e)[:160],
            )
    requested_tab_id = str(body.get("tab_id") or "auto").strip()
    tab_id = requested_tab_id
    if session_id:
        if session_id in _expired_profile_sessions:
            return web.json_response(
                {
                    "error": "Browser profile session expired. Select a Chrome profile and try again.",
                    "code": "profile_session_expired",
                },
                status=409,
            )
        active_tab_id = str(_session_tabs.get(session_id) or "").strip()
        if _session_profile_paths.get(session_id) and not active_tab_id:
            return web.json_response(
                {
                    "error": "Browser profile session is being restored. Please retry shortly.",
                    "code": "profile_session_restoring",
                },
                status=409,
            )
        allowed_tabs = _session_allowed_tabs.setdefault(session_id, set())
        if active_tab_id:
            allowed_tabs.add(active_tab_id)
        if requested_tab_id == "auto" and active_tab_id:
            tab_id = active_tab_id
        elif requested_tab_id != "auto":
            authorized_tab = _resolve_authorized_session_tab(requested_tab_id, allowed_tabs)
            if authorized_tab is None:
                return web.json_response(
                    {"error": "tab is not authorized for this chat session"},
                    status=403,
                )
            tab_id = authorized_tab
        if tab_id != "auto":
            # The exact server-authorized target used by the agent becomes
            # the observer target for this chat turn.
            _session_tabs[session_id] = tab_id
            _session_last_active[session_id] = time.time()
        if action == "navigate":
            # Hosted chat is observed in Agent View. Navigating the physical
            # headed Chrome must not bring it over the chat UI.
            body["bring_to_front"] = False
    _trace(
        "cmd.agent_resolved",
        req_id=req_id,
        agent_id=agent_id or "-",
        source=agent_source,
        session_id=session_id or "-",
        requested_bridge_agent=requested_bridge_agent or "-",
    )
    _trace(
        "cmd.in",
        req_id=req_id,
        user_id=auth_info.get("user_id", ""),
        agent_id=agent_id,
        action=action or "-",
        tab_id=tab_id,
    )

    if not action or not agent_id:
        return web.json_response(
            {"error": "action and agent_id required"}, status=400,
        )

    relay_host, relay_port = _parse_relay()

    import cloud_tools

    try:
        if session_id and tab_id == "auto" and action != "rhythm_query":
            target_info = await cloud_tools.run_cdp_command(
                agent_id,
                "auto",
                "Target.getTargetInfo",
                {},
                relay_host,
                relay_port,
                bring_to_front=False,
            )
            tab_id = str(
                ((target_info or {}).get("targetInfo") or {}).get("targetId") or ""
            ).strip()
            if not tab_id:
                raise RuntimeError("Chrome did not return an active target id")
            _session_allowed_tabs.setdefault(session_id, set()).add(tab_id)
            _session_tabs[session_id] = tab_id
            _session_last_active[session_id] = time.time()
        source_lock = _source_operation_locks.setdefault(
            (str(agent_id), str(tab_id)), asyncio.Lock()
        )
        async with source_lock:
            payload = await run_cmd_action(
                action=action,
                body=body,
                agent_id=agent_id,
                tab_id=tab_id,
                relay_host=relay_host,
                relay_port=relay_port,
                cloud_tools=cloud_tools,
            )
        if session_id:
            new_tab_id = ""
            if action == "new_tab":
                new_tab_id = str(payload.get("tab_id") or "").strip()
            elif action == "ddm" and "--new" in (body.get("flags") or []):
                match = re.search(r"(?:^|\n)Tab:\s*([A-Za-z0-9-]{8,160})", str(payload.get("data") or ""))
                if match:
                    new_tab_id = match.group(1)
            if new_tab_id:
                canonical_tab = _canonical_session_tab(new_tab_id, tab_id)
                _session_allowed_tabs.setdefault(session_id, set()).add(canonical_tab)
                _session_tabs[session_id] = canonical_tab
                _session_last_active[session_id] = time.time()
                payload["tab_id"] = canonical_tab
                if action == "new_tab":
                    payload["data"] = f"Created tab {canonical_tab}"
        _trace("cmd.ok", req_id=req_id, action=action, agent_id=agent_id, tab_id=tab_id)
        return web.json_response(payload)

    except UnknownCmdActionError:
        _trace("cmd.unknown", req_id=req_id, action=action or "-", agent_id=agent_id)
        return web.json_response(
            {"error": f"Unknown action: {action}"}, status=400,
        )

    except CmdInputError as e:
        return web.json_response({"error": str(e)}, status=400)

    except Exception as e:
        if is_chrome_unavailable_error(e):
            _trace("cmd.chrome_unavailable", req_id=req_id, action=action or "-", agent_id=agent_id)
            return web.json_response(
                {"error": "Chrome is not open. Please click Chrome in your dock or re-run start.sh."},
                status=502,
            )
        _trace("cmd.error", req_id=req_id, action=action or "-", agent_id=agent_id, error=str(e)[:160])
        return web.json_response({"error": str(e)}, status=500)


# ---------------------------------------------------------------------------
# MCP docs page content
# ---------------------------------------------------------------------------

_MCP_DOC_SPECS: tuple[tuple[str, str], ...] = (
    ("MCP Local Browser Guide", "mcp-local-browser-guide.md"),
    ("MCP Frontend Route Plan", "mcp-frontend-route-plan.md"),
)


def _mcp_doc_candidates(filename: str) -> tuple[Path, ...]:
    here = Path(__file__).resolve().parent
    return (
        here / "web_app" / "content" / filename,
        here.parent / "docs" / filename,
    )


def _load_mcp_doc_markdown(filename: str) -> str:
    for path in _mcp_doc_candidates(filename):
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        f"# Missing document: {filename}\n\n"
        "This markdown source is not available in the current runtime."
    )


def _build_mcp_guide_html() -> str:
    docs_payload = [
        {
            "title": title,
            "filename": filename,
            "markdown": _load_mcp_doc_markdown(filename),
        }
        for title, filename in _MCP_DOC_SPECS
    ]
    docs_json = json.dumps(docs_payload)
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Docs | Unchained</title>
  <meta name="description" content="Connect AI agents to Unchained browser automation over MCP with local-browser setup and integration guidance.">
  <link rel="canonical" href="https://unchainedsky.com/mcp-guide">
  <meta property="og:type" content="website">
  <meta property="og:url" content="https://unchainedsky.com/mcp-guide">
  <meta property="og:title" content="MCP Browser Automation Docs | Unchained">
  <meta property="og:description" content="Local-browser setup and integration guidance for connecting AI agents to Unchained over MCP.">
  <meta property="og:image" content="https://unchainedsky.com/og-image.png">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <script defer src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      background:#0a0a0f;
      color:#e8e8ec;
      line-height:1.55;
    }
    .wrap{max-width:1240px;margin:0 auto;padding:28px 18px 56px}
    .top{display:flex;flex-wrap:wrap;align-items:center;gap:10px;justify-content:space-between;margin-bottom:18px}
    .brand{font-size:12px;letter-spacing:1.4px;text-transform:uppercase;color:#8e8ea0}
    .btn{
      display:inline-flex;align-items:center;justify-content:center;
      border:1px solid #3a3a44;border-radius:10px;padding:9px 14px;
      color:#e8e8ec;text-decoration:none;font-size:13px;
      background:#141420;
    }
    .btn:hover{border-color:#e94560}
    .hero{
      border:1px solid #252532;border-radius:16px;padding:20px 18px;
      background:linear-gradient(180deg,#141420 0%,#101018 100%);
      margin-bottom:16px;
    }
    .hero h1{margin:0 0 6px;font-size:clamp(24px,3vw,34px)}
    .hero p{margin:0;color:#a6a6b5}
    .notice{
      margin-top:14px;border-left:3px solid #e94560;padding:10px 12px;
      background:rgba(233,69,96,0.08);color:#f7d0d8;font-size:13px;
    }
    .tabs{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}
    .tab{
      border:1px solid #2a2a34;border-radius:999px;padding:8px 12px;
      background:#12121b;color:#aeb0c0;cursor:pointer;font-size:13px;
    }
    .tab.active{border-color:#e94560;background:#23141a;color:#fff}
    .panel{
      border:1px solid #252532;border-radius:14px;background:#0e0e15;
      padding:18px;min-height:460px;
    }
    .panel h2{margin:0 0 6px;font-size:20px}
    .filename{font-size:12px;color:#8e8ea0;margin-bottom:16px}
    .markdown{
      color:#e8e8ec;
    }
    .markdown h1,.markdown h2,.markdown h3,.markdown h4{margin:20px 0 10px;line-height:1.25}
    .markdown p{margin:8px 0}
    .markdown ul,.markdown ol{margin:8px 0 12px 20px}
    .markdown li{margin:4px 0}
    .markdown code{
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      background:#171722;border:1px solid #2f2f3c;border-radius:6px;padding:1px 5px;
      font-size:.92em;
    }
    .markdown pre{
      overflow:auto;padding:12px;border-radius:10px;background:#111118;border:1px solid #2b2b36;
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      font-size:13px;
    }
    .markdown pre code{background:transparent;border:0;padding:0}
    .markdown a{color:#ff8398}
    @media (max-width: 780px){
      .wrap{padding:16px 12px 40px}
      .panel{padding:14px}
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="brand">Unchained • MCP Docs</div>
      <div>
        <a class="btn" href="/">Landing</a>
        <a class="btn" href="/local">Open Chat</a>
      </div>
    </div>
    <section class="hero">
      <h1>Zero-Config MCP Setup</h1>
      <p>Reference docs for connecting your existing installed agent to Unchained MCP and shipping the `/mcp` onboarding route. Comparing approaches? <a href="/browser-mcp-alternative">See BrowserMCP.io vs Unchained.</a></p>
      <div class="notice">If your agent is already installed and running, use `https://api.unchainedsky.com/mcp` directly and reuse your current `agent_id`.</div>
    </section>

    <div class="tabs" id="doc-tabs"></div>

    <section class="panel">
      <h2 id="doc-title"></h2>
      <div class="filename" id="doc-file"></div>
      <article class="markdown" id="doc-content"></article>
    </section>
  </div>

  <script>
    const DOCS = __MCP_DOCS_JSON__;
    const tabs = document.getElementById('doc-tabs');
    const titleEl = document.getElementById('doc-title');
    const fileEl = document.getElementById('doc-file');
    const contentEl = document.getElementById('doc-content');

    function esc(text) {
      return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    }

    function renderDoc(index) {
      const doc = DOCS[index];
      titleEl.textContent = doc.title;
      fileEl.textContent = doc.filename;
      if (typeof marked !== 'undefined' && marked.parse) {
        contentEl.innerHTML = marked.parse(doc.markdown || '');
      } else {
        contentEl.innerHTML = '<pre>' + esc(doc.markdown || '') + '</pre>';
      }
      Array.from(tabs.querySelectorAll('.tab')).forEach((el, i) => {
        el.classList.toggle('active', i === index);
      });
    }

    DOCS.forEach((doc, index) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tab' + (index === 0 ? ' active' : '');
      b.textContent = doc.title;
      b.addEventListener('click', () => renderDoc(index));
      tabs.appendChild(b);
    });

    renderDoc(0);
  </script>
</body>
</html>"""
    return html.replace("__MCP_DOCS_JSON__", docs_json)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

_NON_PAGE_404_PREFIXES = (
    "/api",
    "/auth",
    "/cdp",
    "/core",
    "/health",
    "/mcp",
    "/tunnel",
    "/unbrowser-mcp",
    "/web",
)


def _accepts_html(request: web.Request) -> bool:
    for item in request.headers.get("Accept", "").lower().split(","):
        media_type, *parameters = (part.strip() for part in item.split(";"))
        if media_type != "text/html":
            continue
        quality = 1.0
        for parameter in parameters:
            if parameter.startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality > 0:
            return True
    return False


def _is_browser_page_request(request: web.Request) -> bool:
    if request.method not in {"GET", "HEAD"} or not _accepts_html(request):
        return False
    path = request.path
    return not any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _NON_PAGE_404_PREFIXES
    )


@web.middleware
async def branded_not_found_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPNotFound:
        if not _is_browser_page_request(request):
            raise
        return web.Response(
            status=404,
            text=PUBLIC_404_HTML,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


def _register_routes(app: web.Application):
    register_route_specs(
        app,
        ROUTE_SPECS,
        globals(),
        include_dev_auth=not GOOGLE_CLIENT_ID,
        dev_auth_handler=handle_dev_auth,
    )


# ---------------------------------------------------------------------------
# Financial workspace control plane — eager init + outbox/sweep loops.
# The separate workspace-control slice runs this same app on port 8790.
# ---------------------------------------------------------------------------

def _resolve_fin_workspace_control_token() -> str:
    from financial_workspace import _resolve_control_token as _token
    return _token()


def _init_fin_workspace_control_plane() -> None:
    """Eagerly initialize the financial workspace control plane.

    Fails closed: enabling ``FIN_WORKSPACE_ENABLED`` without the explicit
    control token, S3 bucket, region, KMS key, storage configuration, and a
    *validated* host-side runtime provider is a startup error — the container
    refuses to boot rather than falling back to ``JWT_SECRET`` or local
    storage, and ``/fin-terminal/`` is never falsely routed to the marketing
    index or the public singleton.
    """
    from financial_workspace import (
        FinancialWorkspace,
        is_fin_workspace_enabled,
        runtime_provider_validate,
        validate_fin_workspace_config,
    )
    from checkpoint_store import create_checkpoint_store

    global _fin_workspace
    if _fin_workspace is not None:
        return
    if not is_fin_workspace_enabled():
        return

    errors = validate_fin_workspace_config()
    if errors:
        raise RuntimeError(
            "financial workspace enabled but misconfigured (refusing to start): "
            + "; ".join(errors)
        )

    # Hard enablement gate: activation requires a validated host-side runtime
    # provider (reachable AND declaring accountRuntime+checkpointFile
    # capabilities). Without it the private workspace leg cannot open the
    # imported checkpoint in an isolated runtime — fail activation now.
    provider = runtime_provider_validate()
    if provider is None:
        raise RuntimeError(
            "financial workspace enabled but the runtime provider is not "
            "validated (refusing to start): FIN_WORKSPACE_RUNTIME_PROVIDER_URL "
            "must answer /v1/health with accountRuntime+checkpointFile "
            "capabilities"
        )
    log.info("[fin-workspace] runtime provider validated: %s", provider.get("provider", "unknown"))

    store = create_checkpoint_store(require_s3=True)
    _fin_workspace = FinancialWorkspace(_auth.db_path, store)
    log.info(
        "[fin-workspace] control plane initialized "
        "(store=%s)", type(store).__name__
    )


async def _fin_workspace_effects_loop() -> None:
    """Real outbox consumer: processes pending workspace effects periodically."""
    import os as _os
    interval = max(5, int(_os.environ.get("FIN_WORKSPACE_EFFECT_INTERVAL_SECONDS", "15")))
    while True:
        await asyncio.sleep(interval)
        if _fin_workspace is None:
            continue
        try:
            from credit import CreditLedger
            ledger = CreditLedger(db_path=_auth.db_path)
            effects = await asyncio.to_thread(_fin_workspace.poll_pending_effects, 10)
            for effect in effects:
                eid = effect["effect_id"]
                try:
                    if not _fin_workspace.mark_effect_processing(eid):
                        continue
                    if effect["effect_type"] == "account_grant":
                        await asyncio.to_thread(
                            _fin_workspace.process_account_grant_effect,
                            effect["context"], ledger,
                        )
                    # workspace_upsert/snapshot_import are applied inline during
                    # claim accept; the outbox row exists for idempotent replay.
                    _fin_workspace.mark_effect_completed(eid)
                except Exception as exc:
                    _fin_workspace.mark_effect_completed(eid, failed=True, error=str(exc))
                    log.warning("[fin-workspace] effect %s failed: %s", eid, exc)
        except Exception as exc:
            log.warning("[fin-workspace] effects loop error: %s", exc)


async def _fin_workspace_sweep_loop() -> None:
    """Real sweeper: expires stale checkpoints and deletes their storage."""
    import os as _os
    interval = max(30, int(_os.environ.get("FIN_WORKSPACE_SWEEP_INTERVAL_SECONDS", "300")))
    while True:
        await asyncio.sleep(interval)
        if _fin_workspace is None:
            continue
        try:
            count = await asyncio.to_thread(_fin_workspace.sweep_expired)
            if count:
                log.info("[fin-workspace] sweep: expired %d checkpoint(s)", count)
        except Exception as exc:
            log.warning("[fin-workspace] sweep error: %s", exc)


async def _on_startup(app_: web.Application):
    del app_
    global _stale_tab_task, _gemini_cleanup_task, _headless_watchdog_task
    _state.stale_tab_task = asyncio.create_task(_stale_tab_cleanup_loop())
    _state.gemini_cleanup_task = asyncio.create_task(_cleanup_idle_gemini_agents())
    _state.headless_watchdog_task = asyncio.create_task(_headless_agent_watchdog_loop())
    _stale_tab_task = _state.stale_tab_task
    _gemini_cleanup_task = _state.gemini_cleanup_task
    _headless_watchdog_task = _state.headless_watchdog_task
    # Credit stale-run sweep finalizes unresolved reservations after crashes.
    _state.credit_sweep_task = asyncio.create_task(_credit_stale_sweep_loop())
    # Financial workspace outbox consumer + sweeper (feature-flagged).
    if _fin_workspace is not None:
        _state.fin_workspace_effect_task = asyncio.create_task(_fin_workspace_effects_loop())
        _state.fin_workspace_sweep_task = asyncio.create_task(_fin_workspace_sweep_loop())


async def _credit_stale_sweep_loop():
    """Periodically finalize stale pre-submit and submitted reservations."""
    import os as _os
    interval = max(60, int(_os.environ.get("CREDIT_SWEEP_INTERVAL_SECONDS", "600")))
    try:
        from credit import CreditLedger
        ledger = await asyncio.to_thread(CreditLedger, db_path=_auth.db_path)
        while True:
            await asyncio.sleep(interval)
            try:
                expired = await asyncio.to_thread(ledger.sweep_stale_runs)
                if expired:
                    log.info("[credit] sweep: expired %d stale run(s)", expired)
            except Exception as e:
                log.warning("[credit] sweep error: %s", e)
    except asyncio.CancelledError:
        pass


async def _on_cleanup(app_: web.Application):
    del app_
    global _stale_tab_task, _gemini_cleanup_task, _headless_watchdog_task
    if _state.stale_tab_task:
        _state.stale_tab_task.cancel()
    if _state.gemini_cleanup_task:
        _state.gemini_cleanup_task.cancel()
    if _state.headless_watchdog_task:
        _state.headless_watchdog_task.cancel()
    if getattr(_state, "credit_sweep_task", None):
        _state.credit_sweep_task.cancel()
    if getattr(_state, "fin_workspace_effect_task", None):
        _state.fin_workspace_effect_task.cancel()
    if getattr(_state, "fin_workspace_sweep_task", None):
        _state.fin_workspace_sweep_task.cancel()
    _state.stale_tab_task = None
    _state.gemini_cleanup_task = None
    _state.headless_watchdog_task = None
    _state.credit_sweep_task = None
    _state.fin_workspace_effect_task = None
    _state.fin_workspace_sweep_task = None
    _stale_tab_task = None
    _gemini_cleanup_task = None
    _headless_watchdog_task = None
    # Close persistent HTTP client for private-core.
    try:
        from private_core_client import get_private_core_client
        await get_private_core_client().close()
    except Exception:
        pass
    # Terminate all Gemini agent subprocesses.
    for aid, proc in list(_gemini_procs.items()):
        if proc.poll() is None:
            proc.terminate()
            log.info("[gemini] Terminated agent %s on shutdown", aid)
    for aid, fh in list(_gemini_log_fhs.items()):
        try:
            fh.close()
        except Exception:
            pass
    for aid, proc in list(_codex_sdk_procs.items()):
        if proc.poll() is None:
            proc.terminate()
            log.info("[codexsdk] Terminated agent %s on shutdown", aid)
    for aid, fh in list(_codex_sdk_log_fhs.items()):
        try:
            fh.close()
        except Exception:
            pass
    for aid, proc in list(_codex_cli_procs.items()):
        if proc.poll() is None:
            proc.terminate()
            log.info("[codexcli] Terminated agent %s on shutdown", aid)
    for aid, fh in list(_codex_cli_log_fhs.items()):
        try:
            fh.close()
        except Exception:
            pass
    for aid, proc in list(_claude_sdk_procs.items()):
        if proc.poll() is None:
            proc.terminate()
            log.info("[claudesdk] Terminated agent %s on shutdown", aid)
    for aid, fh in list(_claude_sdk_log_fhs.items()):
        try:
            fh.close()
        except Exception:
            pass


def create_app() -> web.Application:
    # Eager, fail-closed control-plane init runs before the app serves.
    _init_fin_workspace_control_plane()
    app = web.Application(middlewares=[branded_not_found_middleware])
    _register_routes(app)
    app.cleanup_ctx.append(trial_new_chat_status_cleanup_context)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description="Unchained Web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    app = create_app()

    print(f"Web UI listening on {args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
