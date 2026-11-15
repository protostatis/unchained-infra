"""IndexNow submission for Bing and participating search engines.

Bing is the index ChatGPT search reads from, so a page Bing never crawls is
effectively invisible to AI answer engines even when Google eventually picks
it up. IndexNow gives us a *push* channel for new or changed public URLs
instead of waiting to be discovered by a crawl.

Configuration:

    INDEXNOW_KEY   Optional. 8-128 characters from [A-Za-z0-9-]. When unset,
                   submission is a no-op and the key-file route returns 404.

The key is public by design: IndexNow verifies ownership by fetching
``https://<host>/<key>.txt`` and checking that the body equals the key.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

HOST = "unchainedsky.com"
ENDPOINT = "https://api.indexnow.org/indexnow"

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,128}$")

_DEFAULT_TIMEOUT_SECONDS = 10.0


def indexnow_key() -> str:
    """Return the configured IndexNow key, or '' when unconfigured."""
    key = os.environ.get("INDEXNOW_KEY", "").strip()
    if not key:
        return ""
    if not _KEY_PATTERN.fullmatch(key):
        log.warning(
            "INDEXNOW_KEY is not 8-128 chars of [A-Za-z0-9-]; ignoring"
        )
        return ""
    return key


def key_file_path() -> str:
    """Public path IndexNow fetches to verify key ownership."""
    key = indexnow_key()
    return f"/{key}.txt" if key else ""


def key_file_body() -> str:
    """Body the key-file route must serve (the key itself)."""
    return indexnow_key()


def text_resource_path_to_url(path: str) -> str:
    """Absolute URL for a site-relative path, for submission payloads."""
    return f"https://{HOST}/{path.lstrip('/')}"


def _is_own_url(url: object) -> bool:
    """True only for an https URL whose host is exactly HOST."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc == HOST


def submit_urls(urls: list[str], *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Submit absolute URLs to IndexNow.

    Only URLs on our own host are accepted. The key proves ownership of one
    host, so submitting anyone else's URL would at best be rejected and at
    worst look like abuse on our behalf.

    Never raises: a failed submission must not break the request that
    triggered it. Returns a small result dict for logging and tests.
    """
    key = indexnow_key()
    if not key:
        return {"submitted": False, "reason": "unconfigured", "count": 0}

    cleaned = [u for u in urls if _is_own_url(u)]
    rejected = len([u for u in urls if isinstance(u, str)]) - len(cleaned)
    if rejected:
        log.warning("IndexNow: dropped %d URL(s) outside %s", rejected, HOST)
    if not cleaned:
        return {"submitted": False, "reason": "no-urls", "count": 0}

    payload = {
        "host": HOST,
        "key": key,
        "keyLocation": f"https://{HOST}/{key}.txt",
        "urlList": cleaned[:10_000],
    }
    try:
        import httpx

        response = httpx.post(ENDPOINT, json=payload, timeout=timeout)
    except Exception as exc:  # network, DNS, TLS, import — all non-fatal
        log.warning("IndexNow submission failed: %s", exc)
        return {"submitted": False, "reason": "error", "count": 0, "error": str(exc)}

    # 200 = accepted, 202 = accepted but key validation pending.
    ok = response.status_code in (200, 202)
    if not ok:
        log.warning(
            "IndexNow rejected submission: status=%s body=%s",
            response.status_code,
            response.text[:200],
        )
    return {
        "submitted": ok,
        "status": response.status_code,
        "count": len(cleaned),
    }


def submit_published_result(slug: str) -> dict:
    """Announce one newly approved /r/<slug> page."""
    return submit_urls([f"https://{HOST}/r/{slug}"])
