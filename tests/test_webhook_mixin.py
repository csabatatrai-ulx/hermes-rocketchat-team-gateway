"""Unit tests for webhook_mixin.py: the WebhookInboundMixin.

Unlike most tests in this package, this module imports ``gateway.*`` (see `_adapter_test_support.py`), so the tests skip gracefully if the Hermes framework's source (``HERMES_AGENT_PATH`` env, default: ``~/.hermes/hermes-agent``) is unavailable.
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)

BOT = "TestBot"
BOT_UID = "bot-uid-1"


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in: only ``.read()`` is used."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    async def read(self) -> bytes:
        return self._body


def _payload(**overrides):
    body = {
        "token": "shared-secret",
        "message_id": "m1",
        "channel_id": "room1",
        "channel_name": "general",
        "user_id": "user-1",
        "user_name": "some-user",
        "text": f"@{BOT} hello!",
    }
    body.update(overrides)
    return body


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestHandleOutgoingWebhook(unittest.TestCase):
    def setUp(self):
        from dedup import InboundDeduplicator
        from config import load_config

        webhook_mixin = load_mixin_module("webhook_mixin")
        self.WebhookInboundMixin = webhook_mixin.WebhookInboundMixin
        self.cfg = load_config({}, token="t")
        self.dedup = InboundDeduplicator()

    def _instance(self, **extra_attrs):
        attrs = dict(
            _bot_username=BOT,
            _user_id=BOT_UID,
            _webhook_secret="shared-secret",
            _require_mention=True,
            _cfg=self.cfg,
            _inbound_dedup=self.dedup,
            _pending_ack={},
            _safe_react=AsyncMock(),
        )
        attrs.update(extra_attrs)
        return make_bare_instance(self.WebhookInboundMixin, **attrs)

    def test_bad_token_is_rejected_without_touching_dispatch(self):
        instance = self._instance()
        instance._augment_and_handle = AsyncMock()
        request = _FakeRequest(_payload(token="wrong-secret"))

        response = asyncio.run(instance._handle_outgoing_webhook(request))

        self.assertGreaterEqual(response.status, 400)
        instance._augment_and_handle.assert_not_awaited()

    def test_duplicate_message_id_is_ignored_on_second_delivery(self):
        instance = self._instance()
        instance._augment_and_handle = AsyncMock()
        request_1 = _FakeRequest(_payload(message_id="dup-1"))
        request_2 = _FakeRequest(_payload(message_id="dup-1"))

        async def _run():
            r1 = await instance._handle_outgoing_webhook(request_1)
            r2 = await instance._handle_outgoing_webhook(request_2)
            # The webhook handler starts `_augment_and_handle` with
            # `asyncio.create_task` (fire-and-forget); the HTTP response does not wait
            # for it, so the task must be allowed to run before checking the mock's call
            # count.
            await asyncio.sleep(0)
            return r1, r2

        response_1, response_2 = asyncio.run(_run())

        # The first delivery starts a real agent turn (HANDLE_MESSAGE), the second
        # (simulating a Rocket.Chat retry) does NOT.
        self.assertEqual(instance._augment_and_handle.await_count, 1)
        self.assertLess(response_1.status, 300)
        self.assertLess(response_2.status, 300)

    def test_plain_mention_dispatches_and_acks_immediately(self):
        instance = self._instance()
        instance._augment_and_handle = AsyncMock()
        request = _FakeRequest(_payload(message_id="m-plain"))

        async def _run():
            response = await instance._handle_outgoing_webhook(request)
            await asyncio.sleep(0)
            return response

        response = asyncio.run(_run())

        self.assertLess(response.status, 300)
        # The HTTP response goes back BEFORE the detached task; this checks that the
        # dispatch really happened once allowed to run.
        instance._augment_and_handle.assert_awaited_once()
        self.assertEqual(instance._pending_ack.get("room1"), ["m-plain"])
        instance._safe_react.assert_called_once_with("m-plain", instance._cfg.ack_reaction, True)

    def test_dm_only_room_mention_notices_once_per_room_without_a_turn(self):
        from config import load_config

        cfg = load_config({}, {"ROCKETCHAT_DM_ONLY": "true"}, token="t")
        instance = self._instance(_cfg=cfg, _notify=AsyncMock(), _dm_only_noticed_rooms=set())
        instance._augment_and_handle = AsyncMock()

        async def _run():
            for mid in ("m-a", "m-b"):
                await instance._handle_outgoing_webhook(
                    _FakeRequest(_payload(message_id=mid, room_type="c"))
                )
            await instance._handle_outgoing_webhook(
                _FakeRequest(_payload(message_id="m-c", channel_id="room2", room_type="c"))
            )
            await asyncio.sleep(0)

        asyncio.run(_run())

        instance._augment_and_handle.assert_not_awaited()
        self.assertEqual(instance._notify.await_count, 2)
        rooms = [call.args[0] for call in instance._notify.await_args_list]
        self.assertEqual(rooms, ["room1", "room2"])
        self.assertIn("only works in direct messages", instance._notify.await_args.args[1])

    def test_blocked_hyphenated_command_names_the_real_env_key(self):
        # For a hyphenated command (`/reload-mcp`) the blocking message must name the
        # key actually read, with underscores (ROCKETCHAT_ALLOW_BUILTIN_RELOAD_MCP).
        instance = self._instance(_notify=AsyncMock())
        instance._augment_and_handle = AsyncMock()
        request = _FakeRequest(_payload(message_id="m-cmd", text=f"@{BOT} /reload-mcp"))

        async def _run():
            await instance._handle_outgoing_webhook(request)
            await asyncio.sleep(0)

        asyncio.run(_run())

        instance._augment_and_handle.assert_not_awaited()
        instance._notify.assert_awaited_once()
        text = instance._notify.await_args.args[1]
        self.assertIn("ROCKETCHAT_ALLOW_BUILTIN_RELOAD_MCP=true", text)
        self.assertNotIn("RELOAD-MCP", text)

    def _notice_for(self, text):
        instance = self._instance(_notify=AsyncMock())
        instance._augment_and_handle = AsyncMock()

        async def _run():
            await instance._handle_outgoing_webhook(_FakeRequest(_payload(message_id="m-n", text=f"@{BOT} {text}")))
            await asyncio.sleep(0)

        asyncio.run(_run())
        instance._augment_and_handle.assert_not_awaited()
        return instance._notify.await_args.args[1]

    def test_a_documented_command_gets_the_short_notice(self):
        text = self._notice_for("/yolo on")
        self.assertIn("ROCKETCHAT_ALLOW_BUILTIN_YOLO=true", text)
        self.assertNotIn("SKILL", text)

    def test_an_unknown_word_suggests_plain_text_and_names_both_keys(self):
        text = self._notice_for("/api returns 500")
        self.assertIn("leave out the leading slash", text)
        self.assertIn("ROCKETCHAT_ALLOW_BUILTIN_API=true", text)
        self.assertIn("ROCKETCHAT_ALLOW_SKILL_API=true", text)

    def test_persistent_approval_is_refused_and_never_reaches_hermes(self):
        # "Always approve" would write to the gateway's whole config
        # (approvals.destructive_slash_confirm: false) and affect everyone.
        for mid, text in (("m-al1", f"@{BOT} /always"), ("m-al2", f"@{BOT} always"),
                          ("m-al3", f"@{BOT} /approve session")):
            with self.subTest(text=text):
                instance = self._instance(_notify=AsyncMock())
                instance._augment_and_handle = AsyncMock()

                async def _run():
                    await instance._handle_outgoing_webhook(_FakeRequest(_payload(message_id=mid, text=text)))
                    await asyncio.sleep(0)

                asyncio.run(_run())

                instance._augment_and_handle.assert_not_awaited()
                instance._notify.assert_awaited_once()
                reply = instance._notify.await_args.args[1]
                self.assertIn("Persistent approval", reply)
                self.assertIn("/approve", reply)


    def _dispatch(self, instance, *, text, is_direct=False):
        from inbound import HANDLE_MESSAGE, InboundDecision

        decision = InboundDecision(
            outcome=HANDLE_MESSAGE, channel_id="room1", sender_id="uid-alice",
            message_id="m-new", text=text, is_direct=is_direct,
        )

        async def _run():
            await instance._dispatch_inbound_decision(decision)
            await asyncio.sleep(0)

        asyncio.run(_run())

    def test_new_in_a_room_reaches_the_core(self):
        # Sessions belong to core, /new too (confirmation + `/approve` in the same
        # session).
        instance = self._instance(_notify=AsyncMock())
        instance._augment_and_handle = AsyncMock()
        for text in ("/new", "/reset"):
            self._dispatch(instance, text=text)
        self.assertEqual(instance._augment_and_handle.await_count, 2)
        instance._notify.assert_not_awaited()

    def test_new_in_a_dm_reaches_the_core(self):
        instance = self._instance(_notify=AsyncMock())
        instance._augment_and_handle = AsyncMock()
        self._dispatch(instance, text="/new", is_direct=True)
        instance._augment_and_handle.assert_awaited_once()
        instance._notify.assert_not_awaited()

    def test_a_word_starting_with_new_is_a_normal_message(self):
        instance = self._instance(_notify=AsyncMock())
        instance._augment_and_handle = AsyncMock()
        self._dispatch(instance, text="/newsletter question")
        instance._augment_and_handle.assert_awaited_once()


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestWebhookSecretFailsClosed(unittest.TestCase):
    """transport=webhook + empty ROCKETCHAT_WEBHOOK_SECRET -> the platform does NOT start. DDP does not need the secret."""

    BASE_ENV = {
        "ROCKETCHAT_URL": "http://localhost:3000",
        "ROCKETCHAT_TOKEN": "tok",
        "ROCKETCHAT_USER_ID": BOT_UID,
        "ROCKETCHAT_BOT_USERNAME": BOT,
    }

    def setUp(self):
        self.adapter = load_mixin_module("adapter")

    def _validate(self, env):
        from types import SimpleNamespace
        from unittest import mock

        with mock.patch.dict(os.environ, env, clear=True):
            return self.adapter.validate_rocketchat_config(SimpleNamespace(extra={}, token=""))

    def test_webhook_without_secret_is_refused_loudly(self):
        env = dict(self.BASE_ENV, ROCKETCHAT_TRANSPORT="webhook")
        with self.assertLogs(self.adapter.logger, level="WARNING") as captured:
            self.assertFalse(self._validate(env))
        self.assertIn("webhook_secret", captured.output[0])

    def test_webhook_with_secret_is_accepted(self):
        env = dict(self.BASE_ENV, ROCKETCHAT_TRANSPORT="webhook", ROCKETCHAT_WEBHOOK_SECRET="s")
        self.assertTrue(self._validate(env))

    def test_ddp_without_secret_is_accepted(self):
        self.assertTrue(self._validate(dict(self.BASE_ENV, ROCKETCHAT_TRANSPORT="ddp")))

    def test_connect_refuses_before_any_network_call(self):
        from types import SimpleNamespace

        rest = SimpleNamespace(open=AsyncMock(), me=AsyncMock(), close=AsyncMock())
        cfg = self.adapter.load_config(
            {}, dict(self.BASE_ENV, ROCKETCHAT_TRANSPORT="webhook", ROCKETCHAT_DEDUP_STATE_DIR="off")
        )
        instance = make_bare_instance(self.adapter.RocketChatAdapter, _cfg=cfg, _rest=rest)
        with self.assertLogs(self.adapter.logger, level="ERROR") as captured:
            self.assertFalse(asyncio.run(instance.connect()))
        rest.open.assert_not_awaited()
        self.assertTrue(any("ROCKETCHAT_WEBHOOK_SECRET" in line for line in captured.output))


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestConnectReadinessStatus(unittest.TestCase):
    """A failed start must not leave an earlier "connected" in the readiness probe's file (ddp_status.json). Hermes does not exit when a platform fails to start, so a container keeps running."""

    BASE_ENV = TestWebhookSecretFailsClosed.BASE_ENV

    def setUp(self):
        import tempfile

        self.adapter = load_mixin_module("adapter")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._status = os.path.join(self._tmp.name, "ddp_status.json")
        with open(self._status, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "state": "connected", "since": 1}, fh)

    def _state(self):
        with open(self._status, encoding="utf-8") as fh:
            return json.load(fh)["state"]

    def _connect(self, rest, **env):
        cfg = self.adapter.load_config(
            {}, dict(self.BASE_ENV, ROCKETCHAT_DEDUP_STATE_DIR=self._tmp.name, **env)
        )
        instance = make_bare_instance(self.adapter.RocketChatAdapter, _cfg=cfg, _rest=rest)
        with self.assertLogs(self.adapter.logger, level="ERROR"):
            return asyncio.run(instance.connect())

    def test_rejected_token_replaces_a_stale_connected(self):
        from types import SimpleNamespace

        rest = SimpleNamespace(open=AsyncMock(), me=AsyncMock(return_value=None), close=AsyncMock())
        self.assertFalse(self._connect(rest))
        self.assertEqual(self._state(), "auth_failed")

    def test_missing_config_replaces_a_stale_connected(self):
        from types import SimpleNamespace

        rest = SimpleNamespace(open=AsyncMock(), me=AsyncMock(), close=AsyncMock())
        self.assertFalse(self._connect(rest, ROCKETCHAT_TRANSPORT="webhook"))
        rest.open.assert_not_awaited()
        self.assertEqual(self._state(), "connecting")


if __name__ == "__main__":
    unittest.main()
