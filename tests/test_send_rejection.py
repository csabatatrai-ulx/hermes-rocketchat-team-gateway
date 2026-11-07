"""The outbound dedup's `_pending_` mark and Rocket.Chat's refusal: if Rocket.Chat EXPLICITLY refuses the send (4xx or 200 + ``success: false``), the mark is removed so core's retry really sends; on an ambiguous failure (``{}``: 5xx, 3xx, empty answer) the mark stays, as protection against a double message."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)


class _Cfg:
    reply_quote = False
    reply_mention = False
    ack_reaction = ""
    reasoning_leak_formatting_enabled = False
    reasoning_leak_pattern = ""
    reasoning_leak_label = ""
    reasoning_leak_mode = "drop"
    always_fresh_final_streaming = False


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class SendRejectionTests(unittest.TestCase):
    def _instance(self, answers):
        from dedup import OutboundDeduplicator

        adapter = load_mixin_module("adapter")
        self.posts = []

        async def post(chat_id, text, tmid=""):
            self.posts.append(text)
            return answers.pop(0)

        inst = make_bare_instance(
            adapter.RocketChatAdapter,
            _cfg=_Cfg(),
            _bot_username="Bot",
            _outbound_dedup=OutboundDeduplicator(ttl_seconds=10),
            _send_locks={},
            _pending_ack={},
            _safe_react=MagicMock(),
        )
        inst._rest = MagicMock(post_message=post)
        inst.format_message = lambda text: text
        return inst

    def test_explicit_rejection_lets_the_retry_really_post(self):
        inst = self._instance([
            {"success": False, "status": 400},
            {"success": True, "message": {"_id": "out2"}},
        ])
        first = asyncio.run(inst.send("rid1", "The answer"))
        second = asyncio.run(inst.send("rid1", "The answer"))
        self.assertFalse(first.success)
        self.assertTrue(second.success)
        self.assertEqual(second.message_id, "out2")
        self.assertEqual(len(self.posts), 2)

    def test_success_false_body_counts_as_rejection_too(self):
        inst = self._instance([
            {"success": False, "error": "error-action-not-allowed"},
            {"success": True, "message": {"_id": "out2"}},
        ])
        asyncio.run(inst.send("rid1", "The answer"))
        self.assertTrue(asyncio.run(inst.send("rid1", "The answer")).success)
        self.assertEqual(len(self.posts), 2)

    def test_ambiguous_failure_keeps_the_guard(self):
        # {} = 5xx/3xx/empty answer: the message may exist, no resend.
        inst = self._instance([{}, {"success": True, "message": {"_id": "out2"}}])
        self.assertFalse(asyncio.run(inst.send("rid1", "The answer")).success)
        second = asyncio.run(inst.send("rid1", "The answer"))
        self.assertEqual(second.message_id, "_pending_")
        self.assertEqual(len(self.posts), 1)



@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class SendTriggerDedupTests(unittest.TestCase):
    """Real cases at the ``send()`` level: the outbound filter also looks at the asking message (``reply_to`` / ``metadata["reply_to_message_id"]``), see dedup.py ``recent_message_id``."""

    OK = [{"success": True, "message": {"_id": f"out{i}"}} for i in range(1, 5)]
    _instance = SendRejectionTests._instance

    def test_hermes_72131_status_then_final_with_reply_to_posts_once(self):
        # Provider failure: core sends the same text first without reply_to (status),
        # then as the final answer with reply_to.
        inst = self._instance(list(self.OK))
        text = "⚠️ The model provider failed after retries."
        asyncio.run(inst.send("rid1", text))
        second = asyncio.run(inst.send("rid1", text, reply_to="q1"))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(second.message_id, "out1")

    def test_same_turn_final_sent_twice_posts_once(self):
        inst = self._instance(list(self.OK))
        asyncio.run(inst.send("rid1", "How can I help?", reply_to="q1"))
        asyncio.run(inst.send("rid1", "How can I help?", metadata={"reply_to_message_id": "q1"}))
        self.assertEqual(len(self.posts), 1)

    def test_identical_answers_to_two_different_questions_both_post(self):
        # Two separate images, the same correct answer: both go out.
        inst = self._instance(list(self.OK))
        asyncio.run(inst.send("rid1", "I cannot see the image.", reply_to="q1"))
        asyncio.run(inst.send("rid1", "I cannot see the image.", metadata={"reply_to_message_id": "q2"}))
        self.assertEqual(len(self.posts), 2)


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class ProgressBubbleDedupTests(SendRejectionTests):
    """In a room's main channel the identical bubble of two askers' concurrent turns comes with no asker (trigger); the filter does not swallow it, but it still catches the final answer (the #72131 duplicate)."""

    def test_two_turns_identical_progress_bubbles_both_post(self):
        inst = self._instance([
            {"success": True, "message": {"_id": "p1"}},
            {"success": True, "message": {"_id": "p2"}},
        ])
        first = asyncio.run(inst.send("rid1", "⚙️ tool_search: \"Searching tools\""))
        second = asyncio.run(inst.send("rid1", "⚙️ tool_search: \"Searching tools\""))
        self.assertEqual((first.message_id, second.message_id), ("p1", "p2"))
        self.assertEqual(len(self.posts), 2)

    def test_untriggered_identical_final_text_is_still_swallowed(self):
        inst = self._instance([{"success": True, "message": {"_id": "f1"}}])
        first = asyncio.run(inst.send("rid1", "The answer"))
        second = asyncio.run(inst.send("rid1", "The answer"))
        self.assertEqual(second.message_id, first.message_id)
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__":
    unittest.main()
