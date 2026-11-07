"""Accept and deny buttons on the dangerous-command prompt (approval_buttons.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approval_buttons import approval_attachments, is_approval_answer  # noqa: E402

ACTIONS = [("Allow once", "once", "primary"), ("Allow session", "session", ""),
           ("Always", "always", ""), ("Deny", "deny", "danger")]


class ApprovalAttachmentTests(unittest.TestCase):
    def _buttons(self, allowed):
        attachments = approval_attachments(ACTIONS, "hermes", frozenset(allowed))
        return attachments[0]["actions"] if attachments else []

    def test_default_allowlist_offers_accept_and_deny_only(self):
        buttons = self._buttons({"approve", "deny"})
        self.assertEqual([b["msg"] for b in buttons], ["@hermes /approve", "@hermes /deny"])

    def test_persistent_choices_need_their_own_allow_key(self):
        msgs = [b["msg"] for b in self._buttons({"approve", "deny", "always"})]
        self.assertIn("@hermes /approve session", msgs)
        self.assertIn("@hermes /approve always", msgs)

    def test_a_blocked_command_gets_no_button(self):
        self.assertEqual([b["msg"] for b in self._buttons({"deny"})], ["@hermes /deny"])
        self.assertEqual(approval_attachments(ACTIONS, "hermes", frozenset()), [])

    def test_a_click_is_sent_as_the_person_who_clicked(self):
        button = self._buttons({"approve"})[0]
        self.assertTrue(button["msg_in_chat_window"])
        self.assertEqual(button["msg_processing_type"], "sendMessage")
        self.assertEqual(button["text"], "Allow once")

    def test_an_unknown_choice_gets_no_button(self):
        self.assertEqual(approval_attachments([("?", "new-tier", "")], "hermes", frozenset({"approve"})), [])


class IsApprovalAnswerTests(unittest.TestCase):
    def test_answers(self):
        for text in ("/approve", "/deny", " /APPROVE session", "/deny because", "/approve@hermes"):
            self.assertTrue(is_approval_answer(text), text)

    def test_other_text(self):
        for text in ("", "approve", "please /approve", "/approved", "/new"):
            self.assertFalse(is_approval_answer(text), text)


if __name__ == "__main__":
    unittest.main()
