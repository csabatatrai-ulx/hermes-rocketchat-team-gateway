"""Unit tests for adapter.py's instant ack reaction.

An animated multi-step variant was rejected because every reaction change reflows Rocket.Chat's message
list, visibly "jumping" for a user watching the channel. These tests cover the single static reaction
(`_clear_ack`/`_pending_ack`). Uses the same "gracefully skip if the Hermes core isn't installed" pattern
as test_typing_indicator.py, since RocketChatAdapter needs gateway.platforms.base (BasePlatformAdapter) at
class-definition time.
"""

from __future__ import annotations

import asyncio
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


class _Cfg:
    def __init__(self, ack_reaction: str = "eyes"):
        self.ack_reaction = ack_reaction


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestClearAck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        adapter_module = load_mixin_module("adapter")
        self.RocketChatAdapter = adapter_module.RocketChatAdapter

    def _instance(self, **extra_attrs):
        attrs = dict(_cfg=_Cfg(), _pending_ack={})
        attrs.update(extra_attrs)
        return make_bare_instance(self.RocketChatAdapter, **attrs)

    async def test_clear_ack_removes_the_pending_reaction(self):
        instance = self._instance(_pending_ack={"room1": "msg1"})
        instance._safe_react = AsyncMock()
        instance._clear_ack("room1")
        await asyncio.sleep(0)  # let the fire-and-forget create_task run
        self.assertNotIn("room1", instance._pending_ack)
        instance._safe_react.assert_awaited_once_with("msg1", "eyes", False)

    async def test_clear_ack_is_idempotent(self):
        instance = self._instance(_pending_ack={"room1": "msg1"})
        instance._safe_react = AsyncMock()
        instance._clear_ack("room1")
        instance._clear_ack("room1")  # second call: nothing pending anymore
        await asyncio.sleep(0)
        instance._safe_react.assert_awaited_once()

    async def test_clear_ack_with_nothing_pending_is_a_no_op(self):
        instance = self._instance()
        instance._safe_react = AsyncMock()
        instance._clear_ack("room1")  # must not raise
        instance._safe_react.assert_not_awaited()

    async def test_clear_ack_does_nothing_when_ack_reaction_disabled(self):
        instance = self._instance(_cfg=_Cfg(ack_reaction=""), _pending_ack={"room1": "msg1"})
        instance._safe_react = AsyncMock()
        instance._clear_ack("room1")
        instance._safe_react.assert_not_awaited()
        # Still pops the pending entry, matching the original pre-animation
        # behaviour - only the react() call is gated on ack_reaction.
        self.assertNotIn("room1", instance._pending_ack)


if __name__ == "__main__":
    unittest.main()
