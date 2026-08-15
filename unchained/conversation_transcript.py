"""Safe projection and persistence rules for user-visible chat history."""

from __future__ import annotations

import re

from tool_payloads import contains_tool_call_wrapper, strip_tool_call_wrappers


# Version 2 separates durable display history from private provider context.
SESSION_SCHEMA_VERSION = 2


def has_supported_session_schema(payload: object) -> bool:
    """Return whether a payload can be safely interpreted by this runtime."""
    if not isinstance(payload, dict):
        return False
    version = payload.get("schema_version")
    return version is None or version == SESSION_SCHEMA_VERSION


def looks_like_internal_tool_payload(text: str) -> bool:
    """Return whether text is a raw model tool-call payload, not an answer."""
    raw = (text or "").strip()
    if not raw:
        return False
    if contains_tool_call_wrapper(raw):
        return True
    if re.search(
        r'^\s*\{\s*"?name"?\s*:\s*[^,\n]+,\s*"?arguments"?\s*:\s*\{',
        raw,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(r'(?s)```(?:json)?\s*\{.*"name"\s*:.*"arguments"\s*:.*\}\s*```', raw):
        return True
    return raw.count('"name"') >= 1 and raw.count('"arguments"') >= 1 and len(raw) > 120


def strip_internal_tool_payload(text: str) -> str:
    """Best-effort removal of accidental tool-call text from an assistant answer."""
    cleaned = strip_tool_call_wrappers(text or "")
    cleaned = re.sub(r"(?is)```(?:json)?\s*.*?```", " ", cleaned)
    cleaned = re.sub(
        r'(?is)\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
        " ",
        cleaned,
    )
    cleaned = re.sub(
        r'(?is)\{\s*name\s*:\s*[^,\n]+,\s*arguments\s*:\s*\{.*?\}\s*\}',
        " ",
        cleaned,
    )
    kept_lines: list[str] = []
    for line in cleaned.splitlines():
        value = line.strip()
        if not value:
            continue
        lower = value.lower()
        if contains_tool_call_wrapper(value):
            continue
        if value in {"{", "}", "[", "]", ",", "```", "```json"}:
            continue
        if (
            lower.startswith('"name"')
            or lower.startswith('"arguments"')
            or lower.startswith('"tool_call_id"')
            or lower.startswith("name:")
            or lower.startswith("arguments:")
            or lower.startswith("tool_call_id:")
        ):
            continue
        kept_lines.append(value)
    return re.sub(r"\s+", " ", " ".join(kept_lines)).strip()


def _visible_assistant_content(message: dict) -> str:
    """Return assistant text that was safe to display, otherwise an empty string."""
    # Assistant messages carrying tool calls are execution state, even if a
    # provider also attached planning text in ``content``.
    if message.get("tool_calls"):
        return ""
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return ""
    if looks_like_internal_tool_payload(content):
        content = strip_internal_tool_payload(content)
    if not content or looks_like_internal_tool_payload(content):
        return ""
    return content


def project_visible_messages(messages: object) -> list[dict[str, str]]:
    """Project arbitrary provider/session messages into a safe visible transcript."""
    if not isinstance(messages, list):
        return []
    transcript: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                transcript.append({"role": "user", "content": content})
        elif role == "assistant":
            content = _visible_assistant_content(message)
            if content:
                transcript.append({"role": "assistant", "content": content})
    return transcript


def visible_transcript_from_payload(payload: object) -> list[dict[str, str]]:
    """Read canonical transcript data without exposing private resume context.

    Unversioned records predate the transcript field, so they receive a strict
    legacy projection from ``messages``. Versioned records treat ``transcript``
    as authoritative, including an explicitly empty list. Unknown versions fail
    closed rather than guessing how to interpret private provider state.
    """
    if not isinstance(payload, dict):
        return []
    version = payload.get("schema_version")
    if version is None:
        return project_visible_messages(payload.get("messages"))
    if version != SESSION_SCHEMA_VERSION:
        return []
    transcript = payload.get("transcript")
    if not isinstance(transcript, list):
        return []
    return project_visible_messages(transcript)
