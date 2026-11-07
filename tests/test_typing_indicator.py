"""Unit tests for adapter.py's send_typing/stop_typing.

Native Rocket.Chat Realtime API typing indicator. Uses the same "gracefully skip if the Hermes core isn't installed" pattern as test_shutdown_notice.py, since RocketChatAdapter needs gateway.platforms.base (BasePlatformAdapter) at class-definition time.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)


class _Cfg:
    def __init__(self, transport: str = "ddp") -> None:
        self.transport = transport


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestSendTyping(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        adapter_module = load_mixin_module("adapter")
        self.adapter_module = adapter_module
        self.RocketChatAdapter = adapter_module.RocketChatAdapter

    def _instance(self, **extra_attrs):
        attrs = dict(
            _cfg=_Cfg(),
            _bot_username="Hermes",
            _typing_tasks={},
        )
        attrs.update(extra_attrs)
        return make_bare_instance(self.RocketChatAdapter, **attrs)

    async def test_starts_a_refresh_loop_task_for_the_room(self):
        instance = self._instance()
        instance._ddp_notify = AsyncMock()
        await instance.send_typing("room1")
        self.assertIn("room1", instance._typing_tasks)
        # Let the loop's first iteration actually run before tearing down.
        await asyncio.sleep(0)
        instance._ddp_notify.assert_awaited_with(
            "stream-notify-room", ["room1/user-activity", "Hermes", ["user-typing"], {}]
        )
        await instance.stop_typing("room1")

    async def test_second_call_for_the_same_room_is_a_no_op(self):
        # Idempotent - a turn that calls send_typing() more than once for the
        # same room must not leak a second background task.
        instance = self._instance()
        instance._ddp_notify = AsyncMock()
        await instance.send_typing("room1")
        first_task = instance._typing_tasks["room1"]
        await instance.send_typing("room1")
        self.assertIs(instance._typing_tasks["room1"], first_task)
        await instance.stop_typing("room1")

    async def test_webhook_only_transport_is_a_no_op(self):
        # DDP is a Realtime-API-only feature - a pure-webhook deployment has
        # no persistent connection to carry it, and must not spawn a
        # doomed-to-fail refresh loop.
        instance = self._instance(_cfg=_Cfg(transport="webhook"))
        instance._ddp_notify = AsyncMock()
        await instance.send_typing("room1")
        self.assertEqual(instance._typing_tasks, {})
        instance._ddp_notify.assert_not_awaited()

    async def test_stop_typing_cancels_the_task_and_sends_false(self):
        instance = self._instance()
        instance._ddp_notify = AsyncMock()
        await instance.send_typing("room1")
        await instance.stop_typing("room1")
        self.assertNotIn("room1", instance._typing_tasks)
        instance._ddp_notify.assert_awaited_with(
            "stream-notify-room", ["room1/user-activity", "Hermes", [], {}]
        )

    async def test_stop_typing_without_a_running_loop_is_safe(self):
        instance = self._instance(_cfg=_Cfg(transport="webhook"))
        instance._ddp_notify = AsyncMock()
        await instance.stop_typing("room1")  # must not raise

    async def test_unsupported_method_starts_no_loop(self):
        # If the server has signalled that stream-notify-room does not exist, there are
        # no repeated calls.
        instance = self._instance(_ddp_unsupported_methods=frozenset({"stream-notify-room"}))
        instance._ddp_notify = AsyncMock()
        await instance.send_typing("room1")
        await instance.stop_typing("room1")
        self.assertEqual(instance._typing_tasks, {})
        instance._ddp_notify.assert_not_awaited()

    async def test_loop_ends_once_the_method_turns_out_unsupported(self):
        instance = self._instance()

        async def _notify(method, params):
            instance._ddp_unsupported_methods = frozenset({method})

        instance._ddp_notify = AsyncMock(side_effect=_notify)
        with patch.object(self.adapter_module, "_TYPING_REFRESH_SECONDS", 0):
            await instance.send_typing("room1")
            task = instance._typing_tasks["room1"]
            await asyncio.wait_for(task, timeout=1.0)  # returns instead of looping forever
        instance._ddp_notify.assert_awaited_once()
        await instance.stop_typing("room1")
        instance._ddp_notify.assert_awaited_once()  # no "stopped" call either

    async def test_ddp_notify_failure_never_raises(self):
        # Cosmetic feedback - same posture as _safe_react: a network hiccup
        # on the typing call must never surface to the user or abort a turn.
        instance = self._instance()
        instance._ddp_notify = AsyncMock(side_effect=RuntimeError("boom"))
        await instance.send_typing("room1")
        await asyncio.sleep(0)  # let the loop's first iteration raise+catch
        await instance.stop_typing("room1")  # must also not raise


if __name__ == "__main__":
    unittest.main()
