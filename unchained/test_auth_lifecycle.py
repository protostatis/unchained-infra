"""Account lifecycle: rejection must actually revoke access."""

import os
import tempfile
import unittest

from auth import Auth


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Auth(os.path.join(self.tmp.name, "auth.db"))
        self.user = self.auth.get_or_create_user("user@example.test", name="U")
        self.user_id = self.user["user_id"]
        self.key = self.user["api_key"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_reject_deactivates_every_key(self):
        second = self.auth.create_key(self.user_id)
        self.assertTrue(self.auth.reject_user(self.user["email"]))
        self.assertFalse(self.auth.is_key_active(self.key))
        self.assertFalse(self.auth.is_key_active(second))
        self.assertIsNone(self.auth.validate_key(self.key))
        self.assertIsNone(self.auth.validate_key(second))

    def test_reject_sets_status(self):
        self.auth.reject_user(self.user["email"])
        self.assertEqual(self.auth.get_user_status(self.user["email"]), "rejected")

    def test_reject_unknown_user_is_false(self):
        self.assertFalse(self.auth.reject_user("nobody@example.test"))

    def test_reapproval_issues_a_fresh_key(self):
        self.auth.reject_user(self.user["email"])
        approved = self.auth.approve_user(self.user["email"])
        self.assertEqual(approved["status"], "approved")
        self.assertNotEqual(approved["api_key"], self.key)
        self.assertTrue(self.auth.is_key_active(approved["api_key"]))
        self.assertFalse(self.auth.is_key_active(self.key))
        self.assertIsNotNone(self.auth.validate_key(approved["api_key"]))

    def test_create_key_refused_for_rejected_account(self):
        self.auth.reject_user(self.user["email"])
        with self.assertRaises(ValueError):
            self.auth.create_key(self.user_id)
        self.assertEqual(self.auth.get_keys_for_user(self.user_id), [])

    def test_create_key_allowed_for_pending_account(self):
        pending = self.auth.create_pending_user("pending@example.test")
        # Pending accounts keep the pre-existing trial behaviour (a key is
        # created on approval, but creation itself is not blocked).
        key = self.auth.create_key(pending["user_id"])
        self.assertTrue(self.auth.is_key_active(key))

    def test_key_only_identity_still_works(self):
        # Some service identities predate the users table.
        key = self.auth.create_key("service-legacy")
        self.assertTrue(self.auth.is_key_active(key))
        self.assertEqual(self.auth.validate_key(key)["user_id"], "service-legacy")

    def test_status_defaults_to_approved_when_null(self):
        with self.auth._conn() as conn:
            conn.execute(
                "UPDATE users SET status = NULL WHERE user_id = ?", (self.user_id,)
            )
        self.assertEqual(self.auth.find_user_by_id(self.user_id)["status"], "approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
