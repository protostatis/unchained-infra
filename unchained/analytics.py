"""Lightweight product analytics for auth/login funnel tracking."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from urllib.parse import urlparse


DEFAULT_RETENTION_DAYS = 90
MAX_EVENT_NAME_LEN = 64
MAX_ROUTE_LEN = 200
MAX_SOURCE_LEN = 64
MAX_USER_TYPE_LEN = 32
MAX_META_BYTES = 4096

LOGIN_ROUTES = (
    "/trial",
    "/local",
    "/setup",
    "/install",
    "/chat-gemini",
    "/chat-codex",
    "/chat-claude",
    "/demo",
    "/app",
)


def _trim_text(value: str, limit: int) -> str:
    out = (value or "").strip()
    if not out:
        return ""
    return out[:limit]


def _safe_event_name(raw: str) -> str:
    text = (raw or "").strip().lower().replace(" ", "_")
    if not text:
        return ""
    cleaned = []
    for ch in text:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in ("_", ".", "-"):
            cleaned.append(ch)
    name = "".join(cleaned)
    return name[:MAX_EVENT_NAME_LEN]


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


class AnalyticsStore:
    """SQLite-backed event storage + simple funnel summaries."""

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
        self.hash_salt = hash_salt or os.environ.get("ANALYTICS_HASH_SALT", "") or "unchained-analytics-v1"
        self._dedupe_lock = threading.Lock()
        self._dedupe_recent: dict[tuple[str, str, str], float] = {}
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics_events(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event_ts ON analytics_events(event, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_visitor_ts ON analytics_events(visitor_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_route_ts ON analytics_events(route, ts)")

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

    def track(
        self,
        event: str,
        *,
        request=None,
        route: str = "",
        user_id: str = "",
        user_type: str = "",
        source: str = "",
        status_code: int = 0,
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
        route_text = _trim_text(route, MAX_ROUTE_LEN) or _request_route(request)
        ref_path = _referrer_path(request)
        source_text = _trim_text(source, MAX_SOURCE_LEN)
        user_type_text = _trim_text(user_type, MAX_USER_TYPE_LEN)
        payload = meta if isinstance(meta, dict) else {}
        meta_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if len(meta_json.encode("utf-8")) > MAX_META_BYTES:
            meta_json = "{}"
        dedupe_key = (name, visitor_id, route_text)
        if self._dedupe_hit(dedupe_key, ts, float(dedupe_ttl_s or 0.0)):
            return False
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO analytics_events (ts, event, visitor_id, user_id, user_type, route, referrer_path, source, status_code, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    name,
                    visitor_id,
                    uid,
                    user_type_text,
                    route_text,
                    ref_path,
                    source_text,
                    int(status_code or 0),
                    meta_json,
                ),
            )
        return True

    def cleanup_old_events(self, keep_days: int | None = None) -> int:
        days = max(1, int(keep_days or self.retention_days))
        cutoff = time.time() - (86400.0 * float(days))
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM analytics_events WHERE ts < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def login_funnel(self, days: int = 7) -> dict:
        window_days = min(90, max(1, int(days or 7)))
        since_ts = time.time() - (86400.0 * float(window_days))
        events = (
            "login_gate_visible",
            "google_signin_click",
            "auth_google_attempt",
            "auth_google_success",
            "chat_message_send",
            "auth_google_fail",
            "signup_created",
        )
        event_placeholders = ",".join("?" for _ in events)
        route_placeholders = ",".join("?" for _ in LOGIN_ROUTES)
        query = (
            "SELECT visitor_id, event, route, ts, meta_json "
            "FROM analytics_events "
            f"WHERE ts >= ? AND ((event IN ({event_placeholders})) OR (event = 'page_view' AND route IN ({route_placeholders}))) "
            "ORDER BY ts ASC"
        )
        params = [since_ts, *events, *LOGIN_ROUTES]
        per_visitor: dict[str, dict[str, float]] = {}
        auth_fail_reasons: dict[str, int] = {}
        signup_by_source: dict[str, int] = {}
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        for visitor_id, event, route, ts, meta_json in rows:
            if not visitor_id:
                continue
            steps = per_visitor.setdefault(visitor_id, {})
            step = ""
            if event == "page_view" and route in LOGIN_ROUTES:
                step = "login_page_view"
            elif event == "login_gate_visible":
                step = "login_gate_visible"
            elif event == "google_signin_click":
                step = "google_signin_click"
            elif event == "auth_google_attempt":
                step = "auth_google_attempt"
            elif event == "auth_google_success":
                step = "auth_google_success"
            elif event == "chat_message_send":
                step = "chat_message_send"
            if step and step not in steps:
                steps[step] = float(ts or 0.0)

            if event == "auth_google_fail":
                reason = "unknown"
                try:
                    payload = json.loads(meta_json or "{}")
                    reason = str(payload.get("reason", "unknown"))[:48] or "unknown"
                except Exception:
                    pass
                auth_fail_reasons[reason] = auth_fail_reasons.get(reason, 0) + 1

            if event == "signup_created":
                src = "unknown"
                try:
                    payload = json.loads(meta_json or "{}")
                    src = str(payload.get("source", "unknown"))[:48] or "unknown"
                except Exception:
                    pass
                signup_by_source[src] = signup_by_source.get(src, 0) + 1

        step_order = [
            "login_page_view",
            "login_gate_visible",
            "google_signin_click",
            "auth_google_attempt",
            "auth_google_success",
            "chat_message_send",
        ]
        counts = {k: 0 for k in step_order}
        for steps in per_visitor.values():
            prev_ts = -1.0
            for key in step_order:
                ts = steps.get(key)
                if ts is None or ts < prev_ts:
                    break
                counts[key] += 1
                prev_ts = ts

        step_rows = []
        first = counts[step_order[0]]
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

        fail_total = sum(auth_fail_reasons.values())
        top_fail_reasons = [
            {"reason": reason, "count": count}
            for reason, count in sorted(auth_fail_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]
        ]
        signup_sources = [
            {"source": source, "count": count}
            for source, count in sorted(signup_by_source.items(), key=lambda kv: kv[1], reverse=True)
        ]
        return {
            "window_days": window_days,
            "since_ts": since_ts,
            "unique_visitors": len(per_visitor),
            "steps": step_rows,
            "auth_google_fail_total": fail_total,
            "auth_google_fail_reasons": top_fail_reasons,
            "signups_by_source": signup_sources,
        }
