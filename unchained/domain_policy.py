"""Domain execution policy for challenge-prone browsing flows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlparse


MODE_SHARED_HEADLESS_OK = "shared_headless_ok"
MODE_LOCAL_STEALTH_PREFERRED = "local_stealth_preferred"
MODE_LOCAL_HEADFUL_REQUIRED = "local_headful_required"


@dataclass(frozen=True)
class DomainPolicy:
    host: str
    mode: str
    reason: str
    requires_visible_browser: bool

    def to_dict(self) -> dict:
        return asdict(self)


_HEADFUL_REQUIRED_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("zillow.com", "Zillow-class real-estate flows are challenge-prone and should start in a visible local browser."),
    ("redfin.com", "Redfin-style listing SPAs are challenge-prone and should start in a visible local browser."),
    ("apartments.com", "Apartment-search SPAs are challenge-prone and should start in a visible local browser."),
)

_STEALTH_PREFERRED_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ticketmaster.com", "Ticketing flows tend to fingerprint aggressively; prefer local stealth before shared headless."),
    ("stubhub.com", "Ticketing flows tend to fingerprint aggressively; prefer local stealth before shared headless."),
    ("linkedin.com", "Authenticated social/productivity SPAs behave better with a local persisted session."),
    ("instagram.com", "Authenticated social/productivity SPAs behave better with a local persisted session."),
)


def _normalize_host(value: str) -> str:
    host = (value or "").strip().lower()
    if not host:
        return ""
    if "://" in host:
        parsed = urlparse(host)
        host = (parsed.hostname or "").strip().lower()
    return host.lstrip(".")


def _suffix_match(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def execution_policy_for_host(host: str) -> DomainPolicy:
    normalized = _normalize_host(host)
    for suffix, reason in _HEADFUL_REQUIRED_SUFFIXES:
        if _suffix_match(normalized, suffix):
            return DomainPolicy(
                host=normalized,
                mode=MODE_LOCAL_HEADFUL_REQUIRED,
                reason=reason,
                requires_visible_browser=True,
            )
    for suffix, reason in _STEALTH_PREFERRED_SUFFIXES:
        if _suffix_match(normalized, suffix):
            return DomainPolicy(
                host=normalized,
                mode=MODE_LOCAL_STEALTH_PREFERRED,
                reason=reason,
                requires_visible_browser=False,
            )
    return DomainPolicy(
        host=normalized,
        mode=MODE_SHARED_HEADLESS_OK,
        reason="Default to shared headless unless the domain is explicitly challenge-prone.",
        requires_visible_browser=False,
    )


def execution_policy_for_url(url: str) -> DomainPolicy:
    parsed = urlparse((url or "").strip())
    return execution_policy_for_host(parsed.hostname or "")
