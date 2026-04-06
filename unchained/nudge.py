"""nudge.py — Shared nudge/intervention utilities for chat agents.

Provides loop detection, stagnation scoring, and progress-based
intervention logic extracted from chat_agent_openrouter.py.  Consumed by:
  - chat_agent_sdk.py     (full nudge: loop + stagnation + intervention)
  - chat_agent_cli.py     (limited: loop + stagnation, no progress_critic)
  - chat_agent_openrouter.py  (full nudge, refactored to use this module)
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants (env-var controlled)
# ---------------------------------------------------------------------------

STALL_SCORE_THRESHOLD = int(os.environ.get("STALL_SCORE_THRESHOLD", "7"))
STALL_FORCE_FINAL_STRIKES = int(os.environ.get("STALL_FORCE_FINAL_STRIKES", "2"))
STALL_NAV_GRACE_TURNS = int(os.environ.get("STALL_NAV_GRACE_TURNS", "3"))
LOOP_SHORT_CIRCUIT_REPEAT_THRESHOLD = int(
    os.environ.get("LOOP_SHORT_CIRCUIT_REPEAT_THRESHOLD", "2")
)
STALL_VARIETY_WINDOW = int(os.environ.get("STALL_VARIETY_WINDOW", "8"))
STALL_FIND_WINDOW = int(os.environ.get("STALL_FIND_WINDOW", "6"))
STALL_FIND_DISTINCT_MAX = int(os.environ.get("STALL_FIND_DISTINCT_MAX", "2"))

INTERVENTION_ENABLED = os.environ.get("INTERVENTION_ENABLED", "1").lower() not in (
    "0", "false", "no", "off",
)
INTERVENTION_MIN_SEVERITY = os.environ.get(
    "INTERVENTION_MIN_SEVERITY", "nudge"
).strip().lower()
INTERVENTION_MIN_TOOL_STEPS = int(os.environ.get("INTERVENTION_MIN_TOOL_STEPS", "4"))
INTERVENTION_COOLDOWN_TURNS = int(os.environ.get("INTERVENTION_COOLDOWN_TURNS", "3"))
INTERVENTION_MAX_EVENTS = int(os.environ.get("INTERVENTION_MAX_EVENTS", "2"))

try:
    INTERVENTION_NUDGE_STALL_DECAY = float(
        os.environ.get("INTERVENTION_NUDGE_STALL_DECAY", "0.5")
    )
except ValueError:
    INTERVENTION_NUDGE_STALL_DECAY = 0.5
INTERVENTION_NUDGE_STALL_DECAY = min(1.0, max(0.0, INTERVENTION_NUDGE_STALL_DECAY))

INTERVENTION_NUDGE_RESET_PROGRESS = os.environ.get(
    "INTERVENTION_NUDGE_RESET_PROGRESS", "1",
).lower() not in ("0", "false", "no", "off")

INTERVENTION_SCREENSHOT_ON_NUDGE = os.environ.get(
    "INTERVENTION_SCREENSHOT_ON_NUDGE", "1",
).lower() not in ("0", "false", "no", "off")

INTERVENTION_SCREENSHOT_TIMEOUT = int(
    os.environ.get("INTERVENTION_SCREENSHOT_TIMEOUT", "20")
)

# ---------------------------------------------------------------------------
# Soft import of progress_critic (not available in client package)
# ---------------------------------------------------------------------------

_INTERVENTION_IMPORT_ERROR = ""
try:
    from benchmark.progress_critic import build_intervention_feedback, score_tool_log
except Exception as e:
    build_intervention_feedback = None
    score_tool_log = None
    _INTERVENTION_IMPORT_ERROR = str(e)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _is_base64_png_blob(value: str) -> bool:
    """Heuristic check for screenshot payloads (base64-encoded PNG or JPEG)."""
    if not isinstance(value, str) or len(value) < 64:
        return False
    return value.startswith("iVBOR") or value.startswith("/9j/")


def _hash_sig(text: str) -> str:
    """SHA1 signature (first 12 hex chars)."""
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _normalize_for_progress(text: str, max_len: int = 1500) -> str:
    """Collapse whitespace and truncate for progress comparison."""
    return " ".join((text or "").split())[:max_len]


def _tool_progress_sig(tool_name: str, args: dict, result: str) -> str:
    """Deterministic hash of a tool call + truncated result."""
    payload = json.dumps(
        {
            "tool": tool_name or "",
            "args": args or {},
            "result": _normalize_for_progress(result),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _hash_sig(payload)


def _extract_domain(url: str) -> str:
    """Normalize URL hostname for coarse navigation diversity checks."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        host = (urlparse(raw).netloc or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def intervention_runtime_available() -> bool:
    """Check if progress_critic is importable and intervention is enabled."""
    return bool(INTERVENTION_ENABLED and score_tool_log and build_intervention_feedback)


def _severity_rank(level: str) -> int:
    """Numeric ordering of severity levels."""
    order = {"none": 0, "watch": 1, "nudge": 2, "hard_stop": 3}
    return order.get((level or "").strip().lower(), 0)


def should_emit_intervention(
    feedback,
    progress_turns: int,
    model_turn: int,
    emitted_count: int,
    last_emit_model_turn: int,
) -> bool:
    """Decide whether an intervention event should be emitted."""
    if not intervention_runtime_available():
        return False
    if not feedback or not getattr(feedback, "should_intervene", False):
        return False
    if progress_turns < max(1, INTERVENTION_MIN_TOOL_STEPS):
        return False
    if _severity_rank(getattr(feedback, "severity", "none")) < _severity_rank(
        INTERVENTION_MIN_SEVERITY
    ):
        return False
    if emitted_count >= max(0, INTERVENTION_MAX_EVENTS):
        return False
    if model_turn - last_emit_model_turn < max(0, INTERVENTION_COOLDOWN_TURNS):
        return False
    return True


# ---------------------------------------------------------------------------
# NudgeState — encapsulates all per-message tracking
# ---------------------------------------------------------------------------

@dataclass
class NudgeState:
    """Mutable state for loop detection, stagnation scoring, and intervention."""

    last_tool_sig: str | None = None
    repeated_count: int = 0
    stagnation_score: int = 0
    loop_events: int = 0

    recent_turn_sigs: deque = field(default_factory=lambda: deque(maxlen=12))
    recent_step_sigs: deque = field(default_factory=lambda: deque(maxlen=36))
    recent_find_queries: deque = field(
        default_factory=lambda: deque(maxlen=max(2, STALL_FIND_WINDOW))
    )
    recent_nav_turns: deque = field(
        default_factory=lambda: deque(maxlen=max(1, STALL_NAV_GRACE_TURNS))
    )
    recent_domains: deque = field(default_factory=lambda: deque(maxlen=8))
    last_nav_url: str = ""

    live_tool_log: list = field(default_factory=list)
    stall_force_strikes: int = 0
    intervention_events: int = 0
    last_intervention_model_turn: int = -999
    hard_stop_guard: bool = False
    hard_stop_recovery_used: int = 0

    # ------------------------------------------------------------------
    # Loop detection
    # ------------------------------------------------------------------

    def check_loop(self, tool_calls_sig: str) -> tuple[bool, str, object]:
        """Check if tool calls are repeating.

        Returns (loop_detected, nudge_text, feedback_or_None).
        """
        if tool_calls_sig == self.last_tool_sig:
            self.repeated_count += 1
        else:
            self.repeated_count = 0
            self.last_tool_sig = tool_calls_sig

        if self.repeated_count < LOOP_SHORT_CIRCUIT_REPEAT_THRESHOLD:
            return False, "", None

        # Check navigation grace period
        recent_nav = any(self.recent_nav_turns)
        if recent_nav:
            self.repeated_count = 0
            self.last_tool_sig = None
            return False, "", None

        # Loop detected
        self.repeated_count = 0
        self.last_tool_sig = None
        self.loop_events += 1

        nudge = (
            "STOP. You have called the same tool(s) with the same arguments "
            "twice in a row without making progress. Do NOT call any more tools. "
            "Write a text response to the user summarising what you found or "
            "explaining what you were unable to do and why."
        )

        feedback = None
        if intervention_runtime_available() and self.live_tool_log:
            try:
                progress = score_tool_log(
                    self.live_tool_log,
                    final_answer="",
                    error_text="",
                    loops=self.loop_events,
                    in_progress=True,
                )
                feedback = build_intervention_feedback(
                    progress,
                    loops=self.loop_events,
                    in_progress=True,
                )
                if getattr(feedback, "feedback_prompt", ""):
                    nudge = feedback.feedback_prompt
            except Exception:
                pass

        return True, nudge, feedback

    # ------------------------------------------------------------------
    # Stagnation scoring
    # ------------------------------------------------------------------

    def update_stagnation(
        self,
        turn_step_sigs: list[str],
        turn_find_queries: list[str],
        turn_had_navigation: bool,
        turn_domain_switch: bool,
        turn_had_interaction: bool,
    ):
        """Update stagnation score based on a completed tool turn."""
        if not turn_step_sigs:
            return

        self.recent_nav_turns.append(turn_had_navigation or turn_domain_switch)

        repeated_steps = sum(
            1 for sig in turn_step_sigs if sig in self.recent_step_sigs
        )
        low_novelty = repeated_steps == len(turn_step_sigs)
        turn_sig = _hash_sig("|".join(turn_step_sigs))
        repeated_turn = turn_sig in self.recent_turn_sigs

        if repeated_turn:
            self.loop_events += 1
            self.stagnation_score += 2
        elif low_novelty:
            self.stagnation_score += 1
        else:
            self.stagnation_score = max(0, self.stagnation_score - 1)
            self.stall_force_strikes = max(0, self.stall_force_strikes - 1)

        if turn_had_navigation:
            self.stagnation_score = max(0, self.stagnation_score - 2)
            self.stall_force_strikes = 0
        elif turn_domain_switch:
            self.stagnation_score = max(0, self.stagnation_score - 2)
            self.stall_force_strikes = 0
        elif turn_had_interaction and not low_novelty:
            self.stagnation_score = max(0, self.stagnation_score - 1)

        self.recent_turn_sigs.append(turn_sig)
        self.recent_step_sigs.extend(turn_step_sigs)

        # Variety window check
        step_tail = list(self.recent_step_sigs)[-STALL_VARIETY_WINDOW:]
        if (
            len(step_tail) >= STALL_VARIETY_WINDOW
            and len(set(step_tail)) <= 2
        ):
            self.stagnation_score += 2

        # Find-query repetition check
        if turn_find_queries:
            self.recent_find_queries.extend(turn_find_queries)
            find_tail = list(self.recent_find_queries)[-STALL_FIND_WINDOW:]
            if (
                len(find_tail) >= STALL_FIND_WINDOW
                and len(set(find_tail)) <= STALL_FIND_DISTINCT_MAX
            ):
                self.stagnation_score += 1
        else:
            self.recent_find_queries.clear()

    # ------------------------------------------------------------------
    # Stall threshold check
    # ------------------------------------------------------------------

    def check_stall_threshold(self) -> tuple[str, str]:
        """Check if stagnation score exceeds threshold.

        Returns (action, guidance_text) where action is one of:
          - ""          → no action
          - "guidance"  → inject guidance text, continue
          - "force"     → force final response
        """
        if self.stagnation_score < STALL_SCORE_THRESHOLD:
            return "", ""

        recent_nav = any(self.recent_nav_turns)
        if recent_nav:
            self.stagnation_score = max(0, STALL_SCORE_THRESHOLD - 2)
            self.stall_force_strikes = 0
            return "", ""

        self.stall_force_strikes += 1
        if self.stall_force_strikes < max(1, STALL_FORCE_FINAL_STRIKES):
            self.stagnation_score = max(0, STALL_SCORE_THRESHOLD - 1)
            return "guidance", (
                "Continue exploring, but prioritize steps that extract concrete "
                "results (prices, dates, airlines, booking details) and avoid "
                "repeating broad scans without new evidence."
            )

        self.loop_events += 1
        return "force", (
            "I kept using browser tools but stopped making meaningful progress. "
            "Please let me try a different approach or rephrase the request."
        )

    # ------------------------------------------------------------------
    # Intervention (progress_critic)
    # ------------------------------------------------------------------

    def run_intervention(self, model_turn: int) -> tuple[bool, object]:
        """Run progress_critic intervention check.

        Returns (should_emit, feedback_or_None).
        """
        if not intervention_runtime_available() or not self.live_tool_log:
            return False, None

        try:
            progress = score_tool_log(
                self.live_tool_log,
                final_answer="",
                error_text="",
                loops=self.loop_events,
                in_progress=True,
            )
            feedback = build_intervention_feedback(
                progress,
                loops=self.loop_events,
                in_progress=True,
            )
            if should_emit_intervention(
                feedback=feedback,
                progress_turns=progress.turns,
                model_turn=model_turn,
                emitted_count=self.intervention_events,
                last_emit_model_turn=self.last_intervention_model_turn,
            ):
                return True, feedback
        except Exception:
            pass

        return False, None

    def apply_nudge_decay(self):
        """Decay stagnation score after a nudge intervention."""
        self.stagnation_score = max(
            0, int(round(self.stagnation_score * INTERVENTION_NUDGE_STALL_DECAY))
        )
        self.stall_force_strikes = 0

    def apply_nudge_reset(self):
        """Clear loop/stagnation tracking after a nudge intervention.

        Preserves live_tool_log for score_tool_log's EMA continuity,
        and decays loop_events instead of zeroing.
        """
        if not INTERVENTION_NUDGE_RESET_PROGRESS:
            return
        # DON'T clear live_tool_log — score_tool_log's EMA needs
        # the full trajectory to detect recovery vs continued stall.
        self.loop_events = max(0, self.loop_events - 2)  # decay, don't zero
        self.repeated_count = 0
        self.last_tool_sig = None
        self.recent_turn_sigs.clear()
        self.recent_step_sigs.clear()
        self.recent_find_queries.clear()
        self.recent_nav_turns.clear()
        self.recent_domains.clear()

    def should_extend_turns(self) -> bool:
        """Check if trajectory is healthy enough to extend the turn cap.

        Uses score_tool_log + build_intervention_feedback with in_progress=True.
        Returns True if no intervention is needed (severity == none/watch).
        """
        if self.hard_stop_guard:
            return False
        if not intervention_runtime_available() or not self.live_tool_log:
            return False
        try:
            progress = score_tool_log(
                self.live_tool_log, loops=self.loop_events, in_progress=True,
            )
            feedback = build_intervention_feedback(
                progress, loops=self.loop_events, in_progress=True,
            )
            # Extend only if trajectory is clean (no intervention or just watching)
            return feedback.severity in ("none", "watch")
        except Exception:
            return False
