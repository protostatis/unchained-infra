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
import sys
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


def _public_base_url(request: web.Request) -> str:
    requested_host = _host_from_request(request)
    requested_hostname = _hostname_from_host(requested_host)
    if not GOOGLE_CLIENT_ID and requested_host and _is_local_hostname(requested_hostname):
        return f"http://{requested_host}"
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


def _session_cdp_url(agent_id: str) -> str:
    """Build the CDP relay URL for any agent."""
    return _relay_cdp_url(agent_id, "auto")


async def _create_session_tab(session_id: str, agent_id: str) -> str:
    """Create a new Chrome tab via CDP Target.createTarget through the relay."""
    from cdp import CDP
    cdp = CDP(_session_cdp_url(agent_id))
    try:
        await asyncio.wait_for(cdp.connect(), timeout=10)
        result = await cdp.send("Target.createTarget", {"url": "about:blank"})
        return result["targetId"]
    finally:
        if cdp.ws:
            await cdp.ws.close()


async def _close_session_tab(session_id: str):
    """Close the Chrome tab via CDP Target.closeTarget.

    On failure, queues the tab for retry instead of silently dropping it.
    """
    tab_id = _session_tabs.pop(session_id, None)
    agent_id = _session_agent_map.pop(session_id, None)
    _session_last_active.pop(session_id, None)
    if not tab_id or not agent_id:
        return
    from cdp import CDP
    try:
        cdp = CDP(_session_cdp_url(agent_id))
        await asyncio.wait_for(cdp.connect(), timeout=5)
        await cdp.send("Target.closeTarget", {"targetId": tab_id})
        if cdp.ws:
            await cdp.ws.close()
        print(f"[tabs] Closed tab {tab_id} for session {session_id}")
    except Exception:
        # Queue for retry instead of silently dropping
        _tabs_pending_close[tab_id] = (agent_id, 0)


async def _ensure_session_tab(session_id: str, agent_id: str) -> str | None:
    """Create a per-session Chrome tab, enforcing _MAX_TABS_PER_AGENT.

    Returns the new tab_id, or None if tab creation fails (caller falls
    back to "auto").  When the agent is at capacity, evicts the oldest
    session (by _session_last_active) to free a slot.
    """
    # Count current tabs for this agent
    agent_tab_count = sum(
        1 for aid in _session_agent_map.values() if aid == agent_id
    )
    # Evict oldest session if at capacity
    if agent_tab_count >= _MAX_TABS_PER_AGENT:
        # Find the oldest session for this agent
        oldest_sid = None
        oldest_ts = float("inf")
        for sid, aid in list(_session_agent_map.items()):
            if aid == agent_id:
                ts = _session_last_active.get(sid, 0)
                if ts < oldest_ts:
                    oldest_ts = ts
                    oldest_sid = sid
        if oldest_sid:
            log.info("[tabs] Evicting oldest session %s for agent %s (at %d tab limit)",
                     oldest_sid, agent_id, _MAX_TABS_PER_AGENT)
            await _close_session_tab(oldest_sid)

    try:
        tab_id = await _create_session_tab(session_id, agent_id)
        _session_tabs[session_id] = tab_id
        _session_agent_map[session_id] = agent_id
        _session_last_active[session_id] = time.time()
        log.info("[tabs] Created tab %s for session %s (agent %s)", tab_id, session_id, agent_id)
        return tab_id
    except Exception as e:
        log.warning("[tabs] Failed to create tab for session %s: %s", session_id, e)
        return None


async def _stale_tab_cleanup_loop():
    """Periodically close stale tabs, retry failed closes, reconcile headless."""
    while True:
        await asyncio.sleep(60)  # check every minute
        now = time.time()

        # Part 1: Close stale sessions
        stale = [
            sid for sid, ts in _session_last_active.items()
            if now - ts > _STALE_TAB_SECONDS
        ]
        for sid in stale:
            print(f"[tabs] Closing stale tab for session {sid}")
            await _close_session_tab(sid)

        # Part 2: Retry pending closes
        for tab_id, (agent_id, retries) in list(_tabs_pending_close.items()):
            if retries >= _MAX_CLOSE_RETRIES:
                print(f"[tabs] Giving up on tab {tab_id} after {retries} retries")
                del _tabs_pending_close[tab_id]
                continue
            try:
                from cdp import CDP
                cdp = CDP(_session_cdp_url(agent_id))
                await asyncio.wait_for(cdp.connect(), timeout=5)
                await cdp.send("Target.closeTarget", {"targetId": tab_id})
                if cdp.ws:
                    await cdp.ws.close()
                del _tabs_pending_close[tab_id]
                print(f"[tabs] Retry-closed tab {tab_id}")
            except Exception:
                _tabs_pending_close[tab_id] = (agent_id, retries + 1)

        # Part 3: Reconcile headless agent (every cycle, lightweight)
        hkey = os.environ.get("HEADLESS_API_KEY", "")
        if not hkey:
            continue
        h_agent = _agent_id("headless", hkey)
        try:
            from cdp import CDP
            cdp = CDP(_session_cdp_url(h_agent))
            await asyncio.wait_for(cdp.connect(), timeout=10)
            result = await cdp.send("Target.getTargets")
            if cdp.ws:
                await cdp.ws.close()
            chrome_tabs = {
                t["targetId"] for t in result.get("targetInfos", [])
                if t.get("type") == "page"
            }
            tracked = set(_session_tabs.values()) | set(_tabs_pending_close.keys())
            orphans = chrome_tabs - tracked
            # Keep at least 1 tab (Chrome needs one)
            if orphans and len(chrome_tabs) > 1:
                for oid in list(orphans):
                    if len(chrome_tabs) <= 1:
                        break
                    try:
                        cdp2 = CDP(_session_cdp_url(h_agent))
                        await asyncio.wait_for(cdp2.connect(), timeout=5)
                        await cdp2.send("Target.closeTarget", {"targetId": oid})
                        if cdp2.ws:
                            await cdp2.ws.close()
                        chrome_tabs.discard(oid)
                        print(f"[tabs] Reconciled orphan tab {oid}")
                    except Exception:
                        pass
        except Exception:
            pass  # Reconciliation is best-effort


def _spawn_gemini_agent(user_id: str, api_key: str, gemini_key: str):
    """Spawn a per-user chat_agent_gemini.py subprocess if not already running."""
    agent_id = _agent_id("gemini", api_key)

    with _gemini_spawn_lock:
        # Already running?
        proc = _gemini_procs.get(agent_id)
        if proc and proc.poll() is None:
            _gemini_last_active[agent_id] = time.time()
            return

        # Build server URL — inside Docker use ws://web:8080, else ws://127.0.0.1:<port>
        port = int(os.environ.get("WEB_PORT", "8080"))
        if os.path.exists("/.dockerenv"):
            server_url = f"ws://web:{port}"
        else:
            server_url = f"ws://127.0.0.1:{port}"

        # Pass relay host/port so the agent can reach Chrome via CDP
        # Pass secrets via env vars (not CLI args) to avoid exposure in ps aux
        relay_host, relay_port = _parse_relay()
        env = {
            **os.environ,
            "RELAY_HOST": relay_host,
            "RELAY_PORT": str(relay_port),
            "UNCHAINED_API_KEY": api_key,
            "GEMINI_API_KEY": gemini_key,
        }

        script = os.path.join(os.path.dirname(__file__), "chat_agent_gemini.py")
        log_path = os.path.join(tempfile.gettempdir(), f"gemini-{agent_id}.log")
        _gemini_log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            [sys.executable, script,
             "--agent", agent_id,
             "--server", server_url],
            env=env,
            stdout=_gemini_log_fh,
            stderr=_gemini_log_fh,
        )
        _gemini_procs[agent_id] = proc
        _gemini_log_fhs[agent_id] = _gemini_log_fh
        _gemini_last_active[agent_id] = time.time()
    log.info("[gemini] Spawned agent %s for user %s (pid %d)", agent_id, user_id, proc.pid)


def _spawn_codex_agent(mode: str, user_id: str, api_key: str, codex_key: str):
    """Spawn a per-user chat_agent_codex.py subprocess in sdk/cli mode."""
    if mode == "codex-cli":
        prefix = "codexcli"
        procs = _codex_cli_procs
        log_fhs = _codex_cli_log_fhs
        last_active = _codex_cli_last_active
        spawn_lock = _codex_cli_spawn_lock
    else:
        prefix = "codexsdk"
        procs = _codex_sdk_procs
        log_fhs = _codex_sdk_log_fhs
        last_active = _codex_sdk_last_active
        spawn_lock = _codex_sdk_spawn_lock

    agent_id = _agent_id(prefix, api_key)
    with spawn_lock:
        proc = procs.get(agent_id)
        if proc and proc.poll() is None:
            last_active[agent_id] = time.time()
            return

        # Build server URL — inside Docker use ws://web:8080, else ws://127.0.0.1:<port>
        port = int(os.environ.get("WEB_PORT", "8080"))
        if os.path.exists("/.dockerenv"):
            server_url = f"ws://web:{port}"
        else:
            server_url = f"ws://127.0.0.1:{port}"

        relay_host, relay_port = _parse_relay()
        env = {
            **os.environ,
            "RELAY_HOST": relay_host,
            "RELAY_PORT": str(relay_port),
            "UNCHAINED_API_KEY": api_key,
            "CODEX_API_KEY": codex_key,
            "OPENAI_API_KEY": codex_key,
            "CODEX_MODE": mode,
        }

        script = os.path.join(os.path.dirname(__file__), "chat_agent_codex.py")
        log_path = os.path.join(tempfile.gettempdir(), f"{agent_id}.log")
        log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            [sys.executable, script,
             "--agent", agent_id,
             "--server", server_url,
             "--mode", mode],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
        procs[agent_id] = proc
        log_fhs[agent_id] = log_fh
        last_active[agent_id] = time.time()
    log.info("[%s] Spawned agent %s for user %s (pid %d)", prefix, agent_id, user_id, proc.pid)


def _spawn_codex_sdk_agent(user_id: str, api_key: str, codex_key: str):
    _spawn_codex_agent("codex-sdk", user_id, api_key, codex_key)


def _spawn_codex_cli_agent(user_id: str, api_key: str, codex_key: str):
    _spawn_codex_agent("codex-cli", user_id, api_key, codex_key)


def _spawn_claude_sdk_agent(user_id: str, api_key: str, claude_key: str):
    """Spawn a per-user chat_agent_sdk.py subprocess if not already running."""
    agent_id = _agent_id("claudesdk", api_key)
    with _claude_sdk_spawn_lock:
        proc = _claude_sdk_procs.get(agent_id)
        if proc and proc.poll() is None:
            _claude_sdk_last_active[agent_id] = time.time()
            return

        # Build server URL — inside Docker use ws://web:8080, else ws://127.0.0.1:<port>
        port = int(os.environ.get("WEB_PORT", "8080"))
        if os.path.exists("/.dockerenv"):
            server_url = f"ws://web:{port}"
        else:
            server_url = f"ws://127.0.0.1:{port}"

        relay_host, relay_port = _parse_relay()
        env = {
            **os.environ,
            "RELAY_HOST": relay_host,
            "RELAY_PORT": str(relay_port),
            "UNCHAINED_API_KEY": api_key,
            "ANTHROPIC_API_KEY": claude_key,
        }

        script = os.path.join(os.path.dirname(__file__), "chat_agent_sdk.py")
        log_path = os.path.join(tempfile.gettempdir(), f"{agent_id}.log")
        log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            [sys.executable, script,
             "--agent", agent_id,
             "--server", server_url],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
        _claude_sdk_procs[agent_id] = proc
        _claude_sdk_log_fhs[agent_id] = log_fh
        _claude_sdk_last_active[agent_id] = time.time()
    log.info("[claudesdk] Spawned agent %s for user %s (pid %d)", agent_id, user_id, proc.pid)


async def _cleanup_idle_gemini_agents():
    """Periodically terminate idle provider agents and expired pending provisions."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        def _reap_idle(label: str, timeout_s: int,
                       procs: dict[str, subprocess.Popen],
                       last_active: dict[str, float],
                       log_fhs: dict[str, object]):
            for aid in list(procs):
                proc = procs[aid]
                # Reap dead processes
                if proc.poll() is not None:
                    log.info("[%s] Agent %s exited (code %s), removing", label, aid, proc.returncode)
                    procs.pop(aid, None)
                    last_active.pop(aid, None)
                    fh = log_fhs.pop(aid, None)
                    if fh:
                        fh.close()
                    continue
                # Kill idle processes
                last = last_active.get(aid, 0)
                if now - last > timeout_s:
                    log.info("[%s] Agent %s idle for >%ds, terminating", label, aid, timeout_s)
                    proc.terminate()
                    procs.pop(aid, None)
                    last_active.pop(aid, None)
                    fh = log_fhs.pop(aid, None)
                    if fh:
                        fh.close()

        _reap_idle("gemini", _GEMINI_IDLE_TIMEOUT, _gemini_procs, _gemini_last_active, _gemini_log_fhs)
        _reap_idle("codexsdk", _CODEX_IDLE_TIMEOUT, _codex_sdk_procs, _codex_sdk_last_active, _codex_sdk_log_fhs)
        _reap_idle("codexcli", _CODEX_IDLE_TIMEOUT, _codex_cli_procs, _codex_cli_last_active, _codex_cli_log_fhs)
        _reap_idle("claudesdk", _CLAUDE_SDK_IDLE_TIMEOUT, _claude_sdk_procs, _claude_sdk_last_active, _claude_sdk_log_fhs)
        # Clean up expired pending provisions
        for uid in list(_pending_provision):
            _, _, ts = _pending_provision[uid]
            if now - ts > _PENDING_PROVISION_TTL:
                log.info("[provision] Expired pending key for user %s", uid)
                _pending_provision.pop(uid, None)
        # Purge stale provision cooldown entries (older than 5 min)
        for uid in list(_provision_cooldowns):
            if now - _provision_cooldowns[uid] > 300:
                _provision_cooldowns.pop(uid, None)
        # Purge idle provision locks
        try:
            import signup_agent
            signup_agent.cleanup_provision_locks()
        except Exception:
            pass


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
        return {"user_id": p["user_id"], "email": p["email"]}
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def _authenticate(request: web.Request) -> dict | None:
    """Authenticate via session cookie OR Bearer token.

    Returns {user_id, key, agent_id, email, status, user_type} or None.
    """
    # 1. Session cookie (web UI)
    session_cookie = request.cookies.get("uc_session")
    if session_cookie:
        session = verify_session_token(session_cookie)
        if session:
            user = _auth.find_user_by_email(session["email"])
            if user and user.get("api_key"):
                api_key = user["api_key"]
                key_hash = _key_hash(api_key)
                agent_id = f"claude-{key_hash}"
                return {"user_id": session["user_id"], "key": api_key,
                        "agent_id": agent_id, "key_hash": key_hash,
                        "email": session["email"],
                        "status": user.get("status", "approved"),
                        "user_type": user.get("user_type", "claude")}

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
# Landing page
# ---------------------------------------------------------------------------

LANDING_HTML = r"""<!DOCTYPE html>
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
      <a href="/demo" class="card-btn">Launch Demo &#8594;</a>
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
        <div class="step"><span class="step-num">1</span>Provision or paste your OpenAI key</div>
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
    <a href="/demo">Demo</a>
    <a href="/trial">Free Tier</a>
    <a href="/setup">API Setup</a>
    <a href="https://github.com/protostatis/unchained-infra" target="_blank" rel="noopener noreferrer">Infra GitHub</a>
    <a href="mailto:__CONTACT_EMAIL__">Contact</a>
  </div>
  <div>UNCHAINED &mdash; YOUR BROWSER. YOUR DATA. NO WALLS.</div>
</div>

<script>
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
    <a href="/demo" class="cta-btn primary">Try the Demo &rarr;</a>
    <a href="/trial" class="cta-btn secondary">Connect Your Browser &rarr;</a>
  </div>
</div>

<div class="footer">
  <div class="footer-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/trial">Free Tier</a>
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
       data-theme="filled_black"
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
  flex:1;min-height:44px;max-height:120px;padding:10px 12px;
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
       data-theme="filled_black"
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
        <span class="status" id="agentstatus">chat agent offline</span>
        <span class="status" id="bridgestatus">browser bridge offline</span>
      </div>
    </div>
    <div class="nav">
      <a href="/">Home</a>
      <a href="/demo">Demo</a>
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a href="/test" id="control-link" style="display:none">Control</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doDisconnect();return false">Logout</a>
    </div>
  </div>

  <div id="model-notice" aria-live="polite" style="display:block"><strong>Free tier</strong> &mdash; using lightweight models. <a href="/setup">Upgrade to Claude, Gemini, or Codex</a> for 10x better results.</div>
  <div id="claude-request-banner">
    <span id="claude-request-text"></span>
    <button id="claude-request-btn" onclick="requestClaudeAccess()">Request Claude Access</button>
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
      <p id="install-modal-note" style="color:var(--muted);font-size:11px;margin-top:12px">Link expires in 15 minutes. Requires Python 3.9+ and curl.</p>
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
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential, source: 'trial'}),
    });
    const data = await r.json();
    _applyAuthState(data);
    if (data.pending) { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    agentId = data.agent_id;
    if (_redirectAfterLoginIfNeeded()) return;
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    _applyAuthState(data);
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
    _applyAuthState(data);
    if (data.authenticated) { agentId = data.agent_id; showMain(); return; }
    if (data.pending) { msg.textContent = 'Still under review. Check back soon!'; return; }
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

  if (chatConnected) updateStatusPill(el, 'chat agent online', 'online');
  else if (mismatch) updateStatusPill(el, 'chat agent mismatch', 'warn');
  else updateStatusPill(el, 'chat agent offline', '');

  if (bridgeConnected) updateStatusPill(bridgeEl, 'browser bridge online', 'online');
  else updateStatusPill(bridgeEl, 'browser bridge offline', '');

  if (bannerMsg) bannerMsg.textContent = 'Connect your browser to browse.';
  if (bannerCurl) bannerCurl.textContent = mismatch ? 'Reinstall (curl)' : 'Install (curl)';
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
  sessionId = _restoreSessionId() || ('s-' + agentId + '-' + Date.now().toString(36));
  _persistSessionId(sessionId);
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

async function showBannerInstall() {
  await showTrialInstallCmd();
}

async function showTrialInstallCmd() {
  document.getElementById('install-modal-title').textContent = 'Connect Your Browser';
  document.getElementById('install-modal-desc').textContent = 'Run this in your terminal to connect Chrome (Python 3 + curl required):';
  document.getElementById('install-modal-note').textContent = 'Link expires in 15 minutes. Only installs websockets \u2014 no API key needed.';
  const modal = document.getElementById('install-modal');
  modal.style.display = 'flex';
  document.getElementById('install-cmd').textContent = 'Generating link...';
  try {
    const r = await fetch('/trial/token', {method: 'POST'});
    if (!r.ok) { document.getElementById('install-cmd').textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    document.getElementById('install-cmd').textContent = data.curl_command;
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
  flex:1;min-height:44px;max-height:120px;padding:10px 12px;
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
       data-theme="filled_black"
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
        <span class="status" id="agentstatus">chat agent offline</span>
        <span class="status" id="bridgestatus">browser bridge offline</span>
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
      <p id="install-modal-note" style="color:var(--muted);font-size:11px;margin-top:12px">Links expire in 15 minutes. Requires Python 3.9+ and curl.</p>
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

function updateStatusPill(el, text, mode) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status' + (mode ? ' ' + mode : '');
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

  if (chatConnected) updateStatusPill(chatEl, 'chat agent online', 'online');
  else if (mismatch) updateStatusPill(chatEl, 'chat agent mismatch', 'warn');
  else updateStatusPill(chatEl, 'chat agent offline', '');

  if (bridgeConnected) updateStatusPill(bridgeEl, 'browser bridge online', 'online');
  else updateStatusPill(bridgeEl, 'browser bridge offline', '');

  if (bannerMsg) bannerMsg.textContent = 'Local chat agent is offline on this machine.';
  if (bannerCurl) bannerCurl.textContent = mismatch ? 'Reinstall (curl)' : 'Install (curl)';

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
  document.getElementById('install-modal-title').textContent = 'Install Agent (curl)';
  document.getElementById('install-modal-desc').textContent = 'Run this command in your terminal:';
  document.getElementById('install-modal-note').textContent = 'Links expire in 15 minutes. Requires Python 3.9+ and curl.';
  document.getElementById('copy-btn').textContent = 'Copy Command';
  const modal = document.getElementById('install-modal');
  modal.style.display = 'flex';
  document.getElementById('install-cmd').textContent = 'Generating install command...';
  try {
    const r = await fetch('/web/install-token', {method: 'POST'});
    if (!r.ok) { document.getElementById('install-cmd').textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    const command = _normalizeLocalUrl(data.curl_command || '');
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
    const r = await fetch('/web/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg,
        agent_id: agentId,
        session_id: sessionId,
        model: currentModel(),
      }),
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
)

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
  flex:1;min-height:44px;max-height:120px;padding:10px 12px;
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
.hint-sub{color:var(--muted);font-size:13px;margin-bottom:16px;max-width:380px;line-height:1.5}
.hint-examples{display:flex;flex-direction:column;gap:8px;width:100%;max-width:380px}
.hint-item{
  padding:10px 14px;border:1px solid #333;border-radius:10px;
  font-size:13px;color:var(--text);cursor:pointer;text-align:left;
  transition:border-color 0.15s;display:flex;align-items:center;gap:10px;
}
.hint-item:hover{border-color:var(--accent)}
.hint-emoji{font-size:18px;flex-shrink:0}
.hint-footer{
  margin-top:20px;font-size:11px;color:var(--muted);
  letter-spacing:0.5px;text-transform:uppercase;
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
    <h2>Demo limit reached</h2>
    <p class="quota-sub">You've used your free demo interactions. Connect your own browser for unlimited access &mdash; it's even better:</p>
    <div class="quota-grid">
      <div class="quota-item"><strong>Your logins</strong><span>Already signed into Gmail, GitHub? The agent uses them.</span></div>
      <div class="quota-item"><strong>Your cookies</strong><span>No CAPTCHAs &mdash; sites see you, not a bot.</span></div>
      <div class="quota-item"><strong>Your 2FA</strong><span>Works with authenticator apps and hardware keys.</span></div>
      <div class="quota-item"><strong>Your IP</strong><span>Residential connection &mdash; no datacenter flags.</span></div>
    </div>
    <a href="/trial" class="quota-cta">Set up your browser &rarr;</a>
    <button class="quota-dismiss" onclick="dismissQuota()">Stay on demo</button>
  </div>
</div>

<!-- Login -->
<div id="login">
  <h1>Unchained Demo</h1>
  <div class="sub">AI browser agent demo &mdash; watch it browse in real time</div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="filled_black"
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
      <a href="/trial">Free Tier</a>
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doDisconnect();return false">Logout</a>
    </div>
  </div>

  <div id="model-notice" style="display:none"><strong>Demo mode:</strong> Uses lightweight free models. Results may vary &mdash; <a href="/trial">try the free tier</a> for your own browser, or <a href="/setup">set up an API key</a>.</div>

  <div id="agent-bar">
    <span id="agent-action"></span>
    <span id="nav-trail"></span>
    <span id="turn-ctr"></span>
  </div>

  <div id="chat">
      <div id="chat-hints">
        <div class="hint-title">Try it &mdash; ask the agent anything</div>
      <div class="hint-sub">An AI agent will open a real browser, navigate pages, read content, and report back &mdash; all in real time. Pick a prompt below or type your own.</div>
      <div class="hint-examples">
        <div class="hint-item" onclick="fillMsg('Go to Wikipedia and look up the Eiffel Tower. Take a screenshot so I can see the page.')"><span class="hint-emoji">&#127758;</span> Look up the Eiffel Tower on Wikipedia</div>
        <div class="hint-item" onclick="fillMsg('Check the weather forecast on weather.gov for New York City. Screenshot the forecast.')"><span class="hint-emoji">&#9925;</span> Check the NYC weather on weather.gov</div>
        <div class="hint-item" onclick="fillMsg('Open Hacker News and list the top 5 stories right now. Take a screenshot of the page.')"><span class="hint-emoji">&#128240;</span> List the top 5 Hacker News stories</div>
        <div class="hint-item" onclick="fillMsg('Search for the best rated noise-cancelling headphones on rtings.com. Screenshot the results.')"><span class="hint-emoji">&#127911;</span> Find top headphones on rtings.com</div>
      </div>
      <div class="hint-footer">Free to try &mdash; no setup needed</div>
    </div>
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
let demoPromptCount = 0;
let demoUnlimited = false;
let _autoPromptFired = false;
let _userName = '';
let _userPicture = '';

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
    if (data.pending) { showPending(); return; }
    if (!r.ok) { errEl.textContent = data.error || 'Sign-in failed'; return; }
    agentId = data.agent_id;
    demoPromptCount = data.demo_prompt_count || 0;
    demoUnlimited = !!data.demo_unlimited;
    showMain();
  } catch(e) { errEl.textContent = e.message; }
}

async function checkSession() {
  try {
    const r = await fetch('/auth/me');
    const data = await r.json();
    if (data.authenticated) {
      agentId = data.agent_id;
      demoPromptCount = data.demo_prompt_count || 0;
      demoUnlimited = !!data.demo_unlimited;
      _userName = data.name || '';
      _userPicture = data.picture || '';
      showMain();
      return;
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
      agentId = data.agent_id;
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

function showQuotaModal() {
  document.getElementById('quota-modal').classList.add('visible');
}

function dismissQuota() {
  document.getElementById('quota-modal').classList.remove('visible');
  document.getElementById('msginput').disabled = true;
  document.getElementById('sendbtn').disabled = true;
}

function currentModel() {
  return _forcedDemoModel || 'google/gemini-3-flash-preview';
}

function _sessionStoreKey() {
  return 'unchained_session_' + agentId + '_demo';
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
let _forcedDemoModel = '';

function updateAgentStatusUI(connected) {
  const el = document.getElementById('agentstatus');
  el.textContent = 'headless agent';
  el.className = 'status online';
}

function showMain() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('pending').style.display = 'none';
  document.getElementById('main').style.display = 'flex';
  document.getElementById('agentlabel').textContent = _userName || 'Unchained';
  try { localStorage.setItem('unchained_last_route', '/demo'); } catch(e){}
  sessionId = _restoreSessionId() || ('s-' + agentId + '-' + Date.now().toString(36));
  _persistSessionId(sessionId);
  checkAgentStatus();
  setInterval(checkAgentStatus, 10000);
  loadHistory();
}

async function checkAgentStatus() {
  try {
    const r = await fetch('/web/chat/status');
    if (r.ok) {
      const data = await r.json();
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
      maybeAutoPrompt();
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
      const r = await fetch('/web/chat/status');
      if (r.ok) { const d = await r.json(); lastAgentConnected = d.connected; }
    } catch(e) {}
  }
  if (!lastAgentConnected) return;
  // Type the auto-prompt with animation
  const prompt = 'Go to Hacker News and find the top 3 trending stories right now. Take a screenshot so I can see the page.';
  const input = document.getElementById('msginput');
  hideHints();
  for (let i = 0; i < prompt.length; i++) {
    input.value = prompt.slice(0, i + 1);
    await new Promise(r => setTimeout(r, 25 + Math.random() * 20));
  }
  await new Promise(r => setTimeout(r, 400));
  doSend();
}

function showHintsIfEmpty() {
  if (document.getElementById('chat-hints')) return;
  document.getElementById('chat').innerHTML =
    '<div id="chat-hints">' +
    '<div class="hint-title">Try it &mdash; ask the agent anything</div>' +
    '<div class="hint-sub">An AI agent will open a real browser, navigate pages, read content, and report back &mdash; all in real time. Pick a prompt below or type your own.</div>' +
    '<div class="hint-examples">' +
    '<div class="hint-item" onclick="fillMsg(\'Go to Wikipedia and look up the Eiffel Tower. Take a screenshot so I can see the page.\')"><span class="hint-emoji">&#127758;</span> Look up the Eiffel Tower on Wikipedia</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Check the weather forecast on weather.gov for New York City. Screenshot the forecast.\')"><span class="hint-emoji">&#9925;</span> Check the NYC weather on weather.gov</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Open Hacker News and list the top 5 stories right now. Take a screenshot of the page.\')"><span class="hint-emoji">&#128240;</span> List the top 5 Hacker News stories</div>' +
    '<div class="hint-item" onclick="fillMsg(\'Search for the best rated noise-cancelling headphones on rtings.com. Screenshot the results.\')"><span class="hint-emoji">&#127911;</span> Find top headphones on rtings.com</div>' +
    '</div>' +
    '<div class="hint-footer">Free to try &mdash; no setup needed</div>' +
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
        model: currentModel(),
        headless: true,
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
          } else if (evt.type === 'tool_result') {
            if (currentTool) {
              setToolResult(currentTool, evt.data, evt.is_screenshot, evt.visible);
              currentTool = null;
            }
          } else if (evt.type === 'text') {
            appendText(bubble, evt.data);
          } else if (evt.type === 'model_forced') {
            if (evt.model) {
              _forcedDemoModel = evt.model;
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
#topbar .status{font-size:11px;color:var(--muted)}
#topbar .status.online{color:#4caf50}
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
  flex:1;min-height:44px;max-height:120px;padding:10px 12px;
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
  transition:border-color 0.15s;
}
.hint-item:hover{border-color:var(--accent)}
</style>
</head>
<body>

<!-- Login -->
<div id="login">
  <h1>Unchained</h1>
  <div class="sub">Full Claude-powered browser agent</div>
  <div style="margin:8px 0 4px;padding:6px 16px;border-radius:6px;background:#0d2d1a;border:1px solid #1a4a2a;color:#4ade80;font-size:13px;letter-spacing:0.5px">Signing up as: <strong>Claude</strong></div>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleGoogleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="filled_black"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
  <button id="dev-login-btn" onclick="devLogin()" style="display:none;width:320px;height:44px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer;margin-top:10px">Dev Login</button>
  <a href="/trial" style="color:#888;font-size:12px;margin-top:4px;text-decoration:none">Just want to try it free? Use the trial &rarr;</a>
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
        <span class="status" id="agentstatus">chat agent offline</span>
        <span class="status" id="bridgestatus">browser bridge offline</span>
      </div>
    </div>
    <div class="nav">
      <a href="/trial">Free Trial</a>
      <a href="#" onclick="doNewChat();return false">New Chat</a>
      <a href="/test" id="control-link" style="display:none">Control</a>
      <a href="/scheduler">Scheduler</a>
      <a href="#" onclick="doDisconnect();return false">Logout</a>
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
      <p id="install-modal-note" style="color:var(--muted);font-size:11px;margin-top:12px">Links expire in 15 minutes. Requires Python 3.9+ and curl.</p>
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

function onModelChange(model) {
  localStorage.setItem('unchained_model', model);
  // Refresh model-scoped status immediately when switching lanes.
  checkAgentStatus();
}

let lastAgentConnected = false;
let lastCodexCliSupported = true;

function updateStatusPill(el, text, mode) {
  if (!el) return;
  el.textContent = text;
  el.className = 'status' + (mode ? ' ' + mode : '');
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
  if (bannerMsg) bannerMsg.textContent = 'Your local chat agent is offline.';
  if (bannerDetail) bannerDetail.textContent = 'Browser bridge and chat agent are tracked separately.';
  if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
  if (bannerCurl) bannerCurl.textContent = 'Install (curl)';
  if (isCodexCli && bannerMsg) bannerMsg.textContent = 'Codex CLI lane requires the local chat agent and a Codex CLI login.';
  if (isCodexCli && !codexCliSupported && bannerMsg) {
    bannerMsg.textContent = 'Codex CLI requires an updated local chat agent package.';
  }

  if (bridgeConnected) {
    updateStatusPill(bridgeEl, 'browser bridge online', 'online');
  } else {
    updateStatusPill(bridgeEl, 'browser bridge offline', '');
  }

  if (chatConnected) {
    if (isCodexCli) updateStatusPill(chatEl, 'codex cli online', 'online');
    else updateStatusPill(chatEl, 'chat agent online', 'online');
    if (bridgeConnected) {
      if (banner) banner.style.display = 'none';
    } else {
      if (bannerMsg) bannerMsg.textContent = 'Your browser bridge is offline.';
      if (bannerDetail) bannerDetail.textContent = 'Chat is connected, but browser actions and setup profile detection on this machine need the bridge.';
      if (banner) banner.style.display = 'flex';
    }
  } else if (mismatch) {
    updateStatusPill(chatEl, 'chat agent mismatch', 'warn');
    if (bannerMsg) bannerMsg.textContent = 'A different local chat agent is connected for this account.';
    if (bannerDetail) bannerDetail.textContent = 'Your browser bridge may still be online. Reinstall only if this machine should own the active chat agent.';
    if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
    if (bannerCurl) bannerCurl.textContent = 'Reinstall (curl)';
    if (banner) banner.style.display = 'flex';
  } else {
    if (isCodexCli && !codexCliSupported) updateStatusPill(chatEl, 'codex cli needs update', 'warn');
    else if (isCodexCli) updateStatusPill(chatEl, 'codex cli offline', '');
    else updateStatusPill(chatEl, 'chat agent offline', '');
    if (bridgeConnected) {
      if (bannerDetail) bannerDetail.textContent = 'Browser bridge is online, so setup and browser control can still work on this machine.';
    } else if (bannerDetail) {
      bannerDetail.textContent = 'Start the full local agent package to bring both browser bridge and chat agent online.';
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
  sessionId = _restoreSessionId() || ('s-' + agentId + '-' + Date.now().toString(36));
  _persistSessionId(sessionId);
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
  await loadSlots();
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
    const r = await fetch('/web/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg,
        agent_id: agentId,
        session_id: sessionId,
        model: model,
      }),
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
    const slotbar2 = document.getElementById('slotbar');
    if (slotbar2) slotbar2.classList.remove('locked');
    document.getElementById('agent-bar').classList.remove('active');
    _turnCount = 0;
    _navTrail = [];
    renderNavTrail();
  }
}

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
  document.getElementById('install-modal-title').textContent = 'Install Agent (curl)';
  document.getElementById('install-modal-desc').textContent = 'Run this command in your terminal:';
  document.getElementById('install-modal-note').textContent = 'Links expire in 15 minutes. Requires Python 3.9+ and curl.';
  document.getElementById('copy-btn').textContent = 'Copy Command';
  const modal = document.getElementById('install-modal');
  modal.style.display = 'flex';
  document.getElementById('install-cmd').textContent = 'Generating install command...';
  try {
    const r = await fetch('/web/install-token', {method: 'POST'});
    if (!r.ok) { document.getElementById('install-cmd').textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    const command = _normalizeLocalUrl(data.curl_command || '');
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

async def handle_download_agent(request: web.Request) -> web.Response:
    """GET /web/download-agent — download agent ZIP package."""
    install_token = _request_install_token(request)
    if install_token:
        token_info = _auth.validate_install_token(install_token, consume=False)
        if not token_info:
            return web.json_response({"error": "Invalid or expired install token"}, status=401)
    else:
        auth_info = _authenticate(request)
        if not auth_info:
            return web.json_response({"error": "Not authenticated"}, status=401)
        install_token = _auth.create_install_token(auth_info["user_id"], auth_info["key"])

    from agent_package import build_agent_zip
    zip_bytes = build_agent_zip(
        api_key="",
        relay_host="api.unchainedsky.com",
        install_token=install_token,
    )
    return web.Response(
        body=zip_bytes,
        content_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=unchained-agent.zip"},
    )


async def handle_download_installer(request: web.Request) -> web.Response:
    """GET /web/download-installer — download native installer binary."""
    platform_raw = request.query.get("os", "mac")
    platform = _normalize_installer_platform(platform_raw)
    if not platform:
        return web.json_response({"error": "Unsupported os. Use mac or windows"}, status=400)

    install_token = _request_install_token(request)
    auth_info = None
    if install_token:
        token_info = _auth.validate_install_token(install_token, consume=False)
        if not token_info:
            return web.json_response({"error": "Invalid or expired install token"}, status=401)
    else:
        auth_info = _authenticate(request)
        if not auth_info:
            return web.json_response({"error": "Not authenticated"}, status=401)

    native_path = _native_installer_path(platform)
    if native_path:
        return web.FileResponse(
            path=native_path,
            headers={"Content-Disposition": f'attachment; filename="{native_path.name}"'},
        )

    if not _ALLOW_SCRIPT_INSTALLER_FALLBACK:
        expected_assets = _native_installer_candidates(platform)
        return web.json_response(
            {
                "error": "Native installer is not configured for this OS.",
                "os": platform,
                "expected_asset": expected_assets[0] if expected_assets else None,
                "expected_assets": expected_assets,
            },
            status=503,
        )

    # Optional compatibility fallback: return shell/PowerShell script installers
    # if native artifacts are not available and fallback is explicitly enabled.
    if not install_token:
        install_token = _auth.create_install_token(auth_info["user_id"], auth_info["key"])
    from agent_package import generate_platform_installer_script

    base_url = _public_base_url(request)
    script = generate_platform_installer_script(
        platform=platform,
        install_token=install_token,
        relay_host="api.unchainedsky.com",
        base_url=base_url,
    )
    filename = "unchained-installer-windows.ps1" if platform == "windows" else "unchained-installer-mac.sh"
    return web.Response(text=script, content_type="text/plain", headers={"Content-Disposition": f"attachment; filename={filename}"})


async def handle_install_token(request: web.Request) -> web.Response:
    """POST /web/install-token — create a short-lived install token for installers."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    token = _auth.create_install_token(auth_info["user_id"], auth_info["key"])
    base_url = _public_base_url(request)
    curl_command = f'curl -sSL -H "X-Install-Token: {token}" "{base_url}/install/script" | bash'
    powershell_command = (
        "powershell -ExecutionPolicy Bypass -Command "
        f"\"$h=@{{'X-Install-Token'='{token}'}}; "
        f"Invoke-Expression ((Invoke-WebRequest -UseBasicParsing -Headers $h "
        f"'{base_url}/install/windows/script').Content)\""
    )
    mac_native = _native_installer_path("mac") is not None
    windows_native = _native_installer_path("windows") is not None
    return web.json_response({
        "curl_command": curl_command,
        "powershell_command": powershell_command,
        "mac_installer_url": f"{base_url}/web/download-installer?os=mac",
        "windows_installer_url": f"{base_url}/web/download-installer?os=windows",
        "zip_url": f"{base_url}/web/download-agent",
        "native_available": {"mac": mac_native, "windows": windows_native},
        "expires_in": 900,
    })


async def handle_install_script(request: web.Request) -> web.Response:
    """GET /install/script or /install/{token} — serve personalized bash install script."""
    token = _request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = _auth.validate_install_token(token, consume=False)
    if not token_info:
        # Return a bash-friendly error message
        return web.Response(
            text='echo "ERROR: Install link expired or already used. '
                 'Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )

    from agent_package import _generate_install_script
    base_url = _public_base_url(request)
    script = _generate_install_script(
        install_token=token,
        relay_host="api.unchainedsky.com",
        base_url=base_url,
    )
    return web.Response(text=script, content_type="text/plain")


async def handle_install_script_windows(request: web.Request) -> web.Response:
    """GET /install/windows/script or /install/windows/{token} — serve PowerShell install script."""
    token = _request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = _auth.validate_install_token(token, consume=False)
    if not token_info:
        return web.Response(
            text='Write-Error "Install link expired or already used. Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )

    from agent_package import _generate_windows_install_script

    base_url = _public_base_url(request)
    script = _generate_windows_install_script(
        install_token=token,
        relay_host="api.unchainedsky.com",
        base_url=base_url,
    )
    return web.Response(text=script, content_type="text/plain")


async def handle_install_claim_page(request: web.Request) -> web.Response:
    """GET /install/claim/{claim_id} — approval page opened by native installer."""
    claim_id = str(request.match_info.get("claim_id", "")).strip().lower()
    if not _is_valid_claim_id(claim_id):
        return web.Response(text="Invalid install claim id.", status=400)
    html = INSTALL_CLAIM_HTML.replace("__CLAIM_ID__", claim_id)
    return web.Response(text=html, content_type="text/html")


async def handle_install_claim_start(request: web.Request) -> web.Response:
    """POST /web/install/claim/start — create a pending claim for native installer auth."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    claim_id = str(body.get("claim_id", "")).strip().lower()
    claim_secret = str(body.get("claim_secret", "")).strip()
    if not _is_valid_claim_id(claim_id):
        return web.json_response({"error": "claim_id must be 32 hex chars"}, status=400)
    if len(claim_secret) < 24:
        return web.json_response({"error": "claim_secret too short"}, status=400)

    now = time.time()
    source = _request_source_ip(request)
    with _install_claims_lock:
        _cleanup_install_claims(now)
        _cleanup_install_claim_start_hits(now)
        if len(_install_claims) >= _INSTALL_CLAIM_MAX_PENDING:
            return web.json_response({"error": "Too many pending install claims. Retry shortly."}, status=503)
        hits = _install_claim_start_hits.get(source, [])
        if len(hits) >= _INSTALL_CLAIM_START_MAX_PER_IP:
            return web.json_response({"error": "Too many claim attempts. Retry shortly."}, status=429)
        hits.append(now)
        _install_claim_start_hits[source] = hits
        existing = _install_claims.get(claim_id)
        if existing and not hmac.compare_digest(existing.get("secret", ""), claim_secret):
            return web.json_response({"error": "claim_id already exists"}, status=409)
        _install_claims[claim_id] = {
            "secret": claim_secret,
            "created_at": now,
            "expires_at": now + _INSTALL_CLAIM_TTL,
            "install_token": "",
        }
    return web.json_response({"status": "pending", "claim_id": claim_id, "expires_in": _INSTALL_CLAIM_TTL})


async def handle_install_claim_approve(request: web.Request) -> web.Response:
    """POST /web/install/claim/approve — approve a pending installer claim (auth required)."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    claim_id = str(body.get("claim_id", "")).strip().lower()
    if not _is_valid_claim_id(claim_id):
        return web.json_response({"error": "claim_id must be 32 hex chars"}, status=400)

    now = time.time()
    with _install_claims_lock:
        _cleanup_install_claims(now)
        claim = _install_claims.get(claim_id)
        if not claim:
            return web.json_response({"error": "Claim expired or not found"}, status=404)
        token = claim.get("install_token") or _auth.create_install_token(auth_info["user_id"], auth_info["key"])
        claim["install_token"] = token
        claim["approved_at"] = now
        claim["approved_user_id"] = auth_info["user_id"]
        claim["expires_at"] = min(claim.get("expires_at", now + _INSTALL_CLAIM_TTL), now + _INSTALL_CLAIM_TTL)
    return web.json_response({"status": "approved"})


async def handle_install_claim_poll(request: web.Request) -> web.Response:
    """POST /web/install/claim/poll — poll claim status and retrieve install token once approved."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    claim_id = str(body.get("claim_id", "")).strip().lower()
    claim_secret = str(body.get("claim_secret", "")).strip()
    if not _is_valid_claim_id(claim_id):
        return web.json_response({"error": "claim_id must be 32 hex chars"}, status=400)
    if not claim_secret:
        return web.json_response({"error": "claim_secret required"}, status=400)

    now = time.time()
    with _install_claims_lock:
        _cleanup_install_claims(now)
        claim = _install_claims.get(claim_id)
        if not claim:
            return web.json_response({"status": "expired"}, status=404)
        if not hmac.compare_digest(claim.get("secret", ""), claim_secret):
            return web.json_response({"error": "Invalid claim secret"}, status=401)
        install_token = str(claim.get("install_token", "")).strip()
        if install_token:
            _install_claims.pop(claim_id, None)
            return web.json_response({"status": "approved", "install_token": install_token})
        expires_at = float(claim.get("expires_at", now))
    return web.json_response({"status": "pending", "expires_in": max(0, int(expires_at - now))})


async def handle_install_bootstrap(request: web.Request) -> web.Response:
    """POST /web/install/bootstrap — exchange a short-lived install token for an API key."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    token = str(body.get("token", "")).strip()
    if not token:
        return web.json_response({"error": "token required"}, status=400)

    token_info = _auth.validate_install_token(token, consume=True)
    if not token_info:
        return web.json_response({"error": "Invalid or expired install token"}, status=401)

    return web.json_response({"api_key": token_info["api_key"]})


async def handle_trial_connector(request: web.Request) -> web.Response:
    """GET /trial/connector — serve chrome_bridge.py for trial users (no auth required)."""
    bridge_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_bridge.py")
    try:
        with open(bridge_path, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        return web.Response(text="# chrome_bridge.py not found\n", content_type="text/plain")
    return web.Response(body=content, content_type="text/plain")


async def handle_trial_token(request: web.Request) -> web.Response:
    """POST /trial/token — create a short-lived trial connector install token."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    token = _auth.create_install_token(auth_info["user_id"], auth_info["key"])
    base_url = _public_base_url(request)
    return web.json_response({
        "curl_command": f'curl -sSL -H "X-Install-Token: {token}" "{base_url}/trial/script" | bash',
    })


async def handle_trial_script(request: web.Request) -> web.Response:
    """GET /trial/script or /trial/{token} — serve minimal bash trial connector script."""
    token = _request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = _auth.validate_install_token(token, consume=False)
    if not token_info:
        return web.Response(
            text='echo "ERROR: Link expired or already used. Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )
    base_url = _public_base_url(request)
    relay_url = _public_relay_url(request)
    script = f"""#!/bin/bash
# Unchained Trial — Browser Connector
# Connects your Chrome to the Unchained AI agent
# Only requires: Python 3 and curl
set -e

INSTALL_TOKEN="{token}"
RELAY="{relay_url}"
DIR="$HOME/.unchained"
BRIDGE="$DIR/chrome_bridge.py"
BOOTSTRAP_URL="{base_url}/web/install/bootstrap"

echo ""
echo "  Unchained — Connecting your browser..."
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
  echo "  Error: Python 3 not found. Install from https://python.org"
  exit 1
fi

# Stop any existing connector
if [ -f "$DIR/.agent_pid" ]; then
  OLD_PID=$(cat "$DIR/.agent_pid" 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "  Stopping previous connector..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

# Install websockets (the only dependency)
if ! python3 -c "import websockets" 2>/dev/null; then
  echo "  Installing websockets..."
  python3 -m pip install -q websockets
fi

# Download the connector
mkdir -p "$DIR"
echo "  Downloading connector..."
curl -sSL "{base_url}/trial/connector" -o "$BRIDGE"

# Exchange the short-lived install token for the real API key.
PAYLOAD=$(TOKEN="$INSTALL_TOKEN" python3 - <<'PY'
import json, os
print(json.dumps({{"token": os.environ["TOKEN"]}}))
PY
)
BOOTSTRAP=$(curl -sf -H "Content-Type: application/json" -d "$PAYLOAD" "$BOOTSTRAP_URL") || {{
  echo "  Error: install token exchange failed"
  exit 1
}}
API_KEY=$(printf '%s' "$BOOTSTRAP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("api_key",""))' 2>/dev/null || true)
if [ -z "$API_KEY" ]; then
  echo "  Error: invalid install token response"
  exit 1
fi

# Launch Chrome + connector in background
echo "  Starting..."
UNCHAINED_API_KEY="$API_KEY" nohup python3 "$BRIDGE" start --relay "$RELAY" \\
  > "$DIR/connector.log" 2>&1 &
sleep 4

echo ""
echo "  Your browser is connected!"
echo "  An Unchained Chrome window will open — that's where the agent browses."
echo "  Screenshots of each page will appear in the chat so you can see what's happening."
echo ""
echo "  Open https://unchainedsky.com/chat, pick Trinity or StepFun 3.5 Flash, and start chatting."
echo ""
echo "  Stop:  python3 ~/.unchained/chrome_bridge.py stop"
echo "  Logs:  tail -f ~/.unchained/connector.log"
echo ""
"""
    return web.Response(text=script, content_type="text/plain")


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
a{color:#93d5ff}
#auth-panel{display:none}
#ready-panel{display:none}
@media (max-width:680px){
  h1{font-size:30px}
  .shell{padding-top:30px}
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
        <span class="pill" id="install-agentstatus">chat agent offline</span>
        <span class="pill" id="install-bridgestatus">browser bridge offline</span>
      </div>
      <div class="note" id="install-runtime-status">Checking local agent status...</div>

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

function updateInstallAgentStatusUI(data) {
  const chatConnected = !!data.chat_connected;
  const bridgeConnected = !!data.bridge_connected;
  const mismatch = !!data.mismatch;

  if (chatConnected) _setInstallPill('install-agentstatus', 'chat agent online', 'online');
  else if (mismatch) _setInstallPill('install-agentstatus', 'chat agent mismatch', 'warn');
  else _setInstallPill('install-agentstatus', 'chat agent offline', '');

  if (bridgeConnected) _setInstallPill('install-bridgestatus', 'browser bridge online', 'online');
  else _setInstallPill('install-bridgestatus', 'browser bridge offline', '');

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


async def handle_install_page(request: web.Request) -> web.Response:
    """Serve the one-click installer onboarding page."""
    return web.Response(text=INSTALL_ONBOARD_HTML, content_type="text/html")


async def handle_trial_page(request: web.Request) -> web.Response:
    """Serve the trial chat HTML page (OpenRouter models)."""
    html = inject_google_client_id(TRIAL_CHAT_HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_gemini_page(request: web.Request) -> web.Response:
    """Serve the Gemini SDK chat HTML page (per-user provisioned key)."""
    auth_info = _authenticate(request)
    if _is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = inject_google_client_id(CHAT_GEMINI_HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_codex_page(request: web.Request) -> web.Response:
    """Serve the Codex chat HTML page (per-user provisioned key)."""
    auth_info = _authenticate(request)
    if _is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = CHAT_CODEX_HTML.replace("__GOOGLE_CLIENT_ID__", GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_chat_claude_page(request: web.Request) -> web.Response:
    """Serve the Claude SDK chat HTML page (per-user provisioned key)."""
    auth_info = _authenticate(request)
    if _is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = CHAT_CLAUDE_SDK_HTML.replace("__GOOGLE_CLIENT_ID__", GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_case_study_zillow(request: web.Request) -> web.Response:
    """Serve the Zillow rental relisting case study page (public, no auth)."""
    del request
    html = CASE_STUDY_ZILLOW_HTML.replace("__CONTACT_EMAIL__", CONTACT_EMAIL)
    return web.Response(text=html, content_type="text/html")


async def handle_demo_page(request: web.Request) -> web.Response:
    """Serve the headless demo chat HTML page."""
    html = inject_google_client_id(HEADLESS_DEMO_HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_local_page(request: web.Request) -> web.Response:
    """Serve the local agent chat HTML page (Claude CLI + Codex CLI)."""
    auth_info = _authenticate(request)
    if _is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = inject_google_client_id(CLAUDE_CHAT_HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_claude_page(request: web.Request) -> web.Response:
    """Redirect /app to /local for backward compatibility."""
    raise web.HTTPFound("/local")


async def handle_chat_redirect(request: web.Request) -> web.Response:
    """Redirect /chat to /local for backward compatibility."""
    raise web.HTTPFound("/local")


async def handle_chat_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for the local chat agent.

    The chat agent connects here, authenticates, then waits for messages
    from the web server. Events from the agent are routed to the
    appropriate SSE response queue.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # First message must be auth
    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except (asyncio.TimeoutError, TypeError):
        await ws.close(code=4001, message=b"auth timeout")
        return ws

    key = auth_msg.get("key", "")
    is_trial_agent = bool(TRIAL_AGENT_KEY) and hmac.compare_digest(key, TRIAL_AGENT_KEY)
    # Trial-agent service key bypasses DB validation; all other keys must exist in DB.
    if not is_trial_agent:
        key_info = _auth.validate_key(key)
        if not key_info:
            await ws.send_json({"type": "error", "data": "invalid key"})
            await ws.close(code=4003, message=b"invalid key")
            return ws

    if is_trial_agent and TRIAL_AGENT_ID:
        agent_id = TRIAL_AGENT_ID
    elif auth_msg.get("agent_id"):
        agent_id = auth_msg["agent_id"]  # Trust agent-provided ID (key is validated)
    else:
        agent_id = f"claude-{_key_hash(key)}"
    caps = auth_msg.get("capabilities", {})
    if not isinstance(caps, dict):
        caps = {}
    await ws.send_json({"type": "auth_ok"})
    _chat_agents[agent_id] = ws
    _chat_agent_caps[agent_id] = caps
    if not is_trial_agent and key_info:
        _chat_agent_users[agent_id] = key_info.get("user_id", "")
    print(f"[chat] Agent {agent_id} connected")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                # Trial OpenRouter accounting events are internal-only.
                if msg_type == "openrouter_usage":
                    try:
                        usage_user_id = str(data.get("user_id", "")).strip()
                        usage_sid = str(data.get("session_id", "")).strip()
                        usage_model = str(data.get("model", "")).strip()
                        prompt_tokens = max(0, _coerce_int(data.get("prompt_tokens"), 0))
                        completion_tokens = max(0, _coerce_int(data.get("completion_tokens"), 0))
                        total_tokens = max(0, _coerce_int(data.get("total_tokens"), 0))
                        if total_tokens <= 0:
                            total_tokens = prompt_tokens + completion_tokens
                        direct_cost = _coerce_float(data.get("cost_usd"), 0.0)
                        estimated_cost = _coerce_float(data.get("estimated_cost_usd"), 0.0)
                        usage_cost = max(0.0, direct_cost if direct_cost > 0 else estimated_cost)
                        if usage_user_id and (usage_cost > 0 or total_tokens > 0):
                            budget_state = _track_openrouter_usage_for_user(
                                usage_user_id,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=usage_cost,
                            )
                            _trace(
                                "openrouter.usage",
                                user_id=usage_user_id,
                                session_id=usage_sid,
                                model=usage_model or "-",
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=f"{usage_cost:.9f}",
                                estimated_cost_usd=f"{estimated_cost:.9f}",
                                spend_usd=f"{_coerce_float(budget_state.get('spent_usd'), 0.0):.6f}",
                                budget_usd=f"{_coerce_float(budget_state.get('budget_usd'), 0.0):.6f}",
                                remaining_usd=f"{_coerce_float(budget_state.get('remaining_usd'), 0.0):.6f}",
                                usage_events=_coerce_int(budget_state.get("usage_events"), 0),
                                total_user_tokens=_coerce_int(budget_state.get("total_tokens"), 0),
                                capped=int(bool(budget_state.get("capped"))),
                            )
                    except Exception as e:
                        log.warning("[chat] failed to track OpenRouter usage: %s", e)
                    continue

                # Route agent request responses
                req_id = data.get("req_id", "")
                if req_id and msg_type in ("history_response", "new_chat_ok",
                                            "switch_slot_ok", "slots_response"):
                    rq = _agent_req_queues.get(req_id)
                    if rq:
                        await rq.put(data)
                    continue

                # Route SSE stream events by session_id
                sid = data.get("session_id", "")
                q = _response_queues.get(sid)
                if q:
                    await q.put(data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        if _chat_agents.get(agent_id) is ws:
            del _chat_agents[agent_id]
        _chat_agent_caps.pop(agent_id, None)
        _chat_agent_users.pop(agent_id, None)
        # Close all session tabs belonging to this agent
        agent_sessions = [
            sid for sid, aid in list(_session_agent_map.items())
            if aid == agent_id
        ]
        for sid in agent_sessions:
            asyncio.create_task(_close_session_tab(sid))
        print(f"[chat] Agent {agent_id} disconnected, cleaning {len(agent_sessions)} session tabs")

    return ws


async def handle_chat_msg(request: web.Request) -> web.StreamResponse:
    """POST /web/chat — phone sends message, gets SSE stream back.

    Bridges between the phone (SSE) and the local chat agent (WebSocket).
    """
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    body = await request.json()
    req_id = _request_id(request)
    message = body.get("message", "").strip()
    agent_id = auth_info.get("agent_id")  # Never trust client-supplied agent_id
    key_hash = auth_info["key_hash"]
    session_id = body.get("session_id", "")
    model = body.get("model", "")
    if _is_pending_user(auth_info) and not model:
        model = _OPENROUTER_TRIAL_DEFAULT_MODEL
    _trace(
        "chat.msg.in",
        req_id=req_id,
        user_id=auth_info.get("user_id", ""),
        agent_id=agent_id,
        session_id=session_id or "-",
        model=model or "-",
    )

    if not message:
        return web.json_response({"error": "message required"}, status=400)
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)

    # Resolve which chat agent handles this model (must happen before session ID generation)
    is_gemini = model and model.startswith("gemini")
    is_claude_sdk = _is_claude_sdk_model(model)
    is_codex_sdk = _is_codex_sdk_model(model)
    is_codex_cli = _is_codex_cli_model(model)
    is_openrouter = _is_openrouter_model(model)
    if _is_pending_user(auth_info) and not is_openrouter:
        return _pending_limited_response()
    openrouter_forced_model = ""
    openrouter_forced_from_model = ""
    openrouter_forced_notice = ""
    openrouter_budget_state: dict | None = None
    chat_agent_id = _resolve_chat_agent_id(auth_info, model)

    # Validate session belongs to this user.  Session IDs are
    # "s-{type}-{key_hash}-{rand}", so the key_hash is the 3rd segment.
    # Check structurally rather than via substring to avoid false matches.
    def _session_owned(sid: str) -> bool:
        parts = sid.split("-")
        # s-claude-{hash}-xxx  or  s-gemini-{hash}-xxx  →  parts[2] == key_hash
        return len(parts) >= 4 and parts[0] == "s" and parts[2] == key_hash

    if not session_id:
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"
    elif not _session_owned(session_id):
        # Do not allow clients to attach to another user's chat session namespace.
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"

    gemini_key = None
    if is_gemini:
        import signup_agent
        gemini_key = signup_agent.get_provider_key(auth_info["user_id"], "gemini")
        if not gemini_key:
            return web.json_response(
                {"error": "No Gemini API key. Visit /setup to provision one."},
                status=400,
            )
        # Lazy spawn if not running
        _spawn_gemini_agent(auth_info["user_id"], auth_info["key"], gemini_key)
        ws = _chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Gemini API agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_claude_sdk:
        import signup_agent
        user_id = auth_info["user_id"]
        claude_key = signup_agent.get_provider_key(user_id, "claude-sdk")
        if not claude_key:
            return web.json_response(
                {"error": "No Claude API key. Visit /setup to provision one."},
                status=400,
            )
        _spawn_claude_sdk_agent(user_id, auth_info["key"], claude_key)
        # Wait for the agent subprocess to connect its WebSocket (up to ~10s)
        ws = _chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            for _wait in range(20):
                await asyncio.sleep(0.5)
                ws = _chat_agents.get(chat_agent_id)
                if ws is not None and not ws.closed:
                    break
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Claude API agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_codex_sdk:
        import signup_agent

        user_id = auth_info["user_id"]
        primary = "codex-sdk"
        fallback = "codex-cli"
        codex_key = (
            signup_agent.get_provider_key(user_id, primary)
            or signup_agent.get_provider_key(user_id, fallback)
        )
        if not codex_key:
            return web.json_response(
                {"error": "No Codex key. Visit /setup to provision one."},
                status=400,
            )
        _spawn_codex_sdk_agent(user_id, auth_info["key"], codex_key)
        ws = _chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Codex agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_codex_cli:
        # Codex CLI lane runs on the user's local agent (no provider provisioning required).
        local_agent_id = auth_info["agent_id"]
        ws = _chat_agents.get(local_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Codex CLI requires your local agent connection. Open /app and connect your agent."},
                status=503,
            )
        caps = _chat_agent_caps.get(local_agent_id, {})
        if not bool(caps.get("codex_cli")):
            return web.json_response(
                {"error": "Your local agent does not support Codex CLI yet. Please update/restart your local agent package and try again."},
                status=426,
            )
    elif is_openrouter:
        # Route OpenRouter models (contain '/') to the trial agent on EC2
        if not TRIAL_AGENT_ID:
            return web.json_response(
                {"error": "Trial agent is not configured. Please try a Claude model."},
                status=503,
            )
        user_id = auth_info.get("user_id", "")
        requested_model = (model or _OPENROUTER_TRIAL_DEFAULT_MODEL).strip()
        model = requested_model
        if user_id:
            openrouter_budget_state = _openrouter_budget_state_for_user(user_id)
            if openrouter_budget_state.get("capped") and not _is_openrouter_post_cap_allowed_model(requested_model):
                model = _OPENROUTER_TRIAL_FALLBACK_MODEL
                openrouter_forced_model = model
                openrouter_forced_from_model = requested_model
                openrouter_forced_notice = (
                    "Trial model budget reached "
                    f"(${openrouter_budget_state.get('spent_usd', 0):.2f}/"
                    f"${openrouter_budget_state.get('budget_usd', 0):.2f}). "
                    "Switched to a free model for continued access. "
                    "After the $1 cap, available models are Trinity and StepFun."
                )
        ws = _chat_agents.get(TRIAL_AGENT_ID)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Trial agent is not available. Please try a Claude model."},
                status=503,
            )
    else:
        ws = _chat_agents.get(agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Your agent is not connected. Download and run the agent package."},
                status=503,
            )

    # Demo quota enforcement: headless demo requests are limited to _DEMO_PROMPT_LIMIT
    if body.get("headless", False):
        email = auth_info.get("email", "")
        if email:
            user = _auth.find_user_by_email(email)
            if not _is_demo_unlimited(user):
                count = _auth.get_demo_count(email)
                if count >= _DEMO_PROMPT_LIMIT:
                    return web.json_response(
                        {"error": "demo_quota_exceeded", "demo_prompt_count": count,
                         "demo_prompt_limit": _DEMO_PROMPT_LIMIT},
                        status=429,
                    )
                _auth.increment_demo_count(email)

    # Turn-based rate limiting for free-tier users
    email = auth_info.get("email", "")
    if email:
        user = _auth.find_user_by_email(email)
        if _is_rate_limited_user(user):
            result = _auth.check_and_consume_turn(
                email, _FREE_DAILY_TURN_LIMIT, _FREE_WINDOW_TURN_LIMIT, _FREE_WINDOW_SECONDS,
            )
            if not result["allowed"]:
                resp = {
                    "error": "turn_rate_limit",
                    "daily_remaining": result["daily_remaining"],
                    "window_remaining": result["window_remaining"],
                    "resets_in": result.get("resets_in", 0),
                }
                return web.json_response(resp, status=429)

    # Create response queue for this session
    q: asyncio.Queue = asyncio.Queue()
    _response_queues[session_id] = q

    # Forward message to chat agent
    # Route CDP tools to the headless bridge only when the client explicitly
    # requests headless mode (demo page).  Trial users who connected their
    # own browser should use their own agent for CDP.
    use_headless = body.get("headless", False) and HEADLESS_AGENT_ID
    cdp_agent_id = HEADLESS_AGENT_ID if use_headless else agent_id
    try:
        ws_msg = {
            "type": "user_message",
            "session_id": session_id,
            "agent_id": cdp_agent_id,
            "message": message,
        }
        if model:
            ws_msg["model"] = model
        if is_openrouter and auth_info.get("user_id"):
            ws_msg["user_id"] = auth_info["user_id"]
        if is_gemini:
            _gemini_last_active[chat_agent_id] = time.time()
        elif is_claude_sdk:
            _claude_sdk_last_active[chat_agent_id] = time.time()
        elif is_codex_sdk:
            _codex_sdk_last_active[chat_agent_id] = time.time()
        elif is_codex_cli:
            _session_last_active[session_id] = time.time()
        # Per-session tab isolation for headless demo sessions.
        # Reuse existing tab, or create a new one so concurrent demo users
        # don't share a single Chrome tab.
        tab_id = _session_tabs.get(session_id)
        if not tab_id and use_headless:
            tab_id = await _ensure_session_tab(session_id, cdp_agent_id)
        if tab_id:
            ws_msg["tab_id"] = tab_id
        _session_last_active[session_id] = time.time()
        await ws.send_json(ws_msg)
        _trace(
            "chat.msg.forwarded",
            req_id=req_id,
            session_id=session_id,
            chat_agent_id=chat_agent_id,
            cdp_agent_id=cdp_agent_id,
        )
    except Exception:
        _trace(
            "chat.msg.forward_error",
            req_id=req_id,
            session_id=session_id,
            chat_agent_id=chat_agent_id,
        )
        _response_queues.pop(session_id, None)
        return web.json_response({"error": "Failed to reach chat agent"}, status=502)

    # Track which agent is handling this session (for cancel routing)
    routing_agent_id = TRIAL_AGENT_ID if is_openrouter else chat_agent_id
    _session_agents[session_id] = routing_agent_id

    # Stream SSE response
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    if openrouter_forced_model:
        forced_evt = {
            "type": "model_forced",
            "reason": "openrouter_budget_limit",
            "model": openrouter_forced_model,
            "allowed_models": list(_OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS),
        }
        if openrouter_forced_from_model:
            forced_evt["requested_model"] = openrouter_forced_from_model
        if openrouter_budget_state:
            forced_evt["budget"] = openrouter_budget_state
        await resp.write(f"data: {json.dumps(forced_evt)}\n\n".encode())
    if openrouter_forced_notice:
        await resp.write(f"data: {json.dumps({'type': 'text', 'data': openrouter_forced_notice})}\n\n".encode())

    stream_completed = False
    try:
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                # Send SSE keepalive comment to prevent connection timeout
                try:
                    await resp.write(b": keepalive\n\n")
                except (ConnectionResetError, Exception):
                    break
                continue

            sse = f"data: {json.dumps(evt)}\n\n"
            try:
                await resp.write(sse.encode())
            except (ConnectionResetError, Exception):
                break

            if evt.get("type") == "done" or evt.get("type") == "error":
                stream_completed = True
                break
    finally:
        _response_queues.pop(session_id, None)
        _session_agents.pop(session_id, None)
        if not stream_completed:
            asyncio.create_task(_close_session_tab(session_id))
        _trace(
            "chat.msg.stream_end",
            req_id=req_id,
            session_id=session_id,
            stream_completed=stream_completed,
        )

    return resp


async def handle_chat_cancel(request: web.Request) -> web.Response:
    """POST /web/chat/cancel — cancel an active chat session."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    body = await request.json()
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)

    agent_id = auth_info.get("agent_id", "")
    # Look up which agent is actually handling this session (may be trial agent)
    routing_agent_id = _session_agents.get(session_id, agent_id)
    ws = _chat_agents.get(routing_agent_id)
    if ws and not ws.closed:
        await ws.send_json({"type": "cancel", "session_id": session_id})
        return web.json_response({"ok": True})
    return web.json_response({"error": "Agent not connected"}, status=503)


async def handle_chat_status(request: web.Request) -> web.Response:
    """GET /web/chat/status — check if user's agent is connected."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    bridge_agent_id = auth_info.get("agent_id", "")
    agent_id = bridge_agent_id
    ws = _chat_agents.get(agent_id)
    chat_connected = ws is not None and not ws.closed
    connected = chat_connected
    chat_only = request.query.get("chat_only") == "1"
    bridge_connected = False
    if bridge_agent_id:
        bridge_connected = await _check_relay_agent(bridge_agent_id)
    # If not connected via chat WebSocket, check if agent is on the relay
    if not connected and agent_id and not chat_only:
        connected = bridge_connected

    model_hint = request.query.get("model", "")
    if _is_pending_user(auth_info) and model_hint and not _is_openrouter_model(model_hint):
        return _pending_limited_response()
    wants_gemini = request.query.get("gemini") == "1"
    wants_codex = (
        request.query.get("codex") == "1"
        or _is_codex_cli_model(model_hint)
        or _is_codex_sdk_model(model_hint)
    )
    wants_claude_sdk = _is_claude_sdk_model(model_hint) or request.query.get("claude_sdk") == "1"

    # Lazy-spawn Gemini agent if user has a provisioned key and ?gemini=1 hint
    gemini_connected = False
    if wants_gemini:
        gemini_id = f"gemini-{auth_info['key_hash']}"
        proc = _gemini_procs.get(gemini_id)
        if not proc or proc.poll() is not None:
            import signup_agent
            user_id = auth_info.get("user_id", "")
            gemini_key = signup_agent.get_provider_key(user_id, "gemini") if user_id else None
            if gemini_key:
                _spawn_gemini_agent(user_id, auth_info["key"], gemini_key)
        # Check if the Gemini agent's WS is connected
        gws = _chat_agents.get(gemini_id)
        gemini_connected = gws is not None and not gws.closed

    codex_connected = False
    codex_agent_id = ""
    codex_cli_supported = True
    if wants_codex:
        import signup_agent

        user_id = auth_info.get("user_id", "")
        key_hash = auth_info["key_hash"]

        sdk_key = signup_agent.get_provider_key(user_id, "codex-sdk") if user_id else None
        cli_key = signup_agent.get_provider_key(user_id, "codex-cli") if user_id else None
        prefer_cli = _is_codex_cli_model(model_hint)

        if prefer_cli:
            # Codex CLI runs on local CLI agent; no provider key required.
            codex_agent_id = auth_info.get("agent_id", "")
            cws = _chat_agents.get(codex_agent_id)
            codex_chat_connected = cws is not None and not cws.closed
            codex_connected = codex_chat_connected
            if not codex_connected and codex_agent_id and not chat_only:
                codex_connected = await _check_relay_agent(codex_agent_id)
            caps = _chat_agent_caps.get(codex_agent_id, {})
            codex_cli_supported = bool(caps.get("codex_cli"))
            if codex_connected and not codex_cli_supported:
                codex_connected = False
        else:
            # Server-side Codex lane is always codex-sdk process; allow codex-cli key as
            # backward-compatible credential fallback.
            codex_key = sdk_key or cli_key
            if codex_key:
                codex_agent_id = f"codexsdk-{key_hash}"
                proc = _codex_sdk_procs.get(codex_agent_id)
                if not proc or proc.poll() is not None:
                    _spawn_codex_sdk_agent(user_id, auth_info["key"], codex_key)

        if codex_agent_id and not prefer_cli:
            cws = _chat_agents.get(codex_agent_id)
            codex_connected = cws is not None and not cws.closed
        if prefer_cli:
            chat_connected = codex_chat_connected
            connected = codex_connected
            agent_id = codex_agent_id

    claude_sdk_connected = False
    claude_sdk_agent_id = ""
    if wants_claude_sdk:
        import signup_agent

        user_id = auth_info.get("user_id", "")
        key_hash = auth_info["key_hash"]
        claude_key = signup_agent.get_provider_key(user_id, "claude-sdk") if user_id else None
        if claude_key:
            claude_sdk_agent_id = f"claudesdk-{key_hash}"
            proc = _claude_sdk_procs.get(claude_sdk_agent_id)
            if not proc or proc.poll() is not None:
                _spawn_claude_sdk_agent(user_id, auth_info["key"], claude_key)
            cws = _chat_agents.get(claude_sdk_agent_id)
            claude_sdk_connected = cws is not None and not cws.closed

            # Make base status reflect the currently selected model lane.
            chat_connected = claude_sdk_connected
            connected = claude_sdk_connected
            agent_id = claude_sdk_agent_id

    # Detect agent ID mismatch: user's expected agent isn't connected but
    # another agent belonging to the same user IS connected (different profile/key).
    mismatch_agent = ""
    if not chat_connected and agent_id:
        user_id = auth_info.get("user_id", "")
        if user_id:
            for other_id, other_uid in _chat_agent_users.items():
                if other_uid == user_id and other_id != agent_id:
                    other_ws = _chat_agents.get(other_id)
                    if other_ws and not other_ws.closed:
                        mismatch_agent = other_id
                        break

    resp = {"connected": connected, "agent_id": agent_id}
    resp["chat_connected"] = chat_connected
    resp["chat_agent_id"] = agent_id
    resp["bridge_connected"] = bridge_connected
    resp["bridge_agent_id"] = bridge_agent_id
    if mismatch_agent:
        resp["mismatch"] = True
        resp["mismatch_agent_id"] = mismatch_agent
    if wants_gemini:
        resp["gemini_agent_id"] = f"gemini-{auth_info['key_hash']}"
        resp["gemini_connected"] = gemini_connected
    if wants_codex:
        resp["codex_agent_id"] = codex_agent_id
        resp["codex_connected"] = codex_connected
        if _is_codex_cli_model(model_hint):
            resp["codex_cli_supported"] = codex_cli_supported
    if wants_claude_sdk:
        resp["claude_sdk_agent_id"] = claude_sdk_agent_id
        resp["claude_sdk_connected"] = claude_sdk_connected
    return web.json_response(resp)


async def _check_relay_agent(agent_id: str) -> bool:
    """Quick check if an agent is connected to the relay via HTTP API."""
    relay_host, relay_port = _parse_relay()
    scheme = "https" if relay_port == 443 else "http"
    if relay_port in (443, 80):
        url = f"{scheme}://{relay_host}/api/agents"
    else:
        url = f"{scheme}://{relay_host}:{relay_port}/api/agents"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=3, headers=_relay_auth_headers())
            if resp.is_success:
                agents = resp.json()
                return any(a.get("agent_id") == agent_id for a in agents)
    except Exception:
        pass
    # Fallback: try WS connect
    import websockets
    try:
        async with websockets.connect(
            _relay_cdp_url(agent_id, "auto"),
            open_timeout=3,
            additional_headers=_relay_auth_headers() or None,
        ) as ws:
            await ws.close()
            return True
    except Exception:
        return False


def _resolve_chat_agent_id(auth_info: dict, model: str) -> str:
    """Return the chat agent_id for the given model + authenticated user."""
    h = auth_info["key_hash"]
    if model and model.startswith("gemini"):
        return f"gemini-{h}"
    if _is_claude_sdk_model(model):
        return f"claudesdk-{h}"
    if _is_codex_sdk_model(model):
        return f"codexsdk-{h}"
    if _is_codex_cli_model(model):
        # Codex CLI runs on the user's local CLI agent, same lane as Claude CLI.
        return auth_info["agent_id"]
    return auth_info["agent_id"]  # claude-{hash}


async def _agent_request(agent_id: str, msg: dict, timeout: float = 10) -> dict | None:
    """Send a request to the agent WS and wait for a response."""
    ws = _chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return None
    req_id = uuid.uuid4().hex[:8]
    msg["req_id"] = req_id
    q: asyncio.Queue = asyncio.Queue()
    _agent_req_queues[req_id] = q
    try:
        await ws.send_json(msg)
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        _agent_req_queues.pop(req_id, None)


async def handle_chat_history(request: web.Request) -> web.Response:
    """GET /web/chat/history — proxy to agent for local chat history.

    For trial users (local agent offline), reads the session file written by
    chat_agent_openrouter.py from the shared relay_data volume.
    """
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")
    model = request.query.get("model", "")
    if _is_pending_user(auth_info) and not model:
        model = _OPENROUTER_TRIAL_DEFAULT_MODEL
    if _is_pending_user(auth_info) and not _is_openrouter_model(model):
        return _pending_limited_response()
    requested_session_id = request.query.get("session_id", "")
    chat_agent_id = _resolve_chat_agent_id(auth_info, model)

    # OpenRouter trial mode: always read from trial session file keyed by active session_id.
    if _is_openrouter_model(model):
        session_id = _resolve_trial_session_id(agent_id, requested_session_id)
        msgs, found = _read_trial_history(session_id)
        payload = {"messages": msgs, "trial": True, "session_id": session_id}
        if not found:
            payload["offline"] = True
        return web.json_response(payload)

    # Claude/Gemini mode: ask the appropriate agent first.
    resp = await _agent_request(chat_agent_id, {"type": "get_history", "session_id": requested_session_id})
    if resp is not None:
        return web.json_response({"messages": resp.get("messages", [])})

    # Local agent offline — optional fallback to a trial session if one exists.
    session_id = _resolve_trial_session_id(agent_id, requested_session_id)
    msgs, found = _read_trial_history(session_id)
    if found:
        return web.json_response({"messages": msgs, "trial": True, "session_id": session_id})
    return web.json_response({"messages": [], "offline": True})


async def handle_chat_new(request: web.Request) -> web.Response:
    """POST /web/chat/new — proxy to agent to advance to next slot."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    agent_id = auth_info.get("agent_id", "")

    model = body.get("model", "")
    if _is_pending_user(auth_info) and not model:
        model = _OPENROUTER_TRIAL_DEFAULT_MODEL
    if _is_pending_user(auth_info) and not _is_openrouter_model(model):
        return _pending_limited_response()
    requested_session_id = body.get("session_id", "")
    chat_agent_id = _resolve_chat_agent_id(auth_info, model)
    if _is_openrouter_model(model):
        # "New chat" in trial mode means: clear current session file and mint a new session ID.
        old_session = _resolve_trial_session_id(agent_id, requested_session_id)
        _delete_trial_session(old_session)
        # Close the tab for the old session (if any)
        await _close_session_tab(old_session)
        new_session = f"s-{agent_id}-{int(time.time() * 1000):x}"
        return web.json_response({
            "ok": True,
            "active_slot": 1,
            "trial": True,
            "session_id": new_session,
        })

    resp = await _agent_request(chat_agent_id, {"type": "new_chat"})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    result = {"ok": True, "active_slot": resp.get("active_slot", 1)}
    if resp.get("session_id"):
        result["session_id"] = resp["session_id"]
    return web.json_response(result)


async def handle_chat_slots(request: web.Request) -> web.Response:
    """GET /web/chat/slots — get slot info from agent."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")

    model = request.query.get("model", "")
    if _is_pending_user(auth_info) and not model:
        model = _OPENROUTER_TRIAL_DEFAULT_MODEL
    if _is_pending_user(auth_info) and not _is_openrouter_model(model):
        return _pending_limited_response()
    requested_session_id = request.query.get("session_id", "")
    chat_agent_id = _resolve_chat_agent_id(auth_info, model)
    if _is_openrouter_model(model):
        session_id = _resolve_trial_session_id(agent_id, requested_session_id)
        msgs, _ = _read_trial_history(session_id)
        preview = ""
        for m in msgs:
            if m.get("role") == "user":
                preview = m.get("content", "")[:40]
                break
        return web.json_response({
            "active_slot": 1,
            "slots": [
                {"slot": 1, "empty": len(msgs) == 0, "preview": preview},
                {"slot": 2, "empty": True, "preview": ""},
                {"slot": 3, "empty": True, "preview": ""},
            ],
            "trial": True,
            "session_id": session_id,
        })

    resp = await _agent_request(chat_agent_id, {"type": "get_slots"})
    if resp is None:
        return web.json_response({"active_slot": 1, "slots": [
            {"slot": 1, "empty": True, "preview": ""},
            {"slot": 2, "empty": True, "preview": ""},
            {"slot": 3, "empty": True, "preview": ""},
        ], "offline": True})
    return web.json_response({
        "active_slot": resp.get("active_slot", 1),
        "slots": resp.get("slots", []),
    })


async def handle_chat_switch(request: web.Request) -> web.Response:
    """POST /web/chat/switch — switch active slot."""
    auth_info = _authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    slot = body.get("slot", 1)
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    if _is_pending_user(auth_info) and not model:
        model = _OPENROUTER_TRIAL_DEFAULT_MODEL
    if _is_pending_user(auth_info) and not _is_openrouter_model(model):
        return _pending_limited_response()
    chat_agent_id = _resolve_chat_agent_id(auth_info, model)
    if _is_openrouter_model(model):
        # Trial mode does not support multi-slot switching.
        return web.json_response({"ok": True, "active_slot": 1, "trial": True})
    resp = await _agent_request(chat_agent_id, {"type": "switch_slot", "slot": slot})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    return web.json_response({"ok": True, "active_slot": resp.get("active_slot", slot)})


# ---------------------------------------------------------------------------
# API handlers
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


async def handle_google_auth(request: web.Request) -> web.Response:
    """POST /auth/google — verify Google ID token, create session."""
    body = await request.json()
    id_token = body.get("credential", "")
    if not id_token:
        return web.json_response({"error": "Missing credential"}, status=400)

    payload = await verify_google_token(id_token)
    if payload is None:
        return web.json_response({"error": "Invalid Google token"}, status=401)

    email = payload.get("email", "").lower()
    name = payload.get("name", "")
    picture = payload.get("picture", "")
    source = body.get("source", "claude")
    user_type = "trial" if source == "trial" else "claude"

    existing = _auth.find_user_by_email(email)

    if existing:
        status = existing.get("status", "approved")

        if status == "approved":
            # Normal flow — approved user
            # Update last login + profile
            user = _auth.get_or_create_user(email, name, picture)
            api_key = user["api_key"]
            agent_id = f"claude-{_key_hash(api_key)}"
            session_token = create_session_token(user["user_id"], email)
            resp = web.json_response({
                "ok": True, "email": email, "name": name,
                "picture": picture, "agent_id": agent_id,
                "user_type": existing.get("user_type", "claude"),
                "status": "approved",
                "demo_prompt_count": _auth.get_demo_count(email),
                "demo_unlimited": _is_demo_unlimited(existing),
                "claude_access_requested": False,
                "is_admin": email.lower() in ADMIN_EMAILS,
            })
            resp.set_cookie(
                "uc_session", session_token,
                max_age=JWT_EXPIRY_HOURS * 3600,
                httponly=True, secure=True, samesite="Lax", path="/",
            )
            return resp

        if status == "pending":
            # Trial/demo users can access trial flows while account review is pending.
            if source == "trial":
                api_key = existing.get("api_key")
                if not api_key:
                    api_key = _auth.create_key(existing["user_id"])
                now = time.time()
                with _auth._conn() as conn:
                    conn.execute(
                        "UPDATE users SET api_key = COALESCE(api_key, ?), "
                        "last_login_at = ?, name = ?, picture = ? WHERE email = ?",
                        (
                            api_key,
                            now,
                            name or existing.get("name", ""),
                            picture or existing.get("picture", ""),
                            email,
                        ),
                    )
                refreshed = _auth.find_user_by_email(email) or existing
                pending_user_type = refreshed.get("user_type", "trial")
                agent_id = f"claude-{_key_hash(api_key)}"
                session_token = create_session_token(existing["user_id"], email)
                resp = web.json_response({
                    "ok": True, "email": email, "name": name,
                    "picture": picture, "agent_id": agent_id,
                    "user_type": pending_user_type,
                    "status": "pending",
                    "demo_prompt_count": _auth.get_demo_count(email),
                    "demo_unlimited": False,
                    "review_pending": True,
                    "claude_access_requested": pending_user_type == "claude",
                    "is_admin": email.lower() in ADMIN_EMAILS,
                })
                resp.set_cookie(
                    "uc_session", session_token,
                    max_age=JWT_EXPIRY_HOURS * 3600,
                    httponly=True, secure=True, samesite="Lax", path="/",
                )
                return resp

            pending_user_type = existing.get("user_type", "claude")
            return web.json_response({
                "pending": True,
                "status": "pending",
                "user_type": pending_user_type,
                "claude_access_requested": pending_user_type == "claude",
                "message": "Your sign-up request is still being reviewed. We'll notify you by email once approved.",
            })

        if status == "rejected":
            return web.json_response({"error": "Your sign-up request was not approved."}, status=403)

    # Trial/demo sign-ups remain pending for admin review but can use free-tier chat immediately.
    if source == "trial":
        user = _auth.create_pending_user(email, name, picture, user_type="trial")
        api_key = _auth.create_key(user["user_id"])
        with _auth._conn() as conn:
            conn.execute(
                "UPDATE users SET api_key = ?, user_type = 'trial' WHERE email = ?",
                (api_key, email),
            )
        agent_id = f"claude-{_key_hash(api_key)}"
        session_token = create_session_token(user["user_id"], email)
        resp = web.json_response({
            "ok": True, "email": email, "name": name,
            "picture": picture, "agent_id": agent_id,
            "user_type": "trial",
            "status": "pending",
            "demo_prompt_count": _auth.get_demo_count(email),
            "demo_unlimited": False,
            "review_pending": True,
            "claude_access_requested": False,
            "is_admin": email.lower() in ADMIN_EMAILS,
        })
        resp.set_cookie(
            "uc_session", session_token,
            max_age=JWT_EXPIRY_HOURS * 3600,
            httponly=True, secure=True, samesite="Lax", path="/",
        )

        send_email(
            email,
            "Unchained — Trial access enabled (account review pending)",
            f"<p>Hi {name or email},</p>"
            "<p>Your account review is still pending, but you can start using Trial/Demo now.</p>"
            "<p>We'll notify you once your full account is approved.</p>"
            "<p>— The Unchained Team</p>",
        )
        for admin in ADMIN_EMAILS:
            send_email(
                admin,
                f"New trial sign-up (pending review): {email}",
                f"<p>New trial/demo user: <b>{name}</b> ({email}).</p>"
                "<p>Status: <b>pending review</b> (trial/demo access enabled).</p>",
            )
        return resp

    # Non-trial sign-ups require manual admin approval before chat access.
    user = _auth.create_pending_user(email, name, picture, user_type=user_type)
    session_token = create_session_token(user["user_id"], email)

    # Email user: sign-up received
    send_email(
        email,
        "Unchained \u2014 Sign-up request received",
        f"<p>Hi {name or email},</p>"
        "<p>We received your request to join Unchained. "
        "We're reviewing it now and will get back to you shortly.</p>"
        "<p>\u2014 The Unchained Team</p>",
    )

    # Email admin(s): new sign-up
    for admin in ADMIN_EMAILS:
        send_email(
            admin,
            f"New Unchained sign-up: {email}",
            f"<p>New sign-up request from <b>{name}</b> ({email}).</p>"
            f"<p>Source: <b>{user_type}</b></p>"
            f"<p>Approve: <code>POST /admin/approve</code> with body "
            f'<code>{{"email": "{email}"}}</code></p>',
        )

    resp = web.json_response({
        "pending": True,
        "message": "Your sign-up request has been submitted. We'll review it and notify you by email.",
    })
    # Set session cookie so /auth/me can identify pending users
    resp.set_cookie(
        "uc_session", session_token,
        max_age=JWT_EXPIRY_HOURS * 3600,
        httponly=True, secure=True, samesite="Lax", path="/",
    )
    return resp


async def handle_request_claude_access(request: web.Request) -> web.Response:
    """POST /auth/request-claude-access — request full Claude access for pending account."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    email = str(auth_info.get("email", "")).strip().lower()
    if not email:
        return web.json_response({"error": "Missing account email"}, status=400)

    user = _auth.find_user_by_email(email)
    if not user:
        return web.json_response({"error": "User not found"}, status=404)

    status = user.get("status", "approved")
    user_type = user.get("user_type", "claude")
    if status == "approved":
        return web.json_response({
            "ok": True,
            "status": "approved",
            "user_type": user_type,
            "claude_access_requested": user_type == "claude",
            "already_approved": True,
        })
    if status == "rejected":
        return web.json_response(
            {"error": "Your sign-up request was not approved."},
            status=403,
        )

    already_requested = user_type == "claude"
    if not already_requested:
        with _auth._conn() as conn:
            conn.execute(
                "UPDATE users SET user_type = 'claude', last_login_at = ? "
                "WHERE email = ? AND status = 'pending'",
                (time.time(), email),
            )

        send_email(
            email,
            "Unchained — Claude access request received",
            f"<p>Hi {user.get('name') or email},</p>"
            "<p>We received your request for full Claude access.</p>"
            "<p>Your account is still pending review. You can continue using Trial while you wait.</p>"
            "<p>— The Unchained Team</p>",
        )
        for admin in ADMIN_EMAILS:
            send_email(
                admin,
                f"Claude access request (pending): {email}",
                f"<p>User requested full Claude access: <b>{user.get('name') or email}</b> ({email}).</p>"
                "<p>Status: <b>pending review</b>.</p>",
            )

    return web.json_response({
        "ok": True,
        "status": "pending",
        "user_type": "claude",
        "claude_access_requested": True,
        "already_requested": already_requested,
        "message": "Request submitted. You can keep using Trial while your Claude access request is reviewed.",
    })


async def handle_logout(request: web.Request) -> web.Response:
    """POST /auth/logout — clear session cookie."""
    resp = web.json_response({"ok": True})
    resp.del_cookie("uc_session", path="/")
    return resp


async def handle_dev_auth(request: web.Request) -> web.Response:
    """POST /auth/dev — local dev login (no Google). Only available when GOOGLE_CLIENT_ID is unset."""
    if GOOGLE_CLIENT_ID:
        return web.json_response({"error": "Dev auth disabled (Google OAuth configured)"}, status=403)

    body = await request.json()
    email = body.get("email", "dev@localhost").strip().lower()
    name = body.get("name", "Dev User")

    user = _auth.get_or_create_user(email, name, "")
    # Ensure user is approved (auto-approve for dev)
    with _auth._conn() as conn:
        conn.execute("UPDATE users SET status = 'approved' WHERE email = ?", (email,))

    user = _auth.find_user_by_email(email)
    token = create_session_token(user["user_id"], email)
    agent_id = f"claude-{_key_hash(user['api_key'])}"

    resp = web.json_response({"ok": True, "agent_id": agent_id, "email": email})
    resp.set_cookie(
        "uc_session",
        token,
        path="/",
        httponly=True,
        secure=_cookie_secure(request),
        samesite="Lax",
        max_age=JWT_EXPIRY_HOURS * 3600,
    )
    return resp


async def handle_auth_me(request: web.Request) -> web.Response:
    """GET /auth/me — return current user info if session is valid."""
    # First try normal auth (approved users with api_key)
    auth_info = _authenticate(request)
    if auth_info is not None:
        email = auth_info.get("email", "")
        user = _auth.find_user_by_email(email)
        status = user.get("status", auth_info.get("status", "approved")) if user else auth_info.get("status", "approved")
        user_type = user.get("user_type", auth_info.get("user_type", "claude")) if user else auth_info.get("user_type", "claude")
        openrouter_usage = {}
        if user and (user_type == "trial" or status == "pending"):
            openrouter_usage = _openrouter_budget_state_for_user(user["user_id"])
        return web.json_response({
            "authenticated": True,
            "email": email,
            "agent_id": auth_info.get("agent_id", ""),
            "user_type": user_type,
            "status": status,
            "pending": status == "pending",
            "review_pending": status == "pending",
            "claude_access_requested": status == "pending" and user_type == "claude",
            "demo_prompt_count": _auth.get_demo_count(email) if email else 0,
            "demo_unlimited": _is_demo_unlimited(user) if user else False,
            "openrouter_usage": openrouter_usage,
            "is_admin": email.lower() in ADMIN_EMAILS,
            "name": user.get("name", "") if user else "",
            "picture": user.get("picture", "") if user else "",
        })

    # Check if there's a valid session cookie for a pending user
    session_cookie = request.cookies.get("uc_session")
    if session_cookie:
        session = verify_session_token(session_cookie)
        if session:
            status = _auth.get_user_status(session["email"])
            user = _auth.find_user_by_email(session["email"])
            user_type = user.get("user_type", "claude") if user else "claude"
            if status == "pending":
                return web.json_response({
                    "authenticated": False,
                    "pending": True,
                    "status": "pending",
                    "user_type": user_type,
                    "claude_access_requested": user_type == "claude",
                })
            if status == "approved":
                # User was just approved — re-check (they now have an api_key)
                if user and user.get("api_key"):
                    api_key = user["api_key"]
                    agent_id = f"claude-{_key_hash(api_key)}"
                    return web.json_response({
                        "authenticated": True,
                        "email": session["email"],
                        "agent_id": agent_id,
                        "user_type": user.get("user_type", "claude"),
                        "status": "approved",
                        "pending": False,
                        "review_pending": False,
                        "claude_access_requested": False,
                        "is_admin": session["email"].lower() in ADMIN_EMAILS,
                        "name": user.get("name", ""),
                        "picture": user.get("picture", ""),
                    })

    return web.json_response({"authenticated": False}, status=401)


def _is_admin(request: web.Request) -> dict | None:
    """Authenticate and check if user is an admin. Returns auth_info or None."""
    auth_info = _authenticate(request)
    if not auth_info:
        return None
    email = auth_info.get("email", "")
    if email not in ADMIN_EMAILS:
        return None
    return auth_info


async def handle_admin_pending(request: web.Request) -> web.Response:
    """GET /admin/pending — list all pending sign-up requests."""
    if not _is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    pending = _auth.list_pending_users()
    return web.json_response({"pending": pending})


async def handle_admin_approve(request: web.Request) -> web.Response:
    """POST /admin/approve — approve a pending user."""
    if not _is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    body = await request.json()
    email = body.get("email", "").strip().lower()
    if not email:
        return web.json_response({"error": "email required"}, status=400)
    user = _auth.approve_user(email)
    if not user:
        return web.json_response({"error": f"User {email} not found"}, status=404)

    # Notify user of approval
    send_email(
        email,
        "Unchained \u2014 You're in!",
        f"<p>Hi {user.get('name') or email},</p>"
        "<p>Your account has been approved! "
        'Visit <a href="https://api.unchainedsky.com/chat">unchainedsky.com/chat</a> to get started.</p>'
        "<p>\u2014 The Unchained Team</p>",
    )
    return web.json_response({"ok": True, "user": user})


async def handle_admin_reject(request: web.Request) -> web.Response:
    """POST /admin/reject — reject a pending user."""
    if not _is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    body = await request.json()
    email = body.get("email", "").strip().lower()
    if not email:
        return web.json_response({"error": "email required"}, status=400)
    if _auth.reject_user(email):
        return web.json_response({"ok": True})
    return web.json_response({"error": f"User {email} not found"}, status=404)


async def handle_admin_users(request: web.Request) -> web.Response:
    """GET /admin/users — list all users with their status."""
    if not _is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    users = _auth.list_all_users()
    return web.json_response({"users": users})


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
         data-theme="filled_black"
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
        <span class="status" id="setup-agentstatus">chat agent offline</span>
        <span class="status" id="setup-bridgestatus">browser bridge offline</span>
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
      <p style="color:var(--muted);font-size:13px;margin-bottom:10px">Run this in your terminal:</p>
      <div class="install-cmd" id="setup-install-cmd">Loading...</div>
      <button class="copy-btn" onclick="copySetupCmd(this)">Copy</button>
      <p style="color:var(--muted);font-size:11px;margin-top:12px">Requires Python 3 and curl. Link expires in 15 minutes.</p>
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

  if (bridgeConnected) updateSetupStatusPill(bridgeEl, 'browser bridge online', 'online');
  else updateSetupStatusPill(bridgeEl, 'browser bridge offline', '');

  if (chatConnected) updateSetupStatusPill(chatEl, 'chat agent online', 'online');
  else if (mismatch) updateSetupStatusPill(chatEl, 'chat agent mismatch', 'warn');
  else updateSetupStatusPill(chatEl, 'chat agent offline', '');

  if (!banner) return;
  if (bannerMsg) bannerMsg.textContent = 'Your local chat agent is offline.';
  if (bannerDetail) bannerDetail.textContent = 'Start the full local agent package to enable chat and browser control.';
  if (bannerConnect) bannerConnect.textContent = 'Download Agent Installer';
  if (bannerCurl) bannerCurl.textContent = 'Install (curl)';

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
    if (bannerCurl) bannerCurl.textContent = 'Reinstall (curl)';
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
  cmdEl.textContent = 'Generating link...';
  try {
    const r = await fetch('/trial/token', {method: 'POST'});
    if (!r.ok) { cmdEl.textContent = 'Error: ' + (await r.json()).error; return; }
    const data = await r.json();
    cmdEl.textContent = data.curl_command;
  } catch(e) {
    cmdEl.textContent = 'Error: ' + e.message;
  }
}

async function showSetupInstallCmd() {
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
    cmdEl.textContent = _normalizeLocalUrl(data.curl_command || '') || 'No install command available.';
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
  const headless=document.getElementById('f-headless').checked;
  const keepSession=document.getElementById('f-session').checked;

  const job={id:name,prompt:prompt,schedule:schedule,enabled:true,timeout_seconds:timeout};
  if(model) job.model=model;
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
  try{if(await checkSession()) await loadJobs();}
  catch(e){toast('Session check failed: '+e.message,'err');}
})();
</script>
</body>
</html>"""


async def handle_setup_page(request: web.Request) -> web.Response:
    """GET /setup — serve the setup / provisioning UI."""
    auth_info = _authenticate(request)
    if _is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    html = inject_google_client_id(SETUP_HTML, GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_admin_page(request: web.Request) -> web.Response:
    """GET /admin — serve the admin UI."""
    # Allow the page to load; client-side will hit /admin/users which enforces auth
    return web.Response(text=ADMIN_HTML, content_type="text/html")


async def handle_scheduler_page(request: web.Request) -> web.Response:
    """GET /scheduler — authenticated scheduler editor UI."""
    auth_info = _authenticate(request)
    if auth_info is None:
        raise web.HTTPFound("/app")
    if _is_pending_user(auth_info):
        raise web.HTTPFound("/trial")
    return web.Response(text=SCHEDULER_HTML, content_type="text/html")


async def handle_scheduler_jobs(request: web.Request) -> web.Response:
    """GET/POST /web/scheduler/jobs — per-user scheduler config."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = auth_info["user_id"]

    import scheduled_tasks as st

    if request.method == "GET":
        payload = _scheduler_read_jobs_payload(user_id)
        try:
            jobs = st.parse_jobs_payload(payload)
            preview = _scheduler_preview_rows(user_id, jobs)
            return web.json_response({"jobs": st.jobs_to_payload(jobs)["jobs"], "preview": preview})
        except Exception:
            return web.json_response({"jobs": [], "preview": []})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    try:
        jobs = st.parse_jobs_payload(body)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    if len(jobs) > 200:
        return web.json_response({"error": "Too many jobs (max 200)"}, status=400)

    canonical = st.jobs_to_payload(jobs)
    _scheduler_write_jobs_payload(user_id, canonical)
    preview = _scheduler_preview_rows(user_id, jobs)
    return web.json_response({"ok": True, "jobs": canonical["jobs"], "preview": preview})


async def handle_scheduler_preview(request: web.Request) -> web.Response:
    """POST /web/scheduler/preview — preview next run times for job payload."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = auth_info["user_id"]

    import scheduled_tasks as st

    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    payload = body if isinstance(body.get("jobs"), list) else _scheduler_read_jobs_payload(user_id)
    try:
        jobs = st.parse_jobs_payload(payload)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    preview = _scheduler_preview_rows(user_id, jobs)
    return web.json_response({"preview": preview, "server_time": int(time.time())})


async def handle_scheduler_history(request: web.Request) -> web.Response:
    """GET /web/scheduler/history — recent persisted run records for one job."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = auth_info["user_id"]
    job_id = str(request.query.get("job_id", "") or "").strip()
    if not job_id:
        return web.json_response({"error": "job_id required"}, status=400)

    try:
        limit = int(request.query.get("limit", "20") or "20")
    except ValueError:
        return web.json_response({"error": "limit must be an integer"}, status=400)
    limit = max(1, min(limit, 50))

    import scheduled_tasks as st

    try:
        jobs = st.parse_jobs_payload(_scheduler_read_jobs_payload(user_id))
    except Exception:
        jobs = []
    if job_id not in {job.id for job in jobs}:
        return web.json_response({"error": "job not found"}, status=404)

    records = st.load_run_history(_scheduler_state_path(user_id), job_id, limit=limit)
    return web.json_response({"records": records, "job_id": job_id})


# ---------------------------------------------------------------------------
# AI Provider Provisioning endpoints
# ---------------------------------------------------------------------------

async def handle_provision_profiles(request: web.Request) -> web.Response:
    """GET /web/provision/profiles — list Chrome profiles with Google sign-in."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if _is_pending_user(auth_info):
        return _pending_limited_response()

    import signup_agent
    profiles = signup_agent.list_chrome_profiles()

    # If no local profiles found, try querying through the relay (user's bridge).
    if not profiles:
        agent_id = auth_info.get("agent_id", "")
        if agent_id:
            relay_host, relay_port = _parse_relay()
            profiles = await provision_helpers.fetch_relay_profiles(
                agent_id=agent_id,
                relay_host=relay_host,
                relay_port=relay_port,
                headers=_relay_auth_headers(),
            )

    return web.json_response({"profiles": profiles})

def _spawn_provider_agent(provider: str, user_id: str, unchained_key: str, provider_key: str) -> str | None:
    """Spawn provider-specific chat agent and return destination chat URL."""
    if provider == "gemini":
        _spawn_gemini_agent(user_id, unchained_key, provider_key)
        return "/chat-gemini"
    if provider == "claude-sdk":
        _spawn_claude_sdk_agent(user_id, unchained_key, provider_key)
        return "/chat-claude"
    if provider == "codex-sdk":
        _spawn_codex_sdk_agent(user_id, unchained_key, provider_key)
        return "/chat-codex"
    if provider == "codex-cli":
        # Codex CLI lane is local and does not need provider provisioning.
        return "/chat-codex?model=codex-cli:gpt-5.1-codex-mini"
    return None


def _terminate_provider_agent(provider: str, key_hash: str):
    """Terminate a running provider agent process after key revoke."""
    if provider == "gemini":
        agent_id = f"gemini-{key_hash}"
        proc = _gemini_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Gemini agent {agent_id}")
        fh = _gemini_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        _gemini_last_active.pop(agent_id, None)
        return

    if provider == "codex-sdk":
        agent_id = f"codexsdk-{key_hash}"
        proc = _codex_sdk_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Codex SDK agent {agent_id}")
        fh = _codex_sdk_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        _codex_sdk_last_active.pop(agent_id, None)
        return

    if provider == "claude-sdk":
        agent_id = f"claudesdk-{key_hash}"
        proc = _claude_sdk_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Claude SDK agent {agent_id}")
        fh = _claude_sdk_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        _claude_sdk_last_active.pop(agent_id, None)
        return

    if provider == "codex-cli":
        agent_id = f"codexcli-{key_hash}"
        proc = _codex_cli_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Codex CLI agent {agent_id}")
        fh = _codex_cli_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        _codex_cli_last_active.pop(agent_id, None)
async def handle_provision_start(request: web.Request) -> web.Response:
    """POST /web/provision/start — trigger API key provisioning for a provider."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if _is_pending_user(auth_info):
        return _pending_limited_response()

    # Per-user rate limit
    user_id = auth_info["user_id"]
    last = _provision_cooldowns.get(user_id, 0)
    if time.time() - last < _PROVISION_COOLDOWN_SECS:
        remaining = int(_PROVISION_COOLDOWN_SECS - (time.time() - last))
        return web.json_response(
            {"error": f"Please wait {remaining}s before starting another provision."},
            status=429,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider = body.get("provider", "").strip().lower()
    if not provider:
        return web.json_response({"error": "provider required"}, status=400)

    import signup_agent
    if provider not in signup_agent.list_providers():
        return web.json_response(
            {"error": f"Unknown provider: {provider}. Available: {signup_agent.list_providers()}"},
            status=400,
        )

    profile_path = body.get("profile_path")
    use_relay = body.get("use_relay", False)

    # Validate profile_path is under the known Chrome user data directory (local mode only;
    # in relay mode, the bridge validates the path on the user's machine).
    if profile_path and not use_relay:
        import signup_agent as _sa
        chrome_dir = _sa._chrome_user_data_dir()
        if not chrome_dir:
            return web.json_response({"error": "Chrome user data directory not found"}, status=400)
        if not provision_helpers.is_profile_path_within(profile_path, chrome_dir):
            return web.json_response({"error": "Invalid profile path"}, status=403)

    # Record cooldown timestamp
    _provision_cooldowns[user_id] = time.time()

    if use_relay:
        agent_id = auth_info.get("agent_id")
        if not agent_id:
            return web.json_response({"error": "No agent_id resolved for relay provisioning"}, status=400)
        relay_host, relay_port = _parse_relay()
        result = await signup_agent.provision_key(
            provider_name=provider,
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
            user_id=user_id,
            store_key=False,
            profile_path=profile_path or "",
        )
    else:
        result = await signup_agent.provision_key_local(
            provider_name=provider,
            user_id=user_id,
            profile_path=profile_path,
            store_key=False,
        )

    resp = {
        "status": result.status.value,
        "provider": result.provider,
        "message": result.message,
        "duration_ms": result.duration_ms,
        "has_key": result.api_key is not None,
    }

    # On fresh success, stash key for user confirmation instead of storing immediately
    if result.api_key and result.status == signup_agent.ProvisionStatus.SUCCESS:
        _pending_provision[user_id] = (provider, result.api_key, time.time())
        key = result.api_key
        resp["key_preview"] = key[:8] + "..." + key[-4:] if len(key) > 12 else key[:4] + "..."

    # Already-exists: key is already stored, spawn provider agent and link to chat
    if result.status == signup_agent.ProvisionStatus.ALREADY_EXISTS:
        existing_key = result.api_key or signup_agent.get_provider_key(user_id, provider)
        if existing_key:
            chat_url = _spawn_provider_agent(provider, user_id, auth_info["key"], existing_key)
            if chat_url:
                resp["chat_url"] = chat_url

    return web.json_response(resp)


async def handle_provision_status(request: web.Request) -> web.Response:
    """GET /web/provision/status — check which providers have keys."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if _is_pending_user(auth_info):
        return _pending_limited_response()

    import signup_agent

    user_id = auth_info["user_id"]
    providers = []
    # codex-cli is local-only and does not require/stash provider keys.
    # Keep backend compatibility for legacy keys, but hide it from provisioning state.
    visible_providers = [n for n in signup_agent.list_providers() if n != "codex-cli"]
    for name in visible_providers:
        entry = {"name": name, "provisioned": signup_agent.has_provider_key(user_id, name)}
        if entry["provisioned"]:
            key = signup_agent.get_provider_key(user_id, name)
            entry["key_preview"] = key[:10] + "..." if key else ""
        providers.append(entry)

    return web.json_response({"providers": providers})


async def handle_provision_confirm(request: web.Request) -> web.Response:
    """POST /web/provision/confirm — user confirms storing the provisioned key."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if _is_pending_user(auth_info):
        return _pending_limited_response()

    user_id = auth_info["user_id"]
    pending = _pending_provision.pop(user_id, None)
    if not pending:
        return web.json_response({"error": "No pending key to confirm (expired or already stored)"}, status=400)

    provider, api_key, ts = pending
    if time.time() - ts > _PENDING_PROVISION_TTL:
        return web.json_response({"error": "Pending key expired. Please provision again."}, status=400)

    import signup_agent
    signup_agent.store_provider_key(user_id, provider, api_key)
    log.info("[provision] User %s confirmed %s key storage", user_id, provider)

    resp = {"status": "success", "provider": provider, "message": f"{provider} key stored."}

    # Spawn provider agent after confirmed storage
    chat_url = _spawn_provider_agent(provider, user_id, auth_info["key"], api_key)
    if chat_url:
        resp["chat_url"] = chat_url

    return web.json_response(resp)


async def handle_provision_save_manual(request: web.Request) -> web.Response:
    """POST /web/provision/save-manual — store a manually pasted API key."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if _is_pending_user(auth_info):
        return _pending_limited_response()

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider = body.get("provider", "").strip().lower()
    api_key = body.get("api_key", "").strip()
    if not provider or not api_key:
        return web.json_response({"error": "provider and api_key required"}, status=400)
    key_error = provision_helpers.validate_manual_api_key(api_key)
    if key_error:
        return web.json_response({"error": key_error}, status=400)

    import signup_agent
    if provider not in signup_agent.list_providers():
        return web.json_response(
            {"error": f"Unknown provider: {provider}. Available: {signup_agent.list_providers()}"},
            status=400,
        )

    user_id = auth_info["user_id"]
    signup_agent.store_provider_key(user_id, provider, api_key)
    log.info("[provision] User %s manually saved %s key", user_id, provider)

    resp = {"status": "success", "provider": provider, "message": f"{provider} key saved."}
    chat_url = _spawn_provider_agent(provider, user_id, auth_info["key"], api_key)
    if chat_url:
        resp["chat_url"] = chat_url

    return web.json_response(resp)


async def handle_provision_revoke(request: web.Request) -> web.Response:
    """POST /web/provision/revoke — revoke a provisioned key."""
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if _is_pending_user(auth_info):
        return _pending_limited_response()

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider = body.get("provider", "").strip().lower()
    if not provider:
        return web.json_response({"error": "provider required"}, status=400)

    import signup_agent

    user_id = auth_info["user_id"]
    revoked = signup_agent.revoke_provider_key(user_id, provider)

    _terminate_provider_agent(provider, auth_info["key_hash"])

    return web.json_response({
        "revoked": revoked,
        "provider": provider,
    })


# ---------------------------------------------------------------------------
# /web/cmd — Direct CDP command dispatch
# ---------------------------------------------------------------------------

class _CmdInputError(ValueError):
    pass


async def _cmd_ddm(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    flags = body.get("flags", ["--llm-2pass", "--cols", "60"])
    result = await cloud_tools.run_ddm(agent_id, tab_id, flags, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_text(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    flags = ["--text"]
    find = body.get("find")
    if find:
        flags.extend(["--find", find])
    result = await cloud_tools.run_ddm(agent_id, tab_id, flags, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_navigate(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    url = body.get("url")
    if not url:
        raise _CmdInputError("url required")
    result = await cloud_tools.navigate(agent_id, tab_id, url, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_screenshot(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    _ = body
    result = await cloud_tools.screenshot(agent_id, tab_id, relay_host, relay_port)
    return {"type": "image", "data": result}


async def _cmd_js(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    expression = body.get("expression")
    if not expression:
        raise _CmdInputError("expression required")
    result = await cloud_tools.run_js(agent_id, tab_id, expression, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_click(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    x = body.get("x")
    y = body.get("y")
    if x is None or y is None:
        raise _CmdInputError("x and y required")
    result = await cloud_tools.click(agent_id, tab_id, int(x), int(y), relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_type(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    text = body.get("text")
    if not text:
        raise _CmdInputError("text required")
    result = await cloud_tools.type_text(agent_id, tab_id, text, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_intel(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    flags = body.get("flags", ["--probe"])
    result = await cloud_tools.run_intel(agent_id, tab_id, flags, relay_host, relay_port)
    return {"type": "text", "data": result}


_CMD_ACTIONS = {
    "ddm": _cmd_ddm,
    "text": _cmd_text,
    "navigate": _cmd_navigate,
    "screenshot": _cmd_screenshot,
    "js": _cmd_js,
    "click": _cmd_click,
    "type": _cmd_type,
    "intel": _cmd_intel,
}


async def handle_cmd(request: web.Request) -> web.Response:
    auth_info = _authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    body = await request.json()
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
        runner = _CMD_ACTIONS.get(action)
        if runner is None:
            _trace("cmd.unknown", req_id=req_id, action=action or "-", agent_id=agent_id)
            return web.json_response(
                {"error": f"Unknown action: {action}"}, status=400,
            )
        payload = await runner(body, agent_id, tab_id, relay_host, relay_port, cloud_tools)
        _trace("cmd.ok", req_id=req_id, action=action, agent_id=agent_id, tab_id=tab_id)
        return web.json_response(payload)

    except _CmdInputError as e:
        return web.json_response({"error": str(e)}, status=400)

    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ("connect", "refused", "timed out", "not connected",
                                   "no close frame", "ws_error", "1006")):
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

_ROUTES: list[tuple[str, str, object]] = [
    ("GET", "/favicon.svg", handle_favicon),
    ("GET", "/", handle_index),
    ("GET", "/test", handle_test),
    ("POST", "/auth/google", handle_google_auth),
    ("POST", "/auth/request-claude-access", handle_request_claude_access),
    ("POST", "/auth/logout", handle_logout),
    ("GET", "/auth/me", handle_auth_me),
    ("POST", "/web/cmd", handle_cmd),
    ("GET", "/setup", handle_setup_page),
    ("GET", "/scheduler", handle_scheduler_page),
    ("GET", "/web/scheduler/jobs", handle_scheduler_jobs),
    ("POST", "/web/scheduler/jobs", handle_scheduler_jobs),
    ("GET", "/web/scheduler/history", handle_scheduler_history),
    ("POST", "/web/scheduler/preview", handle_scheduler_preview),
    ("GET", "/admin", handle_admin_page),
    ("GET", "/admin/users", handle_admin_users),
    ("GET", "/admin/pending", handle_admin_pending),
    ("POST", "/admin/approve", handle_admin_approve),
    ("POST", "/admin/reject", handle_admin_reject),
    ("GET", "/chat", handle_chat_redirect),
    ("GET", "/trial", handle_trial_page),
    ("GET", "/chat-gemini", handle_chat_gemini_page),
    ("GET", "/chat-codex", handle_chat_codex_page),
    ("GET", "/chat-claude", handle_chat_claude_page),
    ("GET", "/demo", handle_demo_page),
    ("GET", "/case-study/zillow-rental", handle_case_study_zillow),
    ("GET", "/local", handle_local_page),
    ("GET", "/install", handle_install_page),
    ("GET", "/app", handle_claude_page),
    ("GET", "/chat/ws", handle_chat_ws),
    ("POST", "/web/chat", handle_chat_msg),
    ("POST", "/web/chat/cancel", handle_chat_cancel),
    ("GET", "/web/chat/status", handle_chat_status),
    ("GET", "/web/chat/history", handle_chat_history),
    ("POST", "/web/chat/new", handle_chat_new),
    ("GET", "/web/chat/slots", handle_chat_slots),
    ("POST", "/web/chat/switch", handle_chat_switch),
    ("GET", "/web/download-agent", handle_download_agent),
    ("GET", "/web/download-installer", handle_download_installer),
    ("POST", "/web/install-token", handle_install_token),
    ("POST", "/web/install/claim/start", handle_install_claim_start),
    ("POST", "/web/install/claim/poll", handle_install_claim_poll),
    ("POST", "/web/install/claim/approve", handle_install_claim_approve),
    ("POST", "/web/install/bootstrap", handle_install_bootstrap),
    ("GET", "/install/script", handle_install_script),
    ("GET", "/install/windows/script", handle_install_script_windows),
    ("GET", "/install/{token}", handle_install_script),
    ("GET", "/install/windows/{token}", handle_install_script_windows),
    ("GET", "/install/claim/{claim_id}", handle_install_claim_page),
    ("GET", "/trial/connector", handle_trial_connector),
    ("POST", "/trial/token", handle_trial_token),
    ("GET", "/trial/script", handle_trial_script),
    ("GET", "/trial/{token}", handle_trial_script),
    ("GET", "/web/agent/version", handle_agent_version),
    ("GET", "/web/agent/files", handle_agent_files),
    ("GET", "/web/provision/profiles", handle_provision_profiles),
    ("POST", "/web/provision/start", handle_provision_start),
    ("GET", "/web/provision/status", handle_provision_status),
    ("POST", "/web/provision/confirm", handle_provision_confirm),
    ("POST", "/web/provision/save-manual", handle_provision_save_manual),
    ("POST", "/web/provision/revoke", handle_provision_revoke),
]


def _register_routes(app: web.Application):
    for method, path, handler in _ROUTES:
        if method == "GET":
            app.router.add_get(path, handler)
        elif method == "POST":
            app.router.add_post(path, handler)
        else:
            raise ValueError(f"Unsupported route method: {method}")
    if not GOOGLE_CLIENT_ID:
        app.router.add_post("/auth/dev", handle_dev_auth)


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
