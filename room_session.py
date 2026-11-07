"""The boundary of the history block for rooms (and group DMs).

Room sessions belong to Hermes core. A turn reaches core as
``chat_type="channel"``, and ``thread_id`` is the real Rocket.Chat thread
(``tmid``) or ``None``. Core's documented settings decide the rest:
``group_sessions_per_user`` (default yes) gives a per-person session in the main
channel; a thread (``thread_sessions_per_user``, default no) is a session shared
by its participants, with a ``[name]`` prefix. A session lasts until ``/new``.

:func:`find_previous_mention` finds the block's boundary: in the main channel
the asker's previous mention in the same scope, statelessly, from the single
existing history fetch - whatever came before it is already in their session. A
``/new``/``/reset`` mention ends the search (the session restarted since), any
other ``/command`` is not a turn and is skipped. In a thread the shared session
makes the boundary the bot's last reply (history.py).

Pure module (no network, no ``gateway.*`` import).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:  # as part of the plugin package (the normal runtime path)
    from .history import is_system_message
    from .inbound import build_mention_pattern, rc_timestamp_seconds, strip_mention
except ImportError:  # unit tests import it as a bare module (see config.py)
    from history import is_system_message
    from inbound import build_mention_pattern, rc_timestamp_seconds, strip_mention

# The kind of boundary, for the summary log line.
BOUNDARY_PREVIOUS_MENTION = "previous-mention"
BOUNDARY_LAST_REPLY = "last-reply"
BOUNDARY_NONE = "none"

_SESSION_RESET_RE = re.compile(r"^/(new|reset)(?:\s|$)", re.IGNORECASE)


def is_session_reset_command(text: str) -> bool:
    """`/new` or `/reset` (with arguments too) - Hermes's session-clearing commands."""
    return bool(_SESSION_RESET_RE.match((text or "").strip()))


def mentions_bot(msg: Dict[str, Any], *, bot_user_id: str, bot_username: str) -> bool:
    """Whether the message addresses the bot: in the ``mentions`` list (id or
        name), or ``@botname`` in the text (with the inbound filter's bounded pattern).
    """
    uname = str(bot_username or "").lower()
    for mention in (msg or {}).get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        if bot_user_id and str(mention.get("_id") or "") == str(bot_user_id):
            return True
        if uname and str(mention.get("username") or "").lower() == uname:
            return True
    if not bot_username:
        return False
    return bool(build_mention_pattern(bot_username).search(str((msg or {}).get("msg") or "")))


def _is_from_sender(msg: Dict[str, Any], *, user_id: str, username: str) -> bool:
    """Whether this is the asker's own message: the author's ``u._id``; only
        without an id, the lower-cased ``u.username``.
    """
    author = (msg or {}).get("u") or {}
    if user_id and str(author.get("_id") or "") == str(user_id):
        return True
    uname = str(username or "").lower()
    return not user_id and bool(uname) and str(author.get("username") or "").lower() == uname


def _in_scope(msg: Dict[str, Any], tmid: str) -> bool:
    """Whether the message is in the same scope: its ``tmid`` matches. In the main
        channel (``tmid=""``) a thread reply also sent to the channel does not count;
        in a thread neither does the root (it has no ``tmid``: it belongs to the main
        channel).
    """
    return str((msg or {}).get("tmid") or "") == str(tmid or "")


def find_previous_mention(
    messages: List[Dict[str, Any]],
    *,
    user_id: str,
    username: str = "",
    tmid: str = "",
    since_epoch: Optional[float] = None,
    bot_user_id: str,
    bot_username: str,
) -> int:
    """Index of the asker's previous mention in the (chronological) list, or ``-1``.

    The boundary is the last non-system message before the trigger whose author is
    the asker (``u._id``; the name without an id), in the same scope
    (:func:`_in_scope`), that addresses the bot (:func:`mentions_bot`) and, when
    ``since_epoch`` is given, is not older than it (unknown timestamp = does not
    count). Among such mentions:

    * ``/new``/``/reset``: no boundary (the session may have restarted since);
    * any other ``/command`` (``/approve``, ``/whoami``): not an agent turn, skipped;
    * anything else: this is the boundary.
    """
    pattern = build_mention_pattern(bot_username) if bot_username else None
    for index in range(len(messages or []) - 1, -1, -1):
        msg = messages[index]
        if is_system_message(msg) or not _in_scope(msg, tmid):
            continue
        if not _is_from_sender(msg, user_id=user_id, username=username):
            continue
        if not mentions_bot(msg, bot_user_id=bot_user_id, bot_username=bot_username):
            continue
        sent_at = rc_timestamp_seconds((msg or {}).get("ts"))
        if since_epoch is not None and (sent_at is None or sent_at < since_epoch):
            return -1  # the list is chronological: nothing earlier counts either
        text = str((msg or {}).get("msg") or "")
        text = strip_mention(text, pattern) if pattern else text.strip()
        if is_session_reset_command(text):
            return -1
        if text.startswith("/"):
            continue
        return index
    return -1


def select_pending_window(
    messages: List[Dict[str, Any]], boundary: int, *, limit: int
) -> List[Dict[str, Any]]:
    """All real messages after the boundary, no lower bound, at most ``limit``.

    System and empty messages drop out (same filter as the room window); the bot's
    own replies stay in (rendered with a ``(you)`` marker).
    """
    if limit <= 0 or boundary < 0:
        return []
    after = [
        m for m in (messages or [])[boundary + 1:]
        if not is_system_message(m) and str((m or {}).get("msg") or "").strip()
    ]
    return after[-limit:]
