"""Outbound files: Hermes core strips and validates the model's ``MEDIA:/path`` tag; the plugin's documented ``send_document`` / ``send_image_file`` / ``send_voice`` / ``send_video`` overrides upload to Rocket.Chat (``rooms.media`` -> ``rooms.mediaConfirm``)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import (  # noqa: E402
    HERMES_AGENT_AVAILABLE,
    SKIP_REASON,
    load_mixin_module,
    make_bare_instance,
)
from rest_client import RocketChatNetworkError, RocketChatRestClient, upload_error  # noqa: E402


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self.headers = {}
        self._body = body if isinstance(body, str) else json.dumps(body)

    async def text(self):
        return self._body

    async def json(self):
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    """The next answer per request; remembers the calls."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class UploadFileRestTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, *answers):
        client = RocketChatRestClient("https://rc.example.test", "tok", "uid")
        client._session = _RecordingSession(*answers)
        return client

    async def test_uploads_then_confirms_into_the_thread(self):
        client = self._client(
            _FakeResp(200, {"success": True, "file": {"_id": "F1", "url": "/file-upload/F1/a.md"}}),
            _FakeResp(200, {"success": True, "message": {"_id": "M1"}}),
        )
        answer = await client.upload_file("R1", b"hello!\n", "a.md", "text/markdown", msg="", tmid="T1")
        self.assertEqual(answer["message"]["_id"], "M1")
        (url1, kw1), (url2, kw2) = client._session.calls
        self.assertTrue(url1.endswith("/api/v1/rooms.media/R1"))
        self.assertNotIn("Content-Type", kw1["headers"])  # aiohttp writes the multipart boundary
        self.assertEqual(kw1["headers"]["X-Auth-Token"], "tok")
        self.assertFalse(kw1["allow_redirects"])
        self.assertTrue(url2.endswith("/api/v1/rooms.mediaConfirm/R1/F1"))
        self.assertEqual(kw2["json"], {"msg": "", "tmid": "T1"})

    async def test_an_open_file_is_streamed_not_read_whole(self):
        import io

        client = self._client(
            _FakeResp(200, {"success": True, "file": {"_id": "F1"}}),
            _FakeResp(200, {"success": True, "message": {"_id": "M1"}}),
        )
        fh = io.BytesIO(b"abc")
        await client.upload_file("R1", fh, "a.bin", "application/octet-stream")
        form = client._session.calls[0][1]["data"]
        self.assertIs(form._fields[0][2], fh)

    async def test_no_tmid_in_the_main_channel(self):
        client = self._client(
            _FakeResp(200, {"success": True, "file": {"_id": "F1"}}),
            _FakeResp(200, {"success": True, "message": {"_id": "M1"}}),
        )
        await client.upload_file("R1", b"x", "a.txt", "text/plain", msg="Here is the file")
        self.assertEqual(client._session.calls[1][1]["json"], {"msg": "Here is the file"})

    async def test_server_rejection_is_reported_with_its_reason(self):
        client = self._client(_FakeResp(400, {"success": False, "errorType": "error-invalid-file-type"}))
        with self.assertLogs("rest_client", level="ERROR"):
            answer = await client.upload_file("R1", b"<svg/>", "a.svg", "image/svg+xml")
        self.assertEqual(answer, {"success": False, "status": 400, "error": "error-invalid-file-type"})
        self.assertEqual(len(client._session.calls), 1)  # no confirmation

    async def test_server_error_is_ambiguous_and_not_retried(self):
        client = self._client(_FakeResp(500, "oops"))
        with self.assertLogs("rest_client", level="ERROR"):
            self.assertEqual(await client.upload_file("R1", b"x", "a.txt", "text/plain"), {})
        self.assertEqual(len(client._session.calls), 1)

    async def test_network_error_raises_and_is_not_retried(self):
        import aiohttp

        client = self._client(aiohttp.ClientConnectionError("reset"))
        with self.assertLogs("rest_client", level="ERROR"), self.assertRaises(RocketChatNetworkError):
            await client.upload_file("R1", b"x", "a.txt", "text/plain")
        self.assertEqual(len(client._session.calls), 1)

    async def test_missing_file_id_is_a_failure(self):
        client = self._client(_FakeResp(200, {"success": True}))
        with self.assertLogs("rest_client", level="ERROR"):
            self.assertEqual(await client.upload_file("R1", b"x", "a.txt", "text/plain"), {})

    def test_upload_error_reads_error_type_then_error(self):
        self.assertEqual(upload_error('{"errorType": "error-file-too-large", "error": "x"}'),
                         "error-file-too-large")
        self.assertEqual(upload_error('{"error": "File size exceeds"}'), "File size exceeds")
        self.assertEqual(upload_error("not json"), "")
        self.assertEqual(upload_error("[1]"), "")


class _Cfg:
    outbound_files = True
    reasoning_leak_formatting_enabled = False


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class AdapterOutboundFileTests(unittest.TestCase):
    def setUp(self):
        self.adapter = load_mixin_module("adapter")
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".md", delete=False)
        tmp.write(b"hello!\n")
        tmp.close()
        self.path = tmp.name
        self.addCleanup(os.unlink, self.path)

    def _instance(self, answer=None, *, outbound_files=True):
        cfg = _Cfg()
        cfg.outbound_files = outbound_files
        rest = MagicMock()
        rest.upload_file = AsyncMock(
            side_effect=answer if isinstance(answer, BaseException) else None,
            return_value=answer if not isinstance(answer, BaseException) else None,
        )
        inst = make_bare_instance(
            self.adapter.RocketChatAdapter,
            _cfg=cfg,
            _rest=rest,
            _pending_ack={},
            _safe_react=MagicMock(),
        )
        return inst, rest

    def test_document_is_uploaded_with_its_name_and_type(self):
        inst, rest = self._instance({"success": True, "message": {"_id": "M1"}})
        result = asyncio.run(inst.send_document("R1", self.path, file_name="hello.md"))
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "M1")
        args, kwargs = rest.upload_file.await_args
        self.assertEqual((args[0], args[2], args[3]), ("R1", "hello.md", "text/markdown"))
        self.assertEqual(kwargs, {"msg": "", "tmid": ""})
        # a file object goes, not the read content; closed after the call
        self.assertEqual(args[1].name, self.path)
        self.assertTrue(args[1].closed)

    def test_the_file_goes_into_the_reply_thread(self):
        inst, rest = self._instance({"success": True, "message": {"_id": "M1"}})
        asyncio.run(inst.send_image_file("R1", self.path, metadata={"thread_id": "T9"}))
        self.assertEqual(rest.upload_file.await_args.kwargs["tmid"], "T9")

    def test_voice_and_video_upload_the_same_way(self):
        for method in ("send_voice", "send_video"):
            with self.subTest(method=method):
                inst, rest = self._instance({"success": True, "message": {"_id": "M1"}})
                self.assertTrue(asyncio.run(getattr(inst, method)("R1", self.path)).success)
                rest.upload_file.assert_awaited_once()

    def test_unknown_extension_is_sent_as_octet_stream(self):
        inst, rest = self._instance({"success": True, "message": {"_id": "M1"}})
        asyncio.run(inst.send_document("R1", self.path, file_name="adat.zzzz"))
        self.assertEqual(rest.upload_file.await_args.args[3], "application/octet-stream")

    def test_server_rejection_is_a_failure_so_the_core_notifies_the_user(self):
        inst, _rest = self._instance({"success": False, "status": 400, "error": "error-file-too-large"})
        with self.assertLogs(self.adapter.logger, "WARNING"):
            result = asyncio.run(inst.send_document("R1", self.path))
        self.assertFalse(result.success)
        self.assertIn("error-file-too-large", result.error)

    def test_network_error_is_retryable(self):
        inst, _rest = self._instance(self.adapter.RocketChatNetworkError("reset"))
        result = asyncio.run(inst.send_document("R1", self.path))
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_unreadable_file_is_a_failure_without_upload(self):
        inst, rest = self._instance({"success": True})
        with self.assertLogs(self.adapter.logger, "WARNING"):
            result = asyncio.run(inst.send_document("R1", "/nonexistent/x.pdf"))
        self.assertFalse(result.success)
        rest.upload_file.assert_not_awaited()

    def test_switched_off_falls_back_to_the_hermes_default(self):
        from gateway.platforms.base import BasePlatformAdapter

        inst, rest = self._instance(outbound_files=False)
        fallback = AsyncMock(return_value="core-default")
        with unittest.mock.patch.object(BasePlatformAdapter, "send_document", fallback):
            self.assertEqual(asyncio.run(inst.send_document("R1", self.path)), "core-default")
        rest.upload_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
