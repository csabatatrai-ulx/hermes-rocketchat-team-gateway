"""Unit tests for dedup.py: both directions of duplicate suppression.

Time is injected, never slept: expiry and eviction are asserted deterministically.

The persistence test classes near the bottom are the one place real time matters (the debounce test) and real disk
I/O happens (a temp directory cleaned up at module exit), deliberately, since surviving a restart is exactly the
behaviour under test.
"""

import asyncio
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dedup import (  # noqa: E402
    DEFAULT_INBOUND_DEDUP_RETENTION_HOURS,
    DEFAULT_MAX_SEEN_MESSAGE_IDS,
    delivery_fingerprint_source,
    InboundDeduplicator,
    OutboundDeduplicator,
    content_fingerprint,
)

_SCRATCH_DIR = tempfile.mkdtemp(prefix="dedup-persist-test-")
atexit.register(shutil.rmtree, _SCRATCH_DIR, ignore_errors=True)
_scratch_counter = 0


def _scratch_file() -> str:
    global _scratch_counter
    _scratch_counter += 1
    return os.path.join(_SCRATCH_DIR, f"state-{_scratch_counter}.json")


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestContentFingerprint(unittest.TestCase):
    def test_same_input_same_key(self):
        self.assertEqual(
            content_fingerprint("room1", "hi"), content_fingerprint("room1", "hi")
        )

    def test_different_room_different_key(self):
        self.assertNotEqual(
            content_fingerprint("room1", "hi"), content_fingerprint("room2", "hi")
        )

    def test_different_content_different_key(self):
        self.assertNotEqual(
            content_fingerprint("room1", "hi"), content_fingerprint("room1", "hello")
        )

    def test_room_content_boundary_is_unambiguous(self):
        # Without a separator, ("ab", "c") and ("a", "bc") would collide and
        # one room's reply could suppress another's.
        self.assertNotEqual(content_fingerprint("ab", "c"), content_fingerprint("a", "bc"))


class TestInboundDeduplicator(unittest.TestCase):
    def test_first_delivery_is_not_duplicate(self):
        self.assertFalse(InboundDeduplicator().is_duplicate("m1"))

    def test_second_delivery_is_duplicate(self):
        d = InboundDeduplicator()
        d.is_duplicate("m1")
        self.assertTrue(d.is_duplicate("m1"))

    def test_different_ids_are_independent(self):
        d = InboundDeduplicator()
        d.is_duplicate("m1")
        self.assertFalse(d.is_duplicate("m2"))

    def test_empty_id_never_duplicate(self):
        # No id means nothing to correlate; dropping such a message would lose
        # a real user turn.
        d = InboundDeduplicator()
        self.assertFalse(d.is_duplicate(""))
        self.assertFalse(d.is_duplicate(""))

    def test_check_and_record_are_one_step(self):
        # A single call must both answer and remember - two separate steps
        # would leave a window for a concurrent redelivery.
        d = InboundDeduplicator()
        self.assertFalse(d.is_duplicate("m1"))
        self.assertEqual(len(d), 1)

    def test_cache_is_bounded(self):
        d = InboundDeduplicator(max_entries=3)
        for i in range(10):
            d.is_duplicate(f"m{i}")
        self.assertLessEqual(len(d), 3)

    def test_oldest_entry_is_evicted_first(self):
        d = InboundDeduplicator(max_entries=2)
        d.is_duplicate("m1")
        d.is_duplicate("m2")
        d.is_duplicate("m3")  # evicts m1
        self.assertFalse(d.is_duplicate("m1"))
        self.assertTrue(d.is_duplicate("m3"))


class TestInboundDeduplicatorRetention(unittest.TestCase):
    """Time-based retention, with an injected clock and no sleeping."""

    HOUR = 3600.0

    def test_defaults(self):
        self.assertEqual(DEFAULT_INBOUND_DEDUP_RETENTION_HOURS, 72)
        self.assertEqual(DEFAULT_MAX_SEEN_MESSAGE_IDS, 10000)
        self.assertEqual(InboundDeduplicator().retention_seconds, 72 * self.HOUR)

    def test_still_a_duplicate_inside_the_retention(self):
        clock = FakeClock()
        d = InboundDeduplicator(time_fn=clock, retention_seconds=72 * self.HOUR)
        d.is_duplicate("m1")
        clock.advance(71 * self.HOUR)
        self.assertTrue(d.is_duplicate("m1"))

    def test_forgotten_after_the_retention(self):
        clock = FakeClock()
        d = InboundDeduplicator(time_fn=clock, retention_seconds=72 * self.HOUR)
        d.is_duplicate("m1")
        clock.advance(73 * self.HOUR)
        self.assertFalse(d.is_duplicate("m1"))

    def test_expired_entries_are_pruned(self):
        clock = FakeClock()
        d = InboundDeduplicator(time_fn=clock, retention_seconds=10)
        d.is_duplicate("m1")
        d.is_duplicate("m2")
        clock.advance(11)
        d.is_duplicate("m3")
        self.assertEqual(len(d), 1)

    def test_volume_alone_no_longer_evicts_an_old_mention(self):
        # The defect this guards: after 500 new ids an old @mention dropped out, and a
        # resend caused by a reaction ran again. Now only the memory safety net (10000)
        # evicts.
        clock = FakeClock()
        d = InboundDeduplicator(time_fn=clock)
        d.is_duplicate("regi-mention")
        for i in range(2000):
            clock.advance(1)
            d.is_duplicate(f"m{i}")
        self.assertTrue(d.is_duplicate("regi-mention"))

    def test_backstop_cap_evicts_the_oldest_first(self):
        clock = FakeClock()
        d = InboundDeduplicator(max_entries=3, time_fn=clock)
        for i in range(5):
            clock.advance(1)
            d.is_duplicate(f"m{i}")
        self.assertEqual(len(d), 3)
        self.assertFalse(d.is_duplicate("m0"))
        self.assertTrue(d.is_duplicate("m4"))


class TestOutboundDeduplicator(unittest.TestCase):
    def test_unknown_content_is_not_suppressed(self):
        d = OutboundDeduplicator(time_fn=FakeClock())
        self.assertIsNone(d.recent_message_id("room", "answer"))

    def test_identical_content_returns_original_id(self):
        # Returning the original id (not just "suppressed") is what keeps the
        # caller able to address the message it believes it posted.
        clock = FakeClock()
        d = OutboundDeduplicator(time_fn=clock)
        d.remember("room", "answer", "msg1")
        self.assertEqual(d.recent_message_id("room", "answer"), "msg1")

    # By the asking message (trigger). Hermes defect #72131 sends the status line
    # without reply_to, then again as the final answer with reply_to; and two separate
    # images can get identical answers.
    def test_same_trigger_is_still_suppressed(self):
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "I cannot see the image.", "msg1", "q1")
        self.assertEqual(d.recent_message_id("room", "I cannot see the image.", "q1"), "msg1")

    def test_two_different_known_triggers_are_not_a_repeat(self):
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "I cannot see the image.", "msg1", "q1")
        self.assertIsNone(d.recent_message_id("room", "I cannot see the image.", "q2"))

    def test_unknown_trigger_on_either_side_keeps_the_old_behaviour(self):
        # #72131: the status line went out without a trigger, the final answer comes
        # with one.
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "⚠️ The model provider failed after retries.", "msg1")
        self.assertEqual(d.recent_message_id("room", "⚠️ The model provider failed after retries.", "q1"), "msg1")
        d.remember("room", "answer", "msg2", "q1")
        self.assertEqual(d.recent_message_id("room", "answer"), "msg2")

    def test_expires_after_ttl(self):
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock)
        d.remember("room", "answer", "msg1")
        clock.advance(10.5)
        self.assertIsNone(d.recent_message_id("room", "answer"))

    def test_still_suppressed_just_inside_ttl(self):
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock)
        d.remember("room", "answer", "msg1")
        clock.advance(9.5)
        self.assertEqual(d.recent_message_id("room", "answer"), "msg1")

    def test_other_room_is_not_suppressed(self):
        # Two bots answering in different rooms must not silence each other.
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room1", "answer", "msg1")
        self.assertIsNone(d.recent_message_id("room2", "answer"))

    def test_different_content_is_not_suppressed(self):
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "answer A", "msg1")
        self.assertIsNone(d.recent_message_id("room", "answer B"))

    def test_streaming_preview_prefix_is_not_suppressed(self):
        # The first streamed frame is a prefix of the final answer; it must
        # not block the complete answer from being posted.
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "The user ▉", "msg1")
        self.assertIsNone(d.recent_message_id("room", "The user asked a question."))

    def test_disabled_when_ttl_zero(self):
        d = OutboundDeduplicator(ttl_seconds=0, time_fn=FakeClock())
        self.assertFalse(d.enabled)
        d.remember("room", "answer", "msg1")
        self.assertIsNone(d.recent_message_id("room", "answer"))
        self.assertEqual(len(d), 0)

    def test_missing_message_id_is_not_remembered(self):
        # Suppressing while reporting "delivered, id unknown" would leave the
        # caller unable to edit or delete what it thinks it posted.
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "answer", "")
        self.assertIsNone(d.recent_message_id("room", "answer"))

    def test_empty_content_is_never_suppressed(self):
        d = OutboundDeduplicator(time_fn=FakeClock())
        d.remember("room", "", "msg1")
        self.assertIsNone(d.recent_message_id("room", ""))

    def test_expired_entries_are_pruned(self):
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock)
        for i in range(5):
            d.remember("room", f"answer {i}", f"msg{i}")
        self.assertEqual(len(d), 5)
        clock.advance(30)
        d.remember("room", "friss", "msgN")
        self.assertEqual(len(d), 1)

    def test_cache_is_bounded(self):
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=1000.0, max_entries=3, time_fn=clock)
        for i in range(20):
            d.remember("room", f"answer {i}", f"msg{i}")
        self.assertLessEqual(len(d), 3)

    def test_re_remembering_refreshes_the_window(self):
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock)
        d.remember("room", "answer", "msg1")
        clock.advance(8)
        d.remember("room", "answer", "msg2")
        clock.advance(5)  # 13s after the first, 5s after the second
        self.assertEqual(d.recent_message_id("room", "answer"), "msg2")


class TestDuplicateAnswerScenario(unittest.TestCase):
    """The measured failure this module exists for (see dedup.py docstring)."""

    def test_second_identical_final_send_is_suppressed(self):
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock)
        answer = "> 💭 _thinking_\n> ...\n\n| Letter | Number |\n|---|---|\n| A | 1 |"

        # First fresh-final delivery.
        self.assertIsNone(d.recent_message_id("room", answer))
        d.remember("room", answer, "msg-first")

        # The core finalizes a second time ~3s later with the same text.
        clock.advance(3)
        self.assertEqual(d.recent_message_id("room", answer), "msg-first")

    def test_same_question_asked_again_later_answers_again(self):
        # The accepted trade-off has a bound: outside the window a repeat
        # question gets a real answer, not silence.
        clock = FakeClock()
        d = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock)
        d.remember("room", "Berlin.", "msg1")
        clock.advance(120)
        self.assertIsNone(d.recent_message_id("room", "Berlin."))


class TestInboundDeduplicatorPersistence(unittest.IsolatedAsyncioTestCase):
    """Restart survival."""

    def test_no_persist_path_means_no_disk_io_and_unchanged_behaviour(self):
        # Default (None persist_path) must stay exactly the pre-existing
        # pure-memory behaviour - this is what every OTHER test in this file
        # already relies on implicitly.
        d = InboundDeduplicator()
        self.assertIsNone(d.persist_path)
        self.assertFalse(d.is_duplicate("m1"))
        self.assertTrue(d.is_duplicate("m1"))

    async def test_a_duplicate_seen_before_a_restart_is_still_a_duplicate_after(self):
        file_path = _scratch_file()
        before = InboundDeduplicator(persist_path=file_path)
        self.assertFalse(before.is_duplicate("m1"))
        await before.flush()

        # A brand-new instance, exactly what a gateway restart creates.
        after = InboundDeduplicator(persist_path=file_path)
        self.assertTrue(after.is_duplicate("m1"))

    async def test_an_id_never_seen_before_the_restart_is_still_fresh_after(self):
        file_path = _scratch_file()
        before = InboundDeduplicator(persist_path=file_path)
        before.is_duplicate("m1")
        await before.flush()

        after = InboundDeduplicator(persist_path=file_path)
        self.assertFalse(after.is_duplicate("m2"))

    def test_missing_file_on_first_ever_start_is_not_an_error(self):
        file_path = os.path.join(_SCRATCH_DIR, "never-written.json")
        InboundDeduplicator(persist_path=file_path)  # must not raise

    def test_corrupt_file_degrades_to_an_empty_cache_not_a_crash(self):
        file_path = _scratch_file()
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        d = InboundDeduplicator(persist_path=file_path)
        self.assertEqual(len(d), 0)
        self.assertFalse(d.is_duplicate("m1"))

    async def test_reload_respects_max_entries_keeping_the_most_recently_seen(self):
        file_path = _scratch_file()
        before = InboundDeduplicator(persist_path=file_path, max_entries=2)
        before.is_duplicate("m1")
        before.is_duplicate("m2")
        before.is_duplicate("m3")  # evicts m1 in-memory too, same as the non-persisted test above
        await before.flush()

        after = InboundDeduplicator(persist_path=file_path, max_entries=2)
        self.assertEqual(len(after), 2)
        self.assertFalse(after.is_duplicate("m1"))  # was evicted before the restart
        self.assertTrue(after.is_duplicate("m3"))

    def _write(self, file_path, entries):
        import json

        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": entries}, fh)

    def test_old_format_without_timestamps_loads_as_recorded_now(self):
        file_path = _scratch_file()
        self._write(file_path, ["Hermes:m1", ["Hermes:m2"]])
        clock = FakeClock(1_800_000_000.0)
        d = InboundDeduplicator(persist_path=file_path, time_fn=clock, retention_seconds=3600)
        self.assertEqual(len(d), 2)
        self.assertTrue(d.is_duplicate("Hermes:m1"))
        # It loaded as "recorded now": after one retention period it forgets it.
        clock.advance(3601)
        self.assertFalse(d.is_duplicate("Hermes:m2"))

    def test_invalid_timestamps_load_as_recorded_now(self):
        file_path = _scratch_file()
        self._write(file_path, [["a", None], ["b", True], ["c", "x"], "", [""], [1, 2]])
        d = InboundDeduplicator(
            persist_path=file_path, time_fn=FakeClock(1_800_000_000.0), retention_seconds=3600
        )
        # An id with an invalid timestamp stays as "recorded now"; an empty or
        # non-string id drops out.
        self.assertEqual(len(d), 3)
        for message_id in ("a", "b", "c"):
            self.assertTrue(d.is_duplicate(message_id), message_id)

    def test_entries_older_than_the_retention_are_dropped_on_load(self):
        file_path = _scratch_file()
        now = 1_800_000_000.0
        self._write(file_path, [["fresh", now - 60], ["stale", now - 100 * 3600]])
        d = InboundDeduplicator(
            persist_path=file_path, time_fn=FakeClock(now), retention_seconds=72 * 3600
        )
        self.assertEqual(len(d), 1)
        self.assertTrue(d.is_duplicate("fresh"))
        self.assertFalse(d.is_duplicate("stale"))

    async def test_an_unflushed_write_eventually_lands_on_disk_on_its_own(self):
        file_path = _scratch_file()
        before = InboundDeduplicator(persist_path=file_path)
        before.is_duplicate("m1")  # schedules a debounced save, NOT flushed explicitly
        await asyncio.sleep(0.4)

        after = InboundDeduplicator(persist_path=file_path)
        self.assertTrue(after.is_duplicate("m1"))


class TestOutboundDeduplicatorPersistence(unittest.IsolatedAsyncioTestCase):
    """Restart survival."""

    def test_no_persist_path_means_no_disk_io_and_unchanged_behaviour(self):
        d = OutboundDeduplicator(time_fn=FakeClock())
        self.assertIsNone(d.persist_path)

    async def test_a_recent_send_is_still_suppressed_just_after_a_restart(self):
        file_path = _scratch_file()
        clock = FakeClock()
        before = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        before.remember("room", "answer", "msg1")
        await before.flush()

        # Simulates a restart that takes a couple of seconds, well inside the TTL.
        clock.advance(2)
        after = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        self.assertEqual(after.recent_message_id("room", "answer"), "msg1")

    async def test_the_trigger_survives_a_restart_and_old_files_still_load(self):
        file_path = _scratch_file()
        clock = FakeClock()
        before = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        before.remember("room", "answer", "msg1", "q1")
        await before.flush()
        after = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        self.assertEqual(after.recent_message_id("room", "answer", "q1"), "msg1")
        self.assertIsNone(after.recent_message_id("room", "answer", "q2"))
        # A file without a trigger counts as unknown (suppresses).
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        for _key, entry in data["entries"]:
            entry.pop("trigger")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        legacy = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        self.assertEqual(legacy.recent_message_id("room", "answer", "q2"), "msg1")

    async def test_an_entry_already_stale_by_restart_time_is_pruned_on_load(self):
        file_path = _scratch_file()
        clock = FakeClock()
        before = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        before.remember("room", "answer", "msg1")
        await before.flush()

        # A long-down restart - well past the TTL by the time the process comes back.
        clock.advance(120)
        after = OutboundDeduplicator(ttl_seconds=10.0, time_fn=clock, persist_path=file_path)
        self.assertIsNone(after.recent_message_id("room", "answer"))
        self.assertEqual(len(after), 0)

    async def test_disabled_never_schedules_a_write_even_with_a_persist_path(self):
        # remember() returns before ever calling _schedule_persist() when
        # disabled (ld. dedup.py "if not self.enabled ...: return") - so,
        # unlike the other tests here, there is nothing to flush(): asserting
        # the file was never even created is the point (flush() itself always
        # forces a write regardless of `enabled`, so calling it here would
        # defeat the test).
        file_path = _scratch_file()
        d = OutboundDeduplicator(ttl_seconds=0, time_fn=FakeClock(), persist_path=file_path)
        d.remember("room", "answer", "msg1")
        await asyncio.sleep(0.4)
        self.assertFalse(os.path.exists(file_path))


if __name__ == "__main__":
    unittest.main()


class TestDeliveryFingerprintSource(unittest.TestCase):
    """Keying on the DELIVERED text (every reply is plain text)."""

    def test_the_delivered_text_is_the_key(self):
        self.assertEqual(delivery_fingerprint_source("hi"), "hi")

    def test_identical_texts_key_identically(self):
        text = "> 💭 _thinking_\n> ..."
        self.assertEqual(delivery_fingerprint_source(text), delivery_fingerprint_source(str(text)))
