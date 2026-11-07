"""Direct messages: recognising a group DM, and the seed window.

A DM's session belongs to Hermes core. The turn reaches core as ``chat_type="dm"``, and ``thread_id`` is the real
Rocket.Chat thread (or ``None``); core gives a DM one continuous session, kept in check by ``/new`` and core's own
compression. So this module does only three things:

* **A group DM is a room.** Rocket.Chat also stores a private conversation of more than two people as a ``t='d'``
  room. The server tells them apart by ``usersCount`` (``Rooms.findGroupDMsByUids``: ``usersCount > 2``,
  ``find1On1ByUserId``: ``usersCount: 2``). ``uids`` is less reliable: the server removes a deleted user from it but
  does not decrease ``usersCount``. So ``usersCount`` decides and ``uids`` is only a fallback.
* **Seed.** With ``ROCKETCHAT_DM_SEED_MESSAGES > 0``, while the bot has not answered in the fetched DM history (first
  contact), the turn gets at most that many of the DM's last messages, in the room block's format.
* **A DM thread's first turn.** A thread in a DM is its own session that starts empty; someone continuing under a bot
  reply with "Reply in thread" is reacting to that message. While the bot has not written in the thread, the turn gets
  the thread's root (first line) and its replies so far, in the room thread block's format; afterwards the session
  transcript is the context. The pattern of Hermes's Slack adapter (in DMs too: thread history only at cold start, in
  ``channel_context``).

Pure module (no network, no ``gateway.*`` import).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:  # as part of the plugin package (the normal runtime path)
    from .history import is_context_boundary, pin_thread_root, select_context_window
    from .inbound import rc_timestamp_seconds
except ImportError:  # unit tests import it as a bare module (see config.py)
    from history import is_context_boundary, pin_thread_root, select_context_window
    from inbound import rc_timestamp_seconds

DEFAULT_DM_SEED_MESSAGES = 0


def is_group_direct_room(room: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Whether ``rooms.info``'s room object is a DM of more than two participants.

    ``usersCount`` decides (the server's own distinction, see the module docstring), the length of ``uids`` is the
    fallback. ``None`` if neither is readable (a failed lookup): the caller decides what to do.
    """
    if not isinstance(room, dict):
        return None
    count = room.get("usersCount")
    if isinstance(count, int) and not isinstance(count, bool):
        return count > 2
    uids = room.get("uids")
    if isinstance(uids, list):
        return len(uids) > 2
    return None


def bot_replied_since(
    messages: List[Dict[str, Any]],
    since_epoch: Optional[float],
    *,
    bot_user_id: str,
    bot_username: str,
) -> bool:
    """Whether the bot has written (an own, non-system message) since ``since_epoch``.

    ``since_epoch is None``: at any time. An own message with an unknown timestamp counts as an answer (when in doubt,
    no seed: the seed is the rarer case).
    """
    for msg in messages or []:
        if not is_context_boundary(msg, bot_user_id=bot_user_id, bot_username=bot_username):
            continue
        if since_epoch is None:
            return True
        sent_at = rc_timestamp_seconds((msg or {}).get("ts"))
        if sent_at is None or sent_at >= since_epoch:
            return True
    return False


def select_seed_messages(
    messages: List[Dict[str, Any]],
    *,
    count: int,
    bot_user_id: str,
    bot_username: str,
) -> List[Dict[str, Any]]:
    """The seed: at most the last ``count`` real messages of the (chronological) list.

    System and empty messages drop out (same filter as the room window); ``count <= 0`` = no seed.
    """
    if count <= 0:
        return []
    return select_context_window(
        messages,
        bot_user_id=bot_user_id,
        bot_username=bot_username,
        limit=count,
        min_messages=count,
    )


def select_thread_seed(
    root: Optional[Dict[str, Any]],
    replies: List[Dict[str, Any]],
    *,
    limit: int,
    bot_user_id: str,
    bot_username: str,
) -> List[Dict[str, Any]]:
    """The block for a DM thread's first turn: the root and the thread's replies so far.

    ``replies`` are the chronological replies before the trigger, without the root. If the bot has written among them, the
    thread's session is alive (the root is in its transcript): no block. The root itself may be the bot's message, which is
    the typical case, so it does not count as a reply in the thread. The root is always the first line and counts towards
    ``limit`` (``pin_thread_root``, as in a room thread); ``limit <= 0`` = no block.
    """
    if limit <= 0:
        return []
    identity = dict(bot_user_id=bot_user_id, bot_username=bot_username)
    if bot_replied_since(replies, None, **identity):
        return []
    window = select_context_window(replies, limit=limit, min_messages=limit, **identity)
    return pin_thread_root(window, root, limit=limit)
