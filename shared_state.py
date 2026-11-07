"""Process-wide (not per-adapter-instance) shared state singletons.

This is the neutral import point that ``adapter.py`` AND its mixin files (``webhook_mixin.py``,
``ddp_transport_mixin.py``) can all import without a circular import.

Why process-wide: Hermes core's repeated-delivery defect sometimes creates ANOTHER, short-lived adapter
instance for a concurrent call, whose own empty dict would not protect against exactly that case.
"""

from __future__ import annotations

import asyncio
import os
from typing import Dict, Optional, Tuple

from .dedup import InboundDeduplicator, OutboundDeduplicator

_INBOUND_DEDUP: Optional[InboundDeduplicator] = None
_OUTBOUND_DEDUP: Optional[OutboundDeduplicator] = None


def _shared_inbound_dedup(
    dedup_state_dir: str = "", retention_hours: Optional[float] = None
) -> InboundDeduplicator:
    """The one inbound cache for this process.

    ``dedup_state_dir`` (``cfg.dedup_state_dir``, see config.py) is optional: empty gives pure memory
    (``persist_path=None``). Restart-surviving persistence (see dedup.py) uses one file per direction, so a
    failed or interrupted write in one does not touch the other. ``retention_hours``
    (``cfg.inbound_dedup_retention_hours``) ``None`` means dedup.py's default.
    """
    global _INBOUND_DEDUP
    if _INBOUND_DEDUP is None:
        persist_path = os.path.join(dedup_state_dir, "inbound.json") if dedup_state_dir else None
        kwargs = {} if retention_hours is None else {"retention_seconds": retention_hours * 3600.0}
        _INBOUND_DEDUP = InboundDeduplicator(persist_path=persist_path, **kwargs)
    return _INBOUND_DEDUP


def _shared_outbound_dedup(ttl_seconds: float, dedup_state_dir: str = "") -> OutboundDeduplicator:
    """The one outbound cache for this process.

    Built lazily (not at import time) because the TTL comes from the environment, which the gateway loads from
    its ``.env`` before creating an adapter but not necessarily before importing this module. For
    ``dedup_state_dir``, see ``_shared_inbound_dedup``.
    """
    global _OUTBOUND_DEDUP
    if _OUTBOUND_DEDUP is None:
        persist_path = os.path.join(dedup_state_dir, "outbound.json") if dedup_state_dir else None
        _OUTBOUND_DEDUP = OutboundDeduplicator(ttl_seconds=ttl_seconds, persist_path=persist_path)
    return _OUTBOUND_DEDUP


_DIRECT_ROOM_KINDS: Optional[Dict[str, bool]] = None
# Memory bound of the DM room cache: the oldest entry goes.
DIRECT_ROOM_KINDS_LIMIT = 1000


def _shared_direct_room_kinds() -> Dict[str, bool]:
    """The one DM room id -> "more than two participants" cache of this process.

    ``rooms.info`` tells whether a ``t='d'`` room is a group DM (see dm_session.py). A DM's participants are fixed
    at creation (the server recognises a group DM by ``usersCount`` and does not decrease it), so one lookup per
    room is enough; only successful answers are stored. Process-wide so a second adapter instance does not ask
    again.
    """
    global _DIRECT_ROOM_KINDS
    if _DIRECT_ROOM_KINDS is None:
        _DIRECT_ROOM_KINDS = {}
    return _DIRECT_ROOM_KINDS


def remember_direct_room_kind(table: Dict[str, bool], room_id: str, is_group: bool) -> None:
    """One DM room's entry, with the :data:`DIRECT_ROOM_KINDS_LIMIT` bound."""
    if not room_id:
        return
    table.pop(room_id, None)
    table[room_id] = is_group
    while len(table) > DIRECT_ROOM_KINDS_LIMIT:
        table.pop(next(iter(table)))


_SEND_LOCKS: Optional[Dict[str, asyncio.Lock]] = None


def _shared_send_locks() -> Dict[str, asyncio.Lock]:
    """The one (bot, room) send-mutex dict for this process.

    A per-instance ``Dict[str, asyncio.Lock]`` would not work: Hermes core's repeated-delivery defect (see
    ``dedup.py``) sometimes creates ANOTHER, short-lived adapter instance for one of the concurrent sends. Its own
    empty lock dict would not exclude the send running in the other instance, so the mutex would silently fail
    exactly where it matters.
    """
    global _SEND_LOCKS
    if _SEND_LOCKS is None:
        _SEND_LOCKS = {}
    return _SEND_LOCKS


_INFLIGHT_AUGMENT_ROOMS: Optional[Dict[str, Tuple[str, str]]] = None


def _shared_inflight_augment_rooms() -> Dict[str, Tuple[str, str]]:
    """The one session_key -> (room_id, tmid) map of turns still being prepared.

    Only the window before ``handle_message()`` (context fetch, file download); from there Hermes core's own
    shutdown notice covers the turn, thread-aware. The entry is removed right after the handoff. ``tmid`` is a thread
    mention's thread (``""`` in the main channel): the shutdown notice goes where the answer would have gone.

    ``disconnect()`` (adapter.py) is the hook: Hermes core calls it, with a bounded timeout, as part of its graceful
    SIGTERM shutdown. Registering our OWN ``signal.signal(SIGTERM, ...)`` handler would risk REPLACING the handler
    core installed for its own drain sequence (Python's ``signal.signal`` has single-handler-per-signal semantics), so
    ``disconnect()`` is the safe, additive extension point.

    Keyed by SESSION KEY, not by ``_augment_and_handle``'s own task: ``handle_message()`` returns quickly by spawning
    background tasks, so the task's lifetime ends at the handoff.

    PROCESS-WIDE for the same reason as ``_shared_send_locks``: the turn could be running on a short-lived second
    adapter instance, whose empty dict the ``disconnect()`` call on the instance core tears down would not see.
    """
    global _INFLIGHT_AUGMENT_ROOMS
    if _INFLIGHT_AUGMENT_ROOMS is None:
        _INFLIGHT_AUGMENT_ROOMS = {}
    return _INFLIGHT_AUGMENT_ROOMS


# Reply reference (reply_reference.py): the question's data (written at dispatch), the
# prefix of sent messages (for streamed edits) and the server's own address (for the
# quote link). PROCESS-WIDE, because send()/edit_message() can run on another,
# short-lived adapter instance.
_REPLY_TARGETS = None
_SENT_PREFIXES = None
_SITE_URL: Dict[str, str] = {}


def _shared_reply_targets():
    global _REPLY_TARGETS
    if _REPLY_TARGETS is None:
        from .reply_reference import ReplyTargets
        _REPLY_TARGETS = ReplyTargets()
    return _REPLY_TARGETS


def _shared_sent_prefixes():
    global _SENT_PREFIXES
    if _SENT_PREFIXES is None:
        from .reply_reference import SentPrefixes
        _SENT_PREFIXES = SentPrefixes()
    return _SENT_PREFIXES


def _shared_site_url() -> Dict[str, str]:
    """Bot username -> Rocket.Chat's ``Site_Url`` (filled by connect())."""
    return _SITE_URL
