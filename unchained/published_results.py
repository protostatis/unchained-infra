"""Published results storage for shareable task output pages.

Each published result becomes a public page at /r/<slug> with SEO meta tags,
JSON-LD structured data, and OG tags. Bots index these pages and distribute
them across search, AI training, and social preview networks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from html import escape

log = logging.getLogger(__name__)


_DB_PATH = os.environ.get(
    "UNCHAINED_RESULTS_DB_PATH", "/data/published_results.db"
)
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS published_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            query TEXT NOT NULL,
            result_html TEXT NOT NULL,
            result_text TEXT NOT NULL,
            meta_json TEXT,
            created_at REAL NOT NULL,
            user_id TEXT,
            session_id TEXT,
            view_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS publish_blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT UNIQUE NOT NULL,
            reason TEXT,
            created_at REAL NOT NULL
        )"""
    )
    return conn


# Words stripped from slugs — filler, implementation detail, instructions
_SLUG_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "that", "this",
    "go", "can", "you", "please", "find", "me", "i", "want", "search",
    "look", "up", "tell", "show", "get", "give", "make", "do", "take",
    "use", "using", "check", "open", "navigate", "visit",
    # Implementation detail — don't leak where we got the data
    "wikipedia", "google", "flights", "zillow", "redfin", "amazon",
    "youtube", "hacker", "news", "reddit", "craigslist",
    "com", "www", "http", "https", "org", "net",
    # Instruction fragments
    "right", "now", "currently", "today", "tonight", "next", "month",
    "summarize", "compare", "list", "screenshot", "each",
}


def _slugify(text: str) -> str:
    """Generate a keyword-dense, SEO-friendly slug from query text."""
    text = text.lower().strip()
    # Remove punctuation, keep alphanumeric and spaces
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    # Split into words and filter stop words
    words = [w for w in text.split() if w not in _SLUG_STOP_WORDS and len(w) > 1]
    if not words:
        words = [w for w in text.split() if len(w) > 1][:3]
    # Keep first 6 meaningful words (short, keyword-dense)
    slug = "-".join(words[:6])
    # Cap at 60 chars
    if len(slug) > 60:
        slug = slug[:60].rsplit("-", 1)[0]
    return slug if slug else hashlib.md5(text.encode()).hexdigest()[:8]


def _query_hash(query: str) -> str:
    """Normalized hash of a query for deduplication."""
    normalized = re.sub(r"[^a-z0-9]", "", query.lower())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


# Minimum quality thresholds for publishing
_MIN_RESPONSE_CHARS = 200
_NAV_ONLY_PATTERN = re.compile(
    r"^(go to|navigate to|open|visit)\s+\S+\.?\s*$", re.IGNORECASE
)


def _passes_quality_gate(query: str, result_text: str) -> bool:
    """Check if content meets minimum quality for publishing."""
    # Reject navigation-only commands
    if _NAV_ONLY_PATTERN.match(query.strip()):
        return False
    # Reject short responses
    if len(result_text) < _MIN_RESPONSE_CHARS:
        return False
    return True


_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_PII_GUARD_MODEL = os.environ.get(
    "PII_GUARD_MODEL", "google/gemini-2.0-flash-001"
)

_PII_PROMPT = """You are a PII (personally identifiable information) classifier.
Your job is to determine if the following content contains private or
personally identifiable information that should NOT be published on a
public website.

Flag as UNSAFE if the content contains ANY of:
- Real phone numbers, email addresses, or mailing addresses
- Real full names of private individuals (public figures are OK)
- Social security numbers, credit card numbers, bank account numbers
- Order IDs, booking references, tracking numbers
- Medical, legal, or financial details tied to a real person
- Login credentials, API keys, tokens
- Private dashboard data (account balances, earnings, personal metrics)
- Real property addresses tied to a specific owner

Flag as SAFE if the content is:
- General research (Wikipedia, news, public data)
- Price comparisons from public websites
- Flight/hotel searches (no booking confirmations)
- Public business information
- Product reviews or comparisons
- Example/demo data that isn't tied to a real person

Respond with ONLY one word: SAFE or UNSAFE"""


def _pii_guard(query: str, result_text: str) -> bool:
    """Check content for PII via OpenRouter LLM call.

    Returns True if content is safe to publish, False if it contains PII.
    Defaults to False (block) on any error.

    This is a synchronous call. The publish handler should offload to an
    executor (asyncio.to_thread or loop.run_in_executor) to avoid blocking
    the event loop.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        log.warning("PII guard: no OPENROUTER_API_KEY, blocking publish")
        return False

    # Send full content (up to 8000 chars) — Gemini Flash handles large contexts
    content = f"USER QUERY:\n{query}\n\nASSISTANT RESPONSE:\n{result_text[:8000]}"
    messages = [
        {"role": "system", "content": _PII_PROMPT},
        {"role": "user", "content": content},
    ]
    try:
        import httpx
        resp = httpx.post(
            _OPENROUTER_URL,
            json={
                "model": _PII_GUARD_MODEL,
                "messages": messages,
                "max_tokens": 10,
                "temperature": 0.0,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://unchainedsky.com",
                "X-Title": "Unchained PII Guard",
            },
            timeout=15.0,
        )
        if not resp.is_success:
            log.warning("PII guard: API error %d, blocking", resp.status_code)
            return False
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            log.warning("PII guard: no choices in response, blocking")
            return False
        answer = choices[0].get("message", {}).get("content", "").strip().upper()
        if answer == "SAFE":
            return True
        log.info("PII guard: classified as %s, blocking", answer)
        return False
    except Exception as e:
        log.warning("PII guard: exception %s, blocking", e)
        return False


def _extract_visible_messages(session_data: dict) -> list[dict]:
    """Extract user/assistant messages from a session, stripping tool calls."""
    raw = session_data.get("messages", [])
    msgs = []
    for m in raw:
        role = m.get("role")
        if role == "user":
            content = m.get("content", "")
            if content:
                msgs.append({"role": "user", "content": content})
        elif role == "assistant":
            if m.get("tool_calls") and not m.get("content"):
                continue
            content = m.get("content") or ""
            if not content:
                continue
            # Strip leaked tool-call XML tags
            content = re.sub(
                r"(?is)<tool_call\b.*?</tool_call>", "", content
            )
            # Strip raw JSON tool-call payloads (nested braces possible)
            content = re.sub(
                r'\{[^{}]*"(?:name|function)"\s*:[^{}]*"arguments"\s*:[^}]*\}',
                "", content
            )
            # Also strip any remaining JSON-like tool blocks with nested content
            content = re.sub(
                r'\{"(?:name|function|type)"\s*:.*?\}(?:\s*\})*',
                "", content, flags=re.DOTALL
            )
            content = re.sub(r"\n{3,}", "\n\n", content).strip()
            if content:
                msgs.append({"role": "assistant", "content": content})
    return msgs


def _messages_to_html(messages: list[dict]) -> str:
    """Convert messages to chat bubble HTML."""
    parts = []
    for m in messages:
        role = m["role"]
        content = escape(m["content"])
        # Convert markdown tables to HTML tables
        content = _md_to_html(content)
        css_class = "user" if role == "user" else "asst"
        parts.append(f'<div class="bubble {css_class}">{content}</div>')
    return "\n".join(parts)


def _md_to_html(text: str) -> str:
    """Minimal markdown to HTML for display in result pages."""
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Headers
    text = re.sub(r"^### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    # Simple table conversion
    lines = text.split("\n")
    in_table = False
    out = []
    for line in lines:
        stripped = line.strip()
        if "|" in stripped and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                continue  # separator row
            if not in_table:
                out.append("<table>")
                tag = "th"
                in_table = True
            else:
                tag = "td"
            row = "".join(f"<{tag}>{c}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
        else:
            if in_table:
                out.append("</table>")
                in_table = False
            out.append(line)
    if in_table:
        out.append("</table>")
    text = "\n".join(out)
    # List items — wrap runs of list items in <ul>
    def _wrap_lists(t: str) -> str:
        lines = t.split("\n")
        result = []
        in_list = False
        for line in lines:
            stripped = line.strip()
            if re.match(r"^[-*] ", stripped):
                if not in_list:
                    result.append("<ul>")
                    in_list = True
                item = re.sub(r"^[-*] ", "", stripped)
                result.append(f"<li>{item}</li>")
            else:
                if in_list:
                    result.append("</ul>")
                    in_list = False
                result.append(line)
        if in_list:
            result.append("</ul>")
        return "\n".join(result)
    text = _wrap_lists(text)
    # Paragraphs for remaining text blocks
    text = re.sub(r"\n\n+", "</p><p>", text)
    if not text.startswith("<"):
        text = f"<p>{text}</p>"
    return text


def add_blacklist_term(term: str, reason: str = "") -> None:
    """Add a term to the publish blacklist."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO publish_blacklist (term, reason, created_at) VALUES (?, ?, ?)",
                (term.lower().strip(), reason, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def remove_blacklist_term(term: str) -> None:
    """Remove a term from the publish blacklist."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM publish_blacklist WHERE term = ?",
                (term.lower().strip(),),
            )
            conn.commit()
        finally:
            conn.close()


def list_blacklist_terms() -> list[dict]:
    """List all blacklisted terms."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT term, reason, created_at FROM publish_blacklist ORDER BY term"
            ).fetchall()
            return [{"term": r[0], "reason": r[1], "created_at": r[2]} for r in rows]
        finally:
            conn.close()


def _is_query_blacklisted(text: str) -> bool:
    """Return True if text contains any blacklisted term from the database."""
    with _lock:
        conn = _connect()
        try:
            terms = conn.execute(
                "SELECT term FROM publish_blacklist"
            ).fetchall()
        finally:
            conn.close()
    lower = text.lower()
    return any(t[0] in lower for t in terms)


def publish_result(
    session_data: dict,
    *,
    user_id: str = "",
    session_id: str = "",
) -> str | None:
    """Publish a completed task as a public result page. Returns the slug."""
    messages = _extract_visible_messages(session_data)
    if not messages:
        return None
    user_msgs = [m for m in messages if m["role"] == "user"]
    asst_msgs = [m for m in messages if m["role"] == "assistant"]
    if not user_msgs or not asst_msgs:
        return None

    query = user_msgs[0]["content"]
    result_text = asst_msgs[-1]["content"]
    # Quality gate — reject navigation-only or short responses
    if not _passes_quality_gate(query, result_text):
        log.info("Publish blocked by quality gate: %s", query[:80])
        return None
    # Check ALL visible messages against blacklist
    for m in messages:
        if _is_query_blacklisted(m["content"]):
            return None
    # Deduplication — skip if same query already published
    qhash = _query_hash(query)
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT slug FROM published_results WHERE meta_json LIKE ?",
                (f'%"qhash":"{qhash}"%',),
            ).fetchone()
        finally:
            conn.close()
    if existing:
        log.info("Publish skipped (duplicate): %s -> %s", query[:60], existing[0])
        return existing[0]  # return existing slug instead of creating duplicate
    # Combine ALL assistant text for PII check
    all_asst_text = "\n\n".join(m["content"] for m in asst_msgs)
    # LLM-based PII guard — blocks if content contains personal data
    if not _pii_guard(query, all_asst_text):
        log.info("Publish blocked by PII guard: %s", query[:80])
        return None
    result_html = _messages_to_html(messages)
    slug = _slugify(query)

    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO published_results
                   (slug, query, result_html, result_text, meta_json,
                    created_at, user_id, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug,
                    query,
                    result_html,
                    result_text,
                    json.dumps({"message_count": len(messages), "qhash": qhash}),
                    time.time(),
                    user_id,
                    session_id,
                ),
            )
            conn.commit()
            return slug
        except sqlite3.IntegrityError:
            # Slug collision — add more hash
            h = hashlib.md5(f"{slug}-{time.time()}".encode()).hexdigest()[:8]
            slug = f"{slug}-{h}"
            conn.execute(
                """INSERT INTO published_results
                   (slug, query, result_html, result_text, meta_json,
                    created_at, user_id, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug,
                    query,
                    result_html,
                    result_text,
                    json.dumps({"message_count": len(messages), "qhash": qhash}),
                    time.time(),
                    user_id,
                    session_id,
                ),
            )
            conn.commit()
            return slug
        finally:
            conn.close()


def get_result(slug: str) -> dict | None:
    """Load a published result by slug. Returns dict or None."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT slug, query, result_html, result_text,
                          meta_json, created_at, view_count
                   FROM published_results WHERE slug = ?""",
                (slug,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE published_results SET view_count = view_count + 1 WHERE slug = ?",
                (slug,),
            )
            conn.commit()
            return {
                "slug": row[0],
                "query": row[1],
                "result_html": row[2],
                "result_text": row[3],
                "meta": json.loads(row[4]) if row[4] else {},
                "created_at": row[5],
                "view_count": row[6],
            }
        finally:
            conn.close()


def list_results(limit: int = 50) -> list[dict]:
    """List recent published results for sitemap generation."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT slug, query, created_at, view_count
                   FROM published_results
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {
                    "slug": r[0],
                    "query": r[1],
                    "created_at": r[2],
                    "view_count": r[3],
                }
                for r in rows
            ]
        finally:
            conn.close()
