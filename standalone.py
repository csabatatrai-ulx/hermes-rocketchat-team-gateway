"""Out-of-process delivery: cron jobs and ``hermes send`` without a live gateway adapter.

Hermes's ``standalone_sender_fn`` hook. It opens a short-lived REST client, sends and closes. Without
this, a ``deliver=rocketchat`` job running in a separate process fails with "No live adapter".
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any, Dict, List, Optional

from gateway.platforms._shared import get_scoped_secret, send_error

from .rc_markdown import escape_lone_tildes
from .rest_client import (
    RocketChatNetworkError,
    RocketChatRestClient,
    is_successful,
    posted_message_id,
)


def _credentials(pconfig: Any) -> tuple:
    """(url, token, user id): the platform config first, then the profile-scoped environment."""
    extra = getattr(pconfig, "extra", {}) or {}
    url = str(extra.get("url") or get_scoped_secret("ROCKETCHAT_URL", "") or "").strip().rstrip("/")
    token = str(getattr(pconfig, "token", None) or get_scoped_secret("ROCKETCHAT_TOKEN", "") or "").strip()
    user_id = str(extra.get("user_id") or get_scoped_secret("ROCKETCHAT_USER_ID", "") or "").strip()
    return url, token, user_id


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[Any]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send text and files to a room. ``force_document`` is signature parity: every file is a document here."""
    url, token, user_id = _credentials(pconfig)
    if not (url and token and user_id):
        return send_error("Rocket.Chat standalone send: ROCKETCHAT_URL, ROCKETCHAT_TOKEN and ROCKETCHAT_USER_ID must be set")
    client = RocketChatRestClient(url, token, user_id)
    await client.open()
    tmid = str(thread_id or "")
    last_id = ""
    try:
        if message:
            answer = await client.post_message(chat_id, escape_lone_tildes(message), tmid)
            if not is_successful(answer):
                return send_error(f"Rocket.Chat refused the message: {(answer or {}).get('error') or 'unknown error'}")
            last_id = posted_message_id(answer)
        for media in media_files or []:
            path = media.get("path") if isinstance(media, dict) else media
            if not path or not os.path.exists(path):
                return send_error("Rocket.Chat standalone send: a media file does not exist")
            name = os.path.basename(path)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            with open(path, "rb") as handle:
                answer = await client.upload_file(chat_id, handle, name, mime, msg="", tmid=tmid)
            if not is_successful(answer):
                return send_error(f"Rocket.Chat refused the file: {(answer or {}).get('error') or 'unknown error'}")
            last_id = posted_message_id(answer) or last_id
    except RocketChatNetworkError as exc:
        return send_error(f"Rocket.Chat standalone send failed: {exc}")
    finally:
        await client.close()
    return {"success": True, "platform": "rocketchat", "chat_id": chat_id, "message_id": last_id}
