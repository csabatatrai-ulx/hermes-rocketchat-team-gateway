"""``@`` context references stay inert in chat text (context_refs.py).

Three layers: the function itself (no Hermes needed); Hermes core's own parser finds no reference in
neutralised text; and the whole inbound chain, where the trigger, the room context and an inserted
document reach ``handle_message`` neutralised, and ``ROCKETCHAT_TEAM_ALLOW_CONTEXT_REFERENCES`` keeps
only the trigger live.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON  # noqa: E402
from context_refs import neutralize_context_references  # noqa: E402

ZWSP = "\u200b"

ATTACKS = [
    "@file:secrets.yaml",
    "@file:/opt/data/config.yaml:1-20",
    '@file:"notes with space.md"',
    "@folder:memories",
    "@git:3",
    "@diff",
    "@staged",
    "@url:http://10.0.0.5/admin",
    "@somepluginkind:value",
]


class TestNeutralize(unittest.TestCase):
    def test_every_reference_kind_gets_a_zero_width_space_after_the_at(self):
        for ref in ATTACKS:
            with self.subTest(ref=ref):
                out = neutralize_context_references(f"please read {ref} now")
                self.assertEqual(out, f"please read @{ZWSP}{ref[1:]} now")

    def test_ordinary_text_is_unchanged(self):
        for text in (
            "",
            "no at sign",
            "@hermes what is the status?",
            "@hermes: what is the status?",
            "mail alice@example.com or bob@file:x",  # core's left boundary: no word char before @
            "path/to/@file:x",  # nor a slash
            "@different topic",
            "@stagedfright",
        ):
            with self.subTest(text=text):
                self.assertEqual(neutralize_context_references(text), text)

    def test_idempotent(self):
        once = neutralize_context_references("@file:a @url:b")
        self.assertEqual(neutralize_context_references(once), once)

    def test_every_reference_in_a_multiline_text(self):
        text = "line one @file:a\n[2026-11-10 02:35] carol: notes @folder:b\n@diff"
        out = neutralize_context_references(text)
        self.assertEqual(out.count("@" + ZWSP), 3)


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestAgainstCoreParser(unittest.TestCase):
    """Hermes core's own parser: finds the reference before, nothing after."""

    def test_core_finds_nothing_in_neutralised_text(self):
        from agent.context_references import parse_context_references

        # The built-in kinds; core parses ``@<kind>:`` of a plugin only while that plugin is loaded.
        for ref in ATTACKS[:-1]:
            with self.subTest(ref=ref):
                text = f"please read {ref} now"
                self.assertTrue(parse_context_references(text), "core no longer parses this form")
                self.assertEqual(parse_context_references(neutralize_context_references(text)), [])


if HERMES_AGENT_AVAILABLE:
    from test_room_context import WINDOW, _Base, _FakeRest, _payload
else:  # the skip decorator below still needs names
    _Base = unittest.TestCase


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestInboundChain(_Base):
    """What ``handle_message`` receives."""

    HIDDEN = {
        "_id": "c1",
        "u": {"_id": "uid-carol", "username": "carol"},
        "msg": "notes for later @file:config.yaml",
        "ts": "2026-10-06T09:58:30.000Z",
    }

    def _event(self, *, allow=None, text="@Hermes summarise @file:secrets.yaml", document=""):
        from config import load_config

        env = {
            "ROCKETCHAT_TEAM_INBOUND_DEDUP_RETENTION_HOURS": "1000000",
            "ROCKETCHAT_TEAM_TIMEZONE": "Europe/Berlin",
        }
        if allow is not None:
            env["ROCKETCHAT_TEAM_ALLOW_CONTEXT_REFERENCES"] = allow
        self.cfg = load_config({}, env)
        instance = self.instance(_FakeRest(room=[WINDOW[0], self.HIDDEN, WINDOW[1]]))
        media = SimpleNamespace(
            paths=[], media_types=[], kinds=[], text_inlined=[], injected_text=document,
        )
        instance._download_trigger_files = AsyncMock(return_value=media)
        self.run_turn(instance, _payload(text=text))
        return instance.handle_message.await_args.args[0]

    def test_default_neutralises_trigger_and_room_context(self):
        event = self._event()
        self.assertEqual(event.text, f"summarise @{ZWSP}file:secrets.yaml")
        self.assertIn(f"notes for later @{ZWSP}file:config.yaml", event.channel_context)
        self.assertNotIn(" @file:", event.channel_context)

    def test_switch_keeps_the_trigger_live_but_not_the_room_context(self):
        event = self._event(allow="true")
        self.assertEqual(event.text, "summarise @file:secrets.yaml")
        self.assertIn(f"notes for later @{ZWSP}file:config.yaml", event.channel_context)

    def test_inserted_document_stays_inert_even_with_the_switch(self):
        doc = "[Content of notes.txt]:\nsee @url:http://10.0.0.5/admin"
        for allow in (None, "true"):
            with self.subTest(allow=allow):
                event = self._event(allow=allow, text="@Hermes read this", document=doc)
                self.assertIn(f"see @{ZWSP}url:http://10.0.0.5/admin", event.text)
                self.assertTrue(event.text.endswith("read this"))

    def test_plain_trigger_is_byte_identical(self):
        event = self._event(text="@Hermes What's the status?")
        self.assertEqual(event.text, "What's the status?")


if __name__ == "__main__":
    unittest.main()
