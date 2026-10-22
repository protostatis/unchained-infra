"""The device-approval page both clients share.

The installer and the desktop app land on the same page (`/install/claim/<id>`), so its
words have to be true for both. "Installer" alone was wrong for the app; promising that
setup finishes by itself was wrong for either, because the client still has to poll and
bootstrap; and a signed-out stranger could open the page and be invited to approve with no
warning at all.

These assertions are deliberately about the served text, not a snapshot: they name the
things that must stay true, and the one that must change when `/install` becomes a neutral
options page.
"""

import unittest

from web_app.templates import INSTALL_CLAIM_HTML


class ClaimPageCopyTests(unittest.TestCase):
    def test_it_identifies_the_product_and_both_clients(self):
        self.assertIn("Approve the Unchained app or installer", INSTALL_CLAIM_HTML)
        self.assertIn('class="brand"', INSTALL_CLAIM_HTML)

    def test_approval_does_not_promise_that_setup_finishes_by_itself(self):
        self.assertIn(
            "Return to the Unchained app or installer to finish setup.",
            INSTALL_CLAIM_HTML,
        )
        self.assertNotIn("automatically", INSTALL_CLAIM_HTML)

    def test_a_stranger_is_told_not_to_approve(self):
        self.assertIn(
            "Only approve this if you started setting up Unchained yourself",
            INSTALL_CLAIM_HTML,
        )

    def test_installer_only_wording_is_gone(self):
        for phrase in (
            "Authorize This Installer",
            "installer will fetch",
            "Return to the installer window",
            "approve this installer",
        ):
            self.assertNotIn(phrase, INSTALL_CLAIM_HTML, phrase)

    def test_the_install_link_does_not_pretend_to_be_neutral(self):
        # `/install` is still the agent installer's page. When the desktop app becomes a
        # third install option, this assertion is the reminder to update the label.
        self.assertIn("View agent installer page", INSTALL_CLAIM_HTML)


if __name__ == "__main__":
    unittest.main()
