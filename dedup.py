"""Duplicate suppression, in both directions.

Two separate problems that happen to share a shape (a bounded, keyed cache):

**Inbound** - Rocket.Chat's Outgoing Webhook re-sends the call when the response does
not arrive fast enough on ITS side (even when our HTTP response is eventually a clean
200). That produced multiple agent turns for ONE user message. The retry carries the
same ``message_id``, so keying on that filters repeat delivery whatever triggered it.

A repeat can also come much later: on the DDP ``stream-room-messages`` channel
Rocket.Chat re-sends a message with the SAME ``_id`` (a "changed" event) whenever someone
reacts to it, edits it, pins it or replies in its thread, the bot's own ack reaction
included, and the catch-up after a reconnect replays too. A count-based cache filled by
every room message would evict an old @mention's id within hours on a busy server, and a
days-old question would get answered again. Therefore:

* time-based retention (``DEFAULT_INBOUND_DEDUP_RETENTION_HOURS``, env
  ``ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS``); the count limit
  (``DEFAULT_MAX_SEEN_MESSAGE_IDS``) is only a memory safety net;
* age limit: a message older than the retention window (Rocket.Chat ``ts``) never
  reaches the agent, whatever the cache says (``inbound.classify_inbound``);
* only a message that triggers an agent turn or command reply enters the cache (after the
  mention/DM and allowlist filters), not room chatter.

**Outbound** - the same finished answer getting posted to the channel twice. Hermes's
stream consumer had delivery paths that left the gateway believing the final content
still owed delivery, and this adapter's reasoning-leak formatting can rewrite the text
between the streamed preview and the final message, which defeats core's "is this
already on screen?" comparison. Rather than guess which one fired, this guards the
OUTPUT: the same content, to the same room, within a few seconds, is posted once. That is
mechanism-agnostic: it holds for any future core path with the same defect, and it is
the only layer a plugin fully controls.

The guard also records which user message a reply answers (``reply_to`` of the final send,
``metadata["reply_to_message_id"]`` of a streamed one): two identical replies to two
DIFFERENT, known user messages are both posted (two images, the same "I cannot see it"
answer). Everything else is suppressed. A known core duplicate is upstream #72131 (a
provider error is sent once as a status line WITHOUT ``reply_to``, then again as the final
answer), which an unknown trigger keeps covering. If upstream fixes it, nothing here
breaks: the guard simply stops firing.

Deliberately free of Rocket.Chat, HTTP and gateway knowledge: both classes take an
injectable clock, so expiry and eviction are testable without sleeping.

--- restart-surviving persistence ---

Without persistence a gateway restart would wipe the caches, discarding exactly the
protection meant to catch Rocket.Chat's retries at the moment a restart makes a retry
more likely. ``persist_path`` is opt-in (default ``None`` = pure memory); only the real,
process-wide instances (``shared_state.py``) pass a real path.

Writes are DEBOUNCED and CHAINED, never awaited on the caller's path: ``is_duplicate()``
and ``remember()`` are plain, SYNCHRONOUS methods (they sit on the call stack of
``classify_inbound()`` and ``adapter.send()``), so persistence can only be *scheduled* from
inside them. ``asyncio.get_running_loop().call_later()`` coalesces a burst of messages into
one disk write (``SAVE_DEBOUNCE_SECONDS``), and each write is chained onto the previous one
(``_chain_persist``), so a burst can never produce two concurrent writers of the same temp
file. Without a running event loop (only ad-hoc scripts and tests) scheduling is skipped
with a debug log: a cache write must never break the caller. A crash inside the debounce
window can lose the last few entries, which is accepted.

The default clock: both classes use ``time.monotonic`` when no ``persist_path`` is given and
``time.time()`` when persistence is on, because a monotonic clock resets on every process
restart and would make a persisted timestamp look like it is from the future.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Time-based retention (see "Inbound" in the module docstring). 72 hours covers resends
# caused by reactions and edits over a weekend, and the age limit (inbound.py) does not
# pass anything older to the agent anyway.
DEFAULT_INBOUND_DEDUP_RETENTION_HOURS = 72.0
# Only a memory safety net (the oldest goes first); forgetting is governed by the
# retention time above.
DEFAULT_MAX_SEEN_MESSAGE_IDS = 10000
DEFAULT_OUTBOUND_TTL_SECONDS = 10.0
DEFAULT_OUTBOUND_MAX_ENTRIES = 200

# See "restart-surviving persistence" in the module docstring: packs a quick burst of
# messages into a single disk write.
SAVE_DEBOUNCE_SECONDS = 0.25


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON load: a missing or bad file -> ``None``, never raises.

    A missing file is the EXPECTED state on first start; anything else (corrupt JSON, a
    permission error) also degrades to "start empty" instead of crashing the caller over a
    cache that can be rebuilt.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("dedup: could not read the saved state (%s): %s", path, exc)
        return None


def _write_json_file_atomic_sync(path: str, data: Dict[str, Any]) -> None:
    """The actual BLOCKING write, called only through ``asyncio.to_thread``."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp_path, path)  # atomic on the same filesystem


async def _write_json_file_atomic(path: str, data: Dict[str, Any]) -> None:
    """Does not block the event loop: file I/O goes to a worker thread."""
    await asyncio.to_thread(_write_json_file_atomic_sync, path, data)


def _chain_persist(store: Any) -> "asyncio.Task[None]":
    """Schedule ``store._persist()`` AFTER the write already in progress.

    Saves the current ``store._write_chain`` as "previous" and replaces it with the new task,
    so quick successive scheduling never runs two concurrent writers on the same temp file.
    """
    previous = store._write_chain

    async def _run() -> None:
        if previous is not None:
            try:
                await previous
            except Exception:  # noqa: BLE001 - an earlier failure must not block this one
                pass
        try:
            await store._persist()
        except Exception as exc:  # noqa: BLE001 - best effort, must never crash the caller
            logger.warning(
                "dedup: could not save the state (%s): %s", store.persist_path, exc
            )

    task = asyncio.ensure_future(_run())
    store._write_chain = task
    return task


def _schedule_persist(store: Any) -> None:
    """Debounced scheduling; see the module docstring.

    Expects these on ``store``: ``persist_path``, ``_save_handle``, ``_write_chain``,
    ``_persist()``.
    """
    if not store.persist_path:
        return
    if store._save_handle is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop (only ad-hoc scripts and tests; the gateway is entirely
        # asyncio-driven): this save is skipped, and the caller (is_duplicate/remember)
        # never fails because of it.
        logger.debug(
            "dedup: no running event loop, skipping this save (%s)", store.persist_path
        )
        return

    def _fire() -> None:
        store._save_handle = None
        _chain_persist(store)

    store._save_handle = loop.call_later(SAVE_DEBOUNCE_SECONDS, _fire)


async def _flush_persist(store: Any) -> None:
    """Cancel the pending debounce and write IMMEDIATELY, awaited.

    For graceful shutdown (``adapter.disconnect()``) and deterministic tests: afterwards the
    file is guaranteed current.
    """
    if not store.persist_path:
        return
    if store._save_handle is not None:
        store._save_handle.cancel()
        store._save_handle = None
    await _chain_persist(store)


def delivery_fingerprint_source(text: str) -> str:
    """The comparable string the outbound dedup keys on: the DELIVERED text.

    Dedup must key on what the user actually SEES, not on an intermediate representation.
    Every reply is plain Markdown text, so the formatted text itself is that representation.
    """
    return text


def content_fingerprint(chat_id: str, content: str) -> str:
    """Stable key for "this exact text, to this exact room".

    Hashed rather than stored verbatim so the cache size stays independent of
    reply length (a table-bearing answer can be thousands of characters, and
    the cache holds hundreds of entries).
    """
    digest = hashlib.sha256()
    digest.update(chat_id.encode("utf-8", "replace"))
    digest.update(b"\x00")  # separator: keeps room+content unambiguous
    digest.update(content.encode("utf-8", "replace"))
    return digest.hexdigest()


class InboundDeduplicator:
    """Filters repeat deliveries of the same ``message_id`` within the retention window."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_SEEN_MESSAGE_IDS,
        time_fn: Optional[Callable[[], float]] = None,
        persist_path: Optional[str] = None,
        retention_seconds: float = DEFAULT_INBOUND_DEDUP_RETENTION_HOURS * 3600.0,
    ) -> None:
        self._max_entries = max_entries
        self._retention = retention_seconds
        # See the clock paragraph of the module docstring: the monotonic default
        # switches to the wall clock only when persistence is on.
        self._time_fn = time_fn or (time.time if persist_path else time.monotonic)
        self.persist_path = persist_path
        # message_id -> time recorded, in recording order (oldest first).
        self._seen: Dict[str, float] = {}
        self._save_handle: Optional[asyncio.TimerHandle] = None
        self._write_chain: Optional["asyncio.Task[None]"] = None
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Best effort, synchronous (once at startup, not on the request path): loads the dict
        back in the "oldest first" order the eviction logic relies on, dropping entries past the
        retention time and capping at ``max_entries``.

        An entry with no timestamp loads as "recorded now": it stays for at most one retention
        period, which is safer than dropping it.
        """
        if not self.persist_path:
            return
        data = _read_json_file(self.persist_path)
        if not data or not isinstance(data.get("entries"), list):
            return
        now = self._time_fn()
        entries = []
        for item in data["entries"]:
            # A bare id or [id, ts]; a missing or non-finite timestamp -> "now".
            if isinstance(item, str) and item:
                entries.append((item, now))
            elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str) and item[0]:
                seen_at = item[1] if len(item) > 1 else None
                valid = (
                    isinstance(seen_at, (int, float))
                    and not isinstance(seen_at, bool)
                    and math.isfinite(seen_at)
                )
                entries.append((item[0], float(seen_at) if valid else now))
        entries.sort(key=lambda pair: pair[1])
        for message_id, seen_at in entries[-self._max_entries:]:
            self._seen[message_id] = seen_at
        self._prune()

    async def _persist(self) -> None:
        await _write_json_file_atomic(
            self.persist_path,
            {
                "version": 1,
                "savedAt": self._time_fn(),
                "entries": [[message_id, seen_at] for message_id, seen_at in self._seen.items()],
            },
        )

    async def flush(self) -> None:
        """Cancel the pending debounce and write IMMEDIATELY, awaited; see ``_flush_persist``."""
        await _flush_persist(self)

    @property
    def retention_seconds(self) -> float:
        return self._retention

    def _prune(self) -> None:
        """Drop entries past the retention time, from the front (oldest first)."""
        if not self._seen:
            return
        cutoff = self._time_fn() - self._retention
        while self._seen:
            oldest_id, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            del self._seen[oldest_id]

    def is_duplicate(self, message_id: str) -> bool:
        """True when this ``message_id`` was already recorded within the retention.

        Checks AND records in one call on purpose: two separate
        "have I seen it?" / "remember it" steps leave a window where a second
        delivery of the same message slips through between them. The caller
        (``inbound.classify_inbound``) only asks about messages that passed
        the mention/DM and allowlist filters, so ordinary room chatter never
        consumes entries.

        An empty ``message_id`` is never treated as a duplicate — without an
        id there is nothing to correlate, and silently dropping such a message
        would lose a real user turn.
        """
        if not message_id:
            return False
        self._prune()
        if message_id in self._seen:
            return True
        self._seen[message_id] = self._time_fn()
        # Memory safety net: the oldest goes (dict = insertion order).
        while len(self._seen) > self._max_entries:
            del self._seen[next(iter(self._seen))]
        _schedule_persist(self)
        return False

    def __len__(self) -> int:
        return len(self._seen)


class OutboundDeduplicator:
    """Suppresses re-posting identical content to the same room in quick succession.

    ``ttl_seconds <= 0`` disables the guard entirely (the operator escape
    hatch), in which case nothing is remembered and nothing is suppressed.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_OUTBOUND_TTL_SECONDS,
        max_entries: int = DEFAULT_OUTBOUND_MAX_ENTRIES,
        time_fn: Optional[Callable[[], float]] = None,
        persist_path: Optional[str] = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        # See the same line in InboundDeduplicator.__init__.
        self._time_fn = time_fn or (time.time if persist_path else time.monotonic)
        self.persist_path = persist_path
        # fingerprint -> (message_id of the original send, time it was sent,
        # id of the message it answered or "" if unknown)
        self._sent: Dict[str, Tuple[str, float, str]] = {}
        self._save_handle: Optional[asyncio.TimerHandle] = None
        self._write_chain: Optional["asyncio.Task[None]"] = None
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """See ``InboundDeduplicator._load_from_disk``: the same pattern with ``(message_id,
        sent_at)`` records. ``_prune()`` runs right after loading: after a long gateway downtime most
        saved entries would have expired past the TTL anyway.
        """
        if not self.persist_path:
            return
        data = _read_json_file(self.persist_path)
        if not data or not isinstance(data.get("entries"), list):
            return
        for item in data["entries"]:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            key, entry = item
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            message_id = entry.get("messageId")
            sent_at = entry.get("sentAt")
            trigger = entry.get("trigger")  # absent in older files: unknown
            if isinstance(message_id, str) and isinstance(sent_at, (int, float)):
                self._sent[key] = (message_id, float(sent_at), trigger if isinstance(trigger, str) else "")
        self._prune()

    async def _persist(self) -> None:
        await _write_json_file_atomic(
            self.persist_path,
            {
                "version": 1,
                "savedAt": self._time_fn(),
                "entries": [
                    [key, {"messageId": message_id, "sentAt": sent_at, "trigger": trigger}]
                    for key, (message_id, sent_at, trigger) in self._sent.items()
                ],
            },
        )

    async def flush(self) -> None:
        """Cancel the pending debounce and write IMMEDIATELY, awaited; see ``_flush_persist``."""
        await _flush_persist(self)

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def recent_message_id(self, chat_id: str, content: str, trigger: str = "") -> Optional[str]:
        """The id of an identical message sent within the TTL, or None.

        ``trigger`` is the id of the user message this reply answers. Two identical replies to two
        DIFFERENT, known user messages are both legitimate, so that one case is not a repeat.
        Everything else stays suppressed: the same trigger, or a reply whose trigger is unknown on
        either side, such as the status line Hermes core sends without ``reply_to`` and then delivers
        again as the final answer (upstream #72131). If upstream stops sending twice, nothing here
        depends on it: the guard simply never fires.

        Returning the ORIGINAL message id (rather than a bare "suppressed" flag) is what makes
        suppression safe for a streaming caller: it can report success with a real, addressable
        message id, so core's edit/finalize bookkeeping keeps working.
        """
        if not self.enabled or not content:
            return None
        self._prune()
        entry = self._sent.get(content_fingerprint(chat_id, content))
        if entry is None:
            return None
        message_id, sent_at, sent_trigger = entry
        if self._time_fn() - sent_at > self._ttl:
            return None
        if trigger and sent_trigger and trigger != sent_trigger:
            return None
        return message_id

    def remember(self, chat_id: str, content: str, message_id: str, trigger: str = "") -> None:
        """Record a successful send so an immediate repeat can be suppressed.

        Only remembers sends that produced a real ``message_id``: without one
        there would be nothing to return on a later hit, and suppressing a
        send while reporting "delivered, id unknown" would leave the caller
        unable to address the message it thinks it has.
        """
        if not self.enabled or not content or not message_id:
            return
        self._sent[content_fingerprint(chat_id, content)] = (message_id, self._time_fn(), trigger or "")
        self._prune()
        if len(self._sent) > self._max_entries:
            oldest = next(iter(self._sent))
            del self._sent[oldest]
        _schedule_persist(self)

    def forget(self, chat_id: str, content: str) -> None:
        """Delete an entry recorded by ``remember()``.

        The caller removes the ``_pending_`` mark written BEFORE sending when Rocket.Chat EXPLICITLY
        refused the send (the message certainly does not exist): otherwise core's retry would get
        "already sent" and the answer would silently vanish. On an ambiguous failure (the request may
        have gone out) the mark stays; it is the protection against a double message.
        """
        if self._sent.pop(content_fingerprint(chat_id, content), None) is not None:
            _schedule_persist(self)

    def _prune(self) -> None:
        """Drop expired entries so the cache can't grow on stale content."""
        if not self._sent:
            return
        cutoff = self._time_fn() - self._ttl
        expired = [key for key, (_, sent_at, _trigger) in self._sent.items() if sent_at < cutoff]
        for key in expired:
            del self._sent[key]

    def __len__(self) -> int:
        return len(self._sent)
