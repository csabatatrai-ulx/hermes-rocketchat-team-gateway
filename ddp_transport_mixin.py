"""DDP (Rocket.Chat Realtime API) WebSocket transport (the default inbound path).

The webhook-based path (``webhook_mixin.WebhookInboundMixin``) is the fallback. When
``ROCKETCHAT_TRANSPORT`` is ``"ddp"`` (the default), this mixin opens a persistent
WebSocket to the same Rocket.Chat instance, logs in with the SAME Personal Access
Token used for REST, subscribes ONCE to ``stream-room-messages`` on the
``__my_messages__`` virtual room (the same stream the web client uses: every room the
account can access, DMs included), drops every delivery for a room the bot is NOT a
member of, and feeds each remaining message through the SAME ``classify_inbound``
decision chain as the webhook path, by building a synthetic Outgoing-Webhook-shaped
payload from the DDP message (``ddp_message_to_webhook_payload``). Replies always go
over REST (``chat.postMessage``/``chat.update``), never over this socket.

Design choices worth keeping:

* The ``login`` method's ``result``/``error`` response is explicitly awaited
  and checked, not fire-and-forget.
* 401/403 handling is TYPE-based only (``aiohttp.WSServerHandshakeError.
  status``), never a string/substring match on the exception text.
* The previous DDP socket is explicitly closed before a reconnect opens a
  new one (a zombie previous connection would double-process events - the
  same class of bug Hermes's Discord adapter documents fixing).
* A DDP "Method ... not found" answer marks that method unsupported for the
  rest of the process (``ddp_method_supported``), so the same code runs on
  Rocket.Chat 8.x and 9.x (docs/compatibility.md).

Known limitation: the WebSocket shares ``self._rest.session`` (a
``force_close=True`` connector) with REST, and the endpoint-redirect check
is a simple scheme+host+path comparison (the fuller URL validation lives on
the REST side, see rest_client.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

try:  # as part of the plugin package (the normal runtime path)
    from .dedup import _read_json_file, _write_json_file_atomic
    from .inbound import rc_timestamp, rc_timestamp_seconds
    from .media import extract_file_refs, file_refs_payload
    from .reply_reference import ReplyTarget, message_link
    from .user_texts import user_text
except ImportError:  # unit tests import it as a bare module (see config.py)
    from dedup import _read_json_file, _write_json_file_atomic
    from inbound import rc_timestamp, rc_timestamp_seconds
    from media import extract_file_refs, file_refs_payload
    from reply_reference import ReplyTarget, message_link
    from user_texts import user_text

logger = logging.getLogger(__name__)

_DDP_PROTOCOL_VERSION = "1"
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2
_LOGIN_RESULT_TIMEOUT_SECONDS = 15.0


class DdpLoginError(Exception):
    """The DDP ``login`` method call came back with an ``error`` field."""

    def __init__(self, message: str, *, error_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def websocket_url(base_url: str) -> str:
    """``https://host/path`` -> ``wss://host/path/websocket`` (``http``->``ws``)."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = (parts.path or "").rstrip("/") + "/websocket"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def websocket_endpoint_matches(expected_ws_url: str, final_url: Any) -> bool:
    """True unless the connection was silently redirected somewhere else.

    Compares scheme+host+path only (query/fragment on the live response are
    not meaningful here) - a coarser check than the reference project's, see
    the module docstring.
    """
    if final_url is None:
        return True  # nothing to compare against (e.g. in tests) - don't fail closed here
    expected = urlsplit(expected_ws_url)
    actual = urlsplit(str(final_url))
    return (
        actual.scheme in (expected.scheme, "http" if expected.scheme == "ws" else "https")
        and actual.netloc == expected.netloc
        and actual.path == expected.path
    )


def ddp_message_to_webhook_payload(
    post: Dict[str, Any], *, webhook_secret: str
) -> bytes:
    """Map one Rocket.Chat message object (a DDP ``changed`` event's payload)
    onto the SAME JSON shape ``inbound.classify_inbound`` expects from a real
    Outgoing Webhook delivery, so the DDP path reuses that decision chain
    verbatim instead of duplicating it.

    ``classify_inbound`` reads (``inbound.py``): ``token`` (secret check),
    ``bot``/``user_id``/``user_name`` (self-filter), ``message_id``,
    ``channel_id``, ``channel_name``, ``text``, ``timestamp`` (age guard),
    ``tmid`` (a thread reply's root: the room context and the reply stay in
    that thread, like the Outgoing Webhook's ``tmid``), ``message_type`` (the
    message's ``t``: non-empty for a system message, which starts no turn). A DDP message object has no
    readable channel NAME (only ``rid``, the room id) - callers that need
    the display name should resolve it separately (e.g. via a cached
    ``rooms.info`` REST call) and pass it in; this function falls back to
    the room id itself when none is given (a known simplification).
    """
    sender = post.get("u") or {}
    payload = {
        "token": webhook_secret,
        "bot": bool(post.get("bot")),
        "user_id": str(sender.get("_id", "") or ""),
        "user_name": str(sender.get("username", "") or ""),
        "message_id": str(post.get("_id", "") or ""),
        "channel_id": str(post.get("rid", "") or ""),
        "channel_name": str(post.get("_channel_name") or post.get("rid", "") or ""),
        "text": str(post.get("msg", "") or ""),
        # System message type (``t``: ``videoconf``, ``uj``); empty for plain chat.
        # Without this a call start would launch a turn as a "Conference Call" message.
        "message_type": str(post.get("t", "") or ""),
        "room_type": str(post.get("_room_type", "") or ""),
        # For the age limit (inbound.is_stale_message): the raw ``ts`` passes through;
        # inbound.rc_timestamp_seconds parses both the DDP ``{"$date": ms}`` and the
        # REST ISO string.
        "timestamp": post.get("ts"),
        # For a thread reply, the root's id (the Outgoing Webhook sends it the same way:
        # ``if (message.tmid) data.tmid = message.tmid``); see inbound.py.
        "tmid": str(post.get("tmid", "") or ""),
        # The message's files (see media.py): the DDP message carries them, so the
        # download needs no extra REST call. The key is always present (even with an
        # empty list): that tells the caller not to turn to ``chat.getMessage`` (webhook
        # mode).
        "files": file_refs_payload(extract_file_refs(post)),
    }
    return json.dumps(payload).encode("utf-8")


# Overlap kept before the last frame seen, so a message that was in flight at
# the moment the connection died is not missed - the inbound dedup drops the
# ones already handled.
_CATCHUP_OVERLAP_SECONDS = 5
_CATCHUP_PAGE_SIZE = 100


def catch_up_since(
    last_frame_at: Optional[float], now: float, max_seconds: int
) -> Optional[float]:
    """Epoch seconds to replay room history from after a reconnect, or None.

    None on the first connect of the process (nothing seen yet - there is no
    gap to fill) and when catch-up is disabled (``max_seconds <= 0``).
    Otherwise the gap start (minus a small overlap), but never further back
    than ``max_seconds``: after a long outage the bot must not answer
    hours-old mentions.
    """
    if last_frame_at is None or max_seconds <= 0:
        return None
    return max(last_frame_at - _CATCHUP_OVERLAP_SECONDS, now - max_seconds)


# Startup check: the durable cursor (the last seen message's SERVER time) in the dedup
# folder. Written at most this often after a live message, and immediately on shutdown
# (``_ddp_flush_cursor``).
_CURSOR_FILE_NAME = "ddp_cursor.json"
_CURSOR_SAVE_INTERVAL_SECONDS = 5.0

# The DDP connection state in the same folder, for a container readiness probe:
# "connected" only after the subscription, "reconnecting" after every drop,
# "auth_failed" on a permanent authentication error (at DDP login and at the initial
# REST authentication), "stopped" on shutdown. At startup, at the beginning of the
# adapter's connect(), it is written as "connecting", so a "connected" left by an
# earlier process cannot signal false readiness. Its own file, because Hermes's adapter
# guide documents no surface for signalling a reconnect (gateway_state.json stays
# "connected" throughout).
_STATUS_FILE_NAME = "ddp_status.json"


# One notice links to at most this many missed messages.
_MAX_NOTICE_QUOTES = 5


class StartupCatchup:
    """One fresh start's catch-up run: what goes to Hermes, what gets a notice.

    In ``notice`` mode no read-back message addressed to the bot reaches Hermes; in
    ``replay`` mode at most ``max_dispatches`` do, the rest get a notice. Notices are
    collected per room and thread so a busy room gets no series; a notice links to the
    missed messages, so that in ``replay`` mode too it is clear which ones get no answer.
    """

    def __init__(self, mode: str, max_dispatches: int) -> None:
        self.mode = mode
        self.max_dispatches = max_dispatches
        self.dispatched = 0
        self.missed = 0
        # (room_id, tmid) -> the missed messages, in order of arrival
        self.notices: Dict[Tuple[str, str], List[ReplyTarget]] = {}

    def take(self, decision: Any, room_type: str = "") -> bool:
        """True if the ``decision`` may go to Hermes; otherwise it goes to a notice."""
        if self.mode == "replay" and self.dispatched < self.max_dispatches:
            self.dispatched += 1
            return True
        self.missed += 1
        self.notices.setdefault((decision.channel_id, decision.thread_id or ""), []).append(
            ReplyTarget(
                message_id=decision.message_id or "",
                room_id=decision.channel_id,
                room_name=getattr(decision, "channel_name", "") or "",
                room_type=room_type,
                username=decision.sender_username or "",
                is_direct=bool(decision.is_direct),
            )
        )
        return False


def missed_notice_text(missed: List[ReplyTarget], *, site_url: str = "") -> str:
    """The notice for missed messages: in a room it mentions the senders and, when
        Site_Url is known, links at most ``_MAX_NOTICE_QUOTES`` quotes.
    """
    key = "startup_catchup_skipped_one" if len(missed) == 1 else "startup_catchup_skipped_many"
    text = user_text(key, count=str(len(missed)))
    if missed and not missed[0].is_direct:
        senders = sorted({target.username for target in missed if target.username})
        if senders:
            text = " ".join(f"@{name}" for name in senders) + " " + text
    if site_url:
        links = [
            f"[ ]({message_link(site_url, target)})"
            for target in missed[:_MAX_NOTICE_QUOTES] if target.message_id
        ]
        if links:
            text += "\n" + " ".join(links)
    return text


def message_epoch_seconds(message: Dict[str, Any]) -> Optional[float]:
    """A message's SERVER timestamp in epoch seconds, or None.

    Accepts both shapes Rocket.Chat uses: DDP/EJSON ``{"$date": <ms>}`` and
    the REST ISO string. Server time is what the catch-up gap is measured
    in - the local receive time is not reliable (a frozen/suspended process
    reads its socket buffer late, which would make the gap look empty).
    """
    return rc_timestamp_seconds(message.get("ts"))


def is_method_not_found(response: Any) -> bool:
    """True if a DDP ``result`` frame says the server has no such method.

    Meteor/Rocket.Chat shape: ``{"error": {"error": 404, "reason": "Method 'x' not found",
    ...}}``. This must be treated as "unsupported", not "refused": after a server upgrade
    (9.0 marks the ``sendMessage`` DDP method for removal) the call will never succeed
    again, so retrying is pointless. Any other error (permissions, validation) is NOT this;
    the caller handles those as usual.
    """
    if not isinstance(response, dict):
        return False
    err = response.get("error")
    if not isinstance(err, dict):
        return False
    reason = str(err.get("reason") or err.get("message") or "").lower()
    return str(err.get("error")) == "404" and "not found" in reason


# The method names of at most this many "notify" calls awaiting an answer are kept (see
# _ddp_notify). The server answers every call; this is only a memory bound in case it
# does not.
_NOTIFY_INFLIGHT_LIMIT = 256


def stream_meta(args: List[Any]) -> Dict[str, Any]:
    """The metadata Rocket.Chat appends to a ``__my_messages__`` event as its
    SECOND arg: ``{roomParticipant, roomType, roomName}``, or ``{}`` when
    absent (older servers). Needed because ``__my_messages__`` reaches every
    user who can ACCESS the room - for a public channel that is everyone,
    member or not.
    """
    if len(args) < 2 or not isinstance(args[1], dict):
        return {}
    return args[1]


class DdpTransportMixin:
    """DDP WebSocket layer of :class:`~.adapter.RocketChatAdapter`.

    State is created lazily by :meth:`_ddp_start` (called from ``connect()``
    when ``ROCKETCHAT_TRANSPORT`` includes ``"ddp"``), not ``__init__`` -
    keeps this mixin free of any constructor-ordering assumption relative to
    the other mixins.
    """

    _ddp_ws: Any = None
    _ddp_task: Optional["asyncio.Task"] = None
    _ddp_closing: bool = False
    _ddp_ready: Optional[asyncio.Event] = None
    _ddp_next_id: int = 0
    _ddp_subs: Dict[str, bool]
    _ddp_pending_calls: Dict[str, "asyncio.Future"]
    # In-flight per-message handler tasks (strong references, so a running
    # handler is never garbage-collected mid-flight - see _ddp_dispatch_frame).
    _ddp_handler_tasks: set
    # SERVER timestamp of the newest member-room message seen on ANY session
    # of this process - the start of the gap a reconnect has to fill.
    _ddp_last_message_at: Optional[float] = None
    # The pending timer of the throttled cursor save (``_ddp_schedule_cursor_save``).
    _ddp_cursor_handle: Optional[asyncio.TimerHandle] = None
    # DDP methods the server answered with "Method ... not found" (404). Sticky per
    # process: after a server upgrade the method does not come back, and the caller then
    # uses the REST path or (typing indicator) silently skips. Frozenset and assignment,
    # not in-place mutation, so the class-level default is never shared between
    # instances.
    _ddp_unsupported_methods: frozenset = frozenset()
    # call id -> method name for _ddp_notify calls (no future waits on them), so the
    # result frame can reveal that the method does not exist.
    _ddp_notify_inflight: Optional[Dict[str, str]] = None

    async def _ddp_start(self) -> bool:
        """Start the DDP loop as a background task; return once connected once.

        Non-blocking by design (``connect()`` awaits this, not the loop
        itself): a slow/never-connecting DDP endpoint must not hold up the
        webhook side or the rest of the adapter's startup, matching the
        ``connect(*, is_reconnect)`` contract every Hermes platform adapter
        follows.
        """
        self._ddp_closing = False
        self._ddp_subs = {}
        self._ddp_pending_calls = {}
        self._ddp_handler_tasks = set()
        self._ddp_next_id = 0
        self._ddp_ready = asyncio.Event()
        await self._ddp_write_status("connecting")
        self._ddp_task = asyncio.create_task(self._ddp_loop())
        try:
            await asyncio.wait_for(self._ddp_ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Rocket.Chat: DDP did not confirm readiness within 30s - "
                "continuing in the background (reconnect loop keeps trying)."
            )
        # Always True: DDP failures are retried in the background, they must
        # never fail the whole adapter's connect() (the webhook side, if
        # also enabled, must not be held hostage by a DDP outage).
        return True

    async def _ddp_stop(self) -> None:
        self._ddp_closing = True
        if self._ddp_task is not None:
            self._ddp_task.cancel()
            try:
                await self._ddp_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown, best-effort
                pass
            self._ddp_task = None
        await self._ddp_close_socket()
        await self._ddp_write_status("stopped")

    async def _ddp_close_socket(self) -> None:
        ws = self._ddp_ws
        self._ddp_ws = None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - best-effort on shutdown/reconnect
                logger.debug("Rocket.Chat: DDP socket close raised", exc_info=True)

    # ------------------------------------------------------------------
    # Frame send helpers
    # ------------------------------------------------------------------

    def ddp_method_supported(self, method: str) -> bool:
        """False once the server has signalled that this DDP method does not exist.

        Feature detection instead of version numbers: the same code runs on 8.x and 9.0, and a
        missing method is skipped after its first 404 answer (see :func:`is_method_not_found`).
        """
        return method not in self._ddp_unsupported_methods

    def _ddp_note_method_result(self, method: str, response: Any) -> bool:
        """Remember if ``response`` says ``method`` does not exist; True if so.

        Warns once per method (WARNING); callers then quietly avoid it through
        :meth:`ddp_method_supported`.
        """
        if not is_method_not_found(response):
            return False
        if method not in self._ddp_unsupported_methods:
            self._ddp_unsupported_methods = self._ddp_unsupported_methods | {method}
            logger.warning(
                "Rocket.Chat: this server no longer offers the DDP method %r "
                "(Method not found) - not calling it again in this process",
                method,
            )
        return True

    async def _ddp_send(self, frame: Dict[str, Any]) -> None:
        ws = self._ddp_ws
        if ws is None or ws.closed:
            return
        await ws.send_json(frame)

    async def _ddp_call_method(self, method: str, params: List[Any]) -> Any:
        """Invoke a DDP method and WAIT for its ``result``/``error`` response.

        Deliberately not fire-and-forget - see the module docstring. Returns
        the ``result`` payload, or the WHOLE frame when it carries an
        ``error`` (the caller checks it: login raises :class:`DdpLoginError`,
        ``adapter.py``'s ``sendMessage``/``updateMessage`` fall back to
        REST). Times out after ``_LOGIN_RESULT_TIMEOUT_SECONDS``.
        """
        call_id = str(self._ddp_next_id)
        self._ddp_next_id += 1
        future: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self._ddp_pending_calls[call_id] = future
        await self._ddp_send(
            {"msg": "method", "method": method, "id": call_id, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout=_LOGIN_RESULT_TIMEOUT_SECONDS)
        finally:
            self._ddp_pending_calls.pop(call_id, None)

    async def _ddp_notify(self, method: str, params: List[Any]) -> None:
        """Invoke a DDP method WITHOUT waiting for its ``result`` response.

        Deliberately separate from :meth:`_ddp_call_method`: that one blocks the caller up to
        ``_LOGIN_RESULT_TIMEOUT_SECONDS`` and is tuned for call sites that need the response
        (login). A "notify"-style method (``stream-notify-room`` for the typing indicator, see
        ``adapter.py`` ``send_typing``) has no result the caller cares about, and must never add
        latency to the hot path. No pending future is registered, so nothing leaks; a ``result``
        frame for this call id is only inspected for "Method not found".

        The method name is remembered per call id (bounded by ``_NOTIFY_INFLIGHT_LIMIT``), so a
        "Method not found" answer still marks the method unsupported (see
        :meth:`_ddp_note_method_result`): a removed method must not be retried forever.
        """
        call_id = str(self._ddp_next_id)
        self._ddp_next_id += 1
        inflight = self._ddp_notify_inflight
        if inflight is None:
            inflight = self._ddp_notify_inflight = {}
        if len(inflight) >= _NOTIFY_INFLIGHT_LIMIT:
            inflight.pop(next(iter(inflight)))
        inflight[call_id] = method
        await self._ddp_send(
            {"msg": "method", "method": method, "id": call_id, "params": params}
        )

    async def _ddp_sub(self, name: str, params: List[Any]) -> str:
        sub_id = str(uuid.uuid4())
        self._ddp_subs[sub_id] = False
        await self._ddp_send({"msg": "sub", "id": sub_id, "name": name, "params": params})
        return sub_id

    # ------------------------------------------------------------------
    # Reconnect loop
    # ------------------------------------------------------------------

    async def _ddp_loop(self) -> None:
        delay = _RECONNECT_BASE_DELAY
        while not self._ddp_closing:
            try:
                await self._ddp_connect_and_listen()
                delay = _RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - classified below
                if self._ddp_closing:
                    return
                if self._ddp_is_permanent_auth_error(exc):
                    logger.error(
                        "Rocket.Chat: DDP permanent auth error (%s) - stopping "
                        "reconnect attempts. Fix ROCKETCHAT_TOKEN/USER_ID and "
                        "restart the gateway to retry.",
                        exc,
                    )
                    await self._ddp_write_status("auth_failed")
                    if hasattr(self, "_set_fatal_error"):
                        self._set_fatal_error(
                            "ddp_auth_failed", str(exc), retryable=False
                        )
                        if hasattr(self, "_notify_fatal_error"):
                            await self._notify_fatal_error()
                    self._ddp_ready and self._ddp_ready.set()
                    return
                logger.warning(
                    "Rocket.Chat: DDP connection error (%s: %s) - reconnecting in %.0fs",
                    type(exc).__name__, exc, delay,
                )
            finally:
                await self._ddp_close_socket()

            if self._ddp_closing:
                return
            await self._ddp_write_status("reconnecting")
            self._ddp_ready and self._ddp_ready.set()  # unblock _ddp_start even on failure
            jitter = delay * _RECONNECT_JITTER * random.random()
            await asyncio.sleep(delay + jitter)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    @staticmethod
    def _ddp_is_permanent_auth_error(exc: Exception) -> bool:
        """TYPE-based only - see the module docstring on why no substring match."""
        import aiohttp

        if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in (401, 403):
            return True
        if isinstance(exc, DdpLoginError) and exc.error_code in (401, 403):
            return True
        return False

    async def _ddp_connect_and_listen(self) -> None:
        """One DDP session end-to-end: connect, login (checked), subscribe, listen."""
        base_url = getattr(self, "_base_url", "") or self._cfg.base_url
        ws_url = websocket_url(base_url)
        logger.info("Rocket.Chat: DDP connecting to %s", ws_url)

        session = self._rest.session
        if session is None:
            raise RuntimeError("Rocket.Chat: REST session not open yet, cannot start DDP")

        # heartbeat=None leads to silent inbound death (on a half-open socket the read
        # loop blocks forever and the reconnect never fires); see
        # _normalize_ws_heartbeat_seconds() in config.py. 0 -> None (explicit off), any
        # other value is aiohttp's protocol-level PING interval.
        ws = await session.ws_connect(
            ws_url, heartbeat=(self._cfg.ws_heartbeat_seconds or None)
        )
        self._ddp_ws = ws
        final_url = getattr(getattr(ws, "_response", None), "url", None)
        if not websocket_endpoint_matches(ws_url, final_url):
            await self._ddp_close_socket()
            raise RuntimeError(f"Rocket.Chat: DDP endpoint mismatch (got {final_url!r})")

        self._ddp_subs.clear()
        self._ddp_pending_calls.clear()
        self._ddp_notify_inflight = {}
        gap_start = self._ddp_last_message_at

        # Frame reader must be running BEFORE we await the login call's
        # result, otherwise nothing ever reads the response off the socket -
        # run listen() as the loop body and dispatch method-call responses
        # from inside it; the login call below races against that loop via
        # the pending-call future.
        listen_task = asyncio.ensure_future(self._ddp_listen(ws))
        try:
            await self._ddp_send(
                {"msg": "connect", "version": _DDP_PROTOCOL_VERSION,
                 "support": [_DDP_PROTOCOL_VERSION]}
            )
            login_result = await self._ddp_call_method(
                "login", [{"resume": self._token}]
            )
            if isinstance(login_result, dict) and login_result.get("error"):
                err = login_result["error"] or {}
                raise DdpLoginError(
                    f"Rocket.Chat: DDP login rejected: {err.get('reason', err)}",
                    error_code=err.get("error"),
                )
            logger.info("Rocket.Chat: DDP logged in (resume token)")

            # ONE subscription for every room, not one per room: Rocket.Chat rate-limits
            # DDP subscriptions per connection (10 / 10 s by default), and a per-room
            # design had most subscriptions rejected ("too-many-requests") once the bot
            # was in about 40 rooms and DMs. The price, public-channel traffic of
            # non-member rooms also arriving and being dropped in _ddp_dispatch_frame,
            # is small, and the web client pays it too.
            await self._ddp_sub("stream-room-messages", ["__my_messages__", False])
            logger.info("Rocket.Chat: DDP subscribed to stream-room-messages")
            await self._ddp_write_status("connected")
            self._ddp_ready and self._ddp_ready.set()
            await self._ddp_catch_up(gap_start)
            if self._ddp_last_message_at is None:
                # First session of the process: from now on everything is
                # either seen live or replayed - a later outage has a start.
                self._ddp_last_message_at = time.time()
                await self._ddp_save_cursor()

            await listen_task
        finally:
            if not listen_task.done():
                listen_task.cancel()
                try:
                    await listen_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _ddp_catch_up(self, gap_start: Optional[float]) -> None:
        """Replay what arrived while the connection was down.

        Reconnect (``gap_start`` set): messages posted during the outage were never pushed to
        this socket; without this, a mention sent then would never be answered. Each member
        room's history since the gap start is fed through the SAME handler as live frames; the
        inbound dedup skips anything already handled, so the overlap is harmless. Thread
        replies are included explicitly (``showThreadMessages=true``): the server leaves them
        out of ``channels.history``/``dm.history`` by default.

        Fresh process start (``gap_start`` None): see ``_ddp_startup_catch_up``.
        """
        if gap_start is None:
            await self._ddp_startup_catch_up()
            return
        since = catch_up_since(gap_start, time.time(), self._cfg.ddp_catchup_max_seconds)
        if since is None:
            return
        messages = await self._ddp_history_since(since)
        for message in messages:
            await self._ddp_handle_message(message)
        logger.info(
            "Rocket.Chat: DDP catch-up replayed %d message(s) since %s",
            len(messages), rc_timestamp(since),
        )

    async def _ddp_history_since(self, since: float) -> List[Dict[str, Any]]:
        """Every member room's messages since ``since``, thread replies included, ascending
                by server time (with a ``_room_type`` side field).
        """
        oldest = rc_timestamp(since)
        collected: List[Dict[str, Any]] = []
        for room_id, room_type in (await self._rest.subscribed_rooms()).items():
            messages = await self._rest.room_history(
                room_id, oldest=oldest, count=_CATCHUP_PAGE_SIZE, show_thread_messages=True
            )
            collected.extend({**message, "_room_type": room_type} for message in messages)
        collected.sort(key=lambda message: message_epoch_seconds(message) or 0.0)
        return collected

    async def _ddp_startup_catch_up(self) -> None:
        """On a fresh start: what arrived during the downtime.

        DDP replays nothing on a fresh connection, so what arrived during a restart would
        silently go unanswered. Reads back from the last seen message (durable cursor), at most
        ``startup_catchup_max_seconds``, and takes every message through the same decision chain
        as a live one (filtering, authorization, durable dedup: what was recorded once never runs
        again). Messages addressed to the bot are handled per ``startup_catchup_mode``
        (``notice``: a note; ``replay``: the Hermes Discord pattern, capped; see config.py).

        Without a cursor (first start on this state folder, or dedup persistence off) nothing
        is read back: with an empty dedup it would also answer messages an earlier instance
        already answered.
        """
        cfg = self._cfg
        mode = cfg.startup_catchup_mode
        if mode == "off" or cfg.startup_catchup_max_seconds <= 0:
            return
        if not self._ddp_cursor_path():
            logger.warning(
                "Rocket.Chat: startup catch-up skipped: no persistent state directory "
                "(ROCKETCHAT_DEDUP_STATE_DIR is off), messages sent during a restart are not checked"
            )
            return
        cursor = self._ddp_load_cursor()
        if cursor is None:
            logger.info(
                "Rocket.Chat: startup catch-up skipped: no cursor yet (first start on this state directory)"
            )
            return
        since = catch_up_since(cursor, time.time(), cfg.startup_catchup_max_seconds)
        if since is None:
            return
        catchup = StartupCatchup(mode, cfg.startup_catchup_max_dispatches)
        messages = await self._ddp_history_since(since)
        for message in messages:
            await self._ddp_handle_message(message, startup=catchup)
        site_url = getattr(self, "_site_url", "") or ""
        for (room_id, tmid), missed in catchup.notices.items():
            text = missed_notice_text(missed, site_url=site_url)
            try:
                await self._rest.post_message(room_id, text, tmid=tmid)
            except Exception:  # noqa: BLE001 - best effort, the other rooms' notices should still go out
                logger.warning(
                    "Rocket.Chat: could not send the missed-message notice to room %s", room_id,
                    exc_info=True,
                )
        logger.info(
            "Rocket.Chat: startup catch-up (mode=%s) since %s: %d message(s) read, "
            "%d dispatched, %d missed, %d notice(s)",
            mode, rc_timestamp(since), len(messages), catchup.dispatched, catchup.missed,
            len(catchup.notices),
        )

    # ------------------------------------------------------------------ Durable cursor
    # (the start of the startup check)
    # ------------------------------------------------------------------

    def _ddp_cursor_path(self) -> str:
        state_dir = self._cfg.dedup_state_dir
        return os.path.join(state_dir, _CURSOR_FILE_NAME) if state_dir else ""

    def _ddp_load_cursor(self) -> Optional[float]:
        path = self._ddp_cursor_path()
        data = _read_json_file(path) if path else None
        value = data.get("lastMessageAt") if isinstance(data, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        return None

    async def _ddp_save_cursor(self) -> None:
        path = self._ddp_cursor_path()
        if not path or self._ddp_last_message_at is None:
            return
        try:
            await _write_json_file_atomic(
                path, {"version": 1, "lastMessageAt": self._ddp_last_message_at}
            )
        except Exception:  # noqa: BLE001 - a missed write only narrows the next start's window
            logger.warning("Rocket.Chat: could not save the DDP cursor (%s)", path, exc_info=True)

    def _ddp_status_path(self) -> str:
        state_dir = self._cfg.dedup_state_dir
        return os.path.join(state_dir, _STATUS_FILE_NAME) if state_dir else ""

    async def _ddp_write_status(self, state: str) -> None:
        """Write the connection state for the readiness probe (see _STATUS_FILE_NAME)."""
        path = self._ddp_status_path()
        if not path:
            return
        try:
            await _write_json_file_atomic(
                path, {"version": 1, "state": state, "since": time.time()}
            )
        except Exception:  # noqa: BLE001 - a missing probe file must not stop the connection
            logger.warning("Rocket.Chat: could not write the DDP status (%s)", path, exc_info=True)

    def _ddp_schedule_cursor_save(self) -> None:
        """Throttled save after a live message (at most one write per 5 s)."""
        if not self._ddp_cursor_path() or getattr(self, "_ddp_cursor_handle", None) is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ddp_cursor_handle = loop.call_later(
            _CURSOR_SAVE_INTERVAL_SECONDS, self._ddp_fire_cursor_save
        )

    def _ddp_fire_cursor_save(self) -> None:
        self._ddp_cursor_handle = None
        task = asyncio.ensure_future(self._ddp_save_cursor())
        self._ddp_handler_tasks.add(task)
        task.add_done_callback(self._ddp_handler_tasks.discard)

    async def _ddp_flush_cursor(self) -> None:
        """On shutdown: an immediate write instead of the pending throttled save."""
        handle = getattr(self, "_ddp_cursor_handle", None)
        if handle is not None:
            handle.cancel()
            self._ddp_cursor_handle = None
        await self._ddp_save_cursor()

    async def _ddp_listen(self, ws: Any) -> None:
        async for raw_msg in ws:
            if self._ddp_closing:
                return
            msg_type = raw_msg.type
            if msg_type in (msg_type.TEXT, msg_type.BINARY):
                try:
                    frame = json.loads(raw_msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._ddp_dispatch_frame(frame)
            elif msg_type in (
                msg_type.ERROR, msg_type.CLOSE, msg_type.CLOSING, msg_type.CLOSED,
            ):
                logger.info("Rocket.Chat: DDP WebSocket closed (%s)", msg_type)
                return

    # ------------------------------------------------------------------
    # Frame dispatch
    # ------------------------------------------------------------------

    async def _ddp_dispatch_frame(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("msg")

        if kind == "ping":
            pong: Dict[str, Any] = {"msg": "pong"}
            if "id" in frame:
                pong["id"] = frame["id"]
            await self._ddp_send(pong)
            return

        if kind == "ready":
            for sub_id in frame.get("subs", []):
                self._ddp_subs[sub_id] = True
            return

        if kind == "nosub":
            sub_id = frame.get("id", "")
            err = frame.get("error") or {}
            self._ddp_subs.pop(sub_id, None)
            if err:
                logger.warning("Rocket.Chat: DDP subscription rejected: %s", err)
            return

        if kind == "result":
            call_id = frame.get("id", "")
            future = self._ddp_pending_calls.get(call_id)
            if future is not None and not future.done():
                future.set_result(frame.get("result") if "error" not in frame else frame)
            elif future is None and self._ddp_notify_inflight:
                method = self._ddp_notify_inflight.pop(call_id, None)
                if method:
                    self._ddp_note_method_result(method, frame)
            return

        if kind == "changed":
            fields = frame.get("fields") or {}
            args = fields.get("args") or []
            collection = frame.get("collection")
            if collection != "stream-room-messages" or not args:
                return
            meta = stream_meta(args)
            # The bot only works in rooms an admin put it in (and DMs):
            # acting on a non-member delivery would let anyone @mention it
            # into any public channel (and replying there auto-joins it).
            if meta.get("roomParticipant") is False:
                return
            seen_at = message_epoch_seconds(args[0])
            if seen_at is not None:
                self._ddp_last_message_at = max(self._ddp_last_message_at or 0.0, seen_at)
                self._ddp_schedule_cursor_save()
            # The room type rides along as ``_room_type`` (same side-channel
            # convention as ``_channel_name``) so classify_inbound can tell a
            # DM apart.
            #
            # Handled in its OWN task, never inline: the deterministic
            # commands (/whoami, the command-guard refusals) await REST calls,
            # and inline they serialised every burst behind the socket reader
            # (live load test, 40 simultaneous /whoami: p50 7.7 s) and kept
            # it from reading - including the heartbeat replies.
            task = asyncio.create_task(
                self._ddp_handle_message({**args[0], "_room_type": str(meta.get("roomType") or "")})
            )
            self._ddp_handler_tasks.add(task)
            task.add_done_callback(self._ddp_handler_tasks.discard)
            return

    async def _ddp_handle_message(
        self, post: Dict[str, Any], startup: Optional[StartupCatchup] = None
    ) -> None:
        """One inbound DDP message -> the SAME decision chain the webhook uses.

        Builds the synthetic webhook-shaped payload and runs it through the
        ONE shared ``classify_inbound`` call site
        (``WebhookInboundMixin._classify_inbound``, a group-DM lookup and then
        ``_classify_inbound_from_body``, defined in ``webhook_mixin.py``) and the ONE shared side-effect dispatcher
        (``WebhookInboundMixin._dispatch_inbound_decision``) - both mixins
        live on the same composed ``RocketChatAdapter`` class, so ``self.``
        resolves these via the normal MRO. This is deliberate: a SECOND,
        separately-maintained copy of the (long) ``classify_inbound`` kwarg
        list here would be exactly the kind of duplicated call site that has
        caused drift before in this codebase - there must be
        only one place that decides what ``classify_inbound`` is called
        with, regardless of which transport received the message.
        """
        try:
            decision = await self._classify_inbound(
                ddp_message_to_webhook_payload(post, webhook_secret=self._webhook_secret)
            )
            if decision is None:
                return
            if not decision.should_handle:
                self._log_ignored_delivery(decision)
                return
            # In a startup run the decision (and its dedup record) already exists; what
            # may not go to Hermes gets a notice and never runs again
            # (``StartupCatchup``).
            if startup is not None and not startup.take(decision, str(post.get("_room_type") or "")):
                return
            await self._dispatch_inbound_decision(decision)
        except Exception:
            logger.exception("Rocket.Chat: DDP message handling failed")
