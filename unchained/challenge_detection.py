"""Challenge detection heuristics for anti-bot and CAPTCHA pages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class ChallengeDetectionResult:
    blocked: bool
    provider: str
    confidence: float
    reason: str
    matched: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


_PATTERN_GROUPS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    (
        "cloudflare_turnstile",
        0.97,
        (
            r"just a moment",
            r"checking your browser",
            r"cf[-_\s]?challenge",
            r"turnstile",
            r"attention required",
        ),
    ),
    (
        "arkose_labs",
        0.98,
        (
            r"arkoselabs",
            r"funcaptcha",
            r"enforcement\.[a-z0-9.-]*arkoselabs",
        ),
    ),
    (
        "recaptcha",
        0.95,
        (
            r"g-recaptcha",
            r"google recaptcha",
            r"i[' ]?m not a robot",
            r"recaptcha/api2",
        ),
    ),
    (
        "press_hold",
        0.92,
        (
            r"press\s*&?\s*hold",
            r"hold to confirm",
            r"press and hold",
        ),
    ),
    (
        "generic_human_verification",
        0.76,
        (
            r"verify you are human",
            r"verify that you are human",
            r"unusual traffic",
            r"access to this page has been denied",
            r"captcha",
            r"automated requests",
            r"sorry, you have been blocked",
        ),
    ),
)


def _combined_text(*parts: str) -> str:
    return "\n".join(str(p or "") for p in parts).lower()


def detect_challenge(
    *,
    page_text: str = "",
    title: str = "",
    url: str = "",
    html: str = "",
) -> ChallengeDetectionResult:
    haystack = _combined_text(title, url, page_text, html)
    best_provider = "none"
    best_confidence = 0.0
    best_matches: tuple[str, ...] = ()

    for provider, confidence, patterns in _PATTERN_GROUPS:
        matches = tuple(pattern for pattern in patterns if re.search(pattern, haystack, re.I))
        if matches and confidence > best_confidence:
            best_provider = provider
            best_confidence = confidence
            best_matches = matches

    if not best_matches:
        return ChallengeDetectionResult(
            blocked=False,
            provider="none",
            confidence=0.0,
            reason="No challenge signatures detected.",
            matched=(),
        )

    return ChallengeDetectionResult(
        blocked=True,
        provider=best_provider,
        confidence=best_confidence,
        reason=f"Matched {best_provider} challenge signatures.",
        matched=best_matches,
    )
