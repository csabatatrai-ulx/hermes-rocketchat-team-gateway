"""The setup wizard and out-of-process delivery. Skips without Hermes core's source."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON, load_mixin_module  # noqa: E402


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class StandaloneSendTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_mixin_module("standalone")
        self.client = mock.AsyncMock()
        self.client.post_message.return_value = {"success": True, "message": {"_id": "m1"}}
        self.client.upload_file.return_value = {"success": True, "message": {"_id": "f1"}}
        self.client_cls = mock.Mock(return_value=self.client)
        patcher = mock.patch.object(self.mod, "RocketChatRestClient", self.client_cls)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, *args, **kwargs):
        config = types.SimpleNamespace(extra={"url": "https://chat.example/", "user_id": "u"}, token="t")
        return asyncio.run(self.mod.standalone_send(config, "R1", *args, **kwargs))

    def test_text_goes_to_the_room_and_the_client_closes(self):
        result = self._send("a ~ b")
        self.client_cls.assert_called_once_with("https://chat.example", "t", "u")
        self.client.post_message.assert_awaited_once_with("R1", "a \\~ b", "")
        self.assertEqual((result["success"], result["message_id"]), (True, "m1"))
        self.client.close.assert_awaited_once()

    def test_thread_is_kept(self):
        self._send("x", thread_id="T1")
        self.client.post_message.assert_awaited_once_with("R1", "x", "T1")

    def test_file_is_uploaded(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
            result = self._send("", media_files=[{"path": tmp.name}])
        self.assertEqual(self.client.upload_file.await_args.args[0], "R1")
        self.assertEqual(result["message_id"], "f1")

    def test_a_refusal_is_an_error(self):
        self.client.post_message.return_value = {"success": False, "error": "error-not-allowed"}
        result = self._send("x")
        self.assertIn("error", result)
        self.client.close.assert_awaited_once()

    def test_missing_credentials_are_an_error_without_a_connection(self):
        config = types.SimpleNamespace(extra={}, token=None)
        with mock.patch.object(self.mod, "get_scoped_secret", return_value=""):
            result = asyncio.run(self.mod.standalone_send(config, "R1", "x"))
        self.assertIn("error", result)
        self.client_cls.assert_not_called()


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_mixin_module("setup_wizard")
        import hermes_cli.cli_output as output
        import hermes_cli.gateway_setup_wizard as wizard
        import hermes_cli.setup as setup

        self.saved = {}
        self.answers = []
        for target, name, value in (
            (wizard, "confirm_reconfigure", mock.Mock(return_value=True)),
            (wizard, "print_setup_header", mock.Mock()),
            (wizard, "save_env_values", lambda **kw: self.saved.update(kw)),
            (wizard, "prompt_csv", lambda *a: self.answers.pop(0)),
            (setup, "prompt", lambda *a, **k: self.answers.pop(0)),
            (setup, "prompt_yes_no", mock.Mock(return_value=False)),
            (output, "print_info", mock.Mock()),
            (output, "print_success", mock.Mock()),
            (output, "print_warning", mock.Mock()),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, identity):
        with mock.patch.object(self.mod, "fetch_identity", mock.AsyncMock(return_value=identity)):
            self.mod.interactive_setup()

    def test_username_comes_from_the_server(self):
        self.answers = ["https://chat.example/", "u1", "tok", "a1,b1", "R1"]
        self._run({"username": "hermes", "roles": ["user", "bot"]})
        self.assertEqual(self.saved, {
            "ROCKETCHAT_TEAM_URL": "https://chat.example", "ROCKETCHAT_TEAM_USER_ID": "u1",
            "ROCKETCHAT_TEAM_TOKEN": "tok", "ROCKETCHAT_TEAM_BOT_USERNAME": "hermes",
            "ROCKETCHAT_TEAM_ALLOWED_USERS": "a1,b1", "ROCKETCHAT_TEAM_HOME_CHANNEL": "R1",
        })

    def test_refused_credentials_are_not_saved_by_default(self):
        self.answers = ["https://chat.example", "u1", "bad"]
        self._run(None)
        self.assertEqual(self.saved, {})

    def test_missing_answers_cancel(self):
        self.answers = ["https://chat.example", "", ""]
        self._run({"username": "hermes"})
        self.assertEqual(self.saved, {})


if __name__ == "__main__":
    unittest.main()
