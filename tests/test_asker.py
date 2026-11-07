"""Asker attestation (asker.py): the format and signature match the shared test vector, and the context gets the line after the room_id line."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asker import ASKER_TTL_SECONDS, asker_line, issue_asker, sign_asker  # noqa: E402
from history import build_channel_context  # noqa: E402

# The vector that an attestation-verifying MCP server should also pass.
VECTOR = "v1.u1AbC.roomXyZ9.1800007200.hX9Ikg0WAONqlZdCCGsoga"


class AskerTests(unittest.TestCase):
    def test_shared_vector(self):
        self.assertEqual(sign_asker("x" * 32, "u1AbC", "roomXyZ9", 1800007200), VECTOR)

    def test_issue_uses_now_plus_thirty_minutes(self):
        # 30 minutes.
        self.assertEqual(ASKER_TTL_SECONDS, 1800)
        self.assertEqual(issue_asker("x" * 32, "u1AbC", "roomXyZ9", 1800007200 - 1800), VECTOR)

    def test_no_key_or_bad_ids_give_no_token(self):
        self.assertEqual(sign_asker("", "u1", "r1", 1), "")
        self.assertEqual(sign_asker("x" * 32, "u1.x", "r1", 1), "")
        self.assertEqual(sign_asker("x" * 32, "u1", "", 1), "")

    def test_context_carries_the_line_after_the_room_id(self):
        ctx = build_channel_context("roomXyZ9", [], bot_user_id="b", bot_username="Hermes", asker=VECTOR)
        self.assertEqual(ctx, f"[Rocket.Chat room_id: roomXyZ9]\n[Rocket.Chat asker: {VECTOR}]")
        self.assertEqual(asker_line(""), "")
        self.assertEqual(
            build_channel_context("roomXyZ9", [], bot_user_id="b", bot_username="Hermes"),
            "[Rocket.Chat room_id: roomXyZ9]",
        )


if __name__ == "__main__":
    unittest.main()
