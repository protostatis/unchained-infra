"""Lightweight analytics storage and funnel summaries for unchainedsky.com."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import threading
import time
from urllib.parse import urlparse


DEFAULT_RETENTION_DAYS = 90
MAX_EVENT_NAME_LEN = 64
MAX_EVENT_ID_LEN = 80
MAX_SESSION_ID_LEN = 80
MAX_PAGE_VIEW_ID_LEN = 80
MAX_ROUTE_LEN = 200
MAX_SOURCE_LEN = 64
MAX_USER_TYPE_LEN = 32
MAX_GATE_TYPE_LEN = 32
MAX_CTA_ID_LEN = 64
MAX_ERROR_CODE_LEN = 64
MAX_META_BYTES = 4096
SESSION_BUCKET_SECONDS = 30 * 60

LOGIN_ROUTES = (
    "/trial",
    "/local",
    "/setup",
    "/install",
    "/first-look",
    "/chat-gemini",
    "/chat-codex",
    "/chat-claude",
    "/demo",
    "/app",
)

AUTH_INLINE_GSI_ROUTES = (
    "/trial",
    "/local",
    "/setup",
    "/chat-gemini",
    "/chat-codex",
    "/chat-claude",
)

def _trim_text(value: str, limit: int) -> str:
    if value is None:
        return ""
    out = str(value).strip()
    if not out:
        return ""
    return out[:limit]


def _safe_event_name(raw: str) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_")
    if not text:
        return ""
    cleaned = []
    for ch in text:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in ("_", ".", "-"):
            cleaned.append(ch)
    return "".join(cleaned)[:MAX_EVENT_NAME_LEN]


def _source_ip(request) -> str:
    forwarded = (request.headers.get("X-Forwarded-For", "") if request else "").strip()
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip
    remote = ((getattr(request, "remote", "") or "") if request else "").strip()
    return remote or "unknown"


def _user_agent(request) -> str:
    ua = (request.headers.get("User-Agent", "") if request else "").strip().lower()
    return ua[:240]


def _request_route(request) -> str:
    path = (getattr(request, "path", "") or "").strip()
    return path[:MAX_ROUTE_LEN]


def _referrer_path(request) -> str:
    ref = (request.headers.get("Referer", "") if request else "").strip()
    if not ref:
        return ""
    try:
        parsed = urlparse(ref)
    except Exception:
        return ""
    return (parsed.path or "").strip()[:MAX_ROUTE_LEN]


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


class AnalyticsStore:
    """SQLite-backed event storage + funnel summaries."""

    _EVENT_COLUMNS: dict[str, str] = {
        "event_id": "TEXT",
        "session_id": "TEXT",
        "page_view_id": "TEXT",
        "route_intended": "TEXT",
        "route_effective": "TEXT",
        "gate_type": "TEXT",
        "cta_id": "TEXT",
        "error_code": "TEXT",
        "latency_ms": "INTEGER",
    }

    _SESSION_COLUMNS: dict[str, str] = {
        "session_id": "TEXT PRIMARY KEY",
        "visitor_id": "TEXT NOT NULL",
        "first_ts": "REAL NOT NULL",
        "last_ts": "REAL NOT NULL",
        "entry_route": "TEXT",
        "entry_referrer_path": "TEXT",
        "entry_source": "TEXT",
        "entry_gate_type": "TEXT",
        "last_route": "TEXT",
        "user_id": "TEXT",
        "user_type": "TEXT",
    }

    _FUNNEL_SPECS = {
        "auth_inline_gsi": {
            "steps": [
                "login_page_view",
                "login_gate_visible",
                "google_signin_click",
                "auth_google_attempt",
                "auth_google_success",
                "chat_message_send",
            ],
        },
        "install": {
            "steps": [
                "install_page_view",
                "install_signin_click",
                "install_token_issued",
                "installer_download_start",
                "install_bootstrap_success",
            ],
        },
        "provision": {
            "steps": [
                "setup_page_view",
                "provision_start",
                "provision_success",
                "provision_confirm",
                "chat_message_send",
            ],
        },
        "chat_activation": {
            "steps": [
                "local_or_chat_page_view",
                "auth_google_success",
                "chat_message_send",
            ],
        },
    }

    def __init__(
        self,
        db_path: str | None = None,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        hash_salt: str | None = None,
    ):
        if db_path is None:
            db_path = os.environ.get(
                "UNCHAINED_DB_PATH",
                os.path.expanduser("~/.unchained/auth.db"),
            )
        self.db_path = db_path
        self.retention_days = max(1, int(retention_days or DEFAULT_RETENTION_DAYS))
        self.hash_salt = hash_salt or os.environ.get("ANALYTICS_HASH_SALT", "") or "unchained-analytics-v2"
        self._dedupe_lock = threading.Lock()
        self._dedupe_recent: dict[tuple[str, str, str], float] = {}
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @contextmanager
    def _conn_ctx(self):
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, required: dict[str, str]):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, sql_type in required.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def _init_db(self):
        with self._conn_ctx() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event TEXT NOT NULL,
                    visitor_id TEXT NOT NULL,
                    user_id TEXT,
                    user_type TEXT,
                    route TEXT,
                    referrer_path TEXT,
                    source TEXT,
                    status_code INTEGER,
                    meta_json TEXT
                )
                """
            )
            self._ensure_columns(conn, "analytics_events", self._EVENT_COLUMNS)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_sessions (
                    session_id TEXT PRIMARY KEY,
                    visitor_id TEXT NOT NULL,
                    first_ts REAL NOT NULL,
                    last_ts REAL NOT NULL,
                    entry_route TEXT,
                    entry_referrer_path TEXT,
                    entry_source TEXT,
                    entry_gate_type TEXT,
                    last_route TEXT,
                    user_id TEXT,
                    user_type TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics_events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event_ts ON analytics_events(event, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_visitor_ts ON analytics_events(visitor_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_route_ts ON analytics_events(route, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_route_effective_ts ON analytics_events(route_effective, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_session_ts ON analytics_events(session_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_page_view_ts ON analytics_events(page_view_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_sessions_visitor_last ON analytics_sessions(visitor_id, last_ts)")

    def _visitor_id(self, request, user_id: str) -> str:
        if request is not None:
            # Keep a stable request fingerprint so pre-auth and post-auth steps
            # can be correlated in the same funnel.
            ip = _source_ip(request)
            ua = _user_agent(request)
            raw = f"{ip}|{ua}|{self.hash_salt}"
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            return f"anon:{digest[:20]}"
        if user_id:
            return f"user:{user_id}"
        return "anon:unknown"

    def _derived_session_id(self, visitor_id: str, ts: float) -> str:
        bucket = int(ts // SESSION_BUCKET_SECONDS)
        raw = f"{visitor_id}|{bucket}|{self.hash_salt}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"s-{digest[:20]}"

    def _derived_event_id(
        self,
        *,
        event: str,
        visitor_id: str,
        session_id: str,
        route_effective: str,
        ts: float,
    ) -> str:
        raw = f"{event}|{visitor_id}|{session_id}|{route_effective}|{ts:.6f}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"e-{digest[:20]}"

    def _derived_page_view_id(
        self,
        *,
        visitor_id: str,
        session_id: str,
        route_effective: str,
        ts: float,
    ) -> str:
        raw = f"{visitor_id}|{session_id}|{route_effective}|{ts:.6f}|page"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"pv-{digest[:20]}"

    def _dedupe_hit(self, key: tuple[str, str, str], now: float, ttl: float) -> bool:
        if ttl <= 0:
            return False
        with self._dedupe_lock:
            prev = self._dedupe_recent.get(key, 0.0)
            if now - prev < ttl:
                return True
            self._dedupe_recent[key] = now
            # Keep the in-memory dedupe map bounded.
            if len(self._dedupe_recent) > 50000:
                cutoff = now - max(ttl, 120.0)
                stale = [k for k, ts in self._dedupe_recent.items() if ts < cutoff]
                for k in stale[:10000]:
                    self._dedupe_recent.pop(k, None)
            return False

    def _upsert_session(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        visitor_id: str,
        ts: float,
        route_effective: str,
        referrer_path: str,
        source: str,
        gate_type: str,
        user_id: str,
        user_type: str,
    ):
        row = conn.execute(
            "SELECT first_ts, entry_route, entry_referrer_path, entry_source, entry_gate_type, user_id, user_type "
            "FROM analytics_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO analytics_sessions (session_id, visitor_id, first_ts, last_ts, entry_route, entry_referrer_path, "
                "entry_source, entry_gate_type, last_route, user_id, user_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    visitor_id,
                    ts,
                    ts,
                    route_effective,
                    referrer_path,
                    source,
                    gate_type,
                    route_effective,
                    user_id,
                    user_type,
                ),
            )
            return
        conn.execute(
            "UPDATE analytics_sessions SET last_ts = ?, last_route = ?, user_id = COALESCE(NULLIF(?, ''), user_id), "
            "user_type = COALESCE(NULLIF(?, ''), user_type) WHERE session_id = ?",
            (
                ts,
                route_effective,
                user_id,
                user_type,
                session_id,
            ),
        )

    def track(
        self,
        event: str,
        *,
        request=None,
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
        now: float | None = None,
    ) -> bool:
        name = _safe_event_name(event)
        if not name:
            return False
        ts = float(now if now is not None else time.time())
        uid = _trim_text(user_id, 80)
        visitor_id = self._visitor_id(request, uid)
        request_route = _request_route(request)
        route_text = _trim_text(route, MAX_ROUTE_LEN) or request_route
        intended = _trim_text(route_intended, MAX_ROUTE_LEN) or route_text
        effective = _trim_text(route_effective, MAX_ROUTE_LEN) or route_text
        ref_path = _referrer_path(request)
        source_text = _trim_text(source, MAX_SOURCE_LEN)
        user_type_text = _trim_text(user_type, MAX_USER_TYPE_LEN)
        gate_type_text = _trim_text(gate_type, MAX_GATE_TYPE_LEN)
        cta_id_text = _trim_text(cta_id, MAX_CTA_ID_LEN)
        error_code_text = _trim_text(error_code, MAX_ERROR_CODE_LEN)
        payload = meta if isinstance(meta, dict) else {}
        meta_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if len(meta_json.encode("utf-8")) > MAX_META_BYTES:
            meta_json = "{}"
            payload = {}

        session_text = _trim_text(session_id, MAX_SESSION_ID_LEN) or _trim_text(
            str(payload.get("session_id", "")), MAX_SESSION_ID_LEN
        )
        if not session_text:
            session_text = self._derived_session_id(visitor_id, ts)

        page_view_text = _trim_text(page_view_id, MAX_PAGE_VIEW_ID_LEN) or _trim_text(
            str(payload.get("page_view_id", "")), MAX_PAGE_VIEW_ID_LEN
        )
        if not page_view_text and name == "page_view":
            page_view_text = self._derived_page_view_id(
                visitor_id=visitor_id,
                session_id=session_text,
                route_effective=effective,
                ts=ts,
            )

        event_text = _trim_text(event_id, MAX_EVENT_ID_LEN) or _trim_text(
            str(payload.get("event_id", "")), MAX_EVENT_ID_LEN
        )
        if not event_text:
            event_text = self._derived_event_id(
                event=name,
                visitor_id=visitor_id,
                session_id=session_text,
                route_effective=effective,
                ts=ts,
            )

        latency = _parse_int(latency_ms if latency_ms else payload.get("latency_ms", 0))
        dedupe_surface = page_view_text or effective or intended
        dedupe_key = (name, visitor_id, dedupe_surface)
        if self._dedupe_hit(dedupe_key, ts, float(dedupe_ttl_s or 0.0)):
            return False
        with self._conn_ctx() as conn:
            conn.execute(
                "INSERT INTO analytics_events (ts, event, event_id, session_id, page_view_id, visitor_id, user_id, user_type, route, "
                "route_intended, route_effective, referrer_path, source, gate_type, cta_id, error_code, status_code, latency_ms, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    name,
                    event_text,
                    session_text,
                    page_view_text,
                    visitor_id,
                    uid,
                    user_type_text,
                    route_text,
                    intended,
                    effective,
                    ref_path,
                    source_text,
                    gate_type_text,
                    cta_id_text,
                    error_code_text,
                    int(status_code or 0),
                    latency,
                    meta_json,
                ),
            )
            self._upsert_session(
                conn,
                session_id=session_text,
                visitor_id=visitor_id,
                ts=ts,
                route_effective=effective,
                referrer_path=ref_path,
                source=source_text,
                gate_type=gate_type_text,
                user_id=uid,
                user_type=user_type_text,
            )
        return True

    def cleanup_old_events(self, keep_days: int | None = None) -> int:
        days = max(1, int(keep_days or self.retention_days))
        cutoff = time.time() - (86400.0 * float(days))
        with self._conn_ctx() as conn:
            cur = conn.execute("DELETE FROM analytics_events WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM analytics_sessions WHERE last_ts < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def _load_window_rows(self, since_ts: float) -> list[dict]:
        query = (
            "SELECT visitor_id, event, route, route_intended, route_effective, gate_type, cta_id, error_code, "
            "source, status_code, latency_ms, ts, meta_json "
            "FROM analytics_events WHERE ts >= ? ORDER BY ts ASC"
        )
        with self._conn_ctx() as conn:
            rows = conn.execute(query, (since_ts,)).fetchall()
        out: list[dict] = []
        for visitor_id, event, route, route_intended, route_effective, gate_type, cta_id, error_code, source, status_code, latency_ms, ts, meta_json in rows:
            payload: dict
            try:
                payload = json.loads(meta_json or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            out.append(
                {
                    "visitor_id": visitor_id or "",
                    "event": event or "",
                    "route": route or "",
                    "route_intended": route_intended or "",
                    "route_effective": route_effective or route or "",
                    "gate_type": gate_type or "",
                    "cta_id": cta_id or "",
                    "error_code": error_code or "",
                    "source": source or "",
                    "status_code": _parse_int(status_code, 0),
                    "latency_ms": _parse_int(latency_ms, 0),
                    "ts": float(ts or 0.0),
                    "meta": payload,
                }
            )
        return out

    def _step_for_event(self, funnel: str, row: dict) -> str:
        event = row["event"]
        route = row["route_effective"] or row["route"]
        meta = row["meta"]
        cta_id = row["cta_id"] or str(meta.get("cta_id", "")).strip()
        gate_type = row["gate_type"] or str(meta.get("gate_type", "")).strip()

        if funnel == "auth_inline_gsi":
            if event == "page_view" and route in LOGIN_ROUTES:
                return "login_page_view"
            if event == "gsi_iframe_loaded":
                if route in AUTH_INLINE_GSI_ROUTES or gate_type in {"inline_gsi", "gsi"}:
                    return "login_gate_visible"
            if event in {"login_gate_visible", "gate_shown"}:
                if gate_type in {"", "inline_gsi", "inline", "gsi"} or route in AUTH_INLINE_GSI_ROUTES:
                    return "login_gate_visible"
            if event in {"google_signin_click", "gsi_click"}:
                return "google_signin_click"
            if event == "auth_google_attempt":
                return "auth_google_attempt"
            if event == "auth_google_success":
                return "auth_google_success"
            if event == "chat_message_send":
                return "chat_message_send"
            return ""

        if funnel == "install":
            if event == "page_view" and route == "/install":
                return "install_page_view"
            if event == "cta_click" and cta_id in {"install_signin_link", "signin_link"}:
                return "install_signin_click"
            if event == "install_signin_click":
                return "install_signin_click"
            if event == "install_token_issued":
                return "install_token_issued"
            if event in {"installer_download_start", "agent_zip_download_start"}:
                return "installer_download_start"
            if event == "install_bootstrap_success":
                return "install_bootstrap_success"
            return ""

        if funnel == "provision":
            if event == "page_view" and route == "/setup":
                return "setup_page_view"
            if event == "provision_start":
                return "provision_start"
            if event == "provision_success":
                return "provision_success"
            if event in {"provision_confirm", "provision_manual_save"}:
                return "provision_confirm"
            if event == "chat_message_send":
                return "chat_message_send"
            return ""

        if funnel == "chat_activation":
            if event == "auth_google_success":
                return "auth_google_success"
            if event == "page_view" and route in ("/local", "/chat-claude", "/chat-codex", "/chat-gemini", "/trial"):
                return "local_or_chat_page_view"
            if event == "chat_message_send":
                return "chat_message_send"
            return ""

        return ""

    def _ordered_steps_summary(self, per_visitor: dict[str, dict[str, float]], step_order: list[str]) -> list[dict]:
        counts = {k: 0 for k in step_order}
        for visitor_steps in per_visitor.values():
            prev_ts = -1.0
            for key in step_order:
                ts = visitor_steps.get(key)
                if ts is None or ts < prev_ts:
                    break
                counts[key] += 1
                prev_ts = ts

        step_rows = []
        first = counts[step_order[0]] if step_order else 0
        prev = 0
        for i, key in enumerate(step_order):
            value = counts[key]
            from_prev = 1.0 if i == 0 else (float(value) / float(prev) if prev > 0 else 0.0)
            from_start = (float(value) / float(first)) if first > 0 else 0.0
            step_rows.append(
                {
                    "step": key,
                    "visitors": value,
                    "conversion_from_prev": round(from_prev, 4),
                    "conversion_from_start": round(from_start, 4),
                }
            )
            prev = value
        return step_rows

    def funnel_report(self, *, funnel: str = "auth_inline_gsi", days: int = 7) -> dict:
        spec = self._FUNNEL_SPECS.get(funnel, self._FUNNEL_SPECS["auth_inline_gsi"])
        window_days = min(90, max(1, int(days or 7)))
        since_ts = time.time() - (86400.0 * float(window_days))
        rows = self._load_window_rows(since_ts)

        per_visitor: dict[str, dict[str, float]] = {}
        auth_fail_reasons: dict[str, int] = {}
        signup_by_source: dict[str, int] = {}
        top_cta_clicks: dict[str, int] = {}
        gate_seen_by_route: dict[str, int] = {}
        redirects_by_target: dict[str, int] = {}
        total_latency = 0
        latency_count = 0
        for row in rows:
            visitor_id = row["visitor_id"]
            if not visitor_id:
                continue
            steps = per_visitor.setdefault(visitor_id, {})
            step = self._step_for_event(funnel, row)
            if step and step not in steps:
                steps[step] = row["ts"]

            meta = row["meta"]
            event = row["event"]
            route = row["route_effective"] or row["route"]
            cta_id = row["cta_id"] or str(meta.get("cta_id", "")).strip()
            if event == "cta_click" and cta_id:
                top_cta_clicks[cta_id] = top_cta_clicks.get(cta_id, 0) + 1

            if step == "login_gate_visible":
                route_key = route or "unknown"
                gate_seen_by_route[route_key] = gate_seen_by_route.get(route_key, 0) + 1

            if event == "route_redirect":
                target = row["route_effective"] or str(meta.get("to", "")).strip() or "unknown"
                redirects_by_target[target] = redirects_by_target.get(target, 0) + 1

            if event == "auth_google_fail":
                reason = row["error_code"] or str(meta.get("reason", "unknown"))[:48] or "unknown"
                auth_fail_reasons[reason] = auth_fail_reasons.get(reason, 0) + 1

            if event == "signup_created":
                src = str(meta.get("source", row["source"] or "unknown"))[:48] or "unknown"
                signup_by_source[src] = signup_by_source.get(src, 0) + 1

            if row["latency_ms"] > 0:
                total_latency += row["latency_ms"]
                latency_count += 1

        step_rows = self._ordered_steps_summary(per_visitor, spec["steps"])

        payload = {
            "funnel": funnel if funnel in self._FUNNEL_SPECS else "auth_inline_gsi",
            "available_funnels": sorted(self._FUNNEL_SPECS.keys()),
            "window_days": window_days,
            "since_ts": since_ts,
            "unique_visitors": len(per_visitor),
            "events_in_window": len(rows),
            "steps": step_rows,
            "avg_latency_ms": int(total_latency / latency_count) if latency_count else 0,
            "cta_clicks": [
                {"cta_id": cta_id, "count": count}
                for cta_id, count in sorted(top_cta_clicks.items(), key=lambda kv: kv[1], reverse=True)[:12]
            ],
            "gate_exposures_by_route": [
                {"route": route, "count": count}
                for route, count in sorted(gate_seen_by_route.items(), key=lambda kv: kv[1], reverse=True)[:12]
            ],
            "redirects_by_target": [
                {"target": target, "count": count}
                for target, count in sorted(redirects_by_target.items(), key=lambda kv: kv[1], reverse=True)[:12]
            ],
        }
        if payload["funnel"] == "auth_inline_gsi":
            fail_total = sum(auth_fail_reasons.values())
            payload["auth_google_fail_total"] = fail_total
            payload["auth_google_fail_reasons"] = [
                {"reason": reason, "count": count}
                for reason, count in sorted(auth_fail_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]
            ]
            payload["signups_by_source"] = [
                {"source": source, "count": count}
                for source, count in sorted(signup_by_source.items(), key=lambda kv: kv[1], reverse=True)
            ]
        return payload

    def login_funnel(self, days: int = 7) -> dict:
        """Backward-compatible helper for existing admin endpoint users."""
        return self.funnel_report(funnel="auth_inline_gsi", days=days)
