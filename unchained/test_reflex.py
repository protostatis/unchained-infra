"""Tests for reflex.py — lightweight single-turn coaching layer."""

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Mock cdp module before importing reflex (same pattern as test_dom_density)
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock

_mock_tt = MagicMock()
_mock_tt._select_profile = MagicMock()
_mock_tt.CDP = MagicMock
_mock_tt._get_ws_url = MagicMock()
_mock_tt._ensure_chrome = MagicMock()
_mock_tt._list_tabs = MagicMock(return_value=[])
_mock_tt._get_ws_url_for_tab = MagicMock()
_mock_tt._create_new_tab = MagicMock()
_mock_tt._close_tab = MagicMock()
sys.modules.setdefault("cdp", _mock_tt)

from reflex import (
    ReflexState,
    REFLEX_ENABLED,
    REFLEX_MAX_HINTS,
    parse_ddm_elements,
    extract_goal_keywords,
    _distance,
)


# ---------------------------------------------------------------------------
# A: parse_ddm_elements
# ---------------------------------------------------------------------------


class TestParseDdmElements(unittest.TestCase):
    """A – Parse Label@x,y from --llm-2pass output."""

    def test_basic_elements(self):
        output = "Google: >Search@960,300|Google Search@960,400|I'm Feeling Lucky@960,450"
        elems = parse_ddm_elements(output)
        self.assertEqual(len(elems), 3)
        self.assertEqual(elems[0]["label"], "Search")
        self.assertEqual(elems[0]["x"], 960)
        self.assertEqual(elems[0]["y"], 300)
        self.assertTrue(elems[0]["is_input"])
        self.assertEqual(elems[1]["label"], "Google Search")
        self.assertFalse(elems[1]["is_input"])

    def test_with_tab_header(self):
        output = "Tab: A1B2C3D4\nSlickdeals: >Search Slickdeals@540,62|Today's Deals@180,120"
        elems = parse_ddm_elements(output)
        self.assertEqual(len(elems), 2)
        self.assertEqual(elems[0]["label"], "Search Slickdeals")
        self.assertTrue(elems[0]["is_input"])

    def test_empty_output(self):
        elems = parse_ddm_elements("")
        self.assertEqual(elems, [])

    def test_no_elements_just_title(self):
        elems = parse_ddm_elements("My Page:")
        self.assertEqual(elems, [])

    def test_single_element(self):
        elems = parse_ddm_elements("page: Submit@50,25")
        self.assertEqual(len(elems), 1)
        self.assertEqual(elems[0]["label"], "Submit")
        self.assertEqual(elems[0]["x"], 50)
        self.assertEqual(elems[0]["y"], 25)
        self.assertFalse(elems[0]["is_input"])


# ---------------------------------------------------------------------------
# B: extract_goal_keywords
# ---------------------------------------------------------------------------


class TestExtractGoalKeywords(unittest.TestCase):
    """B – Tokenise user message, filter stopwords."""

    def test_basic_extraction(self):
        kws = extract_goal_keywords("Find the search bar on Slickdeals")
        self.assertIn("bar", kws)
        self.assertIn("slickdeals", kws)
        # Stopwords excluded
        self.assertNotIn("the", kws)
        self.assertNotIn("on", kws)
        self.assertNotIn("find", kws)

    def test_empty_message(self):
        self.assertEqual(extract_goal_keywords(""), [])

    def test_all_stopwords(self):
        kws = extract_goal_keywords("find the search")
        # "search" is a stopword in our list
        self.assertEqual(kws, [])

    def test_preserves_numbers(self):
        kws = extract_goal_keywords("navigate to page 3")
        self.assertIn("3", kws)
        self.assertIn("navigate", kws)
        self.assertIn("page", kws)


# ---------------------------------------------------------------------------
# C: Trigger 1 — type_text "no input focused"
# ---------------------------------------------------------------------------


class TestTriggerTypeTextNoFocus(unittest.TestCase):
    """C – type_text fails with 'no input focused'."""

    def test_no_focus_with_cached_inputs(self):
        rs = ReflexState()
        # Simulate a prior --llm-2pass that found inputs
        ddm_output = "page: >Search@540,62|>Email@540,120|Submit@540,180"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass --cols 60"}, ddm_output)

        hint = rs.on_tool_result(
            "type_text", {"text": "hello"}, "Error: no input focused"
        )
        self.assertIsNotNone(hint)
        self.assertIn("[Reflex]", hint)
        self.assertIn("no input is focused", hint)
        self.assertIn("Search@540,62", hint)
        self.assertIn("Email@540,120", hint)

    def test_no_focus_without_cached_elements(self):
        rs = ReflexState()
        hint = rs.on_tool_result(
            "type_text", {"text": "hello"}, "Error: no input focused"
        )
        self.assertIsNotNone(hint)
        self.assertIn("Run ddm --llm-2pass", hint)

    def test_type_text_success_no_hint(self):
        rs = ReflexState()
        hint = rs.on_tool_result(
            "type_text", {"text": "hello"}, "--- changed ---\nTyped into input"
        )
        self.assertIsNone(hint)


# ---------------------------------------------------------------------------
# D: Trigger 2 — click "--- no change ---" streak
# ---------------------------------------------------------------------------


class TestTriggerClickNoChange(unittest.TestCase):
    """D – click fails twice consecutively."""

    def test_first_no_change_no_hint(self):
        rs = ReflexState()
        hint = rs.on_tool_result("click", {"x": 100, "y": 200}, "--- no change ---")
        self.assertIsNone(hint)

    def test_second_no_change_hints(self):
        rs = ReflexState()
        rs.on_tool_result("click", {"x": 100, "y": 200}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 150, "y": 250}, "--- no change ---")
        self.assertIsNotNone(hint)
        self.assertIn("[Reflex]", hint)
        self.assertIn("Two clicks produced no change", hint)

    def test_streak_resets_on_success(self):
        rs = ReflexState()
        rs.on_tool_result("click", {"x": 100, "y": 200}, "--- no change ---")
        # Successful click resets streak
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- changed ---\nNew content")
        # Next no-change should NOT trigger (streak reset)
        hint = rs.on_tool_result("click", {"x": 100, "y": 200}, "--- no change ---")
        self.assertIsNone(hint)

    def test_no_change_with_goal_match(self):
        rs = ReflexState()
        rs.set_user_goal("search for deals on Slickdeals")
        ddm_output = "page: >Search Deals@540,62|Deals@180,120|Categories@400,120"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        rs.on_tool_result("click", {"x": 900, "y": 500}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 900, "y": 550}, "--- no change ---")
        self.assertIsNotNone(hint)
        # "deals" and "slickdeals" are keywords; "Search Deals" and "Deals" match
        self.assertIn("Search Deals@540,62", hint)
        self.assertIn("Deals@180,120", hint)


# ---------------------------------------------------------------------------
# E: Trigger 3 — blind --at probing
# ---------------------------------------------------------------------------


class TestTriggerBlindProbe(unittest.TestCase):
    """E – 3+ ddm --at without prior --llm-2pass."""

    def test_two_probes_no_hint(self):
        rs = ReflexState()
        rs.on_tool_result("ddm", {"flags": "--at 100,200"}, "elem info...")
        hint = rs.on_tool_result("ddm", {"flags": "--at 200,300"}, "elem info...")
        self.assertIsNone(hint)

    def test_three_probes_triggers(self):
        rs = ReflexState()
        rs.on_tool_result("ddm", {"flags": "--at 100,200"}, "elem info...")
        rs.on_tool_result("ddm", {"flags": "--at 200,300"}, "elem info...")
        hint = rs.on_tool_result("ddm", {"flags": "--at 300,400"}, "elem info...")
        self.assertIsNotNone(hint)
        self.assertIn("[Reflex]", hint)
        self.assertIn("ddm --llm-2pass", hint)

    def test_2pass_resets_probe_streak(self):
        rs = ReflexState()
        rs.on_tool_result("ddm", {"flags": "--at 100,200"}, "elem info...")
        rs.on_tool_result("ddm", {"flags": "--at 200,300"}, "elem info...")
        # Running 2pass resets the streak
        rs.on_tool_result(
            "ddm", {"flags": "--llm-2pass"}, "page: Submit@50,25"
        )
        rs.on_tool_result("ddm", {"flags": "--at 100,200"}, "elem info...")
        hint = rs.on_tool_result("ddm", {"flags": "--at 200,300"}, "elem info...")
        self.assertIsNone(hint)  # Only 2 probes after 2pass, not 3

    def test_probe_after_prior_2pass_no_trigger(self):
        rs = ReflexState()
        # First run 2pass
        rs.on_tool_result(
            "ddm", {"flags": "--llm-2pass --cols 60"}, "page: Submit@50,25"
        )
        # Now probing is fine because we have 2pass context
        rs.on_tool_result("ddm", {"flags": "--at 100,200"}, "elem info...")
        rs.on_tool_result("ddm", {"flags": "--at 200,300"}, "elem info...")
        hint = rs.on_tool_result("ddm", {"flags": "--at 300,400"}, "elem info...")
        self.assertIsNone(hint)


# ---------------------------------------------------------------------------
# F: Trigger 4 — click far from DDM elements
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# E2: Trigger 5 — SPA widget (same element clicked 3+ times)
# ---------------------------------------------------------------------------


class TestTriggerSpaWidget(unittest.TestCase):
    """E2 – same element clicked 3+ times within 20px with no change."""

    def test_spa_widget_same_coords_3x(self):
        rs = ReflexState()
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        self.assertIsNotNone(hint)
        self.assertIn("[Reflex]", hint)
        self.assertIn("SPA widget", hint)
        self.assertIn("js_eval", hint)

    def test_spa_widget_nearby_coords(self):
        """Clicks within 20px count as same element."""
        rs = ReflexState()
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        rs.on_tool_result("click", {"x": 305, "y": 403}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 298, "y": 397}, "--- no change ---")
        self.assertIsNotNone(hint)
        self.assertIn("SPA widget", hint)

    def test_spa_widget_different_coords_no_trigger(self):
        """Clicks >20px apart trigger Trigger 2 instead of Trigger 5."""
        rs = ReflexState()
        rs.on_tool_result("click", {"x": 100, "y": 200}, "--- no change ---")
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 500, "y": 600}, "--- no change ---")
        # Should NOT fire trigger 5 (SPA widget) — coords are far apart
        if hint:
            self.assertNotIn("SPA widget", hint)

    def test_spa_widget_resets_on_success(self):
        """Successful click resets SPA widget counter."""
        rs = ReflexState()
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        # Successful click resets
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- changed ---\nNew content")
        # Now start again — should need 3 more to trigger
        rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 300, "y": 400}, "--- no change ---")
        # Only 2 no-change clicks after reset — trigger 2 (generic) may fire but not trigger 5
        if hint:
            self.assertNotIn("SPA widget", hint)


class TestTriggerFarClick(unittest.TestCase):
    """F – click at (x,y) >80px from any DDM element."""

    def test_far_click_warns(self):
        rs = ReflexState()
        ddm_output = "page: Submit@100,100|Cancel@200,100"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        # Click at (500, 500) — far from both elements
        hint = rs.on_tool_result(
            "click", {"x": 500, "y": 500}, "--- changed ---\nSome content"
        )
        self.assertIsNotNone(hint)
        self.assertIn("[Reflex]", hint)
        self.assertIn("from any interactive element", hint)
        self.assertIn("Nearest elements", hint)

    def test_close_click_silent(self):
        rs = ReflexState()
        ddm_output = "page: Submit@100,100|Cancel@200,100"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        # Click at (120, 110) — within 80px of Submit@100,100
        hint = rs.on_tool_result(
            "click", {"x": 120, "y": 110}, "--- changed ---\nClicked"
        )
        self.assertIsNone(hint)

    def test_far_click_no_elements_no_hint(self):
        rs = ReflexState()
        # No prior 2pass → no cached elements → skip far-click check
        hint = rs.on_tool_result(
            "click", {"x": 500, "y": 500}, "--- changed ---\nClicked"
        )
        self.assertIsNone(hint)

    def test_exactly_80px_silent(self):
        rs = ReflexState()
        ddm_output = "page: Submit@100,100"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        # Click at (180, 100) — exactly 80px away
        hint = rs.on_tool_result(
            "click", {"x": 180, "y": 100}, "--- changed ---\nClicked"
        )
        self.assertIsNone(hint)


# ---------------------------------------------------------------------------
# G: Max hints cap
# ---------------------------------------------------------------------------


class TestMaxHintsCap(unittest.TestCase):
    """G – Stop emitting after REFLEX_MAX_HINTS."""

    def test_stops_after_max_hints(self):
        rs = ReflexState()
        hints = []
        for i in range(REFLEX_MAX_HINTS + 2):
            h = rs.on_tool_result(
                "type_text", {"text": "x"}, "Error: no input focused"
            )
            if h:
                hints.append(h)
        self.assertEqual(len(hints), REFLEX_MAX_HINTS)


# ---------------------------------------------------------------------------
# H: REFLEX_ENABLED toggle
# ---------------------------------------------------------------------------


class TestReflexEnabled(unittest.TestCase):
    """H – Hints suppressed when REFLEX_ENABLED is False."""

    def test_disabled_returns_none(self):
        import reflex

        original = reflex.REFLEX_ENABLED
        try:
            reflex.REFLEX_ENABLED = False
            rs = ReflexState()
            hint = rs.on_tool_result(
                "type_text", {"text": "x"}, "Error: no input focused"
            )
            self.assertIsNone(hint)
        finally:
            reflex.REFLEX_ENABLED = original


# ---------------------------------------------------------------------------
# I: Element cache updates
# ---------------------------------------------------------------------------


class TestElementCacheUpdates(unittest.TestCase):
    """I – DDM element cache refreshes on each --llm-2pass."""

    def test_cache_refreshed_on_new_2pass(self):
        rs = ReflexState()
        rs.on_tool_result(
            "ddm", {"flags": "--llm-2pass"}, "page: OldBtn@100,100"
        )
        self.assertEqual(len(rs._elements), 1)
        self.assertEqual(rs._elements[0]["label"], "OldBtn")

        rs.on_tool_result(
            "ddm", {"flags": "--llm-2pass"}, "page: NewBtn@200,200|Other@300,300"
        )
        self.assertEqual(len(rs._elements), 2)
        self.assertEqual(rs._elements[0]["label"], "NewBtn")


# ---------------------------------------------------------------------------
# J: Goal keyword matching
# ---------------------------------------------------------------------------


class TestGoalKeywordMatching(unittest.TestCase):
    """J – _match_elements_to_goal uses substring match."""

    def test_matches_partial_label(self):
        rs = ReflexState()
        rs.set_user_goal("search for something")
        ddm_output = "page: >Search Bar@540,62|Home@100,50|About@200,50"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        # Force a double no-change to trigger hint with goal matching
        rs.on_tool_result("click", {"x": 900, "y": 900}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 900, "y": 900}, "--- no change ---")
        self.assertIn("Search Bar@540,62", hint)

    def test_no_goal_falls_back_to_nearest(self):
        rs = ReflexState()
        # No goal set — keywords are empty
        ddm_output = "page: Submit@100,100|Cancel@200,100"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        rs.on_tool_result("click", {"x": 150, "y": 100}, "--- no change ---")
        hint = rs.on_tool_result("click", {"x": 150, "y": 100}, "--- no change ---")
        self.assertIsNotNone(hint)
        # Falls back to nearest elements
        self.assertIn("Nearest interactive elements", hint)


# ---------------------------------------------------------------------------
# K: Distance helper
# ---------------------------------------------------------------------------


class TestDistanceHelper(unittest.TestCase):
    """K – Euclidean distance helper."""

    def test_zero_distance(self):
        self.assertAlmostEqual(_distance(100, 200, 100, 200), 0.0)

    def test_horizontal(self):
        self.assertAlmostEqual(_distance(0, 0, 80, 0), 80.0)

    def test_diagonal(self):
        self.assertAlmostEqual(_distance(0, 0, 3, 4), 5.0)


# ---------------------------------------------------------------------------
# L: Nearest elements
# ---------------------------------------------------------------------------


class TestNearestElements(unittest.TestCase):
    """L – _nearest_elements returns sorted by distance."""

    def test_sorted_by_distance(self):
        rs = ReflexState()
        ddm_output = "page: A@100,100|B@500,500|C@200,200"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        nearest = rs._nearest_elements(110, 110, n=3)
        self.assertEqual(len(nearest), 3)
        self.assertEqual(nearest[0]["label"], "A")
        self.assertEqual(nearest[2]["label"], "B")

    def test_empty_elements(self):
        rs = ReflexState()
        self.assertEqual(rs._nearest_elements(100, 100), [])


# ---------------------------------------------------------------------------
# M: No-change click + far-click interaction
# ---------------------------------------------------------------------------


class TestNoChangeAndFarClickInteraction(unittest.TestCase):
    """M – no-change streak takes precedence over far-click check."""

    def test_no_change_on_far_click(self):
        rs = ReflexState()
        ddm_output = "page: Submit@100,100"
        rs.on_tool_result("ddm", {"flags": "--llm-2pass"}, ddm_output)

        # Far click with no-change: first one → no hint (streak=1)
        hint1 = rs.on_tool_result(
            "click", {"x": 500, "y": 500}, "--- no change ---"
        )
        self.assertIsNone(hint1)

        # Second no-change → trigger 2 (no-change streak) takes priority
        hint2 = rs.on_tool_result(
            "click", {"x": 600, "y": 600}, "--- no change ---"
        )
        self.assertIsNotNone(hint2)
        self.assertIn("Two clicks produced no change", hint2)


if __name__ == "__main__":
    unittest.main()
