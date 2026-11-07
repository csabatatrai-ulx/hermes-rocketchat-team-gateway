"""Inbound attachments (media.py): reading file references, the room-context marker, the download's failure paths and the DDP payload.

The full round (the Hermes event gets the file) is covered by test_room_context.py's ``TestTriggerFiles``, through the real decision chain.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ddp_transport_mixin as ddp  # noqa: E402
from history import render_context_line  # noqa: E402
from media import (  # noqa: E402
    FileRef,
    describe_files,
    download_attachments,
    extract_file_refs,
    inline_text,
    message_type_name,
)

# Rocket.Chat 8.8's real shape, with no content.
IMAGE_MESSAGE = {
    "_id": "m-img",
    "msg": "What is in the image?",
    "file": {"_id": "f1", "name": "Image 1.png", "type": "image/png", "size": 145289},
    "files": [{"_id": "f1", "name": "Image 1.png", "type": "image/png", "size": 145289,
               "format": "png", "typeGroup": "image"}],
    "attachments": [{"type": "file", "fileId": "f1", "title": "Image 1.png",
                     "title_link": "/file-upload/f1/Image%201.png",
                     "image_url": "/file-upload/f1/Image%201.png", "image_type": "image/png"}],
    "u": {"_id": "uid-alice", "username": "alice"},
    "ts": "2026-10-16T09:10:00.000Z",
}


class TestExtractFileRefs(unittest.TestCase):
    def test_files_list_gives_one_ref_with_an_encoded_download_path(self):
        refs = extract_file_refs(IMAGE_MESSAGE)
        self.assertEqual(refs, [FileRef("f1", "Image 1.png", "image/png", 145289,
                                        "/file-upload/f1/Image%201.png")])

    def test_rocketchat_thumbnail_is_not_a_file_of_the_user(self):
        # Rocket.Chat also stores a thumbnail for an image (typeGroup "thumb"); without
        # this the model gets every image twice.
        msg = dict(IMAGE_MESSAGE, files=IMAGE_MESSAGE["files"] + [
            {"_id": "f1t", "name": "thumb-Image 1.png", "type": "image/png", "size": 4986,
             "format": "png", "typeGroup": "thumb"}])
        self.assertEqual([r.file_id for r in extract_file_refs(msg)], ["f1"])

    def test_legacy_single_file_field(self):
        msg = {"file": {"_id": "f2", "name": "a.md", "type": "text/markdown", "size": 10}}
        self.assertEqual([r.name for r in extract_file_refs(msg)], ["a.md"])

    def test_attachments_only_falls_back_to_title_link(self):
        msg = {"attachments": [{"type": "file", "title": "b.pdf", "format": "application/pdf",
                                "title_link": "/file-upload/f3/b.pdf"}]}
        ref = extract_file_refs(msg)[0]
        self.assertEqual((ref.file_id, ref.path, ref.mime_type), ("f3", "/file-upload/f3/b.pdf", "application/pdf"))

    def test_quotes_and_link_previews_are_not_files(self):
        msg = {"attachments": [{"text": "quote", "message_link": "https://rc/x"},
                               {"type": "file", "title_link": "https://elsewhere/x"}]}
        self.assertEqual(extract_file_refs(msg), [])

    def test_no_file_no_ref(self):
        self.assertEqual(extract_file_refs({"msg": "hi"}), [])
        self.assertEqual(extract_file_refs({}), [])


class TestContextLine(unittest.TestCase):
    def test_file_name_and_type_follow_the_text(self):
        line = render_context_line(IMAGE_MESSAGE, bot_user_id="b", bot_username="Hermes")
        self.assertTrue(line.endswith("alice: What is in the image? [file: Image 1.png (image/png)]"), line)

    def test_a_file_only_message_is_not_dropped(self):
        msg = dict(IMAGE_MESSAGE, msg="")
        line = render_context_line(msg, bot_user_id="b", bot_username="Hermes")
        self.assertTrue(line.endswith("alice: [file: Image 1.png (image/png)]"), line)

    def test_describe_files_is_empty_without_files(self):
        self.assertEqual(describe_files({"msg": "x"}), "")


class TestMessageType(unittest.TestCase):
    def test_kinds_map_like_the_hermes_adapters(self):
        self.assertEqual(message_type_name([]), "TEXT")
        self.assertEqual(message_type_name(["document", "image"]), "PHOTO")
        self.assertEqual(message_type_name(["audio"]), "VOICE")
        self.assertEqual(message_type_name(["video"]), "VIDEO")
        self.assertEqual(message_type_name(["document"]), "DOCUMENT")


def _cached(path, mime, kind):
    return SimpleNamespace(path=path, media_type=mime, kind=kind)


class TestDownloadAttachments(unittest.TestCase):
    def run_download(self, refs, download, cache_media, max_bytes=1000):
        return asyncio.run(download_attachments(
            refs, download=download, cache_media=cache_media, max_bytes=max_bytes))

    def ref(self, file_id, size=10, mime="text/markdown"):
        return FileRef(file_id, f"{file_id}.md", mime, size, f"/file-upload/{file_id}/{file_id}.md")

    def test_each_file_is_downloaded_with_the_limit_and_cached(self):
        download = AsyncMock(return_value=b"content")
        cache = AsyncMock(side_effect=lambda data, filename, mime_type: _cached(f"/c/{filename}", mime_type, "document"))
        media = self.run_download([self.ref("a"), self.ref("b")], download, cache)
        self.assertEqual(media.paths, ["/c/a.md", "/c/b.md"])
        self.assertEqual(media.media_types, ["text/markdown", "text/markdown"])
        self.assertEqual(media.kinds, ["document", "document"])
        self.assertEqual(media.text_inlined, [True, True])
        self.assertEqual(media.injected_text, "[Content of a.md]:\ncontent\n\n[Content of b.md]:\ncontent")
        download.assert_any_await("/file-upload/a/a.md", 1000)
        cache.assert_any_await(b"content", filename="a.md", mime_type="text/markdown")

    def test_a_file_over_the_limit_is_not_even_downloaded(self):
        download = AsyncMock(return_value=b"x")
        cache = AsyncMock()
        with self.assertLogs("media", level="WARNING"):
            media = self.run_download([self.ref("big", size=5000)], download, cache)
        self.assertEqual(media.paths, [])
        self.assertEqual(media.injected_text, (
            "[The user sent 'big.md' (text/markdown), but it is over the size limit and was not passed to "
            "you. Tell the user you cannot see it because it is too large.]"))
        download.assert_not_awaited()

    def test_zero_limit_means_no_limit_like_hermes(self):
        download = AsyncMock(return_value=b"x")
        cache = AsyncMock(return_value=_cached("/c/big", "text/markdown", "document"))
        media = self.run_download([self.ref("big", size=5000)], download, cache, max_bytes=0)
        self.assertEqual(media.paths, ["/c/big"])

    def test_failed_download_raising_cache_and_rejected_image_skip_only_that_file(self):
        download = AsyncMock(side_effect=[None, b"x", b"y", b"z"])
        cache = AsyncMock(side_effect=[RuntimeError("disk full"), None, _cached("/c/ok", "text/plain", "document")])
        with self.assertLogs("media", level="WARNING") as logs:
            media = self.run_download(
                [self.ref("gone"), self.ref("boom"), self.ref("bad", mime="image/png"), self.ref("ok")],
                download, cache)
        self.assertEqual(media.paths, ["/c/ok"])
        self.assertEqual(media.text_inlined, [True])
        self.assertEqual(len(logs.records), 2)  # the REST client logs a None download
        # The model knows about every skipped file (none silently drops out).
        for name in ("gone.md", "boom.md", "bad.md"):
            self.assertIn(f"[The user sent '{name}' (", media.injected_text)
        self.assertTrue(media.injected_text.endswith("[Content of ok.md]:\nz"))

    def test_a_rejected_image_is_announced_without_a_format_list(self):
        download = AsyncMock(return_value=b"\x00\x00\x00\x1cftypavif")
        cache = AsyncMock(return_value=None)
        ref = FileRef("f8", "Clipboard [1].avif", "image/avif", 20, "/file-upload/f8/x.avif")
        with self.assertLogs("media", level="WARNING"):
            media = self.run_download([ref], download, cache)
        self.assertEqual((media.paths, media.kinds), ([], []))
        self.assertEqual(media.injected_text, (
            "[The user sent 'Clipboard _1_.avif' (image/avif), but it could not be shown to you. "
            "Tell the user you cannot see it and ask them to send it in a common format such as PNG or JPEG.]"))


class TestInlineText(unittest.TestCase):
    """The pattern of Hermes's Discord and Slack adapters: extension gate, 100 KB, UTF-8."""

    def test_small_markdown_is_inlined_with_a_header(self):
        self.assertEqual(inline_text("notes.md", "text/markdown", "# Title".encode()), "[Content of notes.md]:\n# Title")

    def test_text_mime_without_known_extension_is_inlined(self):
        self.assertIsNotNone(inline_text("README", "text/plain", b"x"))

    def test_binary_documents_are_not_inlined(self):
        self.assertIsNone(inline_text("a.pdf", "application/pdf", b"%PDF-1.7\n\x00\x01stream"))
        self.assertIsNone(inline_text("a.zip", "application/zip", b"PK\x03\x04\x14\x00"))

    def test_unknown_extension_that_is_wholly_utf8_text_is_inlined(self):
        # A .dsl file with the application/octet-stream type.
        block = inline_text("workspace.dsl", "application/octet-stream", "workspace \"demo\" {\n}".encode())
        self.assertEqual(block, '[Content of workspace.dsl]:\nworkspace "demo" {\n}')

    def test_over_100_kb_or_not_utf8_is_not_inlined(self):
        self.assertIsNone(inline_text("big.txt", "text/plain", b"a" * (100 * 1024 + 1)))
        self.assertIsNone(inline_text("latin.txt", "text/plain", "caf\u00e9".encode("iso-8859-1")))

    def test_unsafe_characters_in_the_name_are_replaced(self):
        self.assertTrue(inline_text("a]b\n.md", "text/markdown", b"x").startswith("[Content of a_b_.md]:"))


class TestDdpPayload(unittest.TestCase):
    def test_files_reach_the_webhook_shaped_payload(self):
        payload = json.loads(ddp.ddp_message_to_webhook_payload(dict(IMAGE_MESSAGE, rid="r1"), webhook_secret=""))
        self.assertEqual(payload["files"], [{"_id": "f1", "name": "Image 1.png", "type": "image/png", "size": 145289}])

    def test_the_files_key_is_there_even_without_files(self):
        payload = json.loads(ddp.ddp_message_to_webhook_payload({"_id": "m", "rid": "r", "msg": "x"}, webhook_secret=""))
        self.assertEqual(payload["files"], [])


if __name__ == "__main__":
    unittest.main()
