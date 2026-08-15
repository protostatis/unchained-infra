"""Bounded serialization policy for chat-agent WebSocket events."""

from __future__ import annotations

import json
import os
from typing import Any, Iterator


CHAT_WS_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_INLINE_SCREENSHOT_BASE64_BYTES = 8 * 1024 * 1024
MAX_AGENT_EVENT_BYTES = 12 * 1024 * 1024
# The in-memory reconnect journal has a much smaller per-event budget than
# the WebSocket transport. Local CLI final responses are chunked to this
# conservative target before they enter that journal.
CHAT_TURN_REPLAY_EVENT_BYTES = 12 * 1024
CHAT_TEXT_REPLAY_TARGET_BYTES = 8 * 1024
SCREENSHOT_OMITTED_MESSAGE = (
    "Screenshot preview omitted because the image exceeded the 8 MiB inline preview limit."
)
EVENT_OMITTED_MESSAGE = "Agent event omitted because it exceeded the transport limit."
MALFORMED_TEXT_EVENT_MESSAGE = (
    "The agent returned an unreadable response update. Please ask it to continue."
)


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _serialized_replay_text_event_size(
    text: str,
    *,
    session_id: str,
    req_id: str,
) -> int:
    """Return the journal's JSON size for one normal text event.

    ``web_state`` uses the default ``ensure_ascii=True`` behavior when it
    measures replay events. Mirror that policy so escaped Unicode, quotes, and
    control characters cannot make a seemingly small raw string overflow the
    replay cap.
    """
    event: dict[str, str] = {
        "type": "text",
        "data": text,
        "session_id": session_id,
    }
    if req_id:
        event["req_id"] = req_id
    return _utf8_size(json.dumps(event, separators=(",", ":"), default=str))


def iter_replay_safe_text_chunks(
    text: str,
    *,
    session_id: str,
    req_id: str = "",
    max_event_bytes: int = CHAT_TEXT_REPLAY_TARGET_BYTES,
) -> Iterator[str]:
    """Yield exact, codepoint-safe text chunks that fit the replay journal.

    This is intentionally narrower than the 12 MiB agent transport limit. The
    browser concatenates ordinary ``text`` events, while the 8 KiB target leaves
    room for journal-added sequence and timestamp metadata below its 12 KiB cap.
    Reconnect replay remains bounded by the journal's event-count retention;
    archived/history text is authoritative for exceptionally large responses.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_event_bytes <= 0:
        raise ValueError("max_event_bytes must be positive")
    if _serialized_replay_text_event_size(
        "", session_id=session_id, req_id=req_id
    ) > max_event_bytes:
        raise ValueError("max_event_bytes is too small for event metadata")
    if not text:
        yield ""
        return

    start = 0
    while start < len(text):
        # A valid chunk can never contain more code points than the byte budget.
        stop = min(len(text), start + max_event_bytes)
        if _serialized_replay_text_event_size(
            text[start:stop], session_id=session_id, req_id=req_id
        ) <= max_event_bytes:
            yield text[start:stop]
            start = stop
            continue

        low = start + 1
        high = stop
        best = start
        while low <= high:
            middle = (low + high) // 2
            if _serialized_replay_text_event_size(
                text[start:middle], session_id=session_id, req_id=req_id
            ) <= max_event_bytes:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == start:
            raise ValueError("max_event_bytes is too small for one text character")
        yield text[start:best]
        start = best


def _identity_fields(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: event[key]
        for key in ("session_id", "req_id")
        if isinstance(event.get(key), str) and event[key]
    }


def normalize_text_event(event: dict[str, Any]) -> dict[str, Any]:
    """Ensure every text event has a safe, renderable string payload.

    Browser clients append ``event.data`` directly to the chat transcript. A
    missing or structured value otherwise becomes the literal ``undefined`` or
    ``[object Object]`` in JavaScript. Text events are string-only protocol
    messages; structured results use their own event shapes. Keep valid event
    shapes unchanged and attach only non-sensitive diagnostics when a payload
    is malformed.
    """
    if str(event.get("type") or "") != "text" or isinstance(event.get("data"), str):
        return event
    normalized = dict(event)
    normalized["data"] = MALFORMED_TEXT_EVENT_MESSAGE
    normalized["malformed_text_event"] = True
    normalized["malformed_text_data_type"] = type(event.get("data")).__name__
    return normalized


def _bound_image_event(event: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(event)
    data = bounded.get("data")
    image_event = bounded.get("is_screenshot") is True or bounded.get("type") == "live_preview"
    if image_event and isinstance(data, str):
        if _utf8_size(data) > MAX_INLINE_SCREENSHOT_BASE64_BYTES:
            if bounded.get("type") == "live_preview":
                bounded = {
                    "type": "live_preview_omitted",
                    "data": "",
                    "screenshot_omitted": True,
                    **_identity_fields(bounded),
                }
            else:
                bounded.update(
                    {
                        "data": SCREENSHOT_OMITTED_MESSAGE,
                        "is_screenshot": False,
                        "screenshot_omitted": True,
                        "visible": True,
                    }
                )
    return bounded


def _oversized_event_fallback(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "error")
    if event_type == "text":
        fallback: dict[str, Any] = {"type": "text", "data": EVENT_OMITTED_MESSAGE}
    elif event_type == "tool_result":
        fallback = {
            "type": "tool_result",
            "name": event.get("name", "result"),
            "data": EVENT_OMITTED_MESSAGE,
            "is_screenshot": False,
            "visible": True,
        }
    else:
        fallback = {
            "type": event_type,
            "error": EVENT_OMITTED_MESSAGE,
        }
    fallback["event_omitted"] = True
    fallback.update(_identity_fields(event))
    return fallback


def bound_agent_event(
    event: dict[str, Any],
    *,
    encoded_size: int | None = None,
) -> dict[str, Any]:
    """Return an event that is safe to pass through WS, SSE, and the browser."""
    normalized = normalize_text_event(event)
    bounded = _bound_image_event(normalized)
    if bounded.get("screenshot_omitted"):
        return bounded

    if encoded_size is None or normalized != event:
        payload = json.dumps(bounded, separators=(",", ":"), ensure_ascii=False)
        encoded_size = _utf8_size(payload)
    if encoded_size <= MAX_AGENT_EVENT_BYTES:
        return bounded
    return _oversized_event_fallback(bounded)


def serialize_agent_event(event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Bound an event and serialize it once for a WebSocket send."""
    bounded = _bound_image_event(normalize_text_event(event))
    payload = json.dumps(bounded, separators=(",", ":"), ensure_ascii=False)
    if _utf8_size(payload) > MAX_AGENT_EVENT_BYTES:
        bounded = _oversized_event_fallback(bounded)
        payload = json.dumps(bounded, separators=(",", ":"), ensure_ascii=False)
    return bounded, payload


async def send_agent_event(ws: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Serialize and send one bounded event, returning the transmitted shape."""
    bounded, payload = serialize_agent_event(event)
    await ws.send(payload)
    return bounded


def read_inline_screenshot(path: str) -> tuple[str | None, bool]:
    """Read a bounded base64 screenshot without loading oversized files."""
    try:
        if os.path.getsize(path) > MAX_INLINE_SCREENSHOT_BASE64_BYTES:
            return None, True
        with open(path, "r", encoding="utf-8") as handle:
            data = handle.read(MAX_INLINE_SCREENSHOT_BASE64_BYTES + 1)
    except (OSError, UnicodeError):
        return None, False
    if _utf8_size(data) > MAX_INLINE_SCREENSHOT_BASE64_BYTES:
        return None, True
    return data, False


def overlay_event(event: dict[str, Any]) -> dict[str, Any]:
    """Remove screenshot bytes from the CDP overlay notification path."""
    if event.get("is_screenshot") is not True and event.get("type") != "live_preview":
        return event
    stripped = dict(event)
    stripped["data"] = ""
    stripped["screenshot_data_omitted"] = True
    return stripped
