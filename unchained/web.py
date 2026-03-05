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
    CASE_STUDY_ZILLOW_HTML,
    CHAT_CLAUDE_SDK_HTML,
    CHAT_CODEX_HTML,
    CHAT_GEMINI_HTML,
    CHAT_HTML,
    CLAUDE_CHAT_HTML,
    HEADLESS_DEMO_HTML,
    HTML,
    INSTALL_CLAIM_HTML,
    INSTALL_ONBOARD_HTML,
    LANDING_HTML,
    SCHEDULER_HTML,
    SETUP_HTML,
    TRIAL_CHAT_HTML,
)

log = logging.getLogger(__name__)

_auth = Auth()

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
CONTACT_EMAIL = (
    os.environ.get("CONTACT_EMAIL", "").strip()
    or (ADMIN_EMAILS[0] if ADMIN_EMAILS else "hello@unchainedsky.com")
)
if not TRIAL_AGENT_KEY:
    log.warning("[chat] TRIAL_AGENT_KEY unset; trial-agent auth bypass disabled.")
if not TRIAL_AGENT_ID:
    log.warning("[chat] TRIAL_AGENT_ID unresolved; OpenRouter trial routing disabled.")

# Demo prompt quota — number of headless demo interactions before requiring trial install
_DEMO_PROMPT_LIMIT = 4

# Turn-based rate limiting for free-tier users
_FREE_DAILY_TURN_LIMIT = 25    # ~25 turns covers 10-20 min of real browsing
_FREE_WINDOW_TURN_LIMIT = 5    # per 5-min window cap (prevents rapid-fire)
_FREE_WINDOW_SECONDS = 300     # 5-minute window
_OPENROUTER_TRIAL_BUDGET_USD = max(
    0.0,
    float(os.environ.get("OPENROUTER_TRIAL_BUDGET_USD", "1.0")),
)
_OPENROUTER_TRIAL_DEFAULT_MODEL = (
    os.environ.get("OPENROUTER_TRIAL_DEFAULT_MODEL", "google/gemini-3-flash-preview").strip()
    or "google/gemini-3-flash-preview"
)
_OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS = tuple(
    m.strip()
    for m in os.environ.get(
        "OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS",
        "arcee-ai/trinity-large-preview:free,stepfun/step-3.5-flash:free",
    ).split(",")
    if m.strip()
)
if not _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS:
    _OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS = (
        "arcee-ai/trinity-large-preview:free",
        "stepfun/step-3.5-flash:free",
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
    # Approved users with API keys bypass rate limiting
    if user.get("status") == "approved" and bool(user.get("api_key")):
        return False
    return True


def send_email(to: str, subject: str, body_html: str):
    """Send email via SMTP. Fails silently with a log if not configured."""
    if not SMTP_HOST:
        log.warning("[email] SMTP_HOST not configured, skipping email to %s: %s", to, subject)
        return
    try:
        msg = MIMEText(body_html, "html")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = to
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, [to], msg.as_string())
        log.info("[email] Sent to %s: %s", to, subject)
    except Exception as e:
        log.error("[email] Failed to send to %s: %s", to, e)


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
_session_agents = _state.session_agents  # session_id -> agent_id that handled it
_agent_req_queues = _state.agent_req_queues  # req_id -> one-shot response queue
_session_tabs = _state.session_tabs  # session_id -> Chrome tab_id
_session_last_active = _state.session_last_active  # session_id -> timestamp
_session_agent_map = _state.session_agent_map  # session_id -> agent_id for CDP routing
_STALE_TAB_SECONDS = 30 * 60
_stale_tab_task: asyncio.Task | None = None
_tabs_pending_close = _state.tabs_pending_close  # tab_id -> (agent_id, retry_count)
_MAX_CLOSE_RETRIES = 3
_MAX_TABS_PER_AGENT = 10
_gemini_procs = _state.gemini_procs  # agent_id -> subprocess
_gemini_log_fhs = _state.gemini_log_fhs  # agent_id -> log file handle
_gemini_last_active = _state.gemini_last_active  # agent_id -> last msg timestamp
_gemini_spawn_lock = _state.gemini_spawn_lock  # prevents duplicate spawn race
_GEMINI_IDLE_TIMEOUT = 600
_gemini_cleanup_task: asyncio.Task | None = None

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
            row["last_output"] = last_output[:500]
    return preview


def _is_openrouter_model(model: str) -> bool:
    return "/" in (model or "")


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
    return os.path.join("/data/sessions", f"{safe_id}.json")


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
        user = _auth.find_user_by_email(session["email"])
        if user and user.get("api_key"):
            api_key = user["api_key"]
            key_hash = _key_hash(api_key)
            agent_id = f"claude-{key_hash}"
            return {
                "user_id": session["user_id"], "key": api_key,
                "agent_id": agent_id, "key_hash": key_hash,
                "email": session["email"],
                "status": user.get("status", "approved"),
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
            return {"user_id": info["user_id"], "key": key,
                    "agent_id": agent_id, "key_hash": key_hash}

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


async def handle_index(request: web.Request) -> web.Response:
    del request
    html = LANDING_HTML.replace("__CONTACT_EMAIL__", CONTACT_EMAIL)
    return web.Response(text=html, content_type="text/html")


async def handle_test(request: web.Request) -> web.Response:
    auth_info = _authenticate(request)
    if not auth_info:
        raise web.HTTPFound("/")  # redirect to landing page
    html = inject_google_client_id(HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


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
    resolve_chat_agent_id as _resolve_chat_agent_id,
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
    agent_id = auth_info.get("agent_id")  # Never trust client-supplied agent_id
    tab_id = body.get("tab_id", "auto")
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
        payload = await run_cmd_action(
            action=action,
            body=body,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
            cloud_tools=cloud_tools,
        )
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
# Server
# ---------------------------------------------------------------------------

def _register_routes(app: web.Application):
    register_route_specs(
        app,
        ROUTE_SPECS,
        globals(),
        include_dev_auth=not GOOGLE_CLIENT_ID,
        dev_auth_handler=handle_dev_auth,
    )


async def _on_startup(app_: web.Application):
    del app_
    global _stale_tab_task, _gemini_cleanup_task
    _state.stale_tab_task = asyncio.create_task(_stale_tab_cleanup_loop())
    _state.gemini_cleanup_task = asyncio.create_task(_cleanup_idle_gemini_agents())
    _stale_tab_task = _state.stale_tab_task
    _gemini_cleanup_task = _state.gemini_cleanup_task


async def _on_cleanup(app_: web.Application):
    del app_
    global _stale_tab_task, _gemini_cleanup_task
    if _state.stale_tab_task:
        _state.stale_tab_task.cancel()
    if _state.gemini_cleanup_task:
        _state.gemini_cleanup_task.cancel()
    _state.stale_tab_task = None
    _state.gemini_cleanup_task = None
    _stale_tab_task = None
    _gemini_cleanup_task = None
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
    app = web.Application()
    _register_routes(app)
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
