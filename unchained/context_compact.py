"""context_compact.py — Deterministic context compaction for browser agents.

Stale browser tool results (DDM layouts, screenshots, page intel) accumulate
in agent conversation histories and waste tokens.  This module compacts them
**deterministically** — no model call needed — based on tool type and recency.

Two-tier approach:
  Tier 1 (free, instant):  Collapse stale tool results by classification.
  Tier 2 (optional):       Model-generated summary of older assistant reasoning
                           via a caller-provided callback.
  Emergency:               Hard tail-trim as last resort on API 400 errors.

Public API
----------
compact_messages(messages, *, fmt, keep_recent=6, max_tool_result_chars=300)
    -> (list, dict)
emergency_trim(messages, *, fmt, keep_tail=10)
    -> list
estimate_tokens(messages)
    -> int
"""

from __future__ import annotations

import json
import re


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

_PAGE_STATE_TOOLS = frozenset({
    "ddm", "navigate", "cdp_navigate", "click", "cdp_click",
    "screenshot", "cdp_screenshot", "intel_probe", "intel_extract",
})
_EPHEMERAL_TOOLS = frozenset({"cdp_type", "type_text", "cdp_set_file", "submit_form", "press_enter"})
_DATA_TOOLS = frozenset({"js_eval", "intel_stores", "intel_shape", "intel_find_paths"})
_META_TOOLS = frozenset({
    "list_connected_agents", "cdp_provision_launch",
    "cdp_provision_cleanup", "list_provisioned_tabs",
})


def _classify_tool(name: str) -> str:
    """Classify a tool name into a compaction category."""
    if not name:
        return "unknown"
    # Normalise MCP-prefixed names: e.g. "mcp__unchainedsky__ddm" -> "ddm"
    short = name.rsplit("__", 1)[-1] if "__" in name else name
    if short in _PAGE_STATE_TOOLS:
        return "page_state"
    if short in _EPHEMERAL_TOOLS:
        return "ephemeral"
    if short in _DATA_TOOLS:
        return "data"
    if short in _META_TOOLS:
        return "meta"
    return "unknown"


# ---------------------------------------------------------------------------
# Page-layout stripping helper
# ---------------------------------------------------------------------------

_PAGE_LAYOUT_RE = re.compile(
    r"=== Page Layout ===.*?(?==== |$)",
    re.DOTALL,
)


def _strip_page_layout(text: str) -> str:
    """Remove the ``=== Page Layout ===`` section from a tool result.

    Keeps everything before and after (action summaries, titles, etc.).
    """
    stripped = _PAGE_LAYOUT_RE.sub("", text).strip()
    return stripped if stripped else text


# ---------------------------------------------------------------------------
# Per-tool compaction
# ---------------------------------------------------------------------------

def _compact_tool_result(
    tool_name: str,
    content: str,
    classification: str,
    max_data_chars: int = 300,
) -> str:
    """Return a compacted version of *content* based on tool classification."""
    if not isinstance(content, str) or not content:
        return content or ""

    if classification == "page_state":
        short = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
        if short in ("screenshot", "cdp_screenshot"):
            return "[screenshot — compacted]"
        if short == "ddm":
            return "[ddm — compacted]"
        # navigate/click/intel: strip page layout, keep action summary
        return _strip_page_layout(content)

    if classification == "ephemeral":
        if len(content) > 100:
            return content[:100] + f" [truncated from {len(content)} chars]"
        return content

    if classification == "data":
        if len(content) > max_data_chars:
            return content[:max_data_chars] + f" [truncated from {len(content)} chars]"
        return content

    if classification == "meta":
        return content  # keep unchanged

    # unknown
    if len(content) > max_data_chars:
        return content[:max_data_chars] + f" [truncated from {len(content)} chars]"
    return content


# ---------------------------------------------------------------------------
# Tool-name index builder  (maps tool_call_id → tool_name)
# ---------------------------------------------------------------------------

def _build_tool_name_index(messages: list, fmt: str) -> dict[str, str]:
    """Scan assistant messages to map each ``tool_call_id`` to its tool name.

    Handles both live Anthropic SDK ContentBlock objects and serialised dicts.
    """
    index: dict[str, str] = {}

    if fmt == "anthropic":
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                # SDK object (e.g. ToolUseBlock)
                if hasattr(item, "type") and getattr(item, "type", "") == "tool_use":
                    tid = getattr(item, "id", None)
                    tname = getattr(item, "name", None)
                    if tid and tname:
                        index[tid] = tname
                # Serialised dict
                elif isinstance(item, dict) and item.get("type") == "tool_use":
                    tid = item.get("id")
                    tname = item.get("name")
                    if tid and tname:
                        index[tid] = tname

    elif fmt == "openai":
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tid = tc.get("id")
                func = tc.get("function") or {}
                tname = func.get("name")
                if tid and tname:
                    index[tid] = tname

    return index


# ---------------------------------------------------------------------------
# Turn boundary detection
# ---------------------------------------------------------------------------

def _find_turn_boundary(messages: list, fmt: str, keep_recent: int) -> int:
    """Return the index that separates 'old' messages from 'recent' ones.

    We count user messages from the end and return the index of the message
    *before* the ``keep_recent``-th user message.  Everything at or after that
    index is considered recent and will not be compacted.

    If the conversation is short enough, returns ``len(messages)`` (compact nothing).
    """
    if fmt == "anthropic":
        # In Anthropic format, user messages carry tool results too,
        # so count actual user turns (messages with role "user" that contain
        # a text string, not just tool_result blocks).
        user_indices = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user":
                content = msg.get("content")
                # Pure text user message
                if isinstance(content, str):
                    user_indices.append(i)
                # List content — check for text blocks (not just tool_result)
                elif isinstance(content, list):
                    has_text = any(
                        (isinstance(b, dict) and b.get("type") == "text")
                        or (isinstance(b, str))
                        for b in content
                    )
                    # Also count pure tool_result messages as turns
                    user_indices.append(i)
    else:
        # OpenAI: count user messages
        user_indices = [
            i for i, msg in enumerate(messages)
            if isinstance(msg, dict) and msg.get("role") in ("user", "tool")
        ]

    if len(user_indices) <= keep_recent:
        return len(messages)  # nothing to compact

    # The boundary is at the keep_recent-th user message from the end
    boundary_user_idx = user_indices[-keep_recent]
    return boundary_user_idx


# ---------------------------------------------------------------------------
# Content extraction helpers
# ---------------------------------------------------------------------------

def _get_tool_result_content(msg: dict, fmt: str) -> str | None:
    """Extract the text content from a tool-result message.

    Returns None if this is not a tool-result message.
    """
    if fmt == "anthropic":
        if msg.get("role") != "user":
            return None
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        # Check if this is a tool_result message
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block.get("content", "")
        return None

    elif fmt == "openai":
        if msg.get("role") != "tool":
            return None
        return msg.get("content", "")

    return None


def _set_tool_result_content(msg: dict, fmt: str, tool_call_id: str, new_content: str):
    """Replace the content of a tool-result message in place."""
    if fmt == "anthropic":
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == tool_call_id):
                    block["content"] = new_content

    elif fmt == "openai":
        if msg.get("tool_call_id") == tool_call_id:
            msg["content"] = new_content


def _iter_tool_results(msg: dict, fmt: str):
    """Yield ``(tool_call_id, content_text)`` pairs from a tool-result message."""
    if fmt == "anthropic":
        if msg.get("role") != "user":
            return
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block.get("tool_use_id", ""), block.get("content", "")

    elif fmt == "openai":
        if msg.get("role") != "tool":
            return
        yield msg.get("tool_call_id", ""), msg.get("content", "")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(messages: list) -> int:
    """Rough token estimate (~4 chars per token) for a message list."""
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            total += len(json.dumps(msg, default=str))
    return total // 4


# ---------------------------------------------------------------------------
# Public API: compact_messages
# ---------------------------------------------------------------------------

def compact_messages(
    messages: list,
    *,
    fmt: str,
    keep_recent: int = 6,
    max_tool_result_chars: int = 300,
) -> tuple[list, dict]:
    """Compact stale tool results in *messages* (Tier 1, deterministic).

    Parameters
    ----------
    messages : list
        Conversation message list (modified **in place** and also returned).
    fmt : ``"anthropic"`` or ``"openai"``
        Message format.
    keep_recent : int
        Number of recent user turns to preserve without compaction.
    max_tool_result_chars : int
        Truncation limit for ``data`` and ``unknown`` tool results.

    Returns
    -------
    (messages, stats) where stats is a dict with compaction metadata.
    """
    stats = {
        "compacted": 0,
        "preserved": 0,
        "tokens_before": estimate_tokens(messages),
    }

    boundary = _find_turn_boundary(messages, fmt, keep_recent)
    if boundary >= len(messages):
        stats["tokens_after"] = stats["tokens_before"]
        return messages, stats

    # Build tool-name index from assistant messages
    tool_index = _build_tool_name_index(messages, fmt)

    # Walk messages in the old portion and compact tool results
    for i in range(boundary):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue

        for tool_call_id, content in _iter_tool_results(msg, fmt):
            tool_name = tool_index.get(tool_call_id, "")
            classification = _classify_tool(tool_name)
            compacted = _compact_tool_result(
                tool_name, content, classification,
                max_data_chars=max_tool_result_chars,
            )
            if compacted != content:
                _set_tool_result_content(msg, fmt, tool_call_id, compacted)
                stats["compacted"] += 1
            else:
                stats["preserved"] += 1

    stats["tokens_after"] = estimate_tokens(messages)
    return messages, stats


# ---------------------------------------------------------------------------
# Public API: emergency_trim
# ---------------------------------------------------------------------------

def emergency_trim(
    messages: list,
    *,
    fmt: str,
    keep_tail: int = 10,
) -> list:
    """Hard tail-trim for API 400 / context-overflow recovery.

    Keeps the first message (system prompt for OpenAI, or first user message
    for Anthropic) plus the last *keep_tail* messages, ensuring no orphaned
    tool results at the start of the kept tail.

    Returns a **new** list (does not mutate the input).
    """
    if len(messages) <= keep_tail + 1:
        return list(messages)

    # Identify head: system prompt or first message
    head = [messages[0]]

    # Candidate tail
    tail_start = len(messages) - keep_tail
    # Adjust forward to avoid orphaned tool results
    tail_start = _skip_orphaned_tool_results(messages, tail_start, fmt)

    tail = messages[tail_start:]
    return head + tail


def _skip_orphaned_tool_results(messages: list, start: int, fmt: str) -> int:
    """Advance *start* past any tool-result messages that lack a preceding
    assistant message with the matching tool_call."""
    while start < len(messages):
        msg = messages[start]
        if not isinstance(msg, dict):
            break

        if fmt == "openai" and msg.get("role") == "tool":
            start += 1
            continue

        if fmt == "anthropic" and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                is_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if is_tool_result:
                    start += 1
                    continue

        break

    return start
