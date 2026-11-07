"""Talking to Rocket.Chat's REST API v1.

Split into two layers on purpose, and the split is what makes it testable:

* **Pure functions** - URL and payload construction, header assembly, response
  interpretation. No network, no aiohttp, no session: asserted against plain
  dicts. These hold the parts that actually encode Rocket.Chat's quirks, so
  they are the parts worth pinning down.
* **``RocketChatRestClient``** - the thin transport wrapper that owns the
  aiohttp session and applies those functions. It has almost no branching left.

Behaviour worth keeping, because each piece was the fix for something observed live:

* ``force_close=True`` on the connector. The session lives for the whole
  gateway process but sends only occasionally, so a pooled keep-alive
  connection goes stale (Rocket.Chat closes it server-side) and the next send
  fails with ``ServerDisconnectedError``. A fresh connection per request costs
  one handshake at this message volume.
* A network failure on POST raises :class:`RocketChatNetworkError` instead of
  being swallowed, so the caller can mark the result retryable. Without that
  distinction core cannot tell a stale-connection hiccup from a real
  formatting error, and mislabels the retry as
  "(Response formatting failed, plain text:)".
* A GET failure returns ``{}`` rather than raising: every GET here is a lookup
  whose caller has a sensible fallback (room name, identity check), so failing
  soft keeps a transient blip from taking the bot down.

Rocket.Chat 8.x/9.0 durability: every endpoint used here remains in 9.0 (see
docs/compatibility.md). Two things prepare for future removals: room metadata starts with the
room-type-independent ``rooms.info`` (the old ``channels.info``/``groups.info`` pair is only a
fallback), and the server's deprecation headers (``x-deprecation-*``) are logged once per endpoint
at WARNING level, so a next major version's removal shows in the log before it breaks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/"
DEFAULT_TIMEOUT_SECONDS = 30

# post()'s retry/backoff policy mirrors Hermes core's _send_with_retry
# (gateway/platforms/base.py: max_retries=2, base_delay=2.0); see post()'s docstring for
# why the retry lives here.
_POST_MAX_RETRIES = 2
_POST_BASE_DELAY_SECONDS = 2.0
# Mirrors the core's _SEND_RETRY_INLINE_WAIT_CAP_SECS - never hold this
# coroutine open for a long server-requested penalty.
_POST_RETRY_AFTER_CAP_SECONDS = 60.0


class RocketChatNetworkError(Exception):
    """Transient network failure talking to the Rocket.Chat REST API.

    Raised instead of swallowing the exception so a caller can mark its result as retryable; see the
    module docstring for why that distinction matters.

    ``retry_after``: the server-requested delay in seconds, when known (a 429's ``Retry-After`` header).
    Mirrors core's ``SendResult.retry_after``, so a caller can cooperate with server-side rate-limit
    hints, even though ``post()`` already retries internally before raising this.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header value (seconds) into a float, or ``None``.

    Rocket.Chat's rate limiter sends this as a plain integer-seconds string
    (not an HTTP-date) — this only handles that numeric form, matching what
    is actually observed; a non-numeric value degrades to ``None`` (the
    caller then falls back to its own exponential backoff) rather than
    raising, since a malformed header must never break the retry path.
    """
    if not header_value:
        return None
    try:
        return float(header_value)
    except (TypeError, ValueError):
        return None


def build_url(base_url: str, path: str) -> str:
    """Absolute URL for a REST endpoint, tolerating slashes on either side."""
    return f"{base_url.rstrip('/')}{API_PREFIX}{path.lstrip('/')}"


def auth_headers(token: str, user_id: str) -> Dict[str, str]:
    """The bot Personal Access Token header pair Rocket.Chat expects."""
    return {
        "X-Auth-Token": token,
        "X-User-Id": user_id,
        "Content-Type": "application/json",
    }


def post_message_payload(room_id: str, text: str = "", tmid: str = "") -> Dict[str, Any]:
    """Body for ``chat.postMessage``.

    ``text`` is omitted entirely when empty rather than sent as ``""``: Rocket.Chat rejects an empty text
    field. ``tmid`` puts the message into that thread; the server accepts it only together with ``roomId``
    (``ChatPostMessageSchema``), so it is sent only when given: a main-channel message has no ``tmid`` key
    at all.
    """
    payload: Dict[str, Any] = {"roomId": room_id}
    if text:
        payload["text"] = text
    if tmid:
        payload["tmid"] = tmid
    return payload


def update_message_payload(room_id: str, msg_id: str, text: str) -> Dict[str, Any]:
    """Body for ``chat.update``.

    Substitutes a single space for empty text because Rocket.Chat rejects an
    empty ``text`` outright — which happens legitimately while streaming, before
    the first token has arrived.

    ``chat.update`` is text-only on Rocket.Chat 8.x (confirmed live).
    """
    return {"roomId": room_id, "msgId": msg_id, "text": text or " "}


def delete_message_payload(room_id: str, msg_id: str) -> Dict[str, Any]:
    return {"roomId": room_id, "msgId": msg_id}


def posted_message_id(response: Optional[Dict[str, Any]]) -> str:
    """The new message's id from a ``chat.postMessage`` response, or ``""``."""
    if not response:
        return ""
    return str((response.get("message") or {}).get("_id") or "")


def is_successful(response: Optional[Dict[str, Any]]) -> bool:
    """Whether a REST call reported success.

    A falsy response means the transport layer already gave up (see the module
    docstring), so it counts as failure without inspecting anything.
    """
    return bool(response and response.get("success"))


def is_rejected(response: Optional[Dict[str, Any]]) -> bool:
    """Whether Rocket.Chat itself ANSWERED that the call failed.

    True only for an explicit ``success: false`` - a 200 body saying so, or
    the 4xx answer ``post()`` passes on (see there). The empty ``{}`` of a
    5xx, a 3xx or a transport failure is NOT a rejection: there the request
    may already have been processed. The outbound dedup relies on this
    distinction (``adapter.py`` ``send()``).
    """
    return bool(response) and response.get("success") is False


def history_query_params(
    room_id: str,
    *,
    oldest: str = "",
    latest: str = "",
    count: int = 0,
    show_thread_messages: Optional[bool] = None,
) -> Dict[str, str]:
    """Query params for ``channels.history``/``groups.history``/``dm.history``.

    ``latest`` (exclusive) is the context window's time limit: only messages BEFORE the trigger count.
    ``show_thread_messages`` is always sent explicitly by the callers: the server default differs per
    endpoint (``channels.history``/``dm.history`` leave thread replies out unless ``"true"``,
    ``groups.history`` keeps them unless ``"false"``), so a private group's window would otherwise contain
    thread replies while a public channel's would not. A thread reply also sent to the channel (``tshow``)
    is kept by the server either way. ``None`` leaves it out.
    """
    params: Dict[str, str] = {"roomId": room_id}
    if oldest:
        params["oldest"] = oldest
    if latest:
        params["latest"] = latest
    if count:
        params["count"] = str(count)
    if show_thread_messages is not None:
        params["showThreadMessages"] = "true" if show_thread_messages else "false"
    return params


# chat.getThreadMessages: newest first, so ``count`` returns the LAST replies
# (the endpoint's own default order is ascending by ``ts``).
THREAD_MESSAGES_SORT = '{"ts":-1}'


def thread_messages_query_params(tmid: str, *, count: int = 0) -> Dict[str, str]:
    """Query params for ``chat.getThreadMessages`` (the replies, root excluded)."""
    params: Dict[str, str] = {"tmid": tmid, "sort": THREAD_MESSAGES_SORT}
    if count:
        params["count"] = str(count)
    return params


def rooms_from_subscriptions(response: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """``{room_id: room_type}`` from a ``subscriptions.get`` response.

    Covers every room the account is a member of - public channels,
    private groups AND direct messages (``t`` = ``c``/``p``/``d``/...).
    Empty on a failed/unsuccessful response.
    """
    if not is_successful(response):
        return {}
    rooms: Dict[str, str] = {}
    for sub in (response or {}).get("update") or []:
        rid = str((sub or {}).get("rid") or "")
        if rid:
            rooms[rid] = str(sub.get("t") or "")
    return rooms


def room_from_info(response: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The room object out of ``rooms.info`` / ``channels.info`` / ``groups.info``.

    ``rooms.info`` (``room``) answers for every room type in one call and is the primary lookup. The
    legacy shapes are still accepted for the fallback: a room is reachable there through a different
    endpoint depending on whether it is a public channel (``channel``) or a private group (``group``), and
    the caller usually does not know which. A private group returns HTTP 400 from ``channels.info``, which
    looks like a broken room id rather than the wrong endpoint.
    """
    if not is_successful(response):
        return None
    return response.get("room") or response.get("channel") or response.get("group") or None


# Rocket.Chat's deprecation headers (on a REST response when the endpoint or parameter
# is deprecated); see deprecation_notice.
_DEPRECATION_HEADERS = (
    "x-deprecation-type",
    "x-deprecation-message",
    "x-deprecation-version",
)


def deprecation_notice(headers: Any) -> Optional[str]:
    """One log line from Rocket.Chat's ``x-deprecation-*`` response headers, or ``None``.

    Rocket.Chat marks a deprecated endpoint or parameter with these headers (type, message, the version
    that removes it). Case-insensitive lookup, because aiohttp gives a ``CIMultiDict`` and the tests a
    plain dict.
    """
    if not headers:
        return None
    try:
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    except AttributeError:
        return None
    parts = [
        f"{name[len('x-deprecation-'):]}={lowered[name]}"
        for name in _DEPRECATION_HEADERS
        if lowered.get(name)
    ]
    return ", ".join(parts) or None


def user_from_info(response: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The user object out of ``users.info``, or ``None`` on failure/unknown user."""
    if not is_successful(response):
        return None
    return response.get("user") or None


RATE_LIMIT_BYPASS_PERMISSION = "api-bypass-rate-limit"


def permission_roles(response: Optional[Dict[str, Any]], permission: str) -> Optional[List[str]]:
    """The roles holding a permission from the ``permissions.listAll`` answer, or ``None`` if the answer
        is unreadable (any logged-in account may request it).
    """
    if not isinstance(response, dict):
        return None
    for entry in response.get("update") or []:
        if isinstance(entry, dict) and entry.get("_id") == permission:
            roles = entry.get("roles")
            return [str(r) for r in roles] if isinstance(roles, list) else []
    return None


def rate_limit_bypass_status(user_roles: Any, bypass_roles: Optional[List[str]]) -> str:
    """Whether the account is exempt from Rocket.Chat's REST rate limit.

    ``ok``: one of its roles holds ``api-bypass-rate-limit`` (by default: admin, bot, app); ``missing``: none
    does; ``unknown``: the account's roles are unreadable. If the permission's roles are unreadable
    (``bypass_roles`` ``None``), the ``bot`` role is the measure. Without the exemption, replies and streamed
    edits run into 429 (by default 10 calls per minute per endpoint).
    """
    if not isinstance(user_roles, list):
        return "unknown"
    roles = {str(r) for r in user_roles}
    allowed = set(bypass_roles) if bypass_roles is not None else {"bot"}
    return "ok" if roles & allowed else "missing"


def upload_error(body: str) -> str:
    """The short code of Rocket.Chat's upload error from the response body (``errorType``, otherwise
        ``error``), or ``""``.
    """
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("errorType") or parsed.get("error") or "")[:120]


class RocketChatRestClient:
    """Thin aiohttp wrapper around the endpoints this integration uses."""

    def __init__(
        self,
        base_url: str,
        token: str,
        user_id: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._user_id = user_id
        self._timeout_seconds = timeout_seconds
        self._session: Any = None  # aiohttp.ClientSession
        # Endpoints whose deprecation warning has already gone out (once per process,
        # see _note_deprecation).
        self._deprecation_logged: set = set()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Create the long-lived session. See the module docstring on force_close."""
        import aiohttp

        connector = aiohttp.TCPConnector(force_close=True)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            connector=connector,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def is_open(self) -> bool:
        return self._session is not None and not self._session.closed

    @property
    def session(self) -> Any:
        """The underlying ``aiohttp.ClientSession``.

        Exposed so ``DdpTransportMixin`` can open its WebSocket on the SAME
        session as the REST calls (one connection pool, one PAT), instead of
        a second, independent ``aiohttp.ClientSession``.
        NOTE: this session's connector is built with ``force_close=True``
        (see ``open()`` above), which governs HTTP keep-alive reuse between
        REST calls - it should not affect an already-upgraded WebSocket
        stream, but this has not been live-verified yet for this specific
        combination.
        """
        return self._session

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _note_deprecation(self, method: str, path: str, headers: Any) -> None:
        """Log Rocket.Chat's deprecation headers once per endpoint.

        WARNING rather than INFO: the Hermes gateway logs at WARNING by default, and this is exactly the signal
        that should be visible before a server upgrade. Once per endpoint, so it does not flood the log.
        """
        notice = deprecation_notice(headers)
        key = f"{method} {path}"
        if notice is None or key in self._deprecation_logged:
            return
        self._deprecation_logged.add(key)
        logger.warning(
            "Rocket.Chat: the server marks %s as deprecated (%s) - "
            "check the plugin against the next Rocket.Chat release",
            key, notice,
        )

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
        *,
        expected_miss: bool = False,
    ) -> Dict[str, Any]:
        """GET a REST endpoint; returns ``{}`` on any failure (fails soft).

        ``expected_miss``: an intermediate step of an endpoint chain (``channels.history`` for a private room or
        DM), where a 400 is expected and the next endpoint answers. Then the 400 is DEBUG, not ERROR, so the
        container output and central log get no false alarm. Other statuses and network errors stay ERROR.
        """
        import aiohttp

        try:
            async with self._session.get(
                build_url(self._base_url, path),
                headers=auth_headers(self._token, self._user_id),
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                allow_redirects=False,
            ) as resp:
                self._note_deprecation("GET", path, resp.headers)
                # >= 300, not just >= 400: redirect following is deliberately off (the
                # PAT must never reach a redirect's target), so a 3xx also takes the
                # error branch with a clean log message instead of running into
                # resp.json().
                if resp.status >= 300:
                    body = await resp.text()
                    level = logging.DEBUG if expected_miss and resp.status == 400 else logging.ERROR
                    logger.log(level, "RC API GET %s -> %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        # The full timeout (ClientTimeout total) expiring is a plain
        # asyncio.TimeoutError, not aiohttp.ClientError; the "fails soft" promise covers
        # it too.
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("RC API GET %s network error: %s", path, exc)
            return {}

    async def get_bytes(self, server_path: str, max_bytes: int = 0) -> Optional[bytes]:
        """A file's bytes from the server's own path (``/file-upload/<id>/<name>``) with the bot's PAT; ``None``
        on error or if the file is larger than ``max_bytes`` (``<= 0``: no limit). Follows no redirects, like
        :meth:`get` (the PAT must never reach a redirect's target).
        """
        import aiohttp

        url = f"{self._base_url.rstrip('/')}/{server_path.lstrip('/')}"
        try:
            async with self._session.get(
                url,
                headers=auth_headers(self._token, self._user_id),
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds * 4),
                allow_redirects=False,
            ) as resp:
                if resp.status >= 300:
                    logger.warning("RC file GET %s -> %s", server_path.split("/")[2:3], resp.status)
                    return None
                chunks: List[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if max_bytes > 0 and total > max_bytes:
                        logger.warning(
                            "RC file GET %s stopped: over the %d-byte limit",
                            server_path.split("/")[2:3], max_bytes,
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("RC file GET network error: %s", exc)
            return None

    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST a REST endpoint, with internal retry/backoff on transient failure (mirrors core's
        ``_send_with_retry`` policy: ``_POST_MAX_RETRIES`` attempts, exponential backoff from
        ``_POST_BASE_DELAY_SECONDS`` with jitter, honoring a server ``Retry-After`` on a 429).

        Implemented HERE, transparently to every caller (``post_message``/``update_message``/``delete_message``/
        ``react`` all funnel through this method): in Hermes core ``_send_with_retry`` wraps ``adapter.send()`` only
        for inline command replies and a busy notice, NOT for the primary agent-response delivery, which calls
        ``adapter.send()`` directly with no retry on every platform. Retrying at THIS layer gives every Rocket.Chat
        REST call the backoff discipline regardless of which core path invoked it.

        Raises :class:`RocketChatNetworkError` when retries are exhausted (a transient network error, or a 429 whose
        ``Retry-After``/backoff would exceed the inline cap), so the caller can mark its result retryable and carry
        the delay. A NON-429 HTTP error still returns ``{}`` immediately, unretried (the server gave a permanent
        rejection; retrying the same request will not help).
        """
        import aiohttp

        attempt = 0
        while True:
            try:
                async with self._session.post(
                    build_url(self._base_url, path),
                    headers=auth_headers(self._token, self._user_id),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                    allow_redirects=False,
                ) as resp:
                    self._note_deprecation("POST", path, resp.headers)
                    if resp.status == 429:
                        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                        body = await resp.text()
                        logger.warning(
                            "RC API POST %s -> 429 (rate limited, retry_after=%s): %s",
                            path, retry_after, body[:200],
                        )
                        delay = retry_after if retry_after is not None else (
                            _POST_BASE_DELAY_SECONDS * (2 ** attempt)
                        )
                        if attempt >= _POST_MAX_RETRIES or delay > _POST_RETRY_AFTER_CAP_SECONDS:
                            raise RocketChatNetworkError(
                                f"rate limit exceeded (429) after {attempt} retries",
                                retry_after=retry_after,
                            )
                        await asyncio.sleep(delay + random.uniform(0, 1))
                        attempt += 1
                        continue
                    if resp.status >= 300:
                        body = await resp.text()
                        logger.error("RC API POST %s -> %s: %s", path, resp.status, body[:200])
                        # A 4xx (except 408) means the server refused the request
                        # WITHOUT processing it: the caller (`is_rejected`)
                        # distinguishes this from an ambiguous 5xx/3xx. `is_successful`
                        # is False for both; nothing changes for other callers.
                        if 400 <= resp.status < 500 and resp.status != 408:
                            return {"success": False, "status": resp.status}
                        return {}
                    return await resp.json()
            except aiohttp.ClientError as exc:
                if attempt >= _POST_MAX_RETRIES:
                    logger.error(
                        "RC API POST %s network error after %d retries: %s", path, attempt, exc
                    )
                    raise RocketChatNetworkError(str(exc)) from exc
                delay = _POST_BASE_DELAY_SECONDS * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "RC API POST %s network error (attempt %d/%d, retrying in %.1fs): %s",
                    path, attempt + 1, _POST_MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
                attempt += 1

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def me(self) -> Dict[str, Any]:
        """The authenticated bot user; empty/absent ``_id`` means auth failed."""
        return await self.get("me")

    async def permission_roles(self, permission: str) -> Optional[List[str]]:
        """The roles holding a permission (``permissions.listAll``), ``None`` on error."""
        return permission_roles(await self.get("permissions.listAll"), permission)

    async def site_url(self) -> str:
        """The server's own public address (``Site_Url``): Rocket.Chat renders a quote from a message link on
                this address. Readable by a bot account (``settings.public``). ``""`` on error.
        """
        data = await self.get("settings.public", {"_id": "Site_Url"})
        for item in (data or {}).get("settings") or []:
            if item.get("_id") == "Site_Url" and isinstance(item.get("value"), str):
                return item["value"].rstrip("/")
        return ""

    async def post_message(self, room_id: str, text: str = "", tmid: str = "") -> Dict[str, Any]:
        return await self.post("chat.postMessage", post_message_payload(room_id, text, tmid))

    async def upload_file(
        self,
        room_id: str,
        data: Any,
        file_name: str,
        mime_type: str,
        *,
        msg: str = "",
        tmid: str = "",
    ) -> Dict[str, Any]:
        """Upload a file into a message (outbound files).

        Rocket.Chat's documented two-step path: ``POST rooms.media/<rid>`` (multipart, temporary upload) -> ``POST
        rooms.mediaConfirm/<rid>/<fileId>`` (creates the message, ``msg`` and ``tmid`` in a thread). The old one-step
        ``rooms.upload`` already answers 404 on 8.8, so there is no fallback.

        ``data`` is bytes or a binary file open for reading: aiohttp sends the latter in chunks, so the file does not
        go into memory whole.

        Returns the ``mediaConfirm`` answer (with ``message``); on rejection
        ``{"success": False, "status": <code>, "error": <the server's error>}`` (``error-file-too-large``,
        ``error-invalid-file-type``); on other errors ``{}``. On a network error :class:`RocketChatNetworkError`. The
        upload is not repeated (after a half-successful request a second attempt could upload twice); the
        confirmation uses :meth:`post`'s usual retry.
        """
        import aiohttp

        headers = {k: v for k, v in auth_headers(self._token, self._user_id).items()
                   if k != "Content-Type"}  # aiohttp writes the multipart boundary
        form = aiohttp.FormData()
        form.add_field("file", data, filename=file_name, content_type=mime_type)
        path = f"rooms.media/{room_id}"
        try:
            async with self._session.post(
                build_url(self._base_url, path),
                headers=headers,
                data=form,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds * 4),
                allow_redirects=False,
            ) as resp:
                self._note_deprecation("POST", "rooms.media", resp.headers)
                if resp.status >= 300:
                    body = await resp.text()
                    logger.error("RC API POST rooms.media -> %s: %s", resp.status, body[:200])
                    if 400 <= resp.status < 500 and resp.status != 408:
                        return {"success": False, "status": resp.status,
                                "error": upload_error(body)}
                    return {}
                uploaded = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("RC API POST rooms.media network error: %s", exc)
            raise RocketChatNetworkError(str(exc)) from exc
        file_id = str(((uploaded or {}).get("file") or {}).get("_id") or "")
        if not file_id:
            logger.error("RC API POST rooms.media: no file id in the answer")
            return {}
        payload: Dict[str, Any] = {"msg": msg}
        if tmid:
            payload["tmid"] = tmid
        return await self.post(f"rooms.mediaConfirm/{room_id}/{file_id}", payload)

    async def update_message(self, room_id: str, msg_id: str, text: str) -> Dict[str, Any]:
        return await self.post("chat.update", update_message_payload(room_id, msg_id, text))

    async def delete_message(self, room_id: str, msg_id: str) -> Dict[str, Any]:
        return await self.post("chat.delete", delete_message_payload(room_id, msg_id))

    async def react(
        self, msg_id: str, emoji: str, *, should_react: bool = True
    ) -> Dict[str, Any]:
        """Add (or remove, ``should_react=False``) an emoji reaction on a message.

        Used for the instant acknowledgement reaction: the moment an @mention is accepted for an agent turn the bot
        reacts on the user's message, well before the LLM answer. ``adapter.send()`` clears it
        (``should_react=False``) when the reply posts. Always fire-and-forget from the caller: a failed reaction must
        never affect the turn. ``emoji`` is a shortname with or without surrounding colons; Rocket.Chat normalises it.
        """
        name = emoji.strip().strip(":")
        if not name or not msg_id:
            return {}
        return await self.post(
            "chat.react", {"messageId": msg_id, "reaction": f":{name}:", "shouldReact": should_react}
        )

    async def room_info(self, room_id: str) -> Optional[Dict[str, Any]]:
        """Room metadata: ``rooms.info`` first, then the legacy endpoint pair.

        ``rooms.info`` covers every room type in one call. The ``channels.info`` -> ``groups.info`` fallback is kept for
        a server or room where ``rooms.info`` does not answer; see ``room_from_info``. Returns ``None`` when nothing
        knows the room.

        Two callers: the room name (``adapter.get_chat_info``) and recognising a group DM
        (``webhook_mixin._is_group_direct``, the ``usersCount`` field, see dm_session.py). Only ``rooms.info`` answers
        for a DM; the two fallback endpoints return an error there.
        """
        chain = ("rooms.info", "channels.info", "groups.info")
        for endpoint in chain:
            room = room_from_info(
                await self.get(endpoint, {"roomId": room_id}, expected_miss=endpoint != chain[-1])
            )
            if room is not None:
                return room
        return None

    async def users_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        """User metadata (incl. ``emails[]``/``services``) for ``/whoami``, or ``None``.

        Single endpoint, unlike ``room_info`` — there is no channel/group-style
        ambiguity for a user lookup.
        """
        return user_from_info(await self.get("users.info", {"userId": user_id}))

    async def room_history(
        self,
        room_id: str,
        *,
        oldest: str = "",
        latest: str = "",
        count: int = 0,
        show_thread_messages: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Recent messages in a room, OLDEST-TO-NEWEST: used by the room context (``webhook_mixin``) and the DDP
        reconnect catch-up.

        Tries ``channels.history`` -> ``groups.history`` -> ``dm.history``, because a private group 400s on
        ``channels.history`` (same reason as the legacy pair in ``room_from_info``). This chain remains in 9.0; the
        room-type-independent ``rooms.history`` exists only from 8.9, so it is deliberately unused (see
        docs/compatibility.md). Rocket.Chat returns newest-first, which this reverses, since every caller wants a
        chronological transcript. Fails soft (``[]``) via ``get()``: a history fetch is a context nicety, not worth
        failing a mention over. One page only: the context window is at most 100 messages, the server's default
        ``API_Upper_Count_Limit``.
        """
        params = history_query_params(
            room_id,
            oldest=oldest,
            latest=latest,
            count=count,
            show_thread_messages=show_thread_messages,
        )
        chain = ("channels.history", "groups.history", "dm.history")
        for endpoint in chain:
            data = await self.get(endpoint, params, expected_miss=endpoint != chain[-1])
            if is_successful(data):
                return list(reversed(data.get("messages") or []))
        return []

    async def subscribed_rooms(self) -> Dict[str, str]:
        """``{room_id: room_type}`` for every room the bot is a member of,
        DMs included - the DDP transport subscribes to exactly these."""
        return rooms_from_subscriptions(await self.get("subscriptions.get", {}))

    async def get_message(self, msg_id: str) -> Optional[Dict[str, Any]]:
        """One message by id (``chat.getMessage``), or ``None``: the thread root, which
                ``chat.getThreadMessages`` does not return.
        """
        if not msg_id:
            return None
        data = await self.get("chat.getMessage", {"msgId": msg_id})
        if not is_successful(data):
            return None
        return data.get("message") or None

    async def thread_messages(self, tmid: str, *, count: int = 0) -> List[Dict[str, Any]]:
        """The last ``count`` replies of a thread, OLDEST-TO-NEWEST, root excluded.

        ``chat.getThreadMessages`` with ``sort={"ts":-1}`` (see
        ``THREAD_MESSAGES_SORT``), reversed here like ``room_history``. Fails
        soft (``[]``), e.g. when threads are disabled on the server
        (``Threads_enabled``) or the route is rate-limited (20 requests / 10 s
        per client IP and route by default; the GET path does not retry), so
        the turn then simply runs without a context block.
        """
        if not tmid:
            return []
        data = await self.get("chat.getThreadMessages", thread_messages_query_params(tmid, count=count))
        if not is_successful(data):
            return []
        return list(reversed(data.get("messages") or []))
