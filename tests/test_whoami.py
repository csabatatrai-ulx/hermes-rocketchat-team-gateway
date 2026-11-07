"""Unit tests for whoami.py — the /whoami command's matching and reply text.

Pure logic only (no HTTP — the adapter gathers those inputs before calling
``build_whoami_reply``), the pure-logic/HTTP split the other command modules follow too.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whoami import (  # noqa: E402
    DEFAULT_WHOAMI_COMMAND,
    build_whoami_reply,
    is_whoami_command,
)


class TestIsWhoamiCommand(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(is_whoami_command("/whoami"))

    def test_surrounding_whitespace_ignored(self):
        # "@Hermes /whoami" arrives here with the mention already stripped,
        # leaving a leading space.
        self.assertTrue(is_whoami_command("  /whoami  "))

    def test_case_insensitive(self):
        self.assertTrue(is_whoami_command("/WHOAMI"))

    def test_prefix_is_not_a_match(self):
        # A real question that happens to start with the command word must
        # reach the model, not be swallowed by /whoami.
        self.assertFalse(is_whoami_command("/whoami or what else can you do?"))

    def test_empty_text(self):
        self.assertFalse(is_whoami_command(""))

    def test_default_command_constant(self):
        self.assertEqual(DEFAULT_WHOAMI_COMMAND, "/whoami")


class TestBuildWhoamiReply(unittest.TestCase):
    def test_username_and_emails_shown(self):
        text = build_whoami_reply(rc_username="alice", emails=["alice@example.com"])
        self.assertIn("alice", text)
        self.assertIn("alice@example.com", text)
        self.assertIn("authenticated", text)

    def test_no_email(self):
        text = build_whoami_reply(rc_username="alice", emails=[])
        self.assertIn("no known e-mail", text)

    def test_english_is_the_default(self):
        text = build_whoami_reply(rc_username="alice", emails=[])
        self.assertIn("**RC user:** `alice`", text)
        self.assertIn("no known e-mail", text)

    def test_username_always_shown(self):
        text = build_whoami_reply(rc_username="bob")
        self.assertIn("bob", text)


if __name__ == "__main__":
    unittest.main()
