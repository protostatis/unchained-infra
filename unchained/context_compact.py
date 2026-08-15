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
compact_active_browser_checkpoints(messages, *, checkpoint_identities)
    -> (list, dict)
emergency_trim(messages, *, fmt, keep_tail=10)
    -> list
estimate_tokens(messages)
    -> int
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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

_SUPERSEDED_DOM_CHECKPOINT = (
    "[Earlier browser DOM checkpoint omitted; a newer checkpoint for this tab "
    "and document state appears later in the active turn.]"
)
_DDM_ORIENTATION_RE = re.compile(
    r"(?:@\d+\s*,\s*\d+|\bpx\(\d+\s*,\s*\d+\)|\bat grid\()",
    re.IGNORECASE,
)
_CHECKPOINT_ERROR_MARKERS = (
    "BROWSER_UNAVAILABLE:",
    "LINK_SCAN_REPEAT_BLOCKED:",
    "JS_EVAL_REPEAT_BLOCKED:",
)
_CHECKPOINT_SUFFIX_MARKERS = ("\n\nNAVIGATION_NOT_FOUND:",)


@dataclass(frozen=True, slots=True)
class BrowserCheckpointIdentity:
    """Concrete browser state identity captured by the execution layer."""

    physical_tab_id: str
    document_id: str


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
        if short in ("intel_probe", "intel_extract"):
            # Intel results are structured data — truncate like data tools
            if len(content) > max_data_chars:
                return content[:max_data_chars] + f" [truncated from {len(content)} chars]"
            return content
        # navigate/click: strip page layout, keep action summary
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
# Active-turn browser checkpoint compaction (OpenAI format)
# ---------------------------------------------------------------------------


def _decode_openai_tool_arguments(tool_call: dict) -> dict:
    function = tool_call.get("function") or {}
    raw = function.get("arguments", {})
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_browser_dom_checkpoint(tool_call: dict, content: str) -> bool:
    """Return whether a tool result is a trustworthy DOM-state checkpoint."""
    if not isinstance(content, str) or not content.strip():
        return False
    if _SUPERSEDED_DOM_CHECKPOINT in content:
        return False
    stripped = content.lstrip()
    if stripped.lower().startswith(("error:", "tool error (")):
        return False
    if any(marker in content for marker in _CHECKPOINT_ERROR_MARKERS):
        return False

    function = tool_call.get("function") or {}
    name = str(function.get("name", "") or "").rsplit("__", 1)[-1]
    args = _decode_openai_tool_arguments(tool_call)

    if name == "navigate":
        if not stripped.startswith("Navigated to:") or "=== Page Layout ===" not in content:
            return False
    elif name in {"click", "cdp_click"}:
        if not stripped.startswith("Clicked ") or "=== Page Layout ===" not in content:
            return False
    elif name == "ddm":
        flags = str(args.get("flags", "--llm-2pass --cols 60") or "").strip()
        tokens = set(flags.split())
        if "--llm-2pass" not in tokens:
            return False
        if tokens.intersection({"--text", "--at", "--js", "--find", "--new", "--tabs", "--close"}):
            return False
        if any(token.startswith("--js=") for token in tokens):
            return False
        if not (_DDM_ORIENTATION_RE.search(content) or "=== Page Layout ===" in content):
            return False
    else:
        return False

    return True


def _browser_checkpoint_bucket(
    tool_call: dict,
    content: str,
    identity: BrowserCheckpointIdentity | None,
) -> tuple[str, str] | None:
    """Return a proven physical-tab and document bucket for one checkpoint."""
    if (
        not is_browser_dom_checkpoint(tool_call, content)
        or not isinstance(identity, BrowserCheckpointIdentity)
    ):
        return None
    physical_tab_id = str(identity.physical_tab_id or "").strip()
    document_id = str(identity.document_id or "").strip()
    if not physical_tab_id or physical_tab_id == "auto" or not document_id:
        return None

    return (physical_tab_id, document_id)


def _compact_browser_checkpoint_content(tool_call: dict, content: str) -> str:
    function = tool_call.get("function") or {}
    name = str(function.get("name", "") or "").rsplit("__", 1)[-1]
    if name == "ddm":
        return _SUPERSEDED_DOM_CHECKPOINT

    prefix, separator, layout = content.partition("=== Page Layout ===")
    if not separator:
        return content
    prefix = prefix.rstrip()
    suffix = ""
    for marker in _CHECKPOINT_SUFFIX_MARKERS:
        marker_index = layout.rfind(marker)
        if marker_index >= 0:
            suffix = layout[marker_index:].strip()
            break
    parts = [part for part in (prefix, _SUPERSEDED_DOM_CHECKPOINT, suffix) if part]
    return "\n\n".join(parts)


def _complete_openai_tool_groups(messages: list, start: int) -> list[dict]:
    """Collect protocol-valid assistant/tool blocks after *start*."""
    groups: list[dict] = []
    index = start
    while index < len(messages):
        assistant = messages[index]
        if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
            index += 1
            continue

        tool_calls = assistant.get("tool_calls") or []
        if not isinstance(tool_calls, list) or not tool_calls:
            index += 1
            continue

        expected_ids: list[str] = []
        valid_calls = True
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                valid_calls = False
                break
            call_id = tool_call.get("id")
            if not isinstance(call_id, str) or not call_id or call_id in expected_ids:
                valid_calls = False
                break
            expected_ids.append(call_id)

        result_index = index + 1
        results: list[dict] = []
        while result_index < len(messages):
            result = messages[result_index]
            if not isinstance(result, dict) or result.get("role") != "tool":
                break
            results.append(result)
            result_index += 1

        result_ids = [result.get("tool_call_id") for result in results]
        if valid_calls and result_ids == expected_ids:
            groups.append({
                "index": index,
                "tool_calls": tool_calls,
                "results": results,
            })
        index = max(result_index, index + 1)
    return groups


def compact_active_browser_checkpoints(
    messages: list,
    *,
    checkpoint_identities: Mapping[str, BrowserCheckpointIdentity] | None = None,
) -> tuple[list, dict]:
    """Collapse superseded DOM snapshots within the current OpenAI user turn.

    The newest checkpoint for each proven physical-tab and document identity stays
    raw. Older navigate/click layouts and orientation DDM maps are replaced in
    place while assistant messages, tool-call IDs, evidence tools, failures,
    and message ordering remain unchanged. Unresolved identities fail closed.
    This intentionally mutates only the bounded provider working context; the
    separately persisted visible transcript is not derived from tool results.
    """
    before_chars = len(json.dumps(messages, ensure_ascii=False, default=str))
    stats = {
        "compacted": 0,
        "preserved": 0,
        "skipped_groups": 0,
        "chars_before": before_chars,
        "chars_after": before_chars,
    }
    if not isinstance(messages, list) or not messages:
        return messages, stats

    last_user_index = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            last_user_index = index
            break
    if last_user_index < 0:
        return messages, stats

    identities = checkpoint_identities or {}
    groups = _complete_openai_tool_groups(messages, last_user_index + 1)
    checkpoint_records: list[dict] = []
    latest_record_by_bucket: dict[tuple[str, str], int] = {}

    for group in groups:
        found_checkpoint = False
        for tool_call, result in zip(group["tool_calls"], group["results"]):
            call_id = tool_call.get("id")
            identity = identities.get(call_id) if isinstance(call_id, str) else None
            bucket = _browser_checkpoint_bucket(
                tool_call,
                result.get("content", ""),
                identity,
            )
            if bucket is None:
                continue
            found_checkpoint = True
            record_index = len(checkpoint_records)
            checkpoint_records.append({
                "bucket": bucket,
                "tool_call": tool_call,
                "result": result,
            })
            latest_record_by_bucket[bucket] = record_index
        if not found_checkpoint:
            stats["skipped_groups"] += 1

    for record_index, record in enumerate(checkpoint_records):
        if record_index == latest_record_by_bucket[record["bucket"]]:
            stats["preserved"] += 1
            continue
        result = record["result"]
        content = result.get("content", "")
        compacted = _compact_browser_checkpoint_content(record["tool_call"], content)
        if compacted != content:
            result["content"] = compacted
            stats["compacted"] += 1

    stats["chars_after"] = len(
        json.dumps(messages, ensure_ascii=False, default=str)
    )
    return messages, stats


# ---------------------------------------------------------------------------
# Turn boundary detection
# ---------------------------------------------------------------------------

def _find_turn_boundary(messages: list, fmt: str, keep_recent: int) -> int:
    """Return the index that separates 'old' messages from 'recent' ones.

    We count **actual user text messages** (not tool-result messages) from the
    end and return the index of the ``keep_recent``-th such message.  Everything
    at or after that index is considered recent and will not be compacted.

    If the conversation is short enough, returns ``len(messages)`` (compact nothing).
    """
    if fmt == "anthropic":
        # Count only user messages that contain real user text — not pure
        # tool_result payloads.
        user_indices = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                # Plain text user message — always a real turn
                user_indices.append(i)
            elif isinstance(content, list):
                # List content: count as a real turn only if it has a text block
                has_text = any(
                    (isinstance(b, dict) and b.get("type") == "text")
                    or isinstance(b, str)
                    for b in content
                )
                if has_text:
                    user_indices.append(i)
                # Pure tool_result messages are NOT counted as user turns
    else:
        # OpenAI: count only role=="user" messages (not role=="tool")
        user_indices = [
            i for i, msg in enumerate(messages)
            if isinstance(msg, dict) and msg.get("role") == "user"
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
