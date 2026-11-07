"""The adapter's dangerous-command prompt and its retirement. Skips without Hermes core's source."""

import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON, load_mixin_module, make_bare_instance  # noqa: E402


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class ApprovalPromptTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_mixin_module("adapter")
        self.rest = AsyncMock()
        self.rest.post_message.return_value = {"success": True, "message": {"_id": "card1"}}
        self.rest.delete_message.return_value = {"success": True}

    def _adapter(self, **cfg):
        settings = dict(approval_buttons=True, bot_username="hermes", allowed_builtin_commands=frozenset({"approve", "deny"}),
                        reasoning_leak_formatting_enabled=False)
        settings.update(cfg)
        return make_bare_instance(self.mod.RocketChatAdapter, _cfg=types.SimpleNamespace(**settings), _rest=self.rest)

    def _prompt(self, key="session-a"):
        return types.SimpleNamespace(chat_id="R1", session_key=key, text="Run this?", metadata={"thread_id": "T1"},
                                     actions=[("Allow Once", "once", "primary"), ("Always", "always", ""), ("Deny", "deny", "danger")])

    def test_prompt_goes_to_the_thread_with_allowed_buttons_and_typed_fallback(self):
        result = asyncio.run(self._adapter()._send_exec_approval_prompt(self._prompt("s1")))
        self.assertTrue(result.success)
        args, kwargs = self.rest.post_message.await_args
        self.assertEqual((args[0], args[2]), ("R1", "T1"))
        self.assertIn("@hermes /approve", args[1])
        self.assertEqual([b["msg"] for b in kwargs["attachments"][0]["actions"]], ["@hermes /approve", "@hermes /deny"])

    def _confirm(self, adapter, key="s9"):
        return asyncio.run(adapter.send_slash_confirm(
            chat_id="R1", title="/new", message="⚠️ **Confirm /new**\n\nFresh session.\n\nChoose:\n• **Always Approve**",
            session_key=key, confirm_id="1", metadata={"thread_id": "T1"},
        ))

    def test_confirmation_has_buttons_and_no_always(self):
        adapter = self._adapter(allowed_builtin_commands=frozenset({"approve", "deny"}))
        self.assertTrue(self._confirm(adapter).success)
        args, kwargs = self.rest.post_message.await_args
        self.assertEqual((args[0], args[2]), ("R1", "T1"))
        self.assertIn("Fresh session.", args[1])
        self.assertNotIn("Always", args[1])
        self.assertIn("@hermes /cancel", args[1])
        self.assertEqual([b["msg"] for b in kwargs["attachments"][0]["actions"]], ["@hermes /approve", "@hermes /deny"])

    def test_a_confirmation_answer_retires_the_card(self):
        adapter = self._adapter(allowed_builtin_commands=frozenset({"approve", "deny"}))
        self._confirm(adapter, "s10")
        asyncio.run(adapter._retire_approval_card("s10", "alice"))
        self.rest.delete_message.assert_awaited_once_with("R1", "card1")
        self.assertIn("Answered by @alice", self.rest.post_message.await_args.args[1])

    def test_an_unanswered_confirmation_does_not_take_the_retirement_from_a_prompt(self):
        adapter = self._adapter(allowed_builtin_commands=frozenset({"approve", "deny"}))
        self.rest.post_message.return_value = {"success": True, "message": {"_id": "confirm-card"}}
        self._confirm(adapter, "s11")
        self.rest.post_message.return_value = {"success": True, "message": {"_id": "danger-card"}}
        asyncio.run(adapter._send_exec_approval_prompt(self._prompt("s11")))
        asyncio.run(adapter._retire_approval_card("s11", "alice"))
        self.rest.delete_message.assert_awaited_once_with("R1", "danger-card")
        asyncio.run(adapter._retire_approval_card("s11", "alice"))
        self.rest.delete_message.assert_awaited_with("R1", "confirm-card")

    def test_a_newer_confirmation_supersedes_the_older_card(self):
        adapter = self._adapter(allowed_builtin_commands=frozenset({"approve", "deny"}))
        self.rest.post_message.return_value = {"success": True, "message": {"_id": "old"}}
        self._confirm(adapter, "s12")
        self.rest.post_message.return_value = {"success": True, "message": {"_id": "new"}}
        self._confirm(adapter, "s12")
        asyncio.run(adapter._retire_approval_card("s12", "alice"))
        self.rest.delete_message.assert_awaited_once_with("R1", "new")

    def test_confirmation_falls_back_to_hermes_text_when_buttons_are_off_or_refused(self):
        self.assertFalse(self._confirm(self._adapter(approval_buttons=False)).success)
        self.assertFalse(self._confirm(self._adapter(allowed_builtin_commands=frozenset())).success)
        self.rest.post_message.return_value = {"success": False, "error": "nope"}
        self.assertFalse(self._confirm(self._adapter(allowed_builtin_commands=frozenset({"approve"}))).success)

    def test_hermes_text_prompts_name_the_form_a_client_sends(self):
        self.assertEqual(self._adapter().typed_command_prefix, "@hermes /")

    def test_disabled_buttons_fall_back_to_hermes_text_prompt(self):
        result = asyncio.run(self._adapter(approval_buttons=False)._send_exec_approval_prompt(self._prompt("s2")))
        self.assertFalse(result.success)
        self.rest.post_message.assert_not_awaited()

    def test_no_allowed_choice_falls_back_too(self):
        adapter = self._adapter(allowed_builtin_commands=frozenset())
        self.assertFalse(asyncio.run(adapter._send_exec_approval_prompt(self._prompt("s3"))).success)

    def test_a_refusal_falls_back_too(self):
        self.rest.post_message.return_value = {"success": False, "error": "error-x"}
        self.assertFalse(asyncio.run(self._adapter()._send_exec_approval_prompt(self._prompt("s4"))).success)

    def test_an_answer_reposts_the_prompt_without_buttons(self):
        adapter = self._adapter()

        async def run():
            await adapter._send_exec_approval_prompt(self._prompt("s5"))
            self.rest.post_message.reset_mock()
            await adapter._retire_approval_card("s5", "alice")

        asyncio.run(run())
        self.rest.delete_message.assert_awaited_once_with("R1", "card1")
        args, kwargs = self.rest.post_message.await_args
        self.assertEqual((args[0], args[2]), ("R1", "T1"))
        self.assertIn("Run this?", args[1])
        self.assertIn("Answered by @alice", args[1])
        self.assertNotIn("/approve", args[1])
        self.assertNotIn("attachments", kwargs)

    def test_without_delete_rights_the_prompt_is_edited(self):
        self.rest.delete_message.return_value = {"success": False}
        adapter = self._adapter()

        async def run():
            await adapter._send_exec_approval_prompt(self._prompt("s6"))
            await adapter._retire_approval_card("s6", "alice")

        asyncio.run(run())
        self.assertIn("Answered by @alice", self.rest.update_message.await_args.args[2])

    def test_a_second_answer_finds_nothing_to_retire(self):
        asyncio.run(self._adapter()._retire_approval_card("unknown-session", "alice"))
        self.rest.delete_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
