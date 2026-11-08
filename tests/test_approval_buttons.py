"""Accept and deny buttons on the dangerous-command prompt (approval_buttons.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approval_buttons import approval_attachments, confirm_attachments, confirm_body, is_approval_answer  # noqa: E402

ACTIONS = [("Allow once", "once", "primary"), ("Allow session", "session", ""),
           ("Always", "always", ""), ("Deny", "deny", "danger")]


class ApprovalAttachmentTests(unittest.TestCase):
    def _buttons(self, allowed):
        attachments = approval_attachments(ACTIONS, "hermes", frozenset(allowed))
        return [b for row in attachments for b in row["actions"]]

    def test_default_allowlist_offers_accept_and_deny_only(self):
        buttons = self._buttons({"approve", "deny"})
        self.assertEqual([b["msg"] for b in buttons], ["@hermes /approve", "@hermes /deny"])

    def test_persistent_choices_need_their_own_allow_key(self):
        msgs = [b["msg"] for b in self._buttons({"approve", "deny", "always"})]
        self.assertIn("@hermes /approve session", msgs)
        self.assertIn("@hermes /approve always", msgs)

    def test_more_than_two_buttons_go_two_per_row(self):
        rows = approval_attachments(ACTIONS, "hermes", frozenset({"approve", "deny", "always"}))
        self.assertEqual([len(r["actions"]) for r in rows], [2, 2])
        self.assertEqual([b["text"] for r in rows for b in r["actions"]],
                         ["Allow once", "Allow session", "Always", "Deny"])
        self.assertEqual(len(approval_attachments(ACTIONS, "hermes", frozenset({"approve", "deny"}))), 1)

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


class ConfirmAttachmentTests(unittest.TestCase):
    def _buttons(self, allowed):
        return [(b["text"], b["msg"]) for row in confirm_attachments("hermes", frozenset(allowed)) for b in row["actions"]]

    def test_always_gets_no_button_by_default(self):
        self.assertEqual(self._buttons({"approve", "deny", "cancel"}),
                         [("Approve Once", "@hermes /approve"), ("Cancel", "@hermes /deny")])

    def test_always_gets_a_button_when_allowed(self):
        self.assertIn(("Always Approve", "@hermes /always"), self._buttons({"approve", "deny", "always"}))

    def test_nothing_allowed_means_no_attachment(self):
        self.assertEqual(confirm_attachments("hermes", frozenset()), [])


class ConfirmBodyTests(unittest.TestCase):
    PROMPT = ("⚠️ **Confirm /new**\n\nThis starts a fresh session.\n\nChoose:\n• **Approve Once** — proceed\n"
              "• **Always Approve** — silence\n• **Cancel** — keep\n\n_Text fallback: reply `/approve`._")

    def test_the_typed_answers_are_cut(self):
        self.assertEqual(confirm_body(self.PROMPT), "⚠️ **Confirm /new**\n\nThis starts a fresh session.")

    def test_another_language_stays_whole(self):
        self.assertEqual(confirm_body("Bestätigen?\n\nWählen: ..."), "Bestätigen?\n\nWählen: ...")

    def test_a_cancel_is_an_answer(self):
        self.assertTrue(is_approval_answer("/cancel"))


if __name__ == "__main__":
    unittest.main()
