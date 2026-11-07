"""Unit tests for the restart-interrupted-turn notice.

A gateway restart interrupting a turn must not leave the user with no message at all (see shared_state.py ``_shared_inflight_augment_rooms``). This file tests the two halves separately:

1. ``_augment_and_handle`` (webhook_mixin.py) registers the room in ``_inflight_augment_rooms`` while preparing the turn and removes it right after ``handle_message()``: from there Hermes core's own shutdown notice covers the turn.
2. ``_notify_inflight_shutdown`` (adapter.py) sends a best-effort notice to the registered rooms and does not propagate errors.

The same "skips gracefully when Hermes core's source is unavailable" pattern as ``test_webhook_mixin.py``; see ``_adapter_test_support.py``.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestAugmentAndHandleTracksInflightRooms(unittest.TestCase):
    def setUp(self):
        from config import load_config
        from inbound import InboundDecision

        self.InboundDecision = InboundDecision
        webhook_mixin = load_mixin_module("webhook_mixin")
        self.webhook_mixin_module = webhook_mixin
        self.WebhookInboundMixin = webhook_mixin.WebhookInboundMixin
        self.cfg = load_config({}, token="t")

    def _instance(self, **extra_attrs):
        attrs = dict(
            _cfg=self.cfg,
            config=MagicMock(extra={}),
            _session_key_profile=MagicMock(return_value=None),
            build_source=MagicMock(return_value=object()),
            handle_message=AsyncMock(),
            _inflight_augment_rooms={},
            _user_id="bot-uid",
            _bot_username="TestBot",
        )
        attrs.update(extra_attrs)
        return make_bare_instance(self.WebhookInboundMixin, **attrs)

    def _decision(self, **overrides):
        # Text starting with "/" is guaranteed to skip the context fetch
        # (should_augment_with_context), so the test does not depend on REST; only the
        # in-flight registration is examined.
        attrs = dict(
            outcome="HANDLE_MESSAGE",
            text="/skip-history-for-test",
            message_id="m1",
            channel_id="room1",
            channel_name="general",
            sender_id="user-1",
            sender_username="some-user",
        )
        attrs.update(overrides)
        return self.InboundDecision(**attrs)

    def test_registered_while_preparing_and_cleared_at_handoff(self):
        # The entry is alive during handle_message() (the preparation window) and
        # removed right after, not at delivery: from there core notifies, and two
        # notices would double up.
        seen_during_handoff = {}

        async def _handle(event):
            seen_during_handoff.update(instance._inflight_augment_rooms)

        instance = self._instance(handle_message=AsyncMock(side_effect=_handle))
        # _source_session_key is the real Hermes core function; here it only matters
        # that it registers and removes with THE RETURNED key.
        instance._source_session_key = MagicMock(return_value="session-key-1")

        asyncio.run(instance._augment_and_handle(self._decision()))

        instance.handle_message.assert_awaited_once()
        instance.build_source.assert_called_once()
        self.assertEqual(seen_during_handoff, {"session-key-1": ("room1", "")})
        self.assertEqual(instance._inflight_augment_rooms, {})

    def test_post_delivery_callback_is_not_used(self):
        # The undocumented register_post_delivery_callback must stay out.
        hook = MagicMock()
        instance = self._instance(register_post_delivery_callback=hook)
        instance._source_session_key = MagicMock(return_value="session-key-6")
        asyncio.run(instance._augment_and_handle(self._decision()))
        hook.assert_not_called()

    def test_the_room_id_line_goes_into_channel_context(self):
        # The room id line goes in channel_context, the text is the bare trigger (core's
        # command recognition sees it that way).
        instance = self._instance()
        instance._source_session_key = MagicMock(return_value="session-key-3")
        asyncio.run(instance._augment_and_handle(self._decision()))
        event = instance.handle_message.await_args.args[0]
        self.assertEqual(event.text, "/skip-history-for-test")
        self.assertEqual(event.channel_context, "[Rocket.Chat room_id: room1]")

    def test_handle_message_failure_still_cleans_up_the_registration(self):
        instance = self._instance(handle_message=AsyncMock(side_effect=RuntimeError("boom")))
        instance._source_session_key = MagicMock(return_value="session-key-2")
        asyncio.run(instance._augment_and_handle(self._decision(thread_id="root-1")))
        self.assertEqual(instance._inflight_augment_rooms, {})

    def test_the_thread_id_is_the_real_thread(self):
        # No segment: the reply's thread is thread_id itself.
        instance = self._instance()
        instance._source_session_key = MagicMock(return_value="session-key-5")
        asyncio.run(instance._augment_and_handle(self._decision(thread_id="root-1")))
        self.assertEqual(instance.build_source.call_args.kwargs["thread_id"], "root-1")
        self.assertEqual(instance.build_source.call_args.kwargs["chat_type"], "channel")


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestNotifyInflightShutdown(unittest.TestCase):
    def setUp(self):
        adapter_module = load_mixin_module("adapter")
        self.RocketChatAdapter = adapter_module.RocketChatAdapter

    def _instance(self, **extra_attrs):
        from types import SimpleNamespace

        attrs = dict(_rest=AsyncMock(), _inflight_augment_rooms={}, _cfg=SimpleNamespace())
        attrs.update(extra_attrs)
        return make_bare_instance(self.RocketChatAdapter, **attrs)

    def test_no_inflight_rooms_sends_nothing(self):
        instance = self._instance()

        asyncio.run(instance._notify_inflight_shutdown())

        instance._rest.post_message.assert_not_called()

    def test_sends_one_notice_per_distinct_room(self):
        instance = self._instance(
            _inflight_augment_rooms={
                "key-a": ("room1", ""), "key-b": ("room1", ""), "key-c": ("room2", "")
            }
        )

        asyncio.run(instance._notify_inflight_shutdown())

        posted_rooms = sorted(
            call.args[0] for call in instance._rest.post_message.await_args_list
        )
        self.assertEqual(posted_rooms, ["room1", "room2"])

    def test_a_thread_turn_gets_its_notice_in_the_thread(self):
        # A thread mention's shutdown notice goes to the thread, a main-channel turn's
        # to the main channel.
        instance = self._instance(
            _inflight_augment_rooms={"key-a": ("room1", "root-1"), "key-b": ("room2", "")}
        )

        asyncio.run(instance._notify_inflight_shutdown())

        sent = sorted(
            (call.args[0], call.kwargs.get("tmid"))
            for call in instance._rest.post_message.await_args_list
        )
        self.assertEqual(sent, [("room1", "root-1"), ("room2", "")])

    def test_a_failed_post_does_not_raise_or_block_the_other_room(self):
        instance = self._instance(
            _inflight_augment_rooms={"key-a": ("room1", ""), "key-b": ("room2", "")}
        )
        instance._rest.post_message.side_effect = [RuntimeError("network down"), {}]

        asyncio.run(instance._notify_inflight_shutdown())  # must not raise

        self.assertEqual(instance._rest.post_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
