"""Unit tests for ddp_transport_mixin.py (the DDP inbound transport).

Two groups, split by whether the code path under test does a RELATIVE
import (``from .inbound import ...`` etc., which needs a real package - see
``_adapter_test_support.py``'s docstring) or not:

* ``ddp_transport_mixin`` itself has NO ``gateway.*``/relative imports at
  module scope (only ``_classify_ddp_inbound`` does, lazily, inside the
  method body) - so the pure-function and transport-state-machine tests
  below use the SAME flat ``sys.path.insert`` pattern as ``test_dedup.py``,
  unconditionally (no Hermes-core/gateway install needed to run them).
* ``_ddp_handle_message``/``_classify_ddp_inbound`` DO need the package
  loader (they import ``.inbound``/``.history``/etc.), so that one test
  class follows ``test_webhook_mixin.py``'s ``HERMES_AGENT_AVAILABLE``-gated
  pattern instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ddp_transport_mixin as ddp  # noqa: E402
from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)


class _FakeMsg:
    def __init__(self, type_, data=""):
        self.type = type_
        self.data = data


class _FakeResponse:
    def __init__(self, url):
        self.url = url


class _FakeWs:
    """Minimal ``aiohttp.ClientWebSocketResponse`` stand-in.

    ``frames`` is the pre-scripted sequence of INCOMING server frames (the
    async iteration never reflects what WE sent - a real WebSocket is two
    independent directions, and the dispatcher only reacts to what the
    server sends). ``sent`` records every outgoing ``send_json`` call for
    assertions on the connect/login/sub sequence.
    """

    def __init__(self, frames, response_url=None):
        self._frames = list(frames)
        self.closed = False
        self.sent = []
        self._response = _FakeResponse(response_url)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._frames.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class _FakeRest:
    def __init__(self, ws, rooms=None):
        self._ws = ws
        self.session = self
        self._rooms = rooms or {}

    async def subscribed_rooms(self):
        return dict(self._rooms)

    async def room_history(self, room_id, *, oldest="", count=0, show_thread_messages=None):
        self.history_calls = getattr(self, "history_calls", []) + [(room_id, oldest)]
        self.show_thread_messages = show_thread_messages
        return list((getattr(self, "history", {}) or {}).get(room_id, []))

    async def ws_connect(self, url, heartbeat=None):
        return self._ws

    async def post_message(self, room_id, text="", tmid=""):
        self.posted = getattr(self, "posted", []) + [(room_id, text, tmid)]
        return {}


TEXT = "TEXT_TYPE_MARKER"  # placeholder swapped for the real aiohttp enum below


def _text(payload: dict) -> _FakeMsg:
    import aiohttp

    return _FakeMsg(aiohttp.WSMsgType.TEXT, json.dumps(payload))


def _close_msg() -> _FakeMsg:
    import aiohttp

    return _FakeMsg(aiohttp.WSMsgType.CLOSE)


def _instance(rest, **extra_attrs):
    class _Cfg:
        base_url = "https://rc.example.test"
        ws_heartbeat_seconds = 30
        ddp_catchup_max_seconds = 600
        startup_catchup_max_seconds = 600
        startup_catchup_max_dispatches = 10
        startup_catchup_mode = "notice"
        dedup_state_dir = ""

    attrs = dict(
        _cfg=_Cfg(),
        _base_url="https://rc.example.test",
        _token="pat-token",
        _user_id="bot-uid",
        _rest=rest,
        _ddp_handler_tasks=set(),
    )
    attrs.update(extra_attrs)
    return make_bare_instance(ddp.DdpTransportMixin, **attrs)


class TestWebsocketUrl(unittest.TestCase):
    def test_https_becomes_wss_with_websocket_path(self):
        self.assertEqual(
            ddp.websocket_url("https://rc.example.test"),
            "wss://rc.example.test/websocket",
        )

    def test_http_becomes_ws(self):
        self.assertEqual(
            ddp.websocket_url("http://localhost:3000"),
            "ws://localhost:3000/websocket",
        )

    def test_trailing_slash_and_subpath_handled(self):
        self.assertEqual(
            ddp.websocket_url("https://rc.example.test/chat/"),
            "wss://rc.example.test/chat/websocket",
        )


class TestWebsocketEndpointMatches(unittest.TestCase):
    def test_none_final_url_is_treated_as_ok(self):
        self.assertTrue(ddp.websocket_endpoint_matches("wss://a/websocket", None))

    def test_matching_url_passes(self):
        self.assertTrue(
            ddp.websocket_endpoint_matches(
                "wss://rc.example.test/websocket", "wss://rc.example.test/websocket"
            )
        )

    def test_redirected_host_is_rejected(self):
        self.assertFalse(
            ddp.websocket_endpoint_matches(
                "wss://rc.example.test/websocket", "wss://evil.example/websocket"
            )
        )


class TestDdpMessageToWebhookPayload(unittest.TestCase):
    def test_maps_ddp_post_fields_onto_webhook_shape(self):
        post = {
            "_id": "msg1",
            "rid": "room1",
            "msg": "@Bot hello",
            "u": {"_id": "user1", "username": "alice"},
            "ts": {"$date": 123},
        }
        raw = ddp.ddp_message_to_webhook_payload(post, webhook_secret="s3cret")
        decoded = json.loads(raw)
        self.assertEqual(decoded["token"], "s3cret")
        self.assertEqual(decoded["message_id"], "msg1")
        self.assertEqual(decoded["channel_id"], "room1")
        self.assertEqual(decoded["text"], "@Bot hello")
        self.assertEqual(decoded["user_id"], "user1")
        self.assertEqual(decoded["user_name"], "alice")
        self.assertFalse(decoded["bot"])
        # The raw ts goes on to the age limit (inbound.is_stale_message).
        self.assertEqual(decoded["timestamp"], {"$date": 123})

    def test_thread_reply_carries_tmid(self):
        # The DDP message's tmid enters the webhook-shaped payload; empty for a
        # main-channel message.
        post = {"_id": "m", "rid": "r", "msg": "hi", "u": {}, "tmid": "root-1"}
        self.assertEqual(
            json.loads(ddp.ddp_message_to_webhook_payload(post, webhook_secret=""))["tmid"], "root-1"
        )
        post.pop("tmid")
        self.assertEqual(
            json.loads(ddp.ddp_message_to_webhook_payload(post, webhook_secret=""))["tmid"], ""
        )

    def test_system_message_type_is_carried(self):
        # ``t`` (system message type) goes on as ``message_type``; empty for a plain
        # message.
        post = {"_id": "m", "rid": "r", "msg": "Conference Call", "u": {}, "t": "videoconf"}
        self.assertEqual(
            json.loads(ddp.ddp_message_to_webhook_payload(post, webhook_secret=""))["message_type"], "videoconf"
        )
        post.pop("t")
        self.assertEqual(
            json.loads(ddp.ddp_message_to_webhook_payload(post, webhook_secret=""))["message_type"], ""
        )

    def test_missing_channel_name_falls_back_to_room_id(self):
        post = {"_id": "m", "rid": "roomX", "msg": "hi", "u": {}}
        decoded = json.loads(
            ddp.ddp_message_to_webhook_payload(post, webhook_secret="")
        )
        self.assertEqual(decoded["channel_name"], "roomX")


class TestRoomTypeInPayload(unittest.TestCase):
    def test_room_type_reaches_the_webhook_shaped_payload(self):
        raw = ddp.ddp_message_to_webhook_payload(
            {"_id": "m1", "rid": "r1", "msg": "hi", "_room_type": "d"}, webhook_secret=""
        )
        self.assertEqual(json.loads(raw)["room_type"], "d")


class TestPermanentAuthErrorClassification(unittest.TestCase):
    def test_ddp_login_error_403_is_permanent(self):
        exc = ddp.DdpLoginError("nope", error_code=403)
        self.assertTrue(ddp.DdpTransportMixin._ddp_is_permanent_auth_error(exc))

    def test_ddp_login_error_other_code_is_not_permanent(self):
        exc = ddp.DdpLoginError("server hiccup", error_code=500)
        self.assertFalse(ddp.DdpTransportMixin._ddp_is_permanent_auth_error(exc))

    def test_ws_handshake_401_is_permanent_type_based_only(self):
        import aiohttp

        exc = aiohttp.WSServerHandshakeError.__new__(aiohttp.WSServerHandshakeError)
        exc.status = 401
        self.assertTrue(ddp.DdpTransportMixin._ddp_is_permanent_auth_error(exc))

    def test_message_text_containing_401_is_NOT_permanent(self):
        # The whole point of the type-only rule: a transient error whose
        # message merely CONTAINS "401" must not be misclassified.
        exc = RuntimeError("upstream returned 401 in an unrelated log line")
        self.assertFalse(ddp.DdpTransportMixin._ddp_is_permanent_auth_error(exc))


class TestFrameDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_ping_gets_a_pong_reply(self):
        instance = _instance(_FakeRest(None), _ddp_subs={}, _ddp_pending_calls={})
        instance._ddp_ws = _FakeWs([])
        await instance._ddp_dispatch_frame({"msg": "ping", "id": "77"})
        self.assertEqual(instance._ddp_ws.sent, [{"msg": "pong", "id": "77"}])

    async def test_ready_marks_subs_true(self):
        instance = _instance(
            _FakeRest(None), _ddp_subs={"sub1": False}, _ddp_pending_calls={}
        )
        await instance._ddp_dispatch_frame({"msg": "ready", "subs": ["sub1"]})
        self.assertTrue(instance._ddp_subs["sub1"])

    async def test_nosub_removes_bookkeeping(self):
        instance = _instance(
            _FakeRest(None), _ddp_subs={"sub1": False}, _ddp_pending_calls={}
        )
        await instance._ddp_dispatch_frame(
            {"msg": "nosub", "id": "sub1", "error": {"reason": "denied"}}
        )
        self.assertNotIn("sub1", instance._ddp_subs)

    async def test_result_resolves_the_matching_pending_call(self):
        instance = _instance(_FakeRest(None), _ddp_subs={}, _ddp_pending_calls={})
        future = asyncio.get_event_loop().create_future()
        instance._ddp_pending_calls["0"] = future
        await instance._ddp_dispatch_frame({"msg": "result", "id": "0", "result": {"ok": True}})
        self.assertEqual(await future, {"ok": True})

    async def test_changed_on_other_collection_is_ignored(self):
        instance = _instance(_FakeRest(None), _ddp_subs={}, _ddp_pending_calls={})
        instance._ddp_handle_message = AsyncMock()
        await instance._ddp_dispatch_frame(
            {"msg": "changed", "collection": "some-other-stream", "fields": {"args": [{}]}}
        )
        instance._ddp_handle_message.assert_not_awaited()

    async def test_changed_for_member_room_dispatches_with_room_type(self):
        instance = _instance(_FakeRest(None), _ddp_subs={}, _ddp_pending_calls={})
        instance._ddp_handle_message = AsyncMock()
        post = {"_id": "m1", "rid": "dm-1", "msg": "hi"}
        meta = {"roomParticipant": True, "roomType": "d", "roomName": None}
        await instance._ddp_dispatch_frame(
            {"msg": "changed", "collection": "stream-room-messages",
             "fields": {"eventName": "__my_messages__", "args": [post, meta]}}
        )
        await asyncio.gather(*instance._ddp_handler_tasks)
        instance._ddp_handle_message.assert_awaited_once_with({**post, "_room_type": "d"})

    async def test_changed_for_non_member_room_is_ignored(self):
        # __my_messages__ delivers public-channel traffic to every account;
        # the bot must never act outside the rooms it was added to (else a
        # mention could pull it into any public channel).
        instance = _instance(_FakeRest(None), _ddp_subs={}, _ddp_pending_calls={})
        instance._ddp_handle_message = AsyncMock()
        meta = {"roomParticipant": False, "roomType": "c", "roomName": "other"}
        await instance._ddp_dispatch_frame(
            {"msg": "changed", "collection": "stream-room-messages",
             "fields": {"eventName": "__my_messages__", "args": [{"_id": "m1", "rid": "other"}, meta]}}
        )
        instance._ddp_handle_message.assert_not_awaited()

    async def test_member_room_message_advances_the_catch_up_mark(self):
        instance = _instance(_FakeRest(None), _ddp_subs={}, _ddp_pending_calls={})
        instance._ddp_handle_message = AsyncMock()
        await instance._ddp_dispatch_frame(
            {"msg": "changed", "collection": "stream-room-messages",
             "fields": {"eventName": "__my_messages__",
                        "args": [{"_id": "m1", "ts": {"$date": 5000}}, {"roomParticipant": True}]}}
        )
        await asyncio.gather(*instance._ddp_handler_tasks)
        self.assertEqual(instance._ddp_last_message_at, 5.0)


class TestStreamMeta(unittest.TestCase):
    def test_returns_the_second_stream_arg(self):
        meta = {"roomParticipant": False, "roomType": "c", "roomName": "x"}
        self.assertEqual(ddp.stream_meta([{}, meta]), meta)

    def test_absent_or_malformed_metadata_is_empty(self):
        self.assertEqual(ddp.stream_meta([{}]), {})
        self.assertEqual(ddp.stream_meta([{}, None]), {})


class TestCatchUpSince(unittest.TestCase):
    def test_first_connect_of_the_process_has_no_gap(self):
        self.assertIsNone(ddp.catch_up_since(None, 1_000.0, 600))

    def test_disabled(self):
        self.assertIsNone(ddp.catch_up_since(900.0, 1_000.0, 0))

    def test_short_gap_replays_from_its_start_with_overlap(self):
        self.assertEqual(ddp.catch_up_since(900.0, 1_000.0, 600), 895.0)

    def test_long_gap_is_capped(self):
        # A long outage must not make the bot answer hours-old mentions.
        self.assertEqual(ddp.catch_up_since(0.0, 10_000.0, 600), 9_400.0)

    def test_message_epoch_seconds_both_shapes(self):
        self.assertEqual(ddp.message_epoch_seconds({"ts": {"$date": 1500}}), 1.5)
        self.assertEqual(ddp.message_epoch_seconds({"ts": "1970-01-01T00:00:02.000Z"}), 2.0)
        self.assertIsNone(ddp.message_epoch_seconds({}))

    def test_rc_timestamp_format(self):
        self.assertEqual(ddp.rc_timestamp(0.5), "1970-01-01T00:00:00.500Z")


class TestCatchUp(unittest.IsolatedAsyncioTestCase):
    async def test_messages_from_the_gap_go_through_the_normal_handler(self):
        # The member-room list comes from REST (subscriptions.get) only when a
        # catch-up is actually due.
        rest = _FakeRest(None, rooms={"dm-1": "d"})
        rest.history = {"dm-1": [{"_id": "m1", "rid": "dm-1", "msg": "who am I?"}]}
        instance = _instance(rest)
        instance._ddp_handle_message = AsyncMock()
        await instance._ddp_catch_up(ddp.time.time() - 30)
        instance._ddp_handle_message.assert_awaited_once_with(
            {"_id": "m1", "rid": "dm-1", "msg": "who am I?", "_room_type": "d"}
        )

    async def test_catch_up_includes_thread_replies(self):
        # A thread mention during an outage must not be missed either
        # (channels/dm.history leave thread replies out by default).
        rest = _FakeRest(None, rooms={"r1": "c"})
        instance = _instance(rest)
        instance._ddp_handle_message = AsyncMock()
        await instance._ddp_catch_up(ddp.time.time() - 30)
        self.assertIs(rest.show_thread_messages, True)

    async def test_first_connect_does_not_fetch_history(self):
        rest = _FakeRest(None, rooms={"r1": "c"})
        instance = _instance(rest)
        instance._ddp_handle_message = AsyncMock()
        await instance._ddp_catch_up(None)
        self.assertFalse(hasattr(rest, "history_calls"))
        instance._ddp_handle_message.assert_not_awaited()


class TestStartupCatchUp(unittest.IsolatedAsyncioTestCase):
    """The fresh-start check: cursor, ``notice``/``replay``/``off``."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _startup_instance(self, rest, *, mode="notice", cap=10, cursor_age=60.0):
        instance = _instance(rest, _webhook_secret="")
        instance._cfg.dedup_state_dir = self._tmp.name
        instance._cfg.startup_catchup_mode = mode
        instance._cfg.startup_catchup_max_dispatches = cap
        if cursor_age is not None:
            with open(os.path.join(self._tmp.name, "ddp_cursor.json"), "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "lastMessageAt": ddp.time.time() - cursor_age}, fh)
        decisions = {}

        async def classify(raw_body):
            return decisions.get(json.loads(raw_body)["message_id"])

        instance._classify_inbound = classify
        instance._dispatch_inbound_decision = AsyncMock()
        instance._log_ignored_delivery = lambda decision: None
        return instance, decisions

    @staticmethod
    def _decision(room, user, *, tmid="", direct=False, handle=True, mid="", name=""):
        return SimpleNamespace(
            should_handle=handle, channel_id=room, thread_id=tmid, is_direct=direct,
            sender_username=user, message_id=mid, channel_name=name,
        )

    @staticmethod
    def _msg(mid, room, seconds_ago):
        return {"_id": mid, "rid": room, "msg": "x", "u": {"username": "u"},
                "ts": {"$date": int((ddp.time.time() - seconds_ago) * 1000)}}

    async def test_notice_mode_dispatches_nothing_and_sends_one_notice_per_room(self):
        rest = _FakeRest(None, rooms={"r1": "c", "dm-1": "d"})
        rest.history = {
            "r1": [self._msg("m1", "r1", 30), self._msg("m2", "r1", 20)],
            "dm-1": [self._msg("m3", "dm-1", 10)],
        }
        instance, decisions = self._startup_instance(rest)
        decisions.update({
            "m1": self._decision("r1", "bea"),
            "m2": self._decision("r1", "anna"),
            "m3": self._decision("dm-1", "anna", direct=True),
        })
        await instance._ddp_catch_up(None)
        instance._dispatch_inbound_decision.assert_not_awaited()
        posted = {room: (text, tmid) for room, text, tmid in rest.posted}
        self.assertEqual(len(rest.posted), 2)
        self.assertTrue(posted["r1"][0].startswith("@anna @bea "))
        self.assertIn("I restarted", posted["r1"][0])
        self.assertFalse(posted["dm-1"][0].startswith("@"))

    async def test_replay_mode_dispatches_up_to_the_cap_oldest_first(self):
        rest = _FakeRest(None, rooms={"r1": "c", "r2": "c"})
        rest.history = {"r1": [self._msg("new", "r1", 5)], "r2": [self._msg("old", "r2", 50)]}
        instance, decisions = self._startup_instance(rest, mode="replay", cap=1)
        first, second = self._decision("r2", "anna"), self._decision("r1", "bea")
        decisions.update({"old": first, "new": second})
        await instance._ddp_catch_up(None)
        instance._dispatch_inbound_decision.assert_awaited_once_with(first)
        self.assertEqual([(room, tmid) for room, _text, tmid in rest.posted], [("r1", "")])

    async def test_notice_counts_and_quotes_the_missed_messages(self):
        rest = _FakeRest(None, rooms={"r1": "c"})
        rest.history = {"r1": [self._msg("m1", "r1", 30), self._msg("m2", "r1", 20)]}
        instance, decisions = self._startup_instance(rest)
        instance._site_url = "https://chat.example"
        decisions.update({
            "m1": self._decision("r1", "anna", mid="m1", name="general"),
            "m2": self._decision("r1", "anna", mid="m2", name="general"),
        })
        await instance._ddp_catch_up(None)
        text = rest.posted[0][1]
        self.assertIn("2 messages", text)
        self.assertIn("[ ](https://chat.example/channel/general?msg=m1)", text)
        self.assertIn("[ ](https://chat.example/channel/general?msg=m2)", text)

    async def test_thread_notice_goes_into_the_thread(self):
        rest = _FakeRest(None, rooms={"r1": "c"})
        rest.history = {"r1": [self._msg("m1", "r1", 30)]}
        instance, decisions = self._startup_instance(rest)
        decisions["m1"] = self._decision("r1", "anna", tmid="root-1")
        await instance._ddp_catch_up(None)
        self.assertEqual(rest.posted[0][2], "root-1")

    async def test_ignored_messages_get_neither_dispatch_nor_notice(self):
        rest = _FakeRest(None, rooms={"r1": "c"})
        rest.history = {"r1": [self._msg("m1", "r1", 30)]}
        instance, decisions = self._startup_instance(rest, mode="replay")
        decisions["m1"] = self._decision("r1", "anna", handle=False)
        await instance._ddp_catch_up(None)
        instance._dispatch_inbound_decision.assert_not_awaited()
        self.assertFalse(hasattr(rest, "posted"))

    async def test_english_notice(self):
        rest = _FakeRest(None, rooms={"dm-1": "d"})
        rest.history = {"dm-1": [self._msg("m1", "dm-1", 30)]}
        instance, decisions = self._startup_instance(rest)
        decisions["m1"] = self._decision("dm-1", "anna", direct=True)
        await instance._ddp_catch_up(None)
        self.assertIn("I restarted", rest.posted[0][1])

    async def test_no_cursor_means_no_history_read(self):
        # First start on this state folder: with an empty dedup it would also notify
        # about or answer a message an earlier instance already answered.
        rest = _FakeRest(None, rooms={"r1": "c"})
        instance, _decisions = self._startup_instance(rest, cursor_age=None)
        await instance._ddp_catch_up(None)
        self.assertFalse(hasattr(rest, "history_calls"))

    async def test_mode_off_reads_nothing(self):
        rest = _FakeRest(None, rooms={"r1": "c"})
        instance, _decisions = self._startup_instance(rest, mode="off")
        await instance._ddp_catch_up(None)
        self.assertFalse(hasattr(rest, "history_calls"))

    async def test_window_is_capped_by_max_seconds(self):
        rest = _FakeRest(None, rooms={"r1": "c"})
        instance, _decisions = self._startup_instance(rest, cursor_age=86_400)
        await instance._ddp_catch_up(None)
        oldest = rest.history_calls[0][1]
        self.assertAlmostEqual(
            ddp.rc_timestamp_seconds(oldest), ddp.time.time() - 600, delta=5
        )

    async def test_cursor_round_trip_and_corrupt_file(self):
        instance, _decisions = self._startup_instance(_FakeRest(None), cursor_age=None)
        instance._ddp_last_message_at = 1234.5
        await instance._ddp_flush_cursor()
        self.assertEqual(instance._ddp_load_cursor(), 1234.5)
        with open(os.path.join(self._tmp.name, "ddp_cursor.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(instance._ddp_load_cursor())


class TestDdpNotify(unittest.IsolatedAsyncioTestCase):
    """``_ddp_notify``: the fire-and-forget sibling of ``_ddp_call_method``. It sends a "method" frame but never
        registers or awaits a pending future.
    """

    async def test_sends_a_method_frame_with_the_given_params(self):
        instance = _instance(_FakeRest(None), _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ws = _FakeWs([])
        await instance._ddp_notify("stream-notify-room", ["room1/user-activity", "Bot", ["user-typing"], {}])
        self.assertEqual(
            instance._ddp_ws.sent,
            [{"msg": "method", "method": "stream-notify-room", "id": "0",
              "params": ["room1/user-activity", "Bot", ["user-typing"], {}]}],
        )

    async def test_does_not_register_a_pending_call(self):
        # No future is created/awaited - a "result" frame for this call id
        # must be a harmless no-op in _ddp_dispatch_frame, not a KeyError or
        # a hang.
        instance = _instance(_FakeRest(None), _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ws = _FakeWs([])
        await instance._ddp_notify("stream-notify-room", ["room1/user-activity", "Bot", [], {}])
        self.assertEqual(instance._ddp_pending_calls, {})
        # Dispatching a (hypothetical) result frame for this id must not raise.
        await instance._ddp_dispatch_frame({"msg": "result", "id": "0", "result": None})

    async def test_increments_the_call_id_across_calls(self):
        instance = _instance(_FakeRest(None), _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ws = _FakeWs([])
        await instance._ddp_notify("stream-notify-room", ["a"])
        await instance._ddp_notify("stream-notify-room", ["b"])
        self.assertEqual([f["id"] for f in instance._ddp_ws.sent], ["0", "1"])

    async def test_no_ws_connection_is_a_silent_no_op(self):
        # _ddp_send already guards on ws is None/closed - _ddp_notify must not
        # add its own, different failure mode on top of that.
        instance = _instance(_FakeRest(None), _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ws = None
        await instance._ddp_notify("stream-notify-room", ["room1/user-activity", "Bot", ["user-typing"], {}])


class TestConnectAndListen(unittest.IsolatedAsyncioTestCase):
    async def test_successful_handshake_sends_connect_login_sub_in_order(self):
        ws = _FakeWs(
            frames=[
                _text({"msg": "result", "id": "0", "result": {"id": "u1"}}),
                _close_msg(),
            ],
            response_url="wss://rc.example.test/websocket",
        )
        instance = _instance(_FakeRest(ws), _ddp_subs={}, _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ready = asyncio.Event()

        await instance._ddp_connect_and_listen()

        kinds = [frame["msg"] for frame in ws.sent]
        self.assertEqual(kinds[:2], ["connect", "method"])
        self.assertEqual(ws.sent[1]["method"], "login")
        self.assertEqual(ws.sent[1]["params"], [{"resume": "pat-token"}])
        subs = [(f["name"], f["params"]) for f in ws.sent if f["msg"] == "sub"]
        # ONE subscription for every room (Rocket.Chat rate-limits DDP
        # subscriptions per connection - a per-room design broke at ~40 rooms).
        self.assertEqual(subs, [("stream-room-messages", ["__my_messages__", False])])
        self.assertTrue(instance._ddp_ready.is_set())

    async def test_login_error_response_raises_ddp_login_error(self):
        ws = _FakeWs(
            frames=[
                _text({"msg": "result", "id": "0", "error": {"error": 403, "reason": "bad token"}}),
            ],
            response_url="wss://rc.example.test/websocket",
        )
        instance = _instance(_FakeRest(ws), _ddp_subs={}, _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ready = asyncio.Event()

        with self.assertRaises(ddp.DdpLoginError) as ctx:
            await instance._ddp_connect_and_listen()
        self.assertEqual(ctx.exception.error_code, 403)
        # A rejected login must NOT have gone on to subscribe.
        self.assertNotIn("sub", [f["msg"] for f in ws.sent])

    async def test_redirected_endpoint_raises_before_any_send(self):
        ws = _FakeWs(frames=[], response_url="wss://evil.example/websocket")
        instance = _instance(_FakeRest(ws), _ddp_subs={}, _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ready = asyncio.Event()

        with self.assertRaises(RuntimeError):
            await instance._ddp_connect_and_listen()
        self.assertEqual(ws.sent, [])
        self.assertTrue(ws.closed)


class TestReconnectLoop(unittest.IsolatedAsyncioTestCase):
    async def test_exponential_backoff_with_no_jitter_doubles_each_time(self):
        instance = _instance(_FakeRest(None))
        instance._ddp_closing = False
        instance._ddp_ready = asyncio.Event()
        sleeps = []

        call_count = {"n": 0}

        async def fake_connect_and_listen():
            call_count["n"] += 1
            if call_count["n"] >= 4:
                instance._ddp_closing = True
            raise RuntimeError("transient network blip")

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        instance._ddp_connect_and_listen = fake_connect_and_listen
        with patch.object(ddp.random, "random", return_value=0.0), \
             patch.object(ddp.asyncio, "sleep", fake_sleep):
            await instance._ddp_loop()

        self.assertEqual(sleeps, [2.0, 4.0, 8.0])

    async def test_permanent_auth_error_stops_reconnecting_without_sleep(self):
        instance = _instance(_FakeRest(None))
        instance._ddp_closing = False
        instance._ddp_ready = asyncio.Event()

        async def fake_connect_and_listen():
            raise ddp.DdpLoginError("nope", error_code=401)

        instance._ddp_connect_and_listen = fake_connect_and_listen
        with patch.object(ddp.asyncio, "sleep", AsyncMock()) as sleep_mock:
            await instance._ddp_loop()
        sleep_mock.assert_not_called()


class TestDdpStatusFile(unittest.IsolatedAsyncioTestCase):
    """The readiness probe's ddp_status.json."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _state(self):
        with open(os.path.join(self._tmp.name, "ddp_status.json"), encoding="utf-8") as fh:
            return json.load(fh)["state"]

    async def test_connected_only_after_subscribe(self):
        ws = _FakeWs(
            frames=[_text({"msg": "result", "id": "0", "result": {"id": "u1"}}), _close_msg()],
            response_url="wss://rc.example.test/websocket",
        )
        instance = _instance(_FakeRest(ws), _ddp_subs={}, _ddp_pending_calls={}, _ddp_next_id=0)
        instance._cfg.dedup_state_dir = self._tmp.name
        instance._ddp_ready = asyncio.Event()

        await instance._ddp_connect_and_listen()

        self.assertEqual(self._state(), "connected")

    async def test_rejected_login_never_reports_connected(self):
        ws = _FakeWs(
            frames=[_text({"msg": "result", "id": "0", "error": {"error": 403, "reason": "bad"}})],
            response_url="wss://rc.example.test/websocket",
        )
        instance = _instance(_FakeRest(ws), _ddp_subs={}, _ddp_pending_calls={}, _ddp_next_id=0)
        instance._cfg.dedup_state_dir = self._tmp.name
        instance._ddp_ready = asyncio.Event()

        with self.assertRaises(ddp.DdpLoginError):
            await instance._ddp_connect_and_listen()
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "ddp_status.json")))

    async def test_outage_reports_reconnecting(self):
        instance = _instance(_FakeRest(None))
        instance._cfg.dedup_state_dir = self._tmp.name
        instance._ddp_closing = False
        instance._ddp_ready = asyncio.Event()
        states = []

        async def fake_connect_and_listen():
            raise RuntimeError("transient network blip")

        async def fake_sleep(seconds):
            states.append(self._state())
            instance._ddp_closing = True

        instance._ddp_connect_and_listen = fake_connect_and_listen
        with patch.object(ddp.asyncio, "sleep", fake_sleep):
            await instance._ddp_loop()

        self.assertEqual(states, ["reconnecting"])

    async def test_permanent_auth_error_reports_auth_failed(self):
        instance = _instance(_FakeRest(None))
        instance._cfg.dedup_state_dir = self._tmp.name
        instance._ddp_closing = False
        instance._ddp_ready = asyncio.Event()

        async def fake_connect_and_listen():
            raise ddp.DdpLoginError("nope", error_code=401)

        instance._ddp_connect_and_listen = fake_connect_and_listen
        await instance._ddp_loop()

        self.assertEqual(self._state(), "auth_failed")

    async def test_no_state_dir_writes_nothing(self):
        instance = _instance(_FakeRest(None))
        await instance._ddp_write_status("connected")
        self.assertEqual(os.listdir(self._tmp.name), [])


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestDdpHandleMessageDispatch(unittest.TestCase):
    """The one piece that DOES need the real package (relative imports) -
    see the module docstring."""

    def setUp(self):
        from config import load_config
        from dedup import InboundDeduplicator

        ddp_module = load_mixin_module("ddp_transport_mixin")
        webhook_module = load_mixin_module("webhook_mixin")
        self.DdpTransportMixin = ddp_module.DdpTransportMixin
        self.ddp_message_to_webhook_payload = ddp_module.ddp_message_to_webhook_payload
        # _ddp_handle_message calls self._classify_inbound_from_body(), which
        # lives on WebhookInboundMixin (the ONE shared classify_inbound call
        # site, see ddp_transport_mixin.py's docstring on that method) - on
        # the real RocketChatAdapter both mixins are always composed together
        # regardless of ROCKETCHAT_TRANSPORT, so combine them here too rather
        # than testing DdpTransportMixin in a shape it never actually runs in.
        self.CombinedMixin = type(
            "CombinedDdpWebhookMixin",
            (ddp_module.DdpTransportMixin, webhook_module.WebhookInboundMixin),
            {},
        )
        self.cfg = load_config({}, token="t")
        self.dedup = InboundDeduplicator()

    def _instance(self, **extra):
        attrs = dict(
            _bot_username="Bot",
            _user_id="bot-uid",
            _webhook_secret="s3cret",
            _require_mention=True,
            _cfg=self.cfg,
            _inbound_dedup=self.dedup,
        )
        attrs.update(extra)
        return make_bare_instance(self.CombinedMixin, **attrs)

    def test_mentioned_message_is_dispatched(self):
        instance = self._instance()
        instance._dispatch_inbound_decision = AsyncMock()
        post = {
            "_id": "m1", "rid": "room1", "msg": "@Bot hello",
            "u": {"_id": "u1", "username": "someone"},
        }
        asyncio.run(instance._ddp_handle_message(post))
        instance._dispatch_inbound_decision.assert_awaited_once()
        decision = instance._dispatch_inbound_decision.call_args[0][0]
        self.assertEqual(decision.channel_id, "room1")
        self.assertEqual(decision.sender_username, "someone")

    def test_thread_mention_keeps_its_thread(self):
        # The DDP message's tmid is the decision's thread_id (same as the webhook path).
        instance = self._instance()
        instance._dispatch_inbound_decision = AsyncMock()
        post = {
            "_id": "m9", "rid": "room1", "msg": "@Bot hello", "tmid": "root-1",
            "u": {"_id": "u1", "username": "someone"},
        }
        asyncio.run(instance._ddp_handle_message(post))
        decision = instance._dispatch_inbound_decision.call_args[0][0]
        self.assertEqual(decision.thread_id, "root-1")

    def test_message_without_mention_is_not_dispatched(self):
        instance = self._instance()
        instance._dispatch_inbound_decision = AsyncMock()
        post = {
            "_id": "m2", "rid": "room1", "msg": "just chatting, no mention",
            "u": {"_id": "u1", "username": "someone"},
        }
        asyncio.run(instance._ddp_handle_message(post))
        instance._dispatch_inbound_decision.assert_not_awaited()

    def test_own_message_is_not_dispatched(self):
        instance = self._instance()
        instance._dispatch_inbound_decision = AsyncMock()
        post = {
            "_id": "m3", "rid": "room1", "msg": "@Bot hello",
            "u": {"_id": "bot-uid", "username": "Bot"},
        }
        asyncio.run(instance._ddp_handle_message(post))
        instance._dispatch_inbound_decision.assert_not_awaited()


class TestIsMethodNotFound(unittest.TestCase):
    """Recognising a DDP method removed by Rocket.Chat 9.0."""

    def test_meteor_method_not_found_frame(self):
        frame = {"msg": "result", "id": "3", "error": {
            "isClientSafe": True, "error": 404,
            "reason": "Method 'sendMessage' not found",
            "message": "Method 'sendMessage' not found [404]",
            "errorType": "Meteor.Error",
        }}
        self.assertTrue(ddp.is_method_not_found(frame))

    def test_string_code_is_accepted(self):
        self.assertTrue(ddp.is_method_not_found(
            {"error": {"error": "404", "reason": "Method 'x' not found"}}
        ))

    def test_other_errors_are_not_not_found(self):
        self.assertFalse(ddp.is_method_not_found(
            {"error": {"error": "error-not-allowed", "reason": "Not allowed"}}
        ))
        self.assertFalse(ddp.is_method_not_found({"error": {"error": 404, "reason": "Room not"}}))
        self.assertFalse(ddp.is_method_not_found({"error": "boom"}))
        self.assertFalse(ddp.is_method_not_found({"_id": "m1"}))
        self.assertFalse(ddp.is_method_not_found(None))


class TestUnsupportedMethodDetection(unittest.IsolatedAsyncioTestCase):
    NOT_FOUND = {"error": 404, "reason": "Method 'stream-notify-room' not found"}

    def _instance(self):
        instance = _instance(_FakeRest(None), _ddp_pending_calls={}, _ddp_next_id=0)
        instance._ddp_ws = _FakeWs([])
        return instance

    async def test_notify_not_found_result_marks_the_method_unsupported(self):
        instance = self._instance()
        self.assertTrue(instance.ddp_method_supported("stream-notify-room"))
        await instance._ddp_notify("stream-notify-room", ["room1/user-activity", "Bot", [], {}])
        with self.assertLogs(ddp.logger, level="WARNING"):
            await instance._ddp_dispatch_frame({"msg": "result", "id": "0", "error": self.NOT_FOUND})
        self.assertFalse(instance.ddp_method_supported("stream-notify-room"))
        self.assertEqual(instance._ddp_notify_inflight, {})

    async def test_other_notify_errors_keep_the_method(self):
        instance = self._instance()
        await instance._ddp_notify("stream-notify-room", ["x"])
        await instance._ddp_dispatch_frame(
            {"msg": "result", "id": "0", "error": {"error": "not-allowed", "reason": "Not allowed"}}
        )
        self.assertTrue(instance.ddp_method_supported("stream-notify-room"))

    async def test_warns_only_once_per_method(self):
        instance = self._instance()
        with self.assertLogs(ddp.logger, level="WARNING") as logs:
            instance._ddp_note_method_result("sendMessage", {"error": self.NOT_FOUND})
            instance._ddp_note_method_result("sendMessage", {"error": self.NOT_FOUND})
        self.assertEqual(len(logs.records), 1)

    async def test_unsupported_set_is_per_instance(self):
        first, second = self._instance(), self._instance()
        first._ddp_note_method_result("sendMessage", {"error": self.NOT_FOUND})
        self.assertFalse(first.ddp_method_supported("sendMessage"))
        self.assertTrue(second.ddp_method_supported("sendMessage"))

    async def test_inflight_map_is_bounded(self):
        instance = self._instance()
        for _ in range(ddp._NOTIFY_INFLIGHT_LIMIT + 10):
            await instance._ddp_notify("stream-notify-room", ["x"])
        self.assertEqual(len(instance._ddp_notify_inflight), ddp._NOTIFY_INFLIGHT_LIMIT)


if __name__ == "__main__":
    unittest.main()
