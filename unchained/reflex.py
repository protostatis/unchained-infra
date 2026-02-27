"""Reflex layer — lightweight single-turn coaching for weak models.

Detects specific tool failure patterns and injects a one-sentence system
hint *immediately* after the failure, before the model spirals into a
sweep-and-guess loop.  Complements nudge.py (which detects multi-turn
stalls) by catching **single wrong moves** early.

Five trigger patterns:

1. type_text fails with "no input focused" → list cached input elements
2. click fails 2× consecutively ("--- no change ---") → suggest elements
3. 3+ ddm --at without prior --llm-2pass → "run 2pass first"
4. click at (x,y) >80px from any DDM element → show nearest elements
5. same element clicked 3+ times within 20px with no change → suggest JS .click()
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Config (env-var controlled)
# ---------------------------------------------------------------------------

REFLEX_ENABLED = os.environ.get("REFLEX_ENABLED", "1") == "1"
REFLEX_MAX_HINTS = int(os.environ.get("REFLEX_MAX_HINTS", "3"))

# ---------------------------------------------------------------------------
# Stopwords for goal keyword extraction
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an the to for of in on at by is it i me my we our you your "
    "this that and or but not with from do does did can could would "
    "should will shall may might have has had be been am are was were "
    "go find get search look click open show me please help want need "
    "use using let".split()
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEGMENT_RE = re.compile(r"(>?)(\S[^@]*)@(\d+),(\d+)")


def parse_ddm_elements(output: str) -> list[dict]:
    """Parse ``Label@x,y`` entries from ``--llm-2pass`` output.

    Returns a list of dicts: ``{'label': str, 'x': int, 'y': int, 'is_input': bool}``.
    Handles the optional ``Tab: …`` header line and the ``PAGE_TITLE:`` prefix.
    """
    elements: list[dict] = []
    # The 2pass format is: PAGE_TITLE: >Label@x,y|Label@x,y|…
    # Split by | to isolate segments, then strip any leading title prefix
    # from the first segment on each line.
    for line in output.splitlines():
        # Strip "Tab: …" header lines
        stripped = line.strip()
        if stripped.startswith("Tab:"):
            continue
        # Split segments on |
        segments = stripped.split("|")
        for i, seg in enumerate(segments):
            # First segment may have "PAGE_TITLE: " prefix — strip it
            if i == 0:
                colon_pos = seg.find(": ")
                if colon_pos != -1:
                    seg = seg[colon_pos + 2:]
            seg = seg.strip()
            m = _SEGMENT_RE.match(seg)
            if m:
                prefix, label, x_s, y_s = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
                elements.append({
                    "label": label,
                    "x": int(x_s),
                    "y": int(y_s),
                    "is_input": prefix == ">",
                })
    return elements


def extract_goal_keywords(message: str) -> list[str]:
    """Tokenise a user message and return non-stopword keywords (lowercase)."""
    tokens = re.findall(r"[a-zA-Z0-9]+", message.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _distance(x1: int, y1: int, x2: int, y2: int) -> float:
    return math.hypot(x2 - x1, y2 - y1)


# ---------------------------------------------------------------------------
# ReflexState
# ---------------------------------------------------------------------------

@dataclass
class ReflexState:
    """Per-message reflex tracker.  Create one per user message, same as NudgeState."""

    # Cached DDM elements from the most recent --llm-2pass output
    _elements: list[dict] = field(default_factory=list)

    # User goal keywords
    _goal_keywords: list[str] = field(default_factory=list)

    # Consecutive "--- no change ---" click results
    _no_change_streak: int = 0

    # Consecutive --at calls without a prior --llm-2pass
    _at_probe_streak: int = 0
    _has_2pass: bool = False

    # SPA widget detection: same element clicked repeatedly with no change
    _last_click_coords: tuple[int, int] | None = None
    _same_click_no_change: int = 0

    # Total hints emitted this message
    _hints_emitted: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_user_goal(self, user_text: str) -> None:
        """Extract and cache goal keywords from the user message."""
        self._goal_keywords = extract_goal_keywords(user_text)

    def on_tool_result(self, tool_name: str, args: dict, result: str) -> str | None:
        """Inspect a tool call + result and return a hint string, or None.

        Call this for every tool execution, in order.  The returned hint
        (if any) should be appended as a ``{"role": "system", …}`` message.
        """
        if not REFLEX_ENABLED:
            return None
        if self._hints_emitted >= REFLEX_MAX_HINTS:
            return None

        hint: str | None = None

        # --- Update DDM element cache on --llm-2pass output ---
        flags = str(args.get("flags", ""))
        if tool_name == "ddm" and "--llm-2pass" in flags:
            self._elements = parse_ddm_elements(result)
            self._has_2pass = True
            self._at_probe_streak = 0  # reset probing streak

        # --- Trigger 1: type_text fails with "no input focused" ---
        if tool_name == "type_text" and "no input focused" in result.lower():
            hint = self._hint_no_focus()

        # --- Trigger 2/5: click "--- no change ---" ---
        elif tool_name == "click":
            x, y = int(args.get("x", 0)), int(args.get("y", 0))
            if "--- no change ---" in result:
                self._no_change_streak += 1
                # Trigger 5: same element clicked 3+ times (within 20px)
                if self._last_click_coords and _distance(x, y, *self._last_click_coords) < 20:
                    self._same_click_no_change += 1
                else:
                    self._same_click_no_change = 1
                    self._last_click_coords = (x, y)
                if self._same_click_no_change >= 3:
                    hint = self._hint_spa_widget(x, y)
                elif self._no_change_streak >= 2:
                    hint = self._hint_click_no_change(args)
            else:
                self._no_change_streak = 0
                self._same_click_no_change = 0
                self._last_click_coords = None

                # --- Trigger 4: click far from any DDM element ---
                if self._elements:
                    hint = self._hint_far_click(args)

        # --- Trigger 3: blind --at probing ---
        elif tool_name == "ddm" and "--at" in flags and "--llm-2pass" not in flags:
            if not self._has_2pass:
                self._at_probe_streak += 1
                if self._at_probe_streak >= 3:
                    hint = self._hint_blind_probe()

        if hint:
            self._hints_emitted += 1
            return f"[Reflex] {hint}"

        return None

    # ------------------------------------------------------------------
    # Private hint builders
    # ------------------------------------------------------------------

    def _hint_no_focus(self) -> str:
        """Trigger 1: type_text failed because no input is focused."""
        inputs = [e for e in self._elements if e["is_input"]]
        if inputs:
            listing = ", ".join(
                f"{e['label']}@{e['x']},{e['y']}" for e in inputs[:5]
            )
            return (
                f"type_text failed — no input is focused. "
                f"Click an input first. Available inputs: {listing}"
            )
        return (
            "type_text failed — no input is focused. "
            "Run ddm --llm-2pass to find input fields, then click one before typing."
        )

    def _hint_click_no_change(self, args: dict) -> str:
        """Trigger 2: two consecutive clicks produced no change."""
        suggestions = self._match_elements_to_goal()
        if suggestions:
            listing = ", ".join(
                f"{e['label']}@{e['x']},{e['y']}" for e in suggestions[:3]
            )
            return (
                f"Two clicks produced no change. "
                f"Try these elements matching your goal: {listing}"
            )
        # Fallback: show nearest elements to last click
        x = int(args.get("x", 0))
        y = int(args.get("y", 0))
        nearest = self._nearest_elements(x, y, n=3)
        if nearest:
            listing = ", ".join(
                f"{e['label']}@{e['x']},{e['y']} ({e['dist']:.0f}px)"
                for e in nearest
            )
            return (
                f"Two clicks produced no change. "
                f"Nearest interactive elements: {listing}"
            )
        return (
            "Two clicks produced no change. "
            "Run ddm --llm-2pass to refresh the element list."
        )

    def _hint_blind_probe(self) -> str:
        """Trigger 3: multiple --at calls without a prior --llm-2pass."""
        return (
            "You've used ddm --at multiple times without running ddm --llm-2pass first. "
            "Run ddm --llm-2pass to see all interactive elements with coordinates, "
            "then use --at only on elements you see in the map."
        )

    def _hint_far_click(self, args: dict) -> str | None:
        """Trigger 4: click coordinates >80px from any known DDM element."""
        x = int(args.get("x", 0))
        y = int(args.get("y", 0))
        min_dist = min(
            _distance(x, y, e["x"], e["y"]) for e in self._elements
        )
        if min_dist <= 80:
            return None
        nearest = self._nearest_elements(x, y, n=3)
        listing = ", ".join(
            f"{e['label']}@{e['x']},{e['y']} ({e['dist']:.0f}px)"
            for e in nearest
        )
        return (
            f"Click at ({x},{y}) is {min_dist:.0f}px from any interactive element. "
            f"Nearest elements: {listing}"
        )

    def _hint_spa_widget(self, x: int, y: int) -> str:
        """Trigger 5: same element clicked 3+ times with no response."""
        return (
            f"Clicked near ({x},{y}) 3+ times with no response — likely an SPA widget "
            f"that ignores CDP mouse events. Switch to: js_eval with "
            f"document.elementFromPoint({x},{y}).click(), "
            f"or construct the destination URL directly and use navigate."
        )

    # ------------------------------------------------------------------
    # Element matching helpers
    # ------------------------------------------------------------------

    def _match_elements_to_goal(self) -> list[dict]:
        """Substring-match goal keywords against cached element labels."""
        if not self._goal_keywords or not self._elements:
            return []
        matches = []
        for el in self._elements:
            label_lower = el["label"].lower()
            if any(kw in label_lower for kw in self._goal_keywords):
                matches.append(el)
        return matches

    def _nearest_elements(self, x: int, y: int, n: int = 3) -> list[dict]:
        """Return the *n* closest cached DDM elements with distance."""
        if not self._elements:
            return []
        scored = []
        for el in self._elements:
            dist = _distance(x, y, el["x"], el["y"])
            scored.append({**el, "dist": dist})
        scored.sort(key=lambda e: e["dist"])
        return scored[:n]
