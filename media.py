"""Inbound attachments: the files of a Rocket.Chat message into Hermes's media store.

* **An attachment belongs to the message, so to the transport.** Hermes's built-in platforms
  (Mattermost, Teams, Telegram) work the same way: the adapter downloads the file with the
  inbound message, puts it in the media store with Hermes's ``cache_media_bytes_async`` and
  hands it over in ``MessageEvent.media_urls`` / ``media_types``. Hermes core then gives the
  image to the model (or the vision helper), extracts text from a document and transcribes
  a voice message. Fetching a file from an earlier message or another room is data access
  and does not belong here.
* **With the bot's own token, no redirects.** ``/file-upload/<id>/<name>`` returns 200 with the
  PAT headers and 403 without them. The bot only downloads what it can see anyway: the files of
  the mentioning message.
* **A size limit before the download too.** The cache function enforces Hermes's
  ``gateway.max_inbound_media_bytes`` itself; Rocket.Chat states the size up front, so an
  oversized file is not even downloaded, and the download stops at the limit.
* **A small text document's content goes into the request text.** Hermes core only adds a note
  to a document ("[The user sent a text document …]"); the adapter must insert the content and
  say so with ``MessageEvent.media_text_inlined`` (the pattern of Hermes's Discord and Slack
  adapters: a text extension or ``text/*`` MIME, at most 100 KB, UTF-8, with a
  ``[Content of <name>]:`` header). Without it the model sees only a file path it cannot open
  with a narrow toolset. A larger or binary document (PDF, DOCX) goes as a path; extraction
  belongs to Hermes and the model's tools.
* **A bad file does not stop the turn.** A failed download is one warning line; the other files
  and the text still reach the model.

The pure parts (reading references, the message type) are testable without a network; the
caller supplies the download (``download``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRef:
    """A file attached to a message, as Rocket.Chat describes it."""

    file_id: str
    name: str
    mime_type: str
    size: int
    path: str  # server-relative download path: /file-upload/<id>/<name>


def _download_path(file_id: str, name: str) -> str:
    return f"/file-upload/{quote(file_id, safe='')}/{quote(name, safe='')}"


def extract_file_refs(message: Dict[str, Any]) -> List[FileRef]:
    """The message's files: the ``files`` list (since Rocket.Chat 6), the ``file`` field on older
        messages; without both, the ``file``-type items of ``attachments`` (``title_link``). A file
        appears once. The thumbnail Rocket.Chat generates for an image (``typeGroup: "thumb"``) is not
        the user's file and is skipped (otherwise the model gets every image twice).
    """
    refs: List[FileRef] = []
    seen: set = set()
    files = (message or {}).get("files") or []
    if not files and (message or {}).get("file"):
        files = [message["file"]]
    for item in files:
        if not isinstance(item, dict) or item.get("typeGroup") == "thumb":
            continue
        file_id = str(item.get("_id") or "")
        name = str(item.get("name") or "")
        if not file_id or not name or file_id in seen:
            continue
        seen.add(file_id)
        refs.append(FileRef(
            file_id=file_id,
            name=name,
            mime_type=str(item.get("type") or ""),
            size=_as_int(item.get("size")),
            path=_download_path(file_id, name),
        ))
    if refs:
        return refs
    for att in (message or {}).get("attachments") or []:
        if not isinstance(att, dict) or att.get("type") != "file":
            continue
        link = str(att.get("title_link") or "")
        if not link.startswith("/file-upload/"):
            continue
        file_id = str(att.get("fileId") or link.split("/")[2])
        if file_id in seen:
            continue
        seen.add(file_id)
        refs.append(FileRef(
            file_id=file_id,
            name=str(att.get("title") or link.rsplit("/", 1)[-1]),
            mime_type=str(att.get("image_type") or att.get("audio_type")
                          or att.get("video_type") or att.get("format") or ""),
            size=_as_int(att.get("image_size") or att.get("size")),
            path=link,
        ))
    return refs


def rejected_media_notice(ref: FileRef, *, too_large: bool = False) -> str:
    """For the model: the user sent a file that did not reach it (the size limit, a download error,
        or Hermes did not accept it). Covers every skipped file, so the model does not describe the
        previous file or guess.
    """
    name = re.sub(r"[\[\]\r\n]", "_", ref.name)
    kind = f" ({ref.mime_type})" if ref.mime_type else ""
    if too_large:
        return (f"[The user sent '{name}'{kind}, but it is over the size limit and was not passed to "
                "you. Tell the user you cannot see it because it is too large.]")
    return (f"[The user sent '{name}'{kind}, but it could not be shown to you. Tell the user you "
            "cannot see it and ask them to send it in a common format such as PNG or JPEG.]")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def file_refs_payload(refs: List[FileRef]) -> List[Dict[str, Any]]:
    """The references' JSON form for the internal payload (DDP path)."""
    return [
        {"_id": r.file_id, "name": r.name, "type": r.mime_type, "size": r.size}
        for r in refs
    ]


def describe_files(message: Dict[str, Any]) -> str:
    """For a room-context line: ``[file: name (type)]``, one per file."""
    return " ".join(
        f"[file: {r.name}" + (f" ({r.mime_type})" if r.mime_type else "") + "]"
        for r in extract_file_refs(message)
    )


# Text files that can be inserted: a copy of Hermes's ``_TEXT_INJECT_EXTENSIONS`` (an
# internal name of Hermes that the plugin contract does not protect) and the
# Discord/Slack adapters' 100 KB limit. An extension gate, not blind UTF-8 decoding: the
# start of a PDF or ZIP can also decode as ASCII.
TEXT_INLINE_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".jsonl", ".ndjson", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties", ".html", ".htm",
    ".css", ".scss", ".sass", ".less", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs",
    ".java", ".kt", ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".r", ".jl", ".swift", ".m",
    ".scala", ".clj", ".ex", ".exs", ".erl", ".sql", ".graphql", ".proto", ".tf", ".hcl",
    ".dockerfile", ".makefile", ".cmake", ".gradle", ".rst", ".tex", ".srt", ".vtt", ".diff",
    ".patch"})
TEXT_INLINE_MAX_BYTES = 100 * 1024


# Whether the whole file is acceptable as text: valid UTF-8 with no control characters
# except tab and line breaks. This looks at the whole content, not just the start, so a
# PDF or ZIP (with binary streams) fails.
_ALLOWED_CONTROL = frozenset("\t\n\r\f")


def _looks_like_text(text: str) -> bool:
    return not any(ord(ch) < 32 and ch not in _ALLOWED_CONTROL for ch in text)


def inline_text(name: str, mime_type: str, data: bytes) -> Optional[str]:
    """The block to insert (``[Content of <name>]:`` + the text), or ``None`` if the file is not text,
    is too large or is not UTF-8.

    A text extension or ``text/*`` MIME counts as text (Hermes's rule), and so does any other file that
    is entirely valid UTF-8: Rocket.Chat marks a text file with an unknown extension (``.dsl``) as
    ``application/octet-stream``.
    """
    if len(data) > TEXT_INLINE_MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    known_text = ext in TEXT_INLINE_EXTENSIONS or (mime_type or "").startswith("text/")
    if not known_text and not _looks_like_text(text):
        return None
    display = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)
    return f"[Content of {display}]:\n{text}"


@dataclass
class DownloadedMedia:
    """The downloaded files, in the order of the ``MessageEvent`` fields."""

    paths: List[str]
    media_types: List[str]
    kinds: List[str]
    text_inlined: List[bool]
    injected_text: str  # the inserted text documents, in front of the request; "" if none


# The first matching prefix decides (as in Hermes's Mattermost adapter); everything else
# is a document.
_KIND_TO_MESSAGE_TYPE = (("image", "PHOTO"), ("audio", "VOICE"), ("video", "VIDEO"))


def message_type_name(kinds: List[str]) -> str:
    """The ``MessageType`` name from the kinds of downloaded files: image, then audio, video, otherwise
        document; ``TEXT`` without files.
    """
    if not kinds:
        return "TEXT"
    for kind, name in _KIND_TO_MESSAGE_TYPE:
        if kind in kinds:
            return name
    return "DOCUMENT"


Download = Callable[[str, int], Awaitable[Optional[bytes]]]
CacheMedia = Callable[..., Awaitable[Any]]


async def download_attachments(
    refs: List[FileRef],
    *,
    download: Download,
    cache_media: CacheMedia,
    max_bytes: int,
) -> DownloadedMedia:
    """Download every file and put it in Hermes's media store.

    ``download(path, max_bytes)`` returns the bytes, or ``None`` on error or for an oversized file;
    ``cache_media`` is Hermes's ``cache_media_bytes_async``. ``max_bytes <= 0``: no limit (as in Hermes).
    A small text document's content goes into ``injected_text`` (:func:`inline_text`).
    """
    paths: List[str] = []
    mimes: List[str] = []
    kinds: List[str] = []
    inlined: List[bool] = []
    injections: List[str] = []
    for ref in refs:
        if max_bytes > 0 and ref.size > max_bytes:
            logger.warning(
                "Rocket.Chat: attachment %s skipped: %d bytes over the %d-byte limit "
                "(gateway.max_inbound_media_bytes)", ref.file_id, ref.size, max_bytes,
            )
            injections.append(rejected_media_notice(ref, too_large=True))
            continue
        try:
            data = await download(ref.path, max_bytes)
            if data is None:
                injections.append(rejected_media_notice(ref))
                continue
            cached = await cache_media(data, filename=ref.name, mime_type=ref.mime_type)
        except Exception as exc:  # noqa: BLE001 - one file's failure does not stop the turn
            logger.warning("Rocket.Chat: attachment %s could not be cached: %s", ref.file_id, exc)
            injections.append(rejected_media_notice(ref))
            continue
        if not cached:
            # Hermes refused it (an AVIF image, say: the media store takes only
            # recognised image formats). The plugin keeps no format list of its own: it
            # only tells the model what Hermes actually refused, so with a newer Hermes
            # the note disappears by itself. Otherwise the file would silently drop out
            # and the model would describe the previous image or report a missing
            # attachment.
            logger.warning("Rocket.Chat: attachment %s failed the media check, skipped", ref.file_id)
            injections.append(rejected_media_notice(ref))
            continue
        paths.append(cached.path)
        mimes.append(cached.media_type)
        kinds.append(cached.kind)
        block = inline_text(ref.name, cached.media_type, data) if cached.kind == "document" else None
        inlined.append(block is not None)
        if block:
            injections.append(block)
    return DownloadedMedia(paths, mimes, kinds, inlined, "\n\n".join(injections))
