"""Reply reference (quote + @mention) and related fixes: reply_reference.py, adapter.send()/edit_message(), the outbound dedup key and filtering progress bubbles out of the room context."""

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
from history import is_progress_bubble, is_system_message  # noqa: E402
from reply_reference import ReplyTarget, ReplyTargets, message_link, reply_prefix  # noqa: E402

SITE = "https://chat.example.com"
ROOM_TARGET = ReplyTarget("m1", "rid1", "general", "c", "alice", False)


class ReplyPrefixTests(unittest.TestCase):
    def test_room_gets_quote_and_mention(self):
        self.assertEqual(
            reply_prefix(ROOM_TARGET, site_url=SITE, quote_enabled=True, mention_enabled=True),
            "[ ](https://chat.example.com/channel/general?msg=m1) @alice\n",
        )

    def test_links_by_room_type(self):
        group = ReplyTarget("m2", "rid2", "private group", "p", "bob", False)
        gdm = ReplyTarget("m3", "rid3", "", "d", "bob", False)
        self.assertEqual(message_link(SITE + "/", group), SITE + "/group/private%20group?msg=m2")
        self.assertEqual(message_link(SITE, gdm), SITE + "/direct/rid3?msg=m3")

    def test_direct_message_gets_nothing(self):
        dm = ReplyTarget("m1", "rid1", "", "d", "alice", True)
        self.assertEqual(reply_prefix(dm, site_url=SITE, quote_enabled=True, mention_enabled=True), "")

    def test_switches_and_missing_site(self):
        self.assertEqual(reply_prefix(ROOM_TARGET, site_url=SITE, quote_enabled=False, mention_enabled=True), "@alice\n")
        self.assertEqual(
            reply_prefix(ROOM_TARGET, site_url=SITE, quote_enabled=True, mention_enabled=False),
            "[ ](https://chat.example.com/channel/general?msg=m1)\n",
        )
        self.assertEqual(reply_prefix(ROOM_TARGET, site_url="", quote_enabled=True, mention_enabled=False), "")
        self.assertEqual(reply_prefix(None, site_url=SITE, quote_enabled=True, mention_enabled=True), "")

    def test_targets_are_bounded(self):
        t = ReplyTargets(max_items=2)
        for i in range(3):
            t.remember(ReplyTarget(f"m{i}", "r", "n", "c", "u", False))
        self.assertIsNone(t.get("m0"))
        self.assertIsNotNone(t.get("m2"))


class ProgressBubbleTests(unittest.TestCase):
    def test_hermes_progress_lines(self):
        for text in (
            "⚙️ tool_describe...",
            "⚙️ mcp__docs__list_directory...",
            "🧠 memory...\n🧠 Updating memory +memory: \"The test fruit is the pear.\"",
            "⏳ Working — 0 min — iteration 3/500, receiving stream response",
            "🔍 search_files: \"pattern\"",
        ):
            self.assertTrue(is_progress_bubble(text), text)
            self.assertTrue(is_system_message({"msg": text}), text)

    def test_ordinary_answers_are_not_progress(self):
        for text in ("Hi! How can I help?", "✅ The item is done.", "- wait...", "Hello...", "👍 okay", ""):
            self.assertFalse(is_progress_bubble(text), text)


class _Cfg:
    reply_quote = True
    reply_mention = True
    ack_reaction = ""
    reasoning_leak_formatting_enabled = False
    reasoning_leak_pattern = ""
    reasoning_leak_label = ""
    reasoning_leak_mode = "drop"
    always_fresh_final_streaming = False


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class SendTests(unittest.TestCase):
    def setUp(self):
        from dedup import OutboundDeduplicator

        self.adapter = load_mixin_module("adapter")
        shared = load_mixin_module("shared_state")
        self.targets = shared._shared_reply_targets()
        shared._shared_site_url()["Bot"] = SITE
        self.targets.remember(ROOM_TARGET)
        self.targets.remember(ReplyTarget("dm1", "dmroom", "", "d", "alice", True))
        self.posts, self.updates = [], []

        async def post(chat_id, text, tmid=""):
            self.posts.append((chat_id, text))
            return {"success": True, "message": {"_id": f"out{len(self.posts)}"}}

        async def update(chat_id, msg_id, text):
            self.updates.append((msg_id, text))
            return {"success": True}

        self.inst = make_bare_instance(
            self.adapter.RocketChatAdapter,
            _cfg=_Cfg(),
            _bot_username="Bot",
            _outbound_dedup=OutboundDeduplicator(ttl_seconds=10),
            _send_locks={},
            _pending_ack={},
            _safe_react=MagicMock(),
        )
        self.inst._rest = MagicMock(post_message=post, update_message=update)
        self.inst.format_message = lambda text: text

    def run_(self, coro):
        return asyncio.run(coro)

    def test_first_reply_in_a_room_quotes_and_mentions_and_edits_keep_it(self):
        result = self.run_(self.inst.send("rid1", "The answer", reply_to="m1"))
        self.assertTrue(self.posts[0][1].startswith("[ ](https://chat.example.com/channel/general?msg=m1) @alice\n"))
        self.assertTrue(self.posts[0][1].endswith("The answer"))
        self.run_(self.inst.edit_message("rid1", result.message_id, "The answer, in more detail"))
        self.assertTrue(self.updates[0][1].startswith("[ ](https://chat.example.com/channel/general?msg=m1) @alice\n"))

    def test_progress_and_dm_replies_get_no_reference(self):
        self.run_(self.inst.send("rid1", "⚙️ tool_describe..."))   # progress: no reply_to
        self.run_(self.inst.send("dmroom", "DM answer", reply_to="dm1"))
        self.assertEqual([p[1] for p in self.posts], ["⚙️ tool_describe...", "DM answer"])

    def test_dedup_does_not_merge_two_turns_progress_in_the_main_channel(self):
        # In the main channel two askers' concurrent turns send bubbles with no thread
        # or asker: both go out.
        a = self.run_(self.inst.send("rid1", "⚙️ tool_search..."))
        b = self.run_(self.inst.send("rid1", "⚙️ tool_search..."))
        self.assertNotEqual(a.message_id, b.message_id)
        self.assertEqual(len(self.posts), 2)

    def test_a_thread_still_dedups_its_own_progress(self):
        a = self.run_(self.inst.send("rid1", "⚙️ tool_search...", metadata={"thread_id": "root-1"}))
        b = self.run_(self.inst.send("rid1", "⚙️ tool_search...", metadata={"thread_id": "root-1"}))
        self.assertEqual(a.message_id, b.message_id)
        c = self.run_(self.inst.send("rid1", "⚙️ tool_search...", metadata={"thread_id": "root-2"}))
        self.assertNotEqual(c.message_id, a.message_id)

if __name__ == "__main__":
    unittest.main()
