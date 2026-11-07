"""Unit tests for rest_client.py: URL, payload and response handling.

Only the pure layer is tested, deliberately: no aiohttp, no network, no session. Everything that encodes a Rocket.Chat quirk lives in these functions. The transport wrapper has almost no branching left, and testing it would mean mocking aiohttp rather than verifying behaviour. The two exceptions (``_note_deprecation``, and ``room_info``'s endpoint order with only ``get`` replaced) need no aiohttp either.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rest_client import (  # noqa: E402
    rooms_from_subscriptions,
    API_PREFIX,
    RocketChatNetworkError,
    RocketChatRestClient,
    deprecation_notice,
    auth_headers,
    build_url,
    delete_message_payload,
    history_query_params,
    is_rejected,
    is_successful,
    parse_retry_after,
    post_message_payload,
    posted_message_id,
    room_from_info,
    update_message_payload,
    thread_messages_query_params,
    user_from_info,
)


class TestBuildUrl(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(
            build_url("http://localhost:3000", "chat.postMessage"),
            "http://localhost:3000/api/v1/chat.postMessage",
        )

    def test_tolerates_trailing_slash_on_base(self):
        self.assertEqual(
            build_url("http://localhost:3000/", "me"),
            "http://localhost:3000/api/v1/me",
        )

    def test_tolerates_leading_slash_on_path(self):
        self.assertEqual(
            build_url("http://localhost:3000", "/me"),
            "http://localhost:3000/api/v1/me",
        )

    def test_no_double_slash_from_either_side(self):
        url = build_url("http://localhost:3000///", "///me")
        self.assertEqual(url, "http://localhost:3000/api/v1/me")
        self.assertNotIn("//api", url)

    def test_prefix_constant_is_used(self):
        self.assertIn(API_PREFIX, build_url("http://x", "me"))


class TestAuthHeaders(unittest.TestCase):
    def test_pat_header_pair(self):
        # Rocket.Chat authenticates a bot with this exact header pair, not a
        # Bearer token.
        self.assertEqual(
            auth_headers("tok", "uid"),
            {
                "X-Auth-Token": "tok",
                "X-User-Id": "uid",
                "Content-Type": "application/json",
            },
        )


class TestPostMessagePayload(unittest.TestCase):
    def test_text_only(self):
        self.assertEqual(
            post_message_payload("room1", "hi"), {"roomId": "room1", "text": "hi"}
        )

    def test_empty_text_is_omitted(self):
        # Rocket.Chat rejects an empty text field.
        self.assertEqual(post_message_payload("room1", ""), {"roomId": "room1"})

    def test_thread_reply_carries_tmid(self):
        # A reply into the thread (tmid is valid only together with roomId).
        self.assertEqual(
            post_message_payload("room1", "hi", "root-1"),
            {"roomId": "room1", "text": "hi", "tmid": "root-1"},
        )

    def test_main_channel_has_no_tmid_key(self):
        self.assertNotIn("tmid", post_message_payload("room1", "hi", ""))


class TestUpdateMessagePayload(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(
            update_message_payload("room1", "m1", "new text"),
            {"roomId": "room1", "msgId": "m1", "text": "new text"},
        )

    def test_empty_text_becomes_a_space(self):
        # Happens legitimately while streaming, before the first token lands -
        # and Rocket.Chat rejects an empty text outright.
        self.assertEqual(update_message_payload("room1", "m1", "")["text"], " ")


class TestDeleteMessagePayload(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(
            delete_message_payload("room1", "m1"), {"roomId": "room1", "msgId": "m1"}
        )


class TestPostedMessageId(unittest.TestCase):
    def test_extracts_id(self):
        self.assertEqual(
            posted_message_id({"success": True, "message": {"_id": "abc"}}), "abc"
        )

    def test_missing_message(self):
        self.assertEqual(posted_message_id({"success": True}), "")

    def test_empty_response(self):
        self.assertEqual(posted_message_id({}), "")
        self.assertEqual(posted_message_id(None), "")


class TestIsSuccessful(unittest.TestCase):
    def test_success_true(self):
        self.assertTrue(is_successful({"success": True}))

    def test_success_false(self):
        self.assertFalse(is_successful({"success": False}))

    def test_missing_key(self):
        self.assertFalse(is_successful({"message": {}}))

    def test_empty_response_counts_as_failure(self):
        # The transport layer returns {} when it already gave up.
        self.assertFalse(is_successful({}))
        self.assertFalse(is_successful(None))


class TestIsRejected(unittest.TestCase):
    def test_explicit_success_false_is_a_rejection(self):
        self.assertTrue(is_rejected({"success": False}))
        self.assertTrue(is_rejected({"success": False, "status": 400}))

    def test_ambiguous_or_successful_answers_are_not(self):
        self.assertFalse(is_rejected({}))
        self.assertFalse(is_rejected(None))
        self.assertFalse(is_rejected({"success": True}))
        self.assertFalse(is_rejected({"message": {}}))


class _FakeResp:
    def __init__(self, status, body="{}"):
        self.status = status
        self.headers = {}
        self._body = body

    async def text(self):
        return self._body

    async def json(self):
        import json
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Instead of the aiohttp session: one preset answer or exception per request."""

    def __init__(self, answer):
        self._answer = answer

    def _call(self, *args, **kwargs):
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer

    get = post = _call


class TestTransportOutcomes(unittest.IsolatedAsyncioTestCase):
    """The failure paths of ``get``/``post`` with a fake session (no aiohttp mock)."""

    def _client(self, answer):
        client = RocketChatRestClient("https://rc.example.test", "tok", "uid")
        client._session = _FakeSession(answer)
        return client

    async def test_post_4xx_is_passed_on_as_a_rejection(self):
        with self.assertLogs("rest_client", level="ERROR"):
            data = await self._client(_FakeResp(400, '{"success": false}')).post("chat.postMessage", {})
        self.assertEqual(data, {"success": False, "status": 400})
        self.assertFalse(is_successful(data))
        self.assertTrue(is_rejected(data))

    async def test_post_5xx_and_408_stay_ambiguous(self):
        for status in (500, 502, 408, 302):
            with self.subTest(status=status), self.assertLogs("rest_client", level="ERROR"):
                data = await self._client(_FakeResp(status)).post("chat.postMessage", {})
                self.assertEqual(data, {})
                self.assertFalse(is_rejected(data))

    async def test_get_total_timeout_fails_soft(self):
        import asyncio
        with self.assertLogs("rest_client", level="ERROR"):
            data = await self._client(asyncio.TimeoutError()).get("me")
        self.assertEqual(data, {})

    async def test_expected_miss_400_is_debug_not_error(self):
        # An intermediate step of the endpoint chain: a 400 is expected and must not
        # raise a false alarm.
        with self.assertLogs("rest_client", level="DEBUG") as logs:
            data = await self._client(_FakeResp(400, '{"success": false}')).get(
                "channels.history", {}, expected_miss=True
            )
        self.assertEqual(data, {})
        self.assertEqual([r.levelname for r in logs.records], ["DEBUG"])

    async def test_expected_miss_keeps_other_failures_at_error(self):
        for status in (401, 403, 500):
            with self.subTest(status=status), self.assertLogs("rest_client", level="ERROR"):
                await self._client(_FakeResp(status)).get("channels.history", {}, expected_miss=True)

    async def test_plain_400_is_still_error(self):
        with self.assertLogs("rest_client", level="ERROR"):
            await self._client(_FakeResp(400)).get("dm.history", {})


class TestRoomFromInfo(unittest.TestCase):
    def test_rooms_info_shape(self):
        # rooms.info (the primary lookup) answers with "room".
        room = room_from_info({"success": True, "room": {"name": "general", "t": "c"}})
        self.assertEqual(room, {"name": "general", "t": "c"})

    def test_public_channel_shape(self):
        room = room_from_info({"success": True, "channel": {"name": "general"}})
        self.assertEqual(room, {"name": "general"})

    def test_private_group_shape(self):
        # A private group returns HTTP 400 from channels.info, which looks like
        # a broken room id rather than the wrong endpoint - hence both shapes.
        room = room_from_info({"success": True, "group": {"name": "private"}})
        self.assertEqual(room, {"name": "private"})

    def test_unsuccessful_response(self):
        self.assertIsNone(room_from_info({"success": False, "channel": {"name": "x"}}))

    def test_empty_response(self):
        self.assertIsNone(room_from_info({}))
        self.assertIsNone(room_from_info(None))

    def test_successful_but_roomless(self):
        self.assertIsNone(room_from_info({"success": True}))


class TestUserFromInfo(unittest.TestCase):
    def test_user_shape(self):
        user = user_from_info(
            {"success": True, "user": {"username": "alice", "emails": []}}
        )
        self.assertEqual(user, {"username": "alice", "emails": []})

    def test_unsuccessful_response(self):
        self.assertIsNone(user_from_info({"success": False, "user": {"username": "x"}}))

    def test_empty_response(self):
        self.assertIsNone(user_from_info({}))
        self.assertIsNone(user_from_info(None))

    def test_successful_but_userless(self):
        self.assertIsNone(user_from_info({"success": True}))


class TestHistoryQueryParams(unittest.TestCase):
    def test_room_id_only(self):
        self.assertEqual(history_query_params("room1"), {"roomId": "room1"})

    def test_with_oldest_and_count(self):
        self.assertEqual(
            history_query_params("room1", oldest="2026-08-04T00:00:00.000Z", count=20),
            {"roomId": "room1", "oldest": "2026-08-04T00:00:00.000Z", "count": "20"},
        )

    def test_zero_count_is_omitted(self):
        self.assertNotIn("count", history_query_params("room1", count=0))

    def test_empty_oldest_is_omitted(self):
        self.assertNotIn("oldest", history_query_params("room1", oldest=""))

    def test_with_latest_cursor(self):
        self.assertEqual(
            history_query_params("room1", latest="2026-08-04T00:00:00.000Z", count=100),
            {"roomId": "room1", "latest": "2026-08-04T00:00:00.000Z", "count": "100"},
        )

    def test_empty_latest_is_omitted(self):
        self.assertNotIn("latest", history_query_params("room1", latest=""))


class TestShowThreadMessages(unittest.TestCase):
    def test_sent_explicitly_both_ways(self):
        # The server default differs per endpoint (groups.history keeps thread replies
        # by default), so it is always explicit.
        self.assertEqual(
            history_query_params("room1", show_thread_messages=False)["showThreadMessages"], "false"
        )
        self.assertEqual(
            history_query_params("room1", show_thread_messages=True)["showThreadMessages"], "true"
        )
        self.assertNotIn("showThreadMessages", history_query_params("room1"))


class TestThreadMessagesQueryParams(unittest.TestCase):
    def test_newest_first_with_count(self):
        self.assertEqual(
            thread_messages_query_params("root-1", count=51),
            {"tmid": "root-1", "sort": '{"ts":-1}', "count": "51"},
        )


class TestNetworkError(unittest.TestCase):
    def test_is_an_exception_carrying_its_message(self):
        # send() turns this into a retryable SendResult, which is the whole
        # reason it is a distinct type rather than a swallowed error.
        with self.assertRaises(RocketChatNetworkError) as ctx:
            raise RocketChatNetworkError("server disconnected")
        self.assertIn("server disconnected", str(ctx.exception))

    def test_retry_after_defaults_to_none(self):
        self.assertIsNone(RocketChatNetworkError("x").retry_after)

    def test_retry_after_is_carried_when_given(self):
        # post()'s 429 handling attaches this so a caller can still see the server's
        # requested delay after post() has exhausted its own retries.
        self.assertEqual(RocketChatNetworkError("x", retry_after=5.0).retry_after, 5.0)


class TestParseRetryAfter(unittest.TestCase):
    """``parse_retry_after`` (for post()'s 429 handling)."""

    def test_integer_seconds(self):
        self.assertEqual(parse_retry_after("5"), 5.0)

    def test_fractional_seconds(self):
        self.assertEqual(parse_retry_after("2.5"), 2.5)

    def test_none_header_is_none(self):
        self.assertIsNone(parse_retry_after(None))

    def test_empty_string_is_none(self):
        self.assertIsNone(parse_retry_after(""))

    def test_non_numeric_value_degrades_to_none(self):
        # Rocket.Chat is observed to always send integer seconds, but an
        # HTTP-date (the OTHER valid Retry-After form) must not raise here -
        # the caller falls back to its own exponential backoff instead.
        self.assertIsNone(parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))



class TestRoomsFromSubscriptions(unittest.TestCase):
    def test_maps_room_id_to_type_including_dms(self):
        response = {"success": True, "update": [
            {"rid": "c1", "t": "c"}, {"rid": "d1", "t": "d"}, {"rid": "", "t": "p"},
        ]}
        self.assertEqual(rooms_from_subscriptions(response), {"c1": "c", "d1": "d"})

    def test_failed_response_is_empty(self):
        self.assertEqual(rooms_from_subscriptions({"success": False}), {})
        self.assertEqual(rooms_from_subscriptions(None), {})

class TestDeprecationNotice(unittest.TestCase):
    """Rocket.Chat's ``x-deprecation-*`` response headers."""

    def test_formats_every_present_header(self):
        notice = deprecation_notice({
            "X-Deprecation-Type": "endpoint-deprecation",
            "X-Deprecation-Message": "use rooms.info",
            "X-Deprecation-Version": "9.0.0",
        })
        self.assertEqual(
            notice,
            "type=endpoint-deprecation, message=use rooms.info, version=9.0.0",
        )

    def test_partial_headers(self):
        self.assertEqual(
            deprecation_notice({"x-deprecation-version": "10.0.0"}), "version=10.0.0"
        )

    def test_no_deprecation_headers(self):
        self.assertIsNone(deprecation_notice({"Content-Type": "application/json"}))
        self.assertIsNone(deprecation_notice({}))
        self.assertIsNone(deprecation_notice(None))

    def test_not_a_mapping(self):
        self.assertIsNone(deprecation_notice(["x-deprecation-type"]))


class TestNoteDeprecation(unittest.TestCase):
    def test_logs_once_per_endpoint(self):
        client = RocketChatRestClient("https://rc.example.test", "tok", "uid")
        headers = {"x-deprecation-version": "9.0.0"}
        with self.assertLogs("rest_client", level="WARNING") as logs:
            client._note_deprecation("GET", "channels.info", headers)
            client._note_deprecation("GET", "channels.info", headers)
            client._note_deprecation("POST", "chat.update", headers)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("GET channels.info", logs.output[0])
        self.assertIn("version=9.0.0", logs.output[0])

    def test_silent_without_headers(self):
        client = RocketChatRestClient("https://rc.example.test", "tok", "uid")
        with self.assertNoLogs("rest_client", level="WARNING"):
            client._note_deprecation("GET", "me", {"content-type": "application/json"})


class TestRoomInfoEndpointOrder(unittest.IsolatedAsyncioTestCase):
    """``room_info``'s endpoint order - only ``get`` is replaced, no aiohttp."""

    def _client(self, answers):
        client = RocketChatRestClient("https://rc.example.test", "tok", "uid")
        client.get = AsyncMock(side_effect=lambda endpoint, params=None, **_kw: answers.get(endpoint, {}))
        return client

    async def test_rooms_info_is_tried_first_and_is_enough(self):
        client = self._client({"rooms.info": {"success": True, "room": {"name": "general"}}})
        self.assertEqual(await client.room_info("r1"), {"name": "general"})
        client.get.assert_awaited_once_with("rooms.info", {"roomId": "r1"}, expected_miss=True)

    async def test_falls_back_to_the_legacy_pair(self):
        client = self._client({"groups.info": {"success": True, "group": {"name": "private"}}})
        self.assertEqual(await client.room_info("r1"), {"name": "private"})
        self.assertEqual(
            [c.args[0] for c in client.get.await_args_list],
            ["rooms.info", "channels.info", "groups.info"],
        )

    async def test_unknown_room(self):
        self.assertIsNone(await self._client({}).room_info("r1"))


class TestHistoryEndpoints(unittest.IsolatedAsyncioTestCase):
    """REST side; only ``get`` is replaced."""

    def _client(self, answers):
        client = RocketChatRestClient("https://rc.example.test", "tok", "uid")
        client.get = AsyncMock(side_effect=lambda endpoint, params=None, **_kw: answers.get(endpoint, {}))
        return client

    async def test_room_history_sends_latest_and_show_thread_messages_false(self):
        client = self._client(
            {"channels.history": {"success": True, "messages": [{"_id": "b"}, {"_id": "a"}]}}
        )
        got = await client.room_history(
            "r1", latest="2026-10-06T10:00:05.000Z", count=50, show_thread_messages=False
        )
        self.assertEqual([m["_id"] for m in got], ["a", "b"])  # chronological
        client.get.assert_awaited_once_with(
            "channels.history",
            {
                "roomId": "r1",
                "latest": "2026-10-06T10:00:05.000Z",
                "count": "50",
                "showThreadMessages": "false",
            },
            expected_miss=True,
        )

    async def test_private_group_also_gets_false(self):
        client = self._client({"groups.history": {"success": True, "messages": [{"_id": "g"}]}})
        await client.room_history("r1", count=5, show_thread_messages=False)
        endpoint, params = client.get.await_args_list[-1].args
        self.assertEqual(endpoint, "groups.history")
        self.assertEqual(params["showThreadMessages"], "false")

    async def test_dm_window_is_not_empty(self):
        # A pager trying only channels/groups would leave a DM's window empty.
        client = self._client({"dm.history": {"success": True, "messages": [{"_id": "d"}]}})
        got = await client.room_history("dm-1", count=5, show_thread_messages=False)
        self.assertEqual(got, [{"_id": "d"}])
        self.assertEqual(
            [c.args[0] for c in client.get.await_args_list],
            ["channels.history", "groups.history", "dm.history"],
        )

    async def test_thread_messages_newest_page_returned_oldest_first(self):
        client = self._client(
            {"chat.getThreadMessages": {"success": True, "messages": [{"_id": "r2"}, {"_id": "r1"}]}}
        )
        got = await client.thread_messages("root-1", count=51)
        self.assertEqual([m["_id"] for m in got], ["r1", "r2"])
        client.get.assert_awaited_once_with(
            "chat.getThreadMessages", {"tmid": "root-1", "sort": '{"ts":-1}', "count": "51"}
        )

    async def test_thread_messages_fail_soft(self):
        # Disabled threads or a 429: an empty window, no exception.
        self.assertEqual(await self._client({}).thread_messages("root-1", count=5), [])
        self.assertEqual(await self._client({}).thread_messages("", count=5), [])

    async def test_get_message_returns_the_thread_root(self):
        client = self._client(
            {"chat.getMessage": {"success": True, "message": {"_id": "root-1", "msg": "question"}}}
        )
        self.assertEqual((await client.get_message("root-1"))["msg"], "question")
        client.get.assert_awaited_once_with("chat.getMessage", {"msgId": "root-1"})
        self.assertIsNone(await self._client({}).get_message("root-1"))
        self.assertIsNone(await self._client({}).get_message(""))


if __name__ == "__main__":
    unittest.main()


class RateLimitBypassTests(unittest.TestCase):
    """The startup warning's decision (bot role / api-bypass-rate-limit)."""

    def test_permission_roles_from_list_all(self):
        from rest_client import permission_roles  # noqa: E402
        resp = {"update": [{"_id": "x", "roles": ["a"]}, {"_id": "api-bypass-rate-limit", "roles": ["admin", "bot", "app"]}]}
        self.assertEqual(permission_roles(resp, "api-bypass-rate-limit"), ["admin", "bot", "app"])
        self.assertIsNone(permission_roles({}, "api-bypass-rate-limit"))
        self.assertIsNone(permission_roles(None, "api-bypass-rate-limit"))

    def test_status(self):
        from rest_client import rate_limit_bypass_status  # noqa: E402
        self.assertEqual(rate_limit_bypass_status(["user", "bot"], ["admin", "bot", "app"]), "ok")
        self.assertEqual(rate_limit_bypass_status(["user"], ["admin", "bot", "app"]), "missing")
        self.assertEqual(rate_limit_bypass_status(["user", "bot"], None), "ok")
        self.assertEqual(rate_limit_bypass_status(["user"], None), "missing")
        self.assertEqual(rate_limit_bypass_status(None, ["bot"]), "unknown")

