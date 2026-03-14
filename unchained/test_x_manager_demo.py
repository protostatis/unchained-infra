"""Tests for the toy X manager demo helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch
import types

from web_app.handlers import x_manager_demo as demo


class TestXManagerDemoHelpers(unittest.TestCase):
    def test_normalize_handle(self):
        self.assertEqual(demo._normalize_handle("@OpenAI"), "OpenAI")
        self.assertEqual(demo._normalize_handle("bad-handle"), "")
        self.assertEqual(demo._normalize_handle(""), "")

    def test_resolve_profile_auth(self):
        auth_info = {"agent_id": "claude-abc12345", "key_hash": "abc12345"}
        default_auth = demo._resolve_profile_auth(auth_info, "")
        guest_auth = demo._resolve_profile_auth(auth_info, "guest")
        self.assertEqual(default_auth["agent_id"], "claude-abc12345")
        self.assertEqual(default_auth["profile"], "default")
        self.assertEqual(guest_auth["agent_id"], "claude-abc12345-guest")
        self.assertEqual(guest_auth["profile"], "guest")
        with self.assertRaises(ValueError):
            demo._resolve_profile_auth(auth_info, "bad profile")

    def test_extract_focus_terms_filters_handle_noise(self):
        terms = demo._extract_focus_terms(
            "I want @myhandle to grow with founders and engineers through product strategy and shipping lessons",
            "myhandle",
            ["lennysan"],
        )
        self.assertIn("founders", terms)
        self.assertIn("engineers", terms)
        self.assertIn("product", terms)
        self.assertNotIn("myhandle", terms)

    def test_choose_action_prefers_profile_first(self):
        stats = {name: demo.ActionStat() for name in demo.ACTION_ORDER}
        action, debug = demo._choose_action(
            step_index=0,
            stats=stats,
            focus_terms=["product", "strategy"],
            competitors=["lennysan"],
            last_action="",
            last_reward=0.0,
            have_evidence=False,
        )
        self.assertEqual(action, "profile_scan")
        self.assertGreater(debug["profile_scan"]["score"], debug["mention_scan"]["score"])

    def test_score_snapshot_rewards_relevant_live_topic(self):
        seen_tokens = {"generic", "advice"}
        rich_snapshot = {
            "title": "Search / X",
            "page_type": "search",
            "article_count": 4,
            "articles": [
                {"text": "Founders discussing product strategy and shipping tradeoffs", "links": ["/alice/status/1"]},
                {"text": "Engineers debating product quality and shipping speed", "links": ["/bob/status/2"]},
            ],
            "text_excerpt": "product strategy founders engineers shipping",
        }
        weak_snapshot = {
            "title": "Sign in to X",
            "page_type": "search",
            "article_count": 0,
            "articles": [],
            "text_excerpt": "",
            "login_gate": True,
        }
        rich_reward, _ = demo._score_snapshot(
            action="topic_scan",
            snapshot=rich_snapshot,
            focus_terms=["product", "strategy", "founders"],
            seen_tokens=seen_tokens,
        )
        weak_reward, _ = demo._score_snapshot(
            action="topic_scan",
            snapshot=weak_snapshot,
            focus_terms=["product", "strategy", "founders"],
            seen_tokens=seen_tokens,
        )
        self.assertGreater(rich_reward["total"], weak_reward["total"])
        self.assertGreater(rich_reward["intent_alignment"], weak_reward["intent_alignment"])
        self.assertGreater(weak_reward["risk"], rich_reward["risk"])

    def test_resolve_local_profile_path_matches_dir_name_and_display_name(self):
        fake_signup_agent = types.SimpleNamespace(
            list_chrome_profiles=lambda: [
                {
                    "path": "/Users/example/Library/Application Support/Google/Chrome/Profile 5",
                    "dir_name": "Profile 5",
                    "name": "UnchainedSky",
                }
            ]
        )
        with patch.dict("sys.modules", {"signup_agent": fake_signup_agent}):
            self.assertEqual(
                demo._resolve_local_profile_path("Profile 5"),
                "/Users/example/Library/Application Support/Google/Chrome/Profile 5",
            )
            self.assertEqual(
                demo._resolve_local_profile_path("UnchainedSky"),
                "/Users/example/Library/Application Support/Google/Chrome/Profile 5",
            )

    def test_normalize_ddm_flags_accepts_string_and_sequence(self):
        self.assertEqual(
            demo._normalize_ddm_flags("--text --max 1600"),
            ["--text", "--max", "1600"],
        )
        self.assertEqual(
            demo._normalize_ddm_flags(["--text", "--max", 1600]),
            ["--text", "--max", "1600"],
        )


if __name__ == "__main__":
    unittest.main()
