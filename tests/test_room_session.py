"""Room sessions and history blocks (sessions belong to Hermes core).

Cases: the key is core's key (per person in the main channel, a thread shared or per person with ``thread_sessions_per_user``, DM), boundary search (one's own previous mention; another user's is not a boundary; another thread's is not), the block's content, ``[unverified sender]``, ``/new`` per user, the reply's thread.

The pure parts (``room_session.py``) run without Hermes; the mixin and core cases need Hermes core's source (otherwise they skip gracefully, see ``_adapter_test_support``). The core cases call the REAL ``build_session_key`` and ``_prefix_inbound_sender_context``.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON  # noqa: E402
from room_session import (  # noqa: E402
    find_previous_mention,
    mentions_bot,
    select_pending_window,
)

BOT = "Hermes"
BOT_UID = "bot-uid"
CEST = timezone(timedelta(hours=2))
TRIGGER_TS = "2026-10-06T10:00:05.000Z"
BOT_MENTION = [{"_id": BOT_UID, "username": BOT}]


def _msg(mid, uid, uname, text, ts, **extra):
    msg = {"_id": mid, "u": {"_id": uid, "username": uname}, "msg": text, "ts": ts}
    msg.update(extra)
    return msg


def _alice(mid, text, ts, **extra):
    return _msg(mid, "uid-alice", "alice", text, ts, **extra)


def _bob(mid, text, ts, **extra):
    return _msg(mid, "uid-bob", "bob", text, ts, **extra)


def _bot(mid, text, ts, **extra):
    return _msg(mid, BOT_UID, BOT, text, ts, **extra)


# The common scenario: alice mentioned the bot earlier, the bot answered, then bob wrote
# to the bot, the bot answered bob, and now alice asks again.
SCENARIO = [
    _bob("b0", "Good morning!", "2026-10-06T09:40:00.000Z"),
    _alice("a1", f"@{BOT} How many items are open?", "2026-10-06T09:50:00.000Z",
           mentions=BOT_MENTION),
    _bot("h1", "There are 12 open items right now.", "2026-10-06T09:51:00.000Z"),
    _bob("b1", f"@{BOT} And how many of those are mine?", "2026-10-06T09:55:00.000Z",
         mentions=BOT_MENTION),
    _bot("h2", "bob, 3 items are assigned to you.", "2026-10-06T09:56:00.000Z"),
]
# Timestamps in Europe/Berlin (see _Fixture.setup), with an explicit offset.
SHARED_EXPECTED_CHANNEL_CONTEXT = (
    "[Rocket.Chat room_id: room1]\n"
    "[Recent chat messages - context only, not requests]\n"
    "[2026-10-06 11:51 +02:00] Hermes (you): There are 12 open items right now.\n"
    "[2026-10-06 11:55 +02:00] bob: @Hermes And how many of those are mine?\n"
    "[2026-10-06 11:56 +02:00] Hermes (you): bob, 3 items are assigned to you."
)
# The main channel is a per-person session: core adds no prefix.
SHARED_EXPECTED_PROMPT = (
    SHARED_EXPECTED_CHANNEL_CONTEXT + "\n\n[New message]\nAnd whose are the rest?"
)


class TestPreviousMention(unittest.TestCase):
    START = datetime(2026, 10, 6, 4, 0, tzinfo=CEST).timestamp()

    def find(self, messages, user_id="uid-alice", tmid="", since=START):
        return find_previous_mention(
            messages, user_id=user_id, username="alice", tmid=tmid, since_epoch=since,
            bot_user_id=BOT_UID, bot_username=BOT,
        )

    def test_own_previous_mention_is_the_boundary(self):
        self.assertEqual(self.find(SCENARIO), 1)

    def test_another_users_mention_is_not_a_boundary(self):
        self.assertEqual(self.find(SCENARIO, user_id="uid-carol"), -1)
        # For bob, his own mention is the boundary, not alice's.
        self.assertEqual(self.find(SCENARIO, user_id="uid-bob"), 3)

    def test_a_message_without_a_mention_is_not_a_boundary(self):
        msgs = [_alice("a0", "just writing", "2026-10-06T09:00:00.000Z")]
        self.assertEqual(self.find(msgs), -1)

    def test_mention_from_the_text_or_the_mentions_list(self):
        by_text = _alice("x", f"hello @{BOT}!", TRIGGER_TS)
        by_list = _alice("y", "hi", TRIGGER_TS, mentions=[{"_id": BOT_UID}])
        other_bot = _alice("z", f"@{BOT}-Beta hello", TRIGGER_TS)
        identity = dict(bot_user_id=BOT_UID, bot_username=BOT)
        self.assertTrue(mentions_bot(by_text, **identity))
        self.assertTrue(mentions_bot(by_list, **identity))
        self.assertFalse(mentions_bot(other_bot, **identity))

    def test_another_threads_mention_is_not_a_boundary(self):
        msgs = [
            _alice("a1", f"@{BOT} main channel", "2026-10-06T09:00:00.000Z"),
            _alice("a2", f"@{BOT} other thread", "2026-10-06T09:10:00.000Z", tmid="other"),
            _alice("a3", f"@{BOT} this thread", "2026-10-06T09:20:00.000Z", tmid="root1"),
        ]
        # In the main channel, thread replies (also sent to the channel) are not
        # boundaries.
        self.assertEqual(self.find(msgs), 0)
        self.assertEqual(self.find(msgs, tmid="root1"), 2)
        self.assertEqual(self.find(msgs[:2], tmid="root1"), -1)

    def test_a_mention_sent_to_the_channel_too_is_not_a_main_channel_boundary(self):
        # ``tshow: true``: visible in the main channel too, but belongs to the thread's
        # session.
        msgs = [
            _alice("a1", f"@{BOT} main channel", "2026-10-06T09:00:00.000Z"),
            _alice("a2", f"@{BOT} in thread, also to channel", "2026-10-06T09:10:00.000Z",
                   tmid="root1", tshow=True),
        ]
        self.assertEqual(self.find(msgs), 0)
        self.assertEqual(self.find(msgs, tmid="root1"), 1)

    def test_the_thread_root_is_not_a_boundary_of_the_thread(self):
        root = _alice("root1", f"@{BOT} starting a thread", "2026-10-06T09:00:00.000Z")
        reply = _bot("r1", "Rendben.", "2026-10-06T09:01:00.000Z", tmid="root1")
        self.assertEqual(self.find([root, reply], tmid="root1"), -1)

    def test_previous_days_mention_is_not_a_boundary(self):
        msgs = [
            _alice("a0", f"@{BOT} yesterday's question", "2026-10-05T10:00:00.000Z"),
            _bot("h0", "yesterday's answer", "2026-10-05T10:01:00.000Z"),
            _alice("a1", f"@{BOT} early question", "2026-10-06T01:59:00.000Z"),
        ]
        self.assertEqual(self.find(msgs), -1)
        self.assertEqual(self.find(msgs, since=None), 2)

    def test_new_ends_the_search_and_other_commands_are_skipped(self):
        msgs = [
            _alice("a1", f"@{BOT} old question", "2026-10-06T08:00:00.000Z"),
            _alice("a2", f"@{BOT} /new", "2026-10-06T08:10:00.000Z"),
            _alice("a3", f"@{BOT} /approve", "2026-10-06T08:11:00.000Z"),
        ]
        self.assertEqual(self.find(msgs), -1)  # not known since /new
        msgs[1] = _alice("a2", f"@{BOT} /whoami", "2026-10-06T08:10:00.000Z")
        self.assertEqual(self.find(msgs), 0)

    def test_every_mention_is_stripped_before_the_command_check(self):
        # "@Hermes @Hermes /new" also closes (every occurrence is stripped).
        msgs = [
            _alice("a1", f"@{BOT} old question", "2026-10-06T08:00:00.000Z"),
            _alice("a2", f"@{BOT} @{BOT.lower()} /new", "2026-10-06T08:10:00.000Z"),
        ]
        self.assertEqual(self.find(msgs), -1)

    def test_system_messages_are_ignored(self):
        msgs = [_alice("s1", f"@{BOT}", "2026-10-06T09:00:00.000Z", t="uj")]
        self.assertEqual(self.find(msgs), -1)

    def test_pending_window_is_everything_after_the_boundary_without_a_lower_bound(self):
        window = select_pending_window(SCENARIO, 1, limit=50)
        self.assertEqual([m["_id"] for m in window], ["h1", "b1", "h2"])
        self.assertEqual([m["_id"] for m in select_pending_window(SCENARIO, 1, limit=2)], ["b1", "h2"])
        self.assertEqual(select_pending_window(SCENARIO, 4, limit=50), [])
        self.assertEqual(select_pending_window(SCENARIO, -1, limit=50), [])
        self.assertEqual(select_pending_window(SCENARIO, 1, limit=0), [])


# --------------------------------------------------------------------------- Through
# the Hermes binding (mixin + real core)
# ---------------------------------------------------------------------------


class _Fixture:
    """The pattern of test_room_context._Base, with a real SessionSource and key."""

    @staticmethod
    def setup(case, env=None):
        from config import load_config
        from dedup import InboundDeduplicator, OutboundDeduplicator
        from _adapter_test_support import load_mixin_module
        from gateway.config import Platform
        from gateway.session import SessionSource, build_session_key

        case.webhook_mixin = load_mixin_module("webhook_mixin")
        case.adapter_module = load_mixin_module("adapter")
        base_env = {
            "ROCKETCHAT_TEAM_INBOUND_DEDUP_RETENTION_HOURS": "1000000",
            # Pinned zone: the block's timestamps do not depend on the machine's zone.
            "ROCKETCHAT_TEAM_TIMEZONE": "Europe/Berlin",
        }
        base_env.update(env or {})
        case.cfg = load_config({}, base_env)
        # In production the plugin's registration takes up the Platform("rocketchat_team")
        # member (core's _missing_); here with the same core call.
        try:
            case.platform = Platform("rocketchat_team")
        except ValueError:
            case.platform = Platform._add_pseudo_member("rocketchat_team")
        case.SessionSource = SessionSource
        case.InboundDeduplicator = InboundDeduplicator
        case.OutboundDeduplicator = OutboundDeduplicator


def _fake_rest(room=None, root=None, replies=None):
    from test_room_context import _FakeRest

    return _FakeRest(room=room, root=root, replies=replies)


def _payload(**overrides):
    from test_room_context import _payload as base

    return base(**overrides)


def _core_prefix(event, source, *, group_sessions_per_user=True, thread_sessions_per_user=False):
    """Hermes core's own output, unmodified."""
    from gateway.run_inbound import GatewayInboundMixin

    runner = SimpleNamespace(config=SimpleNamespace(
        group_sessions_per_user=group_sessions_per_user,
        thread_sessions_per_user=thread_sessions_per_user,
    ))
    return GatewayInboundMixin._prefix_inbound_sender_context(runner, event, source, event.text)


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class _Base(unittest.TestCase):
    ENV = None

    def setUp(self):
        _Fixture.setup(self, self.ENV)
        self.keys = []

    def instance(self, rest, **extra):
        from gateway.session import build_session_key

        keys = self.keys

        class _RecordingRooms(dict):
            # The turn enters the shutdown-notice map during preparation with its
            # session key: this shows which key the plugin computed.
            def __setitem__(self, key, value):
                keys.append(key)
                super().__setitem__(key, value)

        attrs = dict(
            _cfg=self.cfg,
            _rest=rest,
            _bot_username=BOT,
            _user_id=BOT_UID,
            _webhook_secret="",
            _require_mention=True,
            _inbound_dedup=self.InboundDeduplicator(),
            _outbound_dedup=self.OutboundDeduplicator(ttl_seconds=0.001),
            _send_locks={},
            _pending_ack={},
            _safe_react=MagicMock(),
            _inflight_augment_rooms=_RecordingRooms(),
            config=MagicMock(extra={}),
            _session_key_profile=MagicMock(return_value=None),
            build_source=MagicMock(
                side_effect=lambda **kw: self.SessionSource(platform=self.platform, **kw)
            ),
            handle_message=MagicMock(),
        )
        attrs.update(extra)
        from _adapter_test_support import make_bare_instance

        inst = make_bare_instance(self.adapter_module.RocketChatAdapter, **attrs)
        inst._safe_react = _async_noop
        inst.handle_message = _Recorder()
        self.build_session_key = build_session_key
        return inst

    def run_turn(self, instance, raw):
        decision = instance._classify_inbound_from_body(raw)

        async def _go():
            await instance._dispatch_inbound_decision(decision)
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(_go())
        return decision

    def event(self, instance):
        return instance.handle_message.events[-1]


async def _async_noop(*_a, **_k):
    return None


class _Recorder:
    """Instead of ``handle_message``: keeps the events."""

    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


class TestCoreSessionKeysAndPrefix(_Base):
    """Sessions belong to Hermes core, with a real core: a per-person session without a prefix in the main channel, a shared thread session with a ``[name]`` prefix; the key is always core's documented key."""

    def assert_keys_match_the_core(self, instance):
        events = instance.handle_message.events
        self.assertEqual(len(self.keys), len(events))
        for key, event in zip(self.keys, events):
            self.assertEqual(key, instance._event_session_key(event))

    def test_main_channel_is_per_user_without_a_prefix(self):
        from gateway.session import is_shared_multi_user_session

        instance = self.instance(_fake_rest())
        self.run_turn(instance, _payload(message_id="m1", text=f"@{BOT} Hello"))
        self.run_turn(instance, _payload(message_id="m2", user_id="uid-bob", user_name="bob",
                                         text=f"@{BOT} Hello"))
        self.assert_keys_match_the_core(instance)
        self.assertEqual(self.keys, [
            "agent:main:rocketchat_team:channel:room1:uid-alice",
            "agent:main:rocketchat_team:channel:room1:uid-bob",
        ])
        for event in instance.handle_message.events:
            self.assertIsNone(event.source.thread_id)
            self.assertEqual(event.source.chat_type, "channel")
            self.assertFalse(is_shared_multi_user_session(event.source))
            self.assertTrue(_core_prefix(event, event.source).endswith("\n[New message]\nHello"))

    def test_same_user_on_another_day_stays_in_one_session(self):
        # No daily switch.
        instance = self.instance(_fake_rest())
        self.run_turn(instance, _payload(message_id="m1", text=f"@{BOT} one"))
        self.run_turn(instance, _payload(message_id="m2", text=f"@{BOT} two",
                                         timestamp="2026-10-09T15:00:00.000Z"))
        self.assertEqual(self.keys[0], self.keys[1])

    def test_thread_is_the_real_tmid_and_shared_with_the_prefix(self):
        from gateway.session import is_shared_multi_user_session

        instance = self.instance(_fake_rest())
        self.run_turn(instance, _payload(message_id="m1", text=f"@{BOT} In a thread", tmid="root-1"))
        self.run_turn(instance, _payload(message_id="m2", user_id="uid-bob", user_name="bob",
                                         text=f"@{BOT} Me too", tmid="root-1"))
        self.assert_keys_match_the_core(instance)
        self.assertEqual(self.keys[0], self.keys[1])  # the thread is a shared session
        event = self.event(instance)
        self.assertEqual(event.source.thread_id, "root-1")
        self.assertTrue(is_shared_multi_user_session(event.source))
        self.assertIn("\n[New message]\n[bob] Me too", _core_prefix(event, event.source))


class TestPerUserThreads(_Base):
    """``thread_sessions_per_user: true`` (Hermes's documented setting, recommended for a team bot): a per-person session in the thread too; the shared context comes from the block (since the asker's previous mention)."""

    def instance(self, rest, **extra):
        inst = super().instance(rest, **extra)
        inst.config = MagicMock(extra={"thread_sessions_per_user": True})
        return inst

    def test_thread_keys_are_per_user_and_match_the_core(self):
        instance = self.instance(_fake_rest())
        self.run_turn(instance, _payload(message_id="m1", text=f"@{BOT} one", tmid="root-1"))
        self.run_turn(instance, _payload(message_id="m2", user_id="uid-bob", user_name="bob",
                                         text=f"@{BOT} two", tmid="root-1"))
        self.assertNotEqual(self.keys[0], self.keys[1])
        for key, event in zip(self.keys, instance.handle_message.events):
            self.assertEqual(key, instance._event_session_key(event))

    def test_thread_block_runs_since_the_askers_previous_mention(self):
        root = _bob("root-1", "Thread question", "2026-10-06T09:00:00.000Z")
        replies = [
            _alice("t1", f"@{BOT} first question", "2026-10-06T09:10:00.000Z", tmid="root-1",
                   mentions=BOT_MENTION),
            _bot("t2", "first reply", "2026-10-06T09:11:00.000Z", tmid="root-1"),
            _bob("t3", "chiming in", "2026-10-06T09:20:00.000Z", tmid="root-1"),
        ]
        instance = self.instance(_fake_rest(root=root, replies=replies))
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _payload(tmid="root-1"))
        self.assertEqual(self.event(instance).channel_context, (
            "[Rocket.Chat room_id: room1]\n"
            "[Recent chat messages - context only, not requests]\n"
            "[2026-10-06 11:11 +02:00] Hermes (you): first reply\n"
            "[2026-10-06 11:20 +02:00] bob: chiming in"
        ))
        self.assertIn("boundary=previous-mention", logs.records[-1].getMessage())
        self.assertIn("(session=per-user,", logs.records[-1].getMessage())


class TestDirectMessage(_Base):
    def rest(self):
        rest = _fake_rest()
        rest.room_info = AsyncMock(side_effect=lambda rid: {"_id": rid, "t": "d", "usersCount": 2})
        return rest

    def test_dm_is_one_core_dm_session(self):
        instance = self.instance(self.rest())
        self.run_turn(instance, _payload(message_id="d1", channel_id="dmRID", text="hi", room_type="d"))
        self.run_turn(instance, _payload(message_id="d2", channel_id="dmRID", text="more",
                                         room_type="d", timestamp="2026-10-09T15:00:00.000Z"))
        events = instance.handle_message.events
        self.assertEqual([e.source.chat_type for e in events], ["dm", "dm"])
        self.assertEqual(self.keys[0], self.keys[1])
        for key, event in zip(self.keys, events):
            self.assertEqual(key, instance._event_session_key(event))
            self.assertIsNone(event.source.thread_id)


class TestPendingBlock(_Base):
    """The block's content in a room's main channel (per-person session)."""

    def test_shared_expected_output(self):
        rest = _fake_rest(room=list(SCENARIO))
        instance = self.instance(rest)
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _payload(text=f"@{BOT} And whose are the rest?"))
        event = self.event(instance)
        self.assertEqual(event.channel_context, SHARED_EXPECTED_CHANNEL_CONTEXT)
        self.assertEqual(_core_prefix(event, event.source), SHARED_EXPECTED_PROMPT)
        block = SHARED_EXPECTED_CHANNEL_CONTEXT.split("\n", 1)[1]
        self.assertIn(
            "Rocket.Chat: room context for room1 (scope=room): fetched 5, kept 3, "
            f"boundary=previous-mention, ~{(len(block) + 1) // 2} tokens "
            "(session=per-user, limit=50, min=20, budget=off)",
            [r.getMessage() for r in logs.records],
        )

    def test_nothing_after_the_boundary_gives_only_the_room_line(self):
        rest = _fake_rest(room=list(SCENARIO[:2]))
        instance = self.instance(rest)
        self.run_turn(instance, _payload(text=f"@{BOT} And whose are the rest?"))
        self.assertEqual(self.event(instance).channel_context, "[Rocket.Chat room_id: room1]")

    def test_first_question_gets_the_full_window(self):
        # bob has no previous mention: the window since the bot's last reply (with the
        # min_messages lower bound).
        rest = _fake_rest(room=list(SCENARIO[:3]))
        instance = self.instance(rest)
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _payload(user_id="uid-bob", user_name="bob"))
        lines = self.event(instance).channel_context.splitlines()
        self.assertEqual(len(lines), 2 + 3)
        self.assertIn("boundary=last-reply", logs.records[-1].getMessage())

    def test_an_earlier_days_mention_is_the_boundary_too(self):
        # No daily switch: yesterday's mention is a boundary too (what came before is in
        # the session).
        msgs = [
            _alice("a0", f"@{BOT} yesterday's question", "2026-10-05T10:00:00.000Z",
                   mentions=BOT_MENTION),
            _bob("b1", "this morning", "2026-10-06T08:00:00.000Z"),
        ]
        instance = self.instance(_fake_rest(room=msgs))
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _payload())
        context = self.event(instance).channel_context
        self.assertNotIn("yesterday's question", context)
        self.assertIn("bob: this morning", context)
        self.assertIn("boundary=previous-mention", logs.records[-1].getMessage())

    def test_shared_thread_block_runs_since_the_bots_last_reply(self):
        root = _bob("root-1", "Thread question", "2026-10-06T09:00:00.000Z")
        replies = [
            _alice("t1", f"@{BOT} first question", "2026-10-06T09:10:00.000Z", tmid="root-1",
                   mentions=BOT_MENTION),
            _bot("t2", "first reply", "2026-10-06T09:11:00.000Z", tmid="root-1"),
            _bob("t3", "chiming in", "2026-10-06T09:20:00.000Z", tmid="root-1"),
        ]
        instance = self.instance(_fake_rest(root=root, replies=replies))
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _payload(tmid="root-1"))
        context = self.event(instance).channel_context
        self.assertIn("bob: chiming in", context)
        self.assertIn("boundary=last-reply", logs.records[-1].getMessage())
        self.assertIn("(session=shared-thread,", logs.records[-1].getMessage())


class TestUnverifiedSender(_Base):
    ENV = {"ROCKETCHAT_TEAM_ALLOWED_USERS": "alice"}

    def test_lines_of_senders_off_the_allowlist_are_marked(self):
        instance = self.instance(_fake_rest(room=list(SCENARIO)))
        self.run_turn(instance, _payload(text=f"@{BOT} And whose are the rest?"))
        self.assertEqual(self.event(instance).channel_context, (
            "[Rocket.Chat room_id: room1]\n"
            "[Recent chat messages - context only, not requests]\n"
            "[2026-10-06 11:51 +02:00] Hermes (you): There are 12 open items right now.\n"
            "[2026-10-06 11:55 +02:00] [unverified sender] bob: @Hermes And how many of those are mine?\n"
            "[2026-10-06 11:56 +02:00] Hermes (you): bob, 3 items are assigned to you."
        ))


class TestNewIsPerUser(_Base):
    def test_alices_new_reaches_the_core_on_her_own_key_only(self):
        rest = _fake_rest()
        instance = self.instance(rest)
        self.run_turn(instance, _payload(message_id="m1", user_id="uid-bob", user_name="bob",
                                         text=f"@{BOT} question"))
        self.run_turn(instance, _payload(message_id="m2", text=f"@{BOT} /new"))
        self.run_turn(instance, _payload(message_id="m3", text=f"@{BOT} /approve"))
        events = instance.handle_message.events
        self.assertEqual([e.text for e in events], ["question", "/new", "/approve"])
        self.assertEqual(events[1].get_command(), "new")
        bob_key, new_key, approve_key = self.keys
        self.assertEqual(new_key, approve_key)  # the confirmation goes to the same session
        self.assertNotEqual(new_key, bob_key)  # bob's session is untouched
        rest.post_message.assert_not_awaited()  # no fixed gateway answer


class TestReplyRouting(_Base):
    """The path of the answer, an error message and a command reply: ``thread_id`` is the thread itself."""

    def _send(self, instance, source, text):
        asyncio.run(instance.send("room1", text, metadata={"thread_id": source.thread_id}))
        return instance._rest.post_message.await_args

    def test_thread_and_main_channel_replies_also_after_a_restart(self):
        rest = _fake_rest()
        instance = self.instance(rest)
        self.run_turn(instance, _payload(message_id="m1", text=f"@{BOT} one", tmid="root-1"))
        self.run_turn(instance, _payload(message_id="m3", text=f"@{BOT} main"))
        thread, main = (e.source for e in instance.handle_message.events)
        self.assertEqual(self._send(instance, thread, "reply").kwargs, {"tmid": "root-1"})
        self.assertEqual(self._send(instance, thread, "⚠️ error").kwargs, {"tmid": "root-1"})
        self.assertEqual(self._send(instance, main, "main reply").kwargs, {"tmid": ""})
        # After a restart (new adapter instance, empty memory) the same.
        fresh = self.instance(rest)
        self.assertEqual(self._send(fresh, thread, "one more").kwargs, {"tmid": "root-1"})

    def test_gateway_notices_follow_the_trigger_scope(self):
        rest = _fake_rest()
        instance = self.instance(rest)
        self.run_turn(instance, _payload(text=f"@{BOT} /model gpt", tmid="root-1"))
        self.assertEqual(rest.post_message.await_args.kwargs, {"tmid": "root-1"})
        self.run_turn(instance, _payload(message_id="m2", text=f"@{BOT} /model gpt"))
        self.assertEqual(rest.post_message.await_args.kwargs, {"tmid": ""})




if __name__ == "__main__":
    unittest.main()
