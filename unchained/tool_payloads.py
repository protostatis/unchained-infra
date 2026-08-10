"""Helpers for suppressing accidental user-visible XML tool payloads.

The patterns deliberately recognize only ``tool_call`` wrappers.  They do not
HTML-decode arbitrary model text, which would otherwise alter legitimate
user-facing content.
"""

from __future__ import annotations

import re


# Models can emit raw XML, HTML-escaped XML, or text that was escaped twice by
# an upstream transport/UI layer.  Direct-DeepSeek recovery imports these
# boundaries too; longer entity forms must stay first so each tag is consumed
# as one unit.
_XML_LT = r"(?:&amp;lt;|&lt;|<)"
_XML_GT = r"(?:&amp;gt;|&gt;|>)"
_TOOL_CALL_MARKER_RE = re.compile(
    rf"{_XML_LT}\s*/?\s*tool_call\b", re.IGNORECASE
)
_TOOL_CALL_OPEN = rf"{_XML_LT}\s*tool_call\b.*?{_XML_GT}"
_TOOL_CALL_CLOSE = rf"{_XML_LT}\s*/\s*tool_call\s*{_XML_GT}"
_TOOL_CALL_BLOCK_RE = re.compile(
    rf"{_TOOL_CALL_OPEN}.*?{_TOOL_CALL_CLOSE}",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_CALL_DANGLING_RE = re.compile(
    rf"{_XML_LT}\s*/?\s*tool_call\b.*$",
    re.IGNORECASE | re.DOTALL,
)
# DeepSeek can fall back to DSML tool-call markup rather than returning the
# OpenAI-compatible ``tool_calls`` field. Keep the literal delimiter escaped in
# source so this matcher remains readable in every editor.
_DSML_PREFIX = r"\uFF5C\uFF5CDSML\uFF5C\uFF5C"
_DSML_TOOL_CALLS_MARKER_RE = re.compile(
    rf"{_XML_LT}\s*/?\s*{_DSML_PREFIX}tool_calls\b", re.IGNORECASE
)
_DSML_TOOL_CALLS_OPEN = rf"{_XML_LT}\s*{_DSML_PREFIX}tool_calls\b.*?{_XML_GT}"
_DSML_TOOL_CALLS_CLOSE = rf"{_XML_LT}\s*/\s*{_DSML_PREFIX}tool_calls\s*{_XML_GT}"
_DSML_TOOL_CALLS_BLOCK_RE = re.compile(
    rf"{_DSML_TOOL_CALLS_OPEN}.*?{_DSML_TOOL_CALLS_CLOSE}",
    re.IGNORECASE | re.DOTALL,
)
_DSML_TOOL_CALLS_DANGLING_RE = re.compile(
    rf"{_XML_LT}\s*/?\s*{_DSML_PREFIX}tool_calls\b.*$",
    re.IGNORECASE | re.DOTALL,
)


def contains_tool_call_wrapper(text: str) -> bool:
    """Return whether text contains a raw or escaped tool-call wrapper."""
    value = text or ""
    return bool(
        _TOOL_CALL_MARKER_RE.search(value)
        or _DSML_TOOL_CALLS_MARKER_RE.search(value)
    )


def strip_tool_call_wrappers(text: str) -> str:
    """Remove complete blocks and truncate text at a dangling tool wrapper."""
    cleaned = _TOOL_CALL_BLOCK_RE.sub(" ", text or "")
    cleaned = _DSML_TOOL_CALLS_BLOCK_RE.sub(" ", cleaned)
    cleaned = _TOOL_CALL_DANGLING_RE.sub(" ", cleaned)
    return _DSML_TOOL_CALLS_DANGLING_RE.sub(" ", cleaned)
