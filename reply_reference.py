"""Link an answer to its question: quote and @mention.

In a shared room several people can ask at once and an answer can take
minutes; without a reference nobody can tell which answer belongs to whom.
Telegram solves this with a native reply. The Rocket.Chat equivalent:

* **quote**: a message link on the server's own address (``?msg=<id>``) in the
  text, with an empty label - Rocket.Chat renders a quote card from it (the
  asker's name and the start of the question);
* **@mention**: the asker gets notified.

Only in rooms (and DMs with more than two people); a 1:1 DM needs neither.
Both can be switched off (``ROCKETCHAT_TEAM_REPLY_QUOTE``,
``ROCKETCHAT_TEAM_REPLY_MENTION``). Only the first answer message carries them (that
is where Hermes core passes the question's id as ``reply_to``); progress bubbles
and further chunks of a long answer do not.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

# At most this many question references are kept per process (the oldest drops out); a
# turn finishes in minutes, so this is plenty.
MAX_TRACKED = 1000


@dataclass(frozen=True)
class ReplyTarget:
    """A mention the answer refers to."""

    message_id: str
    room_id: str
    room_name: str
    room_type: str  # "c" channel, "p" private group, "d" (group) DM
    username: str
    is_direct: bool  # 1:1 DM: no reference


def message_link(site_url: str, target: ReplyTarget) -> str:
    """The question's message link on the server's own address (rendered as the quote)."""
    base = (site_url or "").rstrip("/")
    if target.room_type == "c" and target.room_name:
        path = f"channel/{quote(target.room_name)}"
    elif target.room_type == "p" and target.room_name:
        path = f"group/{quote(target.room_name)}"
    else:
        path = f"direct/{quote(target.room_id)}"
    return f"{base}/{path}?msg={quote(target.message_id)}"


def reply_prefix(
    target: Optional[ReplyTarget], *, site_url: str, quote_enabled: bool, mention_enabled: bool
) -> str:
    """The reference placed in front of the answer, or ``""`` (DM, disabled, unknown)."""
    if target is None or target.is_direct:
        return ""
    parts = []
    if quote_enabled and site_url and target.message_id:
        parts.append(f"[ ]({message_link(site_url, target)})")
    if mention_enabled and target.username:
        parts.append(f"@{target.username}")
    return (" ".join(parts) + "\n") if parts else ""


class ReplyTargets:
    """Question id -> :class:`ReplyTarget`, size-bounded (per process)."""

    def __init__(self, max_items: int = MAX_TRACKED) -> None:
        self._items: "OrderedDict[str, ReplyTarget]" = OrderedDict()
        self._max = max_items

    def remember(self, target: ReplyTarget) -> None:
        if not target.message_id:
            return
        self._items[target.message_id] = target
        self._items.move_to_end(target.message_id)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def get(self, message_id: Optional[str]) -> Optional[ReplyTarget]:
        return self._items.get(message_id or "")


class SentPrefixes:
    """Sent message id -> the reference added to it: a streamed edit
        (``edit_message``) keeps the same reference at the start of the message.
    """

    def __init__(self, max_items: int = MAX_TRACKED) -> None:
        self._items: "OrderedDict[str, str]" = OrderedDict()
        self._max = max_items

    def remember(self, message_id: Optional[str], prefix: str) -> None:
        if not message_id or not prefix:
            return
        self._items[message_id] = prefix
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def get(self, message_id: Optional[str]) -> str:
        return self._items.get(message_id or "", "")
