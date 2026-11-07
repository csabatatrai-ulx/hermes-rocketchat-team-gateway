"""Direct messages.

A DM's session belongs to Hermes core. The turn reaches core as ``chat_type="dm"``, ``thread_id`` is the real thread (or
``None``); a DM is one continuous session until ``/new``. Cases:

1. key isolation: a DM gets core's DM key, which does not match a room key even with the same rid; two DMs have two keys;
2. context: no block; with ``ROCKETCHAT_DM_SEED_MESSAGES > 0`` first contact (the bot has not answered) gets a seed from
   ``dm.history``;
3. group DM (3 participants): room rules;
4. a ``ROCKETCHAT_IGNORE_SENDERS`` sender starts no turn and does not enter the dedup cache;
5. a refused DM: no outgoing message, no text in the log line;
6. ``/new``: belongs to core, bare, on the same DM key;
7. privacy: a room turn's context never contains a DM message;
8. a DM thread's first turn: the thread's root (often the bot's message) and its replies so far as a block, while the bot
   has not written in the thread.

The pure cases run without Hermes core; the mixin-level ones need its source (see ``_adapter_test_support``).
"""

import asyncio
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)
from config import load_config  # noqa: E402
from dedup import InboundDeduplicator, OutboundDeduplicator  # noqa: E402
from dm_session import (  # noqa: E402
    bot_replied_since,
    is_group_direct_room,
    select_seed_messages,
    select_thread_seed,
)
from inbound import (  # noqa: E402
    HANDLE_MESSAGE,
    IGNORE_NO_MENTION,
    IGNORE_NOT_ALLOWED,
    IGNORE_SENDER,
    classify_inbound,
    is_ignored_sender,
)

BOT = "Hermes"
BOT_UID = "bot-uid"
CEST = timezone(timedelta(hours=2))
# Local (Europe/Berlin, summer time) 2026-10-06 10:00 = 08:00 UTC.
TRIGGER_TS = "2026-10-06T08:00:00.000Z"
SECRET = "Secret DM content: my favourite colour is blue"


def _local_iso(year, month, day, hour, minute, tz=CEST):
    return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _msg(_id, uid, uname, text, ts, **extra):
    return {"_id": _id, "u": {"_id": uid, "username": uname}, "msg": text, "ts": ts, **extra}


# ---------------------------------------------------------------------------
# 1. Segments (pure)
# ---------------------------------------------------------------------------


class TestPureHelpers(unittest.TestCase):
    def test_group_direct_room_by_users_count_then_uids(self):
        self.assertIs(is_group_direct_room({"t": "d", "usersCount": 3, "uids": ["a", "b"]}), True)
        self.assertIs(is_group_direct_room({"t": "d", "usersCount": 2, "uids": ["a", "b"]}), False)
        self.assertIs(is_group_direct_room({"t": "d", "uids": ["a", "b", "c"]}), True)
        self.assertIs(is_group_direct_room({"t": "d", "uids": ["a", "b"]}), False)
        self.assertIsNone(is_group_direct_room({"t": "d"}))
        self.assertIsNone(is_group_direct_room(None))

    def test_bot_replied_since(self):
        identity = dict(bot_user_id=BOT_UID, bot_username=BOT)
        msgs = [
            _msg("b0", BOT_UID, BOT, "yesterday's answer", "2026-10-05T10:00:00.000Z"),
            _msg("u1", "uid-alice", "alice", "hi", "2026-10-06T07:00:00.000Z"),
            _msg("s1", BOT_UID, BOT, "uj", "2026-10-06T07:10:00.000Z", t="uj"),
        ]
        start = datetime(2026, 10, 6, 4, 0, tzinfo=CEST).timestamp()
        self.assertFalse(bot_replied_since(msgs, start, **identity))  # a system message is not an answer
        self.assertTrue(bot_replied_since(msgs, None, **identity))

    def test_seed_is_the_last_n_real_messages(self):
        identity = dict(bot_user_id=BOT_UID, bot_username=BOT)
        msgs = [_msg(f"m{i}", "uid-alice", "alice", f"message {i}", TRIGGER_TS) for i in range(5)]
        msgs.insert(2, _msg("sys", "uid-alice", "alice", "alice", TRIGGER_TS, t="uj"))
        self.assertEqual([m["_id"] for m in select_seed_messages(msgs, count=3, **identity)],
                         ["m2", "m3", "m4"])
        self.assertEqual(select_seed_messages(msgs, count=0, **identity), [])

    def test_thread_seed_pins_the_root_even_if_it_is_the_bots_message(self):
        identity = dict(bot_user_id=BOT_UID, bot_username=BOT)
        root = _msg("r0", BOT_UID, BOT, "three options: A, B, C", "2026-10-06T07:00:00.000Z")
        replies = [_msg(f"t{i}", "uid-alice", "alice", f"thread reply {i}", TRIGGER_TS) for i in range(4)]
        self.assertEqual([m["_id"] for m in select_thread_seed(root, [], limit=50, **identity)], ["r0"])
        self.assertEqual([m["_id"] for m in select_thread_seed(root, replies, limit=3, **identity)],
                         ["r0", "t2", "t3"])
        self.assertEqual([m["_id"] for m in select_thread_seed(None, replies[:1], limit=50, **identity)],
                         ["t0"])
        self.assertEqual(select_thread_seed(root, replies, limit=0, **identity), [])

    def test_no_thread_seed_once_the_bot_wrote_in_the_thread(self):
        identity = dict(bot_user_id=BOT_UID, bot_username=BOT)
        root = _msg("r0", "uid-alice", "alice", "question", "2026-10-06T07:00:00.000Z")
        replies = [_msg("b1", BOT_UID, BOT, "reply in the thread", "2026-10-06T07:01:00.000Z")]
        self.assertEqual(select_thread_seed(root, replies, limit=50, **identity), [])
        # The bot's system message does not count as a reply in the thread.
        system = [_msg("s1", BOT_UID, BOT, "uj", "2026-10-06T07:01:00.000Z", t="uj")]
        self.assertEqual([m["_id"] for m in select_thread_seed(root, system, limit=50, **identity)], ["r0"])


class TestTimezoneConfig(unittest.TestCase):
    """``ROCKETCHAT_TIMEZONE``: the zone of the block's timestamps."""

    def test_default_is_the_local_time_zone(self):
        self.assertIsNone(load_config({}, {}).timezone)
        self.assertIsNone(load_config({}, {"ROCKETCHAT_TIMEZONE": "  "}).timezone)

    def test_iana_name_from_env_and_extra(self):
        cfg = load_config({}, {"ROCKETCHAT_TIMEZONE": " Europe/Berlin "})
        self.assertEqual(cfg.timezone, ZoneInfo("Europe/Berlin"))
        cfg = load_config({"timezone": "UTC"}, {"ROCKETCHAT_TIMEZONE": "Europe/Berlin"})
        self.assertEqual(cfg.timezone, ZoneInfo("Europe/Berlin"))  # az env nyer (env > YAML)

    def test_invalid_name_keeps_the_local_time_zone_with_a_warning(self):
        for raw in ("Mars/Olympus", "Europe", "../Europe/Berlin"):
            with self.subTest(raw=raw):
                with self.assertLogs("config", level="WARNING") as logs:
                    cfg = load_config({}, {"ROCKETCHAT_TIMEZONE": raw})
                self.assertIsNone(cfg.timezone)
                self.assertEqual(
                    [r.getMessage() for r in logs.records],
                    [f"Rocket.Chat: ROCKETCHAT_TIMEZONE={raw!r} is not a valid IANA time "
                     "zone, using the local time zone"],
                )

class TestDmConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config({}, {})
        self.assertEqual(cfg.dm_seed_messages, 0)
        self.assertEqual(cfg.ignore_senders, [])

    def test_values_from_env_and_extra(self):
        cfg = load_config({"dm_seed_messages": "5"}, {
            "ROCKETCHAT_DM_SEED_MESSAGES": "9",
            "ROCKETCHAT_IGNORE_SENDERS": "OtherBot, uid-x ,",
        })
        self.assertEqual(cfg.dm_seed_messages, 9)  # env wins (env over YAML, Hermes's rule)
        self.assertEqual(cfg.ignore_senders, ["OtherBot", "uid-x"])

    def test_invalid_values_keep_the_default_with_a_warning(self):
        with self.assertLogs("config", level="WARNING") as logs:
            cfg = load_config({}, {"ROCKETCHAT_DM_SEED_MESSAGES": "-1"})
        self.assertEqual(cfg.dm_seed_messages, 0)
        self.assertIn("ROCKETCHAT_DM_SEED_MESSAGES", "\n".join(logs.output))

    def test_seed_is_capped_at_100(self):
        with self.assertLogs("config", level="WARNING"):
            cfg = load_config({}, {"ROCKETCHAT_DM_SEED_MESSAGES": "500"})
        self.assertEqual(cfg.dm_seed_messages, 100)


# --------------------------------------------------------------------------- The
# decision chain (pure): the decision part of cases 4, 5 and 6
# ---------------------------------------------------------------------------


def _raw(**overrides):
    body = {
        "token": "",
        "message_id": "m1",
        "channel_id": "dmRID",
        "user_id": "uid-alice",
        "user_name": "alice",
        "text": "what is my favourite colour?",
        "timestamp": TRIGGER_TS,
    }
    body.update(overrides)
    return json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")


class TestClassifyDirect(unittest.TestCase):
    def classify(self, raw, **kw):
        kw.setdefault("bot_username", BOT)
        kw.setdefault("bot_user_id", BOT_UID)
        return classify_inbound(raw, **kw)

    def test_one_to_one_dm_needs_no_mention_and_is_direct(self):
        decision = self.classify(_raw())
        self.assertEqual(decision.outcome, HANDLE_MESSAGE)
        self.assertTrue(decision.is_direct)

    def test_group_dm_follows_the_room_rules(self):
        self.assertEqual(self.classify(_raw(), group_direct=True).outcome, IGNORE_NO_MENTION)
        decision = self.classify(_raw(text=f"@{BOT} hello"), group_direct=True)
        self.assertEqual(decision.outcome, HANDLE_MESSAGE)
        self.assertFalse(decision.is_direct)

    def test_room_message_is_not_direct(self):
        decision = self.classify(_raw(channel_name="general", text=f"@{BOT} hello"))
        self.assertFalse(decision.is_direct)

    def test_ignored_sender_by_username_case_insensitive_or_by_id(self):
        self.assertTrue(is_ignored_sender("uid-x", "OtherBot", ["otherbot"]))
        self.assertTrue(is_ignored_sender("uid-x", "other", ["uid-x"]))
        self.assertFalse(is_ignored_sender("uid-x", "other", ["UID-X"]))  # az id pontos
        self.assertFalse(is_ignored_sender("uid-x", "other", []))
        self.assertFalse(is_ignored_sender("", "", ["otherbot"]))

    def test_ignored_sender_comes_before_the_allowlist_and_the_dedup(self):
        seen = []
        decision = self.classify(
            _raw(user_name="OtherBot", user_id="uid-oc"),
            ignore_senders=["otherbot"],
            allowed_users=["uid-bob"],
            is_duplicate=lambda key: seen.append(key) or False,
        )
        self.assertEqual(decision.outcome, IGNORE_SENDER)
        self.assertEqual(seen, [])

    def test_ignored_sender_in_a_room_needs_the_mention_first(self):
        raw = _raw(channel_name="general", user_name="OtherBot", text="chatter")
        self.assertEqual(self.classify(raw, ignore_senders=["OtherBot"]).outcome, IGNORE_NO_MENTION)
        raw = _raw(channel_name="general", user_name="OtherBot", text=f"@{BOT} hello")
        self.assertEqual(self.classify(raw, ignore_senders=["OtherBot"]).outcome, IGNORE_SENDER)

    def test_not_allowed_dm_is_flagged_direct(self):
        decision = self.classify(_raw(), allowed_users=["uid-bob"])
        self.assertEqual(decision.outcome, IGNORE_NOT_ALLOWED)
        self.assertTrue(decision.is_direct)


# ---------------------------------------------------------------------------
# Mixin-level cases (need Hermes core's source)
# ---------------------------------------------------------------------------


class _FakeRest:
    """Per-room history and ``rooms.info``; every call recorded."""

    def __init__(self, histories=None, rooms=None, room_info_error=False):
        self.histories = histories or {}
        self.rooms = rooms or {}
        self.room_info_error = room_info_error
        self.room_history = AsyncMock(side_effect=self._room_history)
        self.room_info = AsyncMock(side_effect=self._room_info)
        self.get_message = AsyncMock(return_value=None)
        self.thread_messages = AsyncMock(return_value=[])
        self.post_message = AsyncMock(return_value={"success": True, "message": {"_id": "out"}})

    async def _room_history(self, room_id, **kwargs):
        return list(self.histories.get(room_id, []))

    async def _room_info(self, room_id):
        if self.room_info_error:
            return None
        return self.rooms.get(room_id, {"_id": room_id, "t": "d", "usersCount": 2})


def _dm_payload(**overrides):
    return _raw(**overrides)


def _room_payload(**overrides):
    body = dict(channel_id="room1", channel_name="general", text=f"@{BOT} what did the admin write?")
    body.update(overrides)
    return _raw(**body)


def _real_session_key(source):
    """Hermes core's real ``build_session_key`` for a bound ``source``."""
    from gateway.session import build_session_key

    full = SimpleNamespace(
        platform=SimpleNamespace(value="rocketchat"), scope_id=None, user_id_alt=None,
        prospective_thread_id=None, **vars(source),
    )
    full.message_id = None
    return build_session_key(full, group_sessions_per_user=True, thread_sessions_per_user=False)


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class _Base(unittest.TestCase):
    ENV = {}

    def setUp(self):
        # Pinned zone so the test runs the same on any machine.
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        self.webhook_mixin = load_mixin_module("webhook_mixin")
        self.adapter_module = load_mixin_module("adapter")
        env = {"ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS": "1000000",
               "ROCKETCHAT_TIMEZONE": "Europe/Berlin"}
        env.update(self.ENV)
        self.cfg = load_config({}, env)

    def tearDown(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()

    def instance(self, rest, cfg=None, **extra):
        attrs = dict(
            _cfg=cfg or self.cfg,
            _rest=rest,
            _bot_username=BOT,
            _user_id=BOT_UID,
            _webhook_secret="",
            _require_mention=True,
            _inbound_dedup=InboundDeduplicator(),
            _outbound_dedup=OutboundDeduplicator(ttl_seconds=10),
            _send_locks={},
            _pending_ack={},
            _safe_react=AsyncMock(),
            _inflight_augment_rooms={},
            _direct_room_kinds={},
            config=MagicMock(extra={}),
            _session_key_profile=MagicMock(return_value=None),
            _source_session_key=MagicMock(side_effect=lambda source: _real_session_key(source)),
            build_source=MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw)),
            handle_message=AsyncMock(),
        )
        attrs.update(extra)
        return make_bare_instance(self.adapter_module.RocketChatAdapter, **attrs)

    def run_turn(self, instance, raw):
        """One delivery through the whole chain (the entry point of the DDP and webhook paths)."""

        async def _go():
            decision = await instance._classify_inbound(raw)
            if decision.should_handle:
                await instance._dispatch_inbound_decision(decision)
            else:
                instance._log_ignored_delivery(decision)
            for _ in range(5):
                await asyncio.sleep(0)
            return decision

        return asyncio.run(_go())

    def last_event(self, instance):
        return instance.handle_message.await_args.args[0]


class TestKeyIsolation(_Base):
    """1. eset."""

    def test_dm_key_differs_from_a_room_key_with_the_same_rid_and_between_dms(self):
        instance = self.instance(_FakeRest())
        self.run_turn(instance, _dm_payload())
        dm_source = self.last_event(instance).source
        self.assertEqual(dm_source.chat_type, "dm")
        self.assertIsNone(dm_source.thread_id)

        # The same rid with room rules (group DM): a room key.
        group = self.instance(_FakeRest(rooms={"dmRID": {"t": "d", "usersCount": 3}}))
        self.run_turn(group, _dm_payload(message_id="m2", text=f"@{BOT} hello"))
        room_source = self.last_event(group).source
        self.assertEqual(room_source.chat_type, "channel")

        other = self.instance(_FakeRest())
        self.run_turn(other, _dm_payload(message_id="m3", channel_id="dmOTHER"))
        other_source = self.last_event(other).source

        keys = {_real_session_key(s) for s in (dm_source, room_source, other_source)}
        self.assertEqual(len(keys), 3)
        self.assertEqual(_real_session_key(dm_source), "agent:main:rocketchat:dm:dmRID")

    def test_no_daily_switch(self):
        instance = self.instance(_FakeRest())
        keys = []
        for mid, ts in (("m1", _local_iso(2026, 10, 6, 3, 58)),
                        ("m2", _local_iso(2026, 10, 6, 4, 1)),
                        ("m3", _local_iso(2026, 10, 9, 12, 0))):
            self.run_turn(instance, _dm_payload(message_id=mid, timestamp=ts))
            keys.append(_real_session_key(self.last_event(instance).source))
        self.assertEqual(len(set(keys)), 1)

    def test_a_dm_thread_is_its_own_session_and_the_reply_goes_there(self):
        rest = _FakeRest()
        instance = self.instance(rest)
        self.run_turn(instance, _dm_payload(tmid="root-1"))
        thread_source = self.last_event(instance).source
        self.assertEqual(thread_source.thread_id, "root-1")
        self.run_turn(instance, _dm_payload(message_id="m2"))
        main_source = self.last_event(instance).source
        self.assertNotEqual(_real_session_key(thread_source), _real_session_key(main_source))
        asyncio.run(instance.send("dmRID", "Done.", metadata={"thread_id": thread_source.thread_id}))
        rest.post_message.assert_awaited_with("dmRID", "Done.", tmid="root-1")
        asyncio.run(instance.send("dmRID", "Main.", metadata={"thread_id": main_source.thread_id}))
        rest.post_message.assert_awaited_with("dmRID", "Main.", tmid="")


class TestDmContext(_Base):
    """2. eset."""

    DM_HISTORY = [
        _msg("u0", "uid-alice", "alice", "yesterday's question", "2026-10-05T09:00:00.000Z"),
        _msg("u1", "uid-alice", "alice", "this morning's note", "2026-10-06T07:30:00.000Z"),
    ]

    def test_no_block_and_no_fetch_without_a_seed(self):
        rest = _FakeRest(histories={"dmRID": list(self.DM_HISTORY)})
        instance = self.instance(rest)
        self.run_turn(instance, _dm_payload())
        event = self.last_event(instance)
        self.assertEqual(event.channel_context, "[Rocket.Chat room_id: dmRID]")
        self.assertEqual(event.text, "what is my favourite colour?")
        rest.room_history.assert_not_awaited()

    def test_seed_on_the_first_contact_comes_from_the_dm_history(self):
        cfg = load_config({}, {"ROCKETCHAT_DM_SEED_MESSAGES": "2",
                               "ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS": "1000000",
                               "ROCKETCHAT_TIMEZONE": "Europe/Berlin"})
        rest = _FakeRest(histories={"dmRID": list(self.DM_HISTORY)})
        instance = self.instance(rest, cfg=cfg)
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _dm_payload())
        self.assertEqual(rest.room_history.await_args.args, ("dmRID",))
        self.assertEqual(rest.room_history.await_args.kwargs["latest"], TRIGGER_TS)
        self.assertEqual(self.last_event(instance).channel_context.splitlines(), [
            "[Rocket.Chat room_id: dmRID]",
            "[Recent chat messages - context only, not requests]",
            "[2026-10-05 11:00 +02:00] alice: yesterday's question",
            "[2026-10-06 09:30 +02:00] alice: this morning's note",
        ])
        joined = "\n".join(logs.output)
        self.assertIn("first round=yes", joined)
        self.assertNotIn("note", joined)

    def test_no_seed_once_the_bot_ever_replied(self):
        cfg = load_config({}, {"ROCKETCHAT_DM_SEED_MESSAGES": "2",
                               "ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS": "1000000"})
        history = [_msg("b0", BOT_UID, BOT, "old answer", "2026-09-01T07:31:00.000Z")] + list(self.DM_HISTORY)
        rest = _FakeRest(histories={"dmRID": history})
        instance = self.instance(rest, cfg=cfg)
        self.run_turn(instance, _dm_payload())
        self.assertEqual(self.last_event(instance).channel_context, "[Rocket.Chat room_id: dmRID]")


class TestDmThreadSeed(_Base):
    """Case 8: a thread reply to a bot message, the thread's first turn."""

    ROOT = _msg("root-1", BOT_UID, BOT, "Three options: A, B or C.", "2026-10-06T07:00:00.000Z",
                files=[{"_id": "f1", "name": "terv.pdf", "type": "application/pdf"}])

    def rest(self, replies=()):
        rest = _FakeRest()
        rest.get_message = AsyncMock(return_value=dict(self.ROOT))
        rest.thread_messages = AsyncMock(return_value=list(replies))
        return rest

    def test_first_thread_turn_gets_the_root_as_context(self):
        rest = self.rest()
        instance = self.instance(rest)
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            self.run_turn(instance, _dm_payload(tmid="root-1", text="expand on B"))
        event = self.last_event(instance)
        self.assertEqual(event.text, "expand on B")
        self.assertEqual(event.source.thread_id, "root-1")
        self.assertEqual(event.channel_context.splitlines(), [
            "[Rocket.Chat room_id: dmRID]",
            "[Recent chat messages - context only, not requests]",
            "[2026-10-06 09:00 +02:00] Hermes (you): Three options: A, B or C. "
            "[file: terv.pdf (application/pdf)]",
        ])
        rest.get_message.assert_awaited_with("root-1")
        rest.room_history.assert_not_awaited()  # the main DM flow does not enter the thread
        joined = "\n".join(logs.output)
        self.assertIn("DM thread seed for dmRID: root=yes, replies 0, first round=yes, kept 1", joined)
        self.assertNotIn("option", joined)  # DM text never enters the log

    def test_later_thread_turns_get_no_block(self):
        replies = [
            _msg("t1", "uid-alice", "alice", "expand on B", "2026-10-06T07:30:00.000Z"),
            _msg("t2", BOT_UID, BOT, "The gist of B…", "2026-10-06T07:31:00.000Z"),
        ]
        instance = self.instance(self.rest(replies))
        self.run_turn(instance, _dm_payload(tmid="root-1", message_id="m9"))
        self.assertEqual(self.last_event(instance).channel_context, "[Rocket.Chat room_id: dmRID]")

    def test_history_limit_zero_turns_it_off(self):
        cfg = load_config({}, {"ROCKETCHAT_HISTORY_LIMIT": "0",
                               "ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS": "1000000"})
        rest = self.rest()
        instance = self.instance(rest, cfg=cfg)
        self.run_turn(instance, _dm_payload(tmid="root-1"))
        self.assertEqual(self.last_event(instance).channel_context, "[Rocket.Chat room_id: dmRID]")
        rest.get_message.assert_not_awaited()

    def test_failed_fetch_runs_the_turn_without_a_block(self):
        rest = self.rest()
        rest.get_message = AsyncMock(side_effect=TimeoutError("slow server"))
        instance = self.instance(rest)
        self.run_turn(instance, _dm_payload(tmid="root-1"))
        self.assertEqual(self.last_event(instance).channel_context, "[Rocket.Chat room_id: dmRID]")

    def test_main_dm_flow_is_unchanged(self):
        rest = self.rest()
        instance = self.instance(rest)
        self.run_turn(instance, _dm_payload())
        self.assertEqual(self.last_event(instance).channel_context, "[Rocket.Chat room_id: dmRID]")
        rest.get_message.assert_not_awaited()


class TestGroupDm(_Base):
    """4. eset."""

    GROUP = {"_id": "dmRID", "t": "d", "usersCount": 3, "uids": ["bot-uid", "uid-alice", "uid-bob"]}

    def test_group_dm_needs_a_mention_and_runs_as_a_room_with_room_context(self):
        history = [_msg("u0", "uid-bob", "bob", "hello everyone", "2026-10-06T07:00:00.000Z")]
        rest = _FakeRest(histories={"dmRID": history}, rooms={"dmRID": dict(self.GROUP)})
        instance = self.instance(rest)
        decision = self.run_turn(instance, _dm_payload())
        self.assertEqual(decision.outcome, IGNORE_NO_MENTION)
        instance.handle_message.assert_not_awaited()

        decision = self.run_turn(instance, _dm_payload(message_id="m2", text=f"@{BOT} what's new?"))
        self.assertFalse(decision.is_direct)
        event = self.last_event(instance)
        # Runs as a room (channel, no thread), with the full window at the first
        # question.
        self.assertEqual(event.source.chat_type, "channel")
        self.assertIsNone(event.source.thread_id)
        self.assertIn("bob: hello everyone", event.channel_context)
        # One rooms.info per room.
        self.assertEqual(rest.room_info.await_count, 1)

    def test_one_to_one_lookup_is_cached_too(self):
        rest = _FakeRest()
        instance = self.instance(rest)
        self.run_turn(instance, _dm_payload())
        self.run_turn(instance, _dm_payload(message_id="m2"))
        self.assertEqual(rest.room_info.await_count, 1)
        self.assertEqual(instance._direct_room_kinds, {"dmRID": False})

    def test_failed_lookup_runs_as_one_to_one_and_asks_again(self):
        rest = _FakeRest(room_info_error=True)
        instance = self.instance(rest)
        with self.assertLogs(self.webhook_mixin.logger, level="WARNING") as logs:
            decision = self.run_turn(instance, _dm_payload())
            self.run_turn(instance, _dm_payload(message_id="m2"))
        self.assertTrue(decision.is_direct)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(rest.room_info.await_count, 2)

    def test_no_lookup_for_rooms_or_the_bots_own_dm_messages(self):
        rest = _FakeRest()
        instance = self.instance(rest)
        self.run_turn(instance, _room_payload())
        self.run_turn(instance, _dm_payload(message_id="m9", user_id=BOT_UID, user_name=BOT))
        rest.room_info.assert_not_awaited()


class TestIgnoredSender(_Base):
    """5. eset."""

    ENV = {"ROCKETCHAT_IGNORE_SENDERS": "OtherBot"}

    def test_ignored_sender_dm_starts_no_turn_and_is_not_recorded(self):
        rest = _FakeRest()
        instance = self.instance(rest)
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            decision = self.run_turn(instance, _dm_payload(user_id="uid-oc", user_name="otherbot",
                                                           text=SECRET))
        self.assertEqual(decision.outcome, IGNORE_SENDER)
        instance.handle_message.assert_not_awaited()
        rest.post_message.assert_not_awaited()
        # It did not enter the dedup cache: the same id is "first seen".
        self.assertFalse(instance._inbound_dedup.is_duplicate(f"{BOT}:m1"))
        joined = "\n".join(logs.output)
        self.assertIn("uid-oc", joined)
        self.assertNotIn("favourite", joined)


class TestNotAllowedDm(_Base):
    """6. eset."""

    ENV = {"ROCKETCHAT_ALLOWED_USERS": "uid-bob"}

    def test_not_allowed_dm_is_dropped_silently_with_an_id_only_log_line(self):
        rest = _FakeRest()
        instance = self.instance(rest)
        with self.assertLogs(self.webhook_mixin.logger, level="INFO") as logs:
            decision = self.run_turn(instance, _dm_payload(text=SECRET))
        self.assertEqual(decision.outcome, IGNORE_NOT_ALLOWED)
        instance.handle_message.assert_not_awaited()
        rest.post_message.assert_not_awaited()
        instance._safe_react.assert_not_awaited()
        joined = "\n".join(logs.output)
        self.assertIn("uid-alice", joined)
        self.assertIn("not on the allowlist", joined)
        self.assertNotIn("favourite", joined)


class TestNewInDm(_Base):
    """Case 6: ``/new`` belongs to core, on the same DM key."""

    def test_new_reaches_the_core_bare_on_the_dm_key(self):
        rest = _FakeRest()
        instance = self.instance(rest)
        self.run_turn(instance, _dm_payload())
        day_source = self.last_event(instance).source
        self.run_turn(instance, _dm_payload(message_id="m2", text="/new"))
        event = self.last_event(instance)
        self.assertEqual(event.text, "/new")
        self.assertEqual(event.get_command(), "new")
        self.assertEqual(_real_session_key(event.source), _real_session_key(day_source))
        rest.room_history.assert_not_awaited()


class TestCoreSenderInDm(_Base):
    """A DM is ``chat_type="dm"``: core adds no prefix (not a shared session); the session block names the user."""

    def test_no_prefix_in_a_dm(self):
        from gateway.run_inbound import GatewayInboundMixin

        instance = self.instance(_FakeRest())
        self.run_turn(instance, _dm_payload())
        event = self.last_event(instance)
        runner = SimpleNamespace(
            config=SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
        )
        prompt = GatewayInboundMixin._prefix_inbound_sender_context(
            runner, event, SimpleNamespace(platform=None, **vars(event.source)), event.text
        )
        self.assertEqual(prompt, "[Rocket.Chat room_id: dmRID]\n\n[New message]\nwhat is my favourite colour?")


class TestPrivacy(_Base):
    """Case 7: DM content cannot structurally enter a room prompt."""

    def _check(self, cfg):
        rest = _FakeRest(histories={
            "dmRID": [_msg("d1", "uid-alice", "alice", SECRET, "2026-10-06T07:00:00.000Z")],
            "room1": [_msg("r1", "uid-bob", "bob", "room message", "2026-10-06T07:05:00.000Z")],
        })
        instance = self.instance(rest, cfg=cfg)
        self.run_turn(instance, _dm_payload(text=SECRET))
        self.run_turn(instance, _room_payload(message_id="m2"))
        event = self.last_event(instance)
        self.assertEqual(event.source.chat_id, "room1")
        self.assertNotIn("favourite", event.channel_context)
        self.assertIn("room message", event.channel_context)
        self.assertEqual(rest.room_history.await_args.args, ("room1",))

    def test_room_turn_after_a_dm_with_and_without_a_seed(self):
        for seed in ("0", "5"):
            with self.subTest(seed=seed):
                self._check(load_config({}, {
                    "ROCKETCHAT_DM_SEED_MESSAGES": seed,
                    "ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS": "1000000",
                }))


if __name__ == "__main__":
    unittest.main()
