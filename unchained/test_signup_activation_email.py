"""Activation-CTA contracts for approved-account email flows."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_app.handlers import auth_admin


ACTIVATION_URL = (
    "https://unchainedsky.com/install?utm_source=lifecycle_email"
    "&utm_medium=email&utm_campaign=approved_account_activation"
    "&ref=welcome_install"
)
LEGACY_CHAT_URL = "https://api.unchainedsky.com/chat"


class _JsonRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class TestSignupActivationEmail(unittest.IsolatedAsyncioTestCase):
    def test_auto_approved_email_uses_attributed_installer_cta(self):
        sent: list[tuple[str, str, str]] = []
        core = SimpleNamespace(
            ADMIN_EMAILS=set(),
            send_email=lambda *message: sent.append(message),
        )

        auth_admin._send_signup_emails(
            core,
            user={"status": "approved"},
            email="member@example.test",
            name="Member",
            user_type="claude",
            is_trial_branch=False,
        )

        self.assertEqual(len(sent), 1)
        recipient, subject, body = sent[0]
        self.assertEqual(recipient, "member@example.test")
        self.assertEqual(subject, "Unchained — You're in!")
        self.assertIn("Connect one computer to run browser tasks", body)
        self.assertIn(f'href="{ACTIVATION_URL}"', body)
        self.assertNotIn(LEGACY_CHAT_URL, body)

    def test_pending_and_trial_emails_remain_pending_messages(self):
        for is_trial_branch, expected in (
            (False, "We're reviewing it now"),
            (True, "Trial/Demo now"),
        ):
            with self.subTest(is_trial_branch=is_trial_branch):
                sent: list[tuple[str, str, str]] = []
                core = SimpleNamespace(
                    ADMIN_EMAILS=set(),
                    send_email=lambda *message: sent.append(message),
                )

                auth_admin._send_signup_emails(
                    core,
                    user={"status": "pending"},
                    email="pending@example.test",
                    name="Pending",
                    user_type="trial" if is_trial_branch else "claude",
                    is_trial_branch=is_trial_branch,
                )

                self.assertEqual(len(sent), 1)
                body = sent[0][2]
                self.assertIn(expected, body)
                self.assertNotIn(ACTIVATION_URL, body)

    async def test_manual_approval_uses_same_activation_destination(self):
        sent: list[tuple[str, str, str]] = []
        approved_user = {
            "user_id": "opaque-user-a",
            "email": "member@example.test",
            "name": "Member",
            "status": "approved",
        }
        core = SimpleNamespace(
            ADMIN_EMAILS={"admin@example.test"},
            _authenticate=lambda _request: {"email": "admin@example.test"},
            _auth=SimpleNamespace(approve_user=lambda _email: approved_user),
            send_email=lambda *message: sent.append(message),
        )

        with patch.object(auth_admin, "_core", return_value=core):
            response = await auth_admin.handle_admin_approve(
                _JsonRequest({"email": "MEMBER@example.test"})
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(sent), 1)
        body = sent[0][2]
        self.assertIn(f'href="{ACTIVATION_URL}"', body)
        self.assertNotIn(LEGACY_CHAT_URL, body)


if __name__ == "__main__":
    unittest.main()
