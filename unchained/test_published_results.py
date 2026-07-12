"""Contracts behind the privacy policy's public-result disclosures."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import published_results
import web


class TestPublishedResultDisclosureContracts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "published-results.db"
        self.original_db_path = published_results._DB_PATH
        published_results._DB_PATH = str(self.db_path)

    def tearDown(self):
        published_results._DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _session(query: str) -> dict:
        return {
            "messages": [
                {"role": "user", "content": query},
                {"role": "assistant", "content": "Detailed public result. " * 20},
            ]
        }

    def _publish(self, query: str) -> str:
        with patch.object(published_results, "_pii_guard", return_value=True):
            slug = published_results.publish_result(
                self._session(query),
                user_id="account@example.test",
                session_id="s-account-session",
            )
        self.assertIsNotNone(slug)
        return str(slug)

    def test_pending_record_persists_disclosed_content_and_metadata(self):
        slug = self._publish("Compare public launch options")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT query, result_html, result_text, meta_json, user_id, "
                "session_id, status, query_hash, view_count "
                "FROM published_results WHERE slug = ?",
                (slug,),
            ).fetchone()

        self.assertEqual(row[0], "Compare public launch options")
        self.assertIn("Detailed public result.", row[1])
        self.assertIn("Detailed public result.", row[2])
        self.assertEqual(json.loads(row[3]), {"message_count": 2})
        self.assertEqual(row[4], "account@example.test")
        self.assertEqual(row[5], "s-account-session")
        self.assertEqual(row[6], "pending")
        self.assertTrue(row[7])
        self.assertEqual(row[8], 0)
        self.assertIsNone(published_results.get_result(slug))

    def test_approval_makes_result_public_and_adds_it_to_sitemap(self):
        slug = self._publish("Compare public indexing options")

        self.assertTrue(published_results.approve_result(slug))
        self.assertIsNotNone(published_results.get_result(slug))
        self.assertIn(slug, {row["slug"] for row in published_results.list_results()})

        sitemap = asyncio.run(web.handle_sitemap_xml(None))
        self.assertIn(f"https://unchainedsky.com/r/{slug}", sitemap.text)

    def test_rejection_deletes_pending_but_not_approved_result(self):
        pending_slug = self._publish("Compare pending deletion behavior")
        self.assertTrue(published_results.reject_result(pending_slug, "review"))

        approved_slug = self._publish("Compare approved deletion behavior")
        self.assertTrue(published_results.approve_result(approved_slug))
        self.assertFalse(published_results.reject_result(approved_slug, "review"))

        with sqlite3.connect(self.db_path) as conn:
            rows = dict(
                conn.execute("SELECT slug, status FROM published_results").fetchall()
            )
        self.assertNotIn(pending_slug, rows)
        self.assertEqual(rows[approved_slug], "approved")

    def test_pii_guard_sends_prompt_and_limited_result_to_openrouter(self):
        response = Mock(is_success=True)
        response.json.return_value = {
            "choices": [{"message": {"content": "SAFE"}}]
        }
        assistant_output = "x" * 9000

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch("httpx.post", return_value=response) as post:
                self.assertTrue(
                    published_results._pii_guard(
                        "Classify this publication prompt",
                        assistant_output,
                    )
                )

        self.assertEqual(post.call_args.args[0], published_results._OPENROUTER_URL)
        payload = post.call_args.kwargs["json"]
        classifier_input = payload["messages"][1]["content"]
        self.assertIn("Classify this publication prompt", classifier_input)
        self.assertIn("x" * 8000, classifier_input)
        self.assertNotIn("x" * 8001, classifier_input)


if __name__ == "__main__":
    unittest.main()
