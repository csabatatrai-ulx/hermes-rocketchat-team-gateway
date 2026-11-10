"""Rocket.Chat gateway adapter.

Connects a Rocket.Chat bot account to Hermes with a Personal Access Token
(the X-Auth-Token / X-User-Id header pair). Inbound messages arrive over the
Realtime API (DDP WebSocket, ``ddp_transport_mixin.py``, the default) or,
with ``ROCKETCHAT_TRANSPORT=webhook``, over a Rocket.Chat Outgoing Webhook
pointed at this adapter's own small HTTP listener (``webhook_mixin.py``).
Both feed the same ``inbound.classify_inbound`` decision chain. Replies always go
over REST (``chat.postMessage``/``chat.update``): the DDP ``sendMessage``/
``updateMessage`` methods are gone in Rocket.Chat 9.0.

Configuration: every setting is read and coerced in ``config.py``
(``load_config``, precedence ``ROCKETCHAT_*`` env -> ``extra`` -> default, Hermes's rule);
``docs/configuration.md`` lists every key with its default. The four
required ones are ``ROCKETCHAT_URL``, ``ROCKETCHAT_TOKEN``,
``ROCKETCHAT_USER_ID`` and ``ROCKETCHAT_BOT_USERNAME``. On/off settings
accept true/1/yes/on and false/0/no/off; anything else keeps the setting's
default, with a warning (``config.as_bool``).

Replies to a thread mention go into that thread: the turn's
``source.thread_id`` is the real Rocket.Chat ``tmid`` (sessions belong to Hermes
core), and Hermes core passes it back as ``metadata["thread_id"]``.

Before ever setting ``ROCKETCHAT_ALWAYS_FRESH_FINAL_STREAMING=true``, read
``prefers_fresh_final_streaming``'s docstring: it re-triggers a duplicate
final delivery that the outbound dedup guard does not catch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple


from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import env_is_connected, get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from .config import (
    DEFAULT_WEBHOOK_HOST,
    DEFAULT_WEBHOOK_PORT,
    MAX_MESSAGE_LENGTH,
    RocketChatConfig,
    load_config,
    shared_memory_risk,
)
from .dedup import (
    delivery_fingerprint_source,
    InboundDeduplicator,
    OutboundDeduplicator,
)
from .approval_buttons import approval_attachments, confirm_attachments, confirm_body
from .scoped_env import ScopedEnv as _ScopedEnv
from .setup_wizard import interactive_setup
from .standalone import standalone_send
from .user_texts import user_text
from .prompt_guidance import build_platform_hint
from .whoami import build_whoami_reply, is_whoami_command
from .rest_client import (
    RATE_LIMIT_BYPASS_PERMISSION,
    RocketChatNetworkError,
    RocketChatRestClient,
    is_rejected,
    is_successful,
    posted_message_id,
    rate_limit_bypass_status,
)
from .history import is_progress_bubble
from .rc_markdown import escape_lone_tildes
from .reasoning_format import wrap_leaked_reasoning
logger = logging.getLogger(__name__)


# send_typing()'s DDP refresh-loop interval - a Rocket.Chat client-side
# typing-bubble expiry was NOT confirmed against primary Rocket.Chat
# documentation (the dedicated developer-docs page has 404'd, see
# send_typing()'s docstring); chosen conservatively short, in the same
# spirit as the Hermes core's Discord (12s) and Telegram (4s) refresh
# loops, which exist precisely because those platforms' typing indicators
# also expire client-side and must be kept alive for a multi-second turn.
_TYPING_REFRESH_SECONDS = 4.0
# The DDP method that carries the typing indicator (no REST equivalent).
_TYPING_DDP_METHOD = "stream-notify-room"



from .reply_reference import ReplyTarget, reply_prefix
from .shared_state import (
    pop_approval_card,
    remember_approval_card,
    remember_confirm_card,
    _shared_direct_room_kinds,
    _shared_reply_targets,
    _shared_sent_prefixes,
    _shared_site_url,
    _shared_inbound_dedup,
    _shared_inflight_augment_rooms,
    _shared_outbound_dedup,
    _shared_send_locks,
)
from .webhook_mixin import WebhookInboundMixin
from .ddp_transport_mixin import DdpTransportMixin


def check_rocketchat_requirements() -> bool:
    """Return True if the Rocket.Chat adapter runtime dependency is available."""
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        logger.warning("Rocket.Chat: aiohttp not installed")
        return False


def _config_from_platform(config: PlatformConfig) -> RocketChatConfig:
    """Read this platform's settings via the config module (see config.py)."""
    return load_config(
        getattr(config, "extra", {}) or {},
        token=(getattr(config, "token", None) or ""),
        env=_ScopedEnv(),
    )


def validate_rocketchat_config(config: PlatformConfig) -> bool:
    """Return True when Rocket.Chat has enough config to connect."""
    missing = _config_from_platform(config).missing_required()
    if missing:
        # A missing webhook secret is not "not configured yet" but an unsafe
        # configuration (transport=webhook without a secret): report it loudly, do not
        # swallow it at debug level.
        log = logger.warning if "webhook_secret" in missing else logger.debug
        log("Rocket.Chat: not configured, missing: %s", ", ".join(missing))
        return False
    return True




# Hermes core's endpoint failure message: "API call failed after N retries: <summary>".
# A summary indicating 401/403 or an authentication error is "auth", otherwise
# "unavailable"; any other text: None.
_API_FAILURE_RE = re.compile(r"^\s*API call failed after \d+ retries\b", re.IGNORECASE)
_AUTH_FAILURE_RE = re.compile(
    r"\b40[13]\b|PermissionDenied|Authentication|unauthori[sz]ed|forbidden|auth issue", re.IGNORECASE
)

# The texts are the ``model_endpoint_auth``/``_unavailable`` keys of user_texts.py.
MODEL_ENDPOINT_FAILURE_KEYS = {"auth": "model_endpoint_auth", "unavailable": "model_endpoint_unavailable"}


def classify_model_endpoint_failure(text: str) -> Optional[str]:
    """Recognise core's endpoint failure message: "auth", "unavailable" or None."""
    if not text or not _API_FAILURE_RE.match(text):
        return None
    return "auth" if _AUTH_FAILURE_RE.search(text) else "unavailable"


class RocketChatAdapter(
    WebhookInboundMixin,
    DdpTransportMixin,
    BasePlatformAdapter,
):
    """Gateway adapter for a self-hosted Rocket.Chat instance."""

    splits_long_messages = True  # send() chunks via truncate_message(MAX_MESSAGE_LENGTH)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("rocketchat"))

        # Every knob is read and coerced in config.py; this adapter keeps only
        # the resulting object plus a few flat aliases for the values used on
        # the hot path.
        cfg = _config_from_platform(config)
        self._cfg: RocketChatConfig = cfg
        self._warn_if_memory_is_shared(cfg)
        if cfg.allow_context_references:
            logger.warning(
                "Rocket.Chat: ROCKETCHAT_ALLOW_CONTEXT_REFERENCES is on, so @file:, @folder:, @git: and @url: in a "
                "message are expanded by Hermes before the model runs: anyone who can reach this bot can attach "
                "files from its workspace or make it fetch a URL. Turn it off for a shared bot."
            )


        self._base_url: str = cfg.base_url
        self._token: str = cfg.token
        self._user_id: str = cfg.user_id
        self._bot_username: str = cfg.bot_username
        self._home_channel: str = cfg.home_channel
        self._webhook_secret: str = cfg.webhook_secret
        self._webhook_host: str = cfg.webhook_host
        self._webhook_port: int = cfg.webhook_port
        self._require_mention: bool = cfg.require_mention

        # All REST traffic goes through rest_client.py; this adapter no longer
        # knows what a header or an endpoint path looks like.
        self._rest = RocketChatRestClient(cfg.base_url, cfg.token, cfg.user_id)
        self._runner: Any = None   # aiohttp.web.AppRunner

        # Both directions of duplicate suppression live in dedup.py, including the
        # reasoning for why each is needed.
        #
        # PROCESS-WIDE, not per adapter instance: a per-instance guard suppressed
        # nothing on the first turn after a gateway restart, the signature of a second,
        # short-lived adapter instance handling one of the two sends. Sharing the caches
        # across instances removes that whole class of miss.
        self._inbound_dedup = _shared_inbound_dedup(
            cfg.dedup_state_dir, cfg.inbound_dedup_retention_hours
        )
        self._outbound_dedup = _shared_outbound_dedup(
            cfg.outbound_dedup_ttl_seconds, cfg.dedup_state_dir
        )
        # Per-(bot, room) mutex around the outbound duplicate guard's check + send +
        # remember. Between the check and remember() the send awaits, so TWO concurrent
        # send() calls (Hermes core's repeated-delivery defect, see dedup.py) both found
        # "not seen yet" and BOTH posted. The dedup's two-step API is not atomic by
        # itself. PROCESS-WIDE for the same reason as the caches above.
        self._send_locks: Dict[str, asyncio.Lock] = _shared_send_locks()
        # session_key -> (room_id, tmid) for turns `_augment_and_handle`
        # (webhook_mixin.py) is still preparing, before Hermes core takes
        # them over - `disconnect()` below reads this to give an honest
        # "gateway restarted" notice for exactly that window. PROCESS-WIDE,
        # same reasoning as the two locks above - ld.
        # `_shared_inflight_augment_rooms` fejjegyzete.
        self._inflight_augment_rooms: Dict[str, Tuple[str, str]] = _shared_inflight_augment_rooms()
        # chat_id -> the @mention message_id currently carrying the instant "seen you"
        # ack reaction. `_resolve_ack` (called from `send()`) pops it and removes the
        # reaction once the reply is ready. Per-instance is fine: a stale entry at worst
        # leaves one 👀 on an old message, and the turn that set it runs in this same
        # process.
        #
        # Deliberately one static reaction: an animated sequence was tried, but every
        # reaction change reorders Rocket.Chat's message list, which looks like a jump
        # to someone watching a live channel.
        self._pending_ack: Dict[str, List[str]] = {}
        # ROCKETCHAT_DM_ONLY: rooms where the "I only work in DMs" notice has already
        # gone out (once per room per process, see webhook_mixin).
        self._dm_only_noticed_rooms: Set[str] = set()
        # chat_id -> the background task re-sending the DDP typing indicator
        # every _TYPING_REFRESH_SECONDS while a turn is in flight (see
        # send_typing/stop_typing below). Per-instance, same reasoning as
        # _pending_ack directly above - a stale entry at worst leaves one
        # room's typing bubble running a few seconds too long after a
        # process crash, never across processes.
        self._typing_tasks: Dict[str, "asyncio.Task"] = {}
        # DM room id -> whether it has more than two participants (``rooms.info``).
        # PROCESS-WIDE, see `_shared_direct_room_kinds`.
        self._direct_room_kinds: Dict[str, bool] = _shared_direct_room_kinds()

    # ------------------------------------------------------------------
    # HTTP helpers (Rocket.Chat REST API v1)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        from aiohttp import web

        # The readiness probe's status file says "connecting" already here: a
        # "connected" left by an earlier process (after OOM or SIGKILL) would otherwise
        # signal false readiness if startup fails before DDP. Hermes does not exit in
        # that case, so a container keeps running.
        await self._ddp_write_status("connecting")
        missing = self._cfg.missing_required()
        if missing:
            # FAIL CLOSED: with transport=webhook an empty ROCKETCHAT_WEBHOOK_SECRET
            # also counts as missing (see config.py missing_required). Without a secret
            # the listener would accept anyone, in any user's name.
            logger.error("Rocket.Chat: not configured, missing: %s", ", ".join(missing))
            if "webhook_secret" in missing:
                logger.error(
                    "Rocket.Chat: ROCKETCHAT_TRANSPORT=webhook requires "
                    "ROCKETCHAT_WEBHOOK_SECRET (without it anyone who can reach "
                    "the listener could post as any user); not starting."
                )
            return False

        await self._rest.open()

        me = await self._rest.me()
        if not me or "_id" not in me:
            logger.error("Rocket.Chat: failed to authenticate - check ROCKETCHAT_TOKEN/ROCKETCHAT_USER_ID/ROCKETCHAT_URL")
            await self._ddp_write_status("auth_failed")
            await self._rest.close()
            return False
        logger.info(
            "Rocket.Chat: authenticated as @%s (%s) on %s",
            me.get("username", self._bot_username), me["_id"], self._base_url,
        )
        # Exemption from the REST rate limit: without the bot role, replies and streamed
        # edits run into 429. Only warns, does not stop startup.
        bypass = rate_limit_bypass_status(
            me.get("roles"), await self._rest.permission_roles(RATE_LIMIT_BYPASS_PERMISSION)
        )
        if bypass == "missing":
            logger.warning(
                "Rocket.Chat: bot role missing for @%s (no role with %s): replies and streaming will be "
                "rate limited; give the account the 'bot' role",
                me.get("username", self._bot_username), RATE_LIMIT_BYPASS_PERMISSION,
            )
        # Reply quote: Rocket.Chat only renders a quote from a message link on its own
        # Site_Url; without it the quote is omitted.
        if self._cfg.reply_quote:
            site = await self._rest.site_url()
            if site:
                _shared_site_url()[self._bot_username] = site
                # For the quote links of the startup notice (ddp_transport_mixin).
                self._site_url = site
            else:
                logger.warning("Rocket.Chat: Site_Url not readable, replies will carry no quote")

        # ROCKETCHAT_TRANSPORT (see ddp_transport_mixin.py): either-or. The webhook HTTP
        # listener starts only with an explicit "webhook", never with "ddp" (the
        # default).
        transport = self._cfg.transport
        if transport == "webhook":
            app = web.Application()
            app.router.add_get("/health", self._handle_health)
            app.router.add_post("/rocketchat", self._handle_outgoing_webhook)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
            try:
                await site.start()
            except OSError as exc:
                logger.error(
                    "Rocket.Chat: could not bind %s:%d: %s",
                    self._webhook_host, self._webhook_port, exc,
                )
                await self._runner.cleanup()
                self._runner = None
                await self._rest.close()
                return False
            logger.info(
                "Rocket.Chat: inbound webhook listening on %s:%d/rocketchat",
                self._webhook_host, self._webhook_port,
            )

        if transport == "ddp":
            await self._ddp_start()

        self._mark_connected()

        if not is_reconnect and self._home_channel and self._cfg.startup_greeting:
            # Deliberately posted RAW (post_message), not via send() - the
            # greeting is a payload we built ourselves, so the reasoning-leak
            # / table-parsing formatting send() applies to model output does
            # not belong here.
            try:
                await self._rest.post_message(
                    self._home_channel,
                    user_text("startup_greeting"),
                )
            except RocketChatNetworkError as exc:
                logger.warning("Rocket.Chat: startup greeting failed: %s", exc)

        return True

    async def disconnect(self) -> None:
        await self._notify_inflight_shutdown()
        for task in self._typing_tasks.values():
            task.cancel()
        for task in list(self._typing_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown, best-effort
                pass
        self._typing_tasks.clear()
        if self._cfg.transport == "ddp":
            await self._ddp_stop()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        # Restart-surviving dedup: the debounced save might never fire if the process
        # stops right here. Deliberately NOT our own signal.signal() (see
        # `_shared_inflight_augment_rooms`: Python's one-handler signal semantics would
        # replace Hermes core's SIGTERM handler). This existing, documented extension
        # point (core calls it, time-bounded, on every platform shutdown) is the right
        # place. Best effort: a failed flush loses at most the last debounce window of
        # entries and does not block shutdown.
        try:
            await self._inbound_dedup.flush()
        except Exception:  # noqa: BLE001 - shutdown, best-effort
            logger.warning("Rocket.Chat: inbound dedup flush failed on shutdown", exc_info=True)
        try:
            await self._ddp_flush_cursor()
        except Exception:  # noqa: BLE001 - shutdown, best-effort
            logger.warning("Rocket.Chat: DDP cursor flush failed on shutdown", exc_info=True)
        try:
            await self._outbound_dedup.flush()
        except Exception:  # noqa: BLE001 - shutdown, best-effort
            logger.warning("Rocket.Chat: outbound dedup flush failed on shutdown", exc_info=True)
        await self._rest.close()
        logger.info("Rocket.Chat: disconnected")

    # The webhook answers 200 at once and the turn runs as Hermes core's background
    # task, so a gateway restart would leave the user waiting forever with no message.
    # For a running turn core itself notifies (``gateway.shutdown.notice_*``, into the
    # thread too); this method covers only the window BEFORE the handoff (see
    # webhook_mixin ``_augment_and_handle``). `disconnect()` (called by core,
    # time-bounded, on every platform shutdown) sends an honest best-effort notice to
    # every room still open, BEFORE the REST client closes.
    async def _notify_inflight_shutdown(self) -> None:
        if not self._inflight_augment_rooms:
            return
        # Several concurrent turns can run in one room: deduplicate so the user does not
        # get several identical notices for the same room or thread. For a thread
        # mention the notice goes into the thread too.
        targets = sorted(set(self._inflight_augment_rooms.values()))
        rooms = sorted({room_id for room_id, _tmid in targets})
        logger.warning(
            "Rocket.Chat: gateway shutting down with %d room(s) mid-turn (%s) - "
            "sending best-effort restart notices",
            len(rooms), ", ".join(rooms),
        )
        for room_id, tmid in targets:
            try:
                await asyncio.wait_for(
                    self._rest.post_message(
                        room_id,
                        user_text("shutdown_notice"),
                        tmid=tmid,
                    ),
                    timeout=3.0,
                )
            except Exception:  # noqa: BLE001 - shutdown, best-effort, never block it
                logger.warning(
                    "Rocket.Chat: could not send shutdown notice to room %s", room_id,
                    exc_info=True,
                )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not content:
            return SendResult(success=True)

        # The reply is posting now: drop the instant "seen you" reaction we put on the
        # user's @mention. Fire-and-forget inside.
        self._clear_ack(chat_id, reply_to or (metadata or {}).get("reply_to_message_id") or "")

        # Model provider failure: after failed API calls Hermes core sends an English
        # error ("API call failed after N retries: ..."). User and operator should both
        # see that the model endpoint is at fault, not the gateway: the log gets a
        # filterable `model endpoint problem (kind=...)` line, the room gets a short
        # notice with core's detail.
        endpoint_problem = classify_model_endpoint_failure(content)
        if endpoint_problem:
            logger.warning(
                "Rocket.Chat: model endpoint problem (kind=%s): the model provider rejected "
                "or did not answer the request; check the provider endpoint and its credentials",
                endpoint_problem,
            )
            detail = content.strip().splitlines()[0][:300]
            content = user_text(
                "model_endpoint_detail",
                message=user_text(MODEL_ENDPOINT_FAILURE_KEYS[endpoint_problem]),
                detail=detail,
            )

        formatted = self.format_message(content)
        tmid = self._reply_thread_id(metadata)

        # Reply reference (reply_reference.py): the first answer message to a question
        # (core passes the question's id as reply_to there) gets a quote and an @mention
        # in rooms; not in DMs, progress bubbles or further chunks of a long answer.
        target = _shared_reply_targets().get(reply_to)
        prefix = ""
        if target is not None and target.room_id == chat_id:
            prefix = reply_prefix(
                target,
                site_url=_shared_site_url().get(self._bot_username, ""),
                quote_enabled=self._cfg.reply_quote,
                mention_enabled=self._cfg.reply_mention,
            )
        if prefix and formatted:
            formatted = prefix + formatted

        # Outbound duplicate guard (see dedup.py for the core defect this exists for).
        # Keyed on the DELIVERED (formatted) text. A bot username is folded into the
        # room key because the caches are process-wide: a shared cache must not silence
        # a different bot that happens to give the same answer in the same room. A
        # thread is its own key: the same answer in two threads is not a duplicate.
        #
        # In a room's main channel two askers' concurrent turns are not separated by a
        # thread. Final answers are separated by the asking message (``trigger``,
        # below), but core sends the progress bubble with no asker. So in the main
        # channel a bubble (``is_progress_bubble``) with an unknown asker escapes the
        # filter (at worst one bubble appears twice): otherwise the identical first line
        # of two turns ("⚙️ tool_search...") would collide within 10 s, and the second
        # turn would edit the first one's message.
        segment = str((metadata or {}).get("thread_id") or "")
        dedup_room_key = (
            f"{self._bot_username}:{chat_id}" + (f":{tmid}" if tmid else "")
            + (f":s={segment}" if segment else "")
        )
        delivery_key = delivery_fingerprint_source(formatted)
        # The asking message (see dedup.py recent_message_id): for the final answer
        # `reply_to` carries it, for a streamed message the metadata does.
        trigger = str(reply_to or (metadata or {}).get("reply_to_message_id") or "")

        # Between the check ("did I send this?") and remember() this method awaits the
        # actual HTTP send, so two concurrent send() calls for the same answer (core's
        # defect, see dedup.py) would both find "not seen yet" and both post. The whole
        # check + send + remember runs UNDER a per-room mutex: the second call waits
        # here for the first, and its own check then finds the fresh entry.
        skip_dedup = not trigger and not segment and is_progress_bubble(content)
        async with self._send_lock(dedup_room_key):
            already_sent_id = None if skip_dedup else self._outbound_dedup.recent_message_id(
                dedup_room_key, delivery_key, trigger
            )
            if already_sent_id:
                logger.warning(
                    "Rocket.Chat: identical reply already posted moments ago, "
                    "not re-posting (message_id=%s)",
                    already_sent_id,
                )
                return SendResult(success=True, message_id=already_sent_id)

            # remember() runs BEFORE the post, with a temporary placeholder message_id.
            # If `post_message` raises `RocketChatNetworkError` after Rocket.Chat
            # already created the message on the server (only reading the response
            # failed, for example on a reused keep-alive connection the server closed),
            # and remember() ran only after a clean loop, core's next retry with the
            # same content (see dedup.py) would find "not seen yet" and post a
            # byte-identical duplicate; the mutex no longer protects, because the first
            # call has returned. This way a retry after an ambiguous failure takes the
            # "already tried" branch. The placeholder is swapped for the real id below
            # after a successful post.
            self._outbound_dedup.remember(dedup_room_key, delivery_key, "_pending_", trigger)

            chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH) if formatted else []

            last_id = None
            for chunk in chunks:
                try:
                    data = await self._rest.post_message(chat_id, chunk, tmid=tmid)
                except RocketChatNetworkError as exc:
                    # Transient (e.g. a reused keep-alive connection Rocket.Chat
                    # already closed server-side) — tell the core so it retries
                    # with its own network-retry path instead of falling into
                    # the generic "formatting failed" plain-text fallback.
                    return SendResult(success=False, error=str(exc), retryable=True)
                if not is_successful(data):
                    # Rocket.Chat explicitly refused and no chunk went out: the message
                    # certainly does not exist, so the `_pending_` mark above is removed
                    # and core's retry really sends it (otherwise it would get "already
                    # sent" and the answer would silently vanish). On an ambiguous
                    # failure (5xx, empty response) the mark stays.
                    if last_id is None and is_rejected(data):
                        self._outbound_dedup.forget(dedup_room_key, delivery_key)
                    return SendResult(success=False, error="Failed to post message")
                if last_id is None and prefix:
                    _shared_sent_prefixes().remember(posted_message_id(data), prefix)
                last_id = posted_message_id(data)

            self._outbound_dedup.remember(dedup_room_key, delivery_key, last_id or "", trigger)
        return SendResult(success=True, message_id=last_id)

    # ------------------------------------------------------------------ Outbound files
    # ------------------------------------------------------------------ Hermes's
    # documented overrides (ADDING_A_PLATFORM.md, "Optional methods"; core's platforms
    # such as Mattermost do the same): core strips the model's ``MEDIA:/path`` tag from
    # the reply, validates the path (``validate_media_delivery_path``) and calls these;
    # on an unsuccessful ``SendResult`` it notifies the user. Only the upload lives
    # here. Images reach ``send_image_file`` through core's ``send_multiple_images``.
    # ``send_image`` (URL) stays the base class's: Rocket.Chat previews the link itself,
    # and downloading a foreign URL would be an SSRF surface.

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        if not self._cfg.outbound_files:
            return await super().send_document(chat_id, file_path, caption=caption, file_name=file_name,
                                               reply_to=reply_to, metadata=metadata, **kwargs)
        return await self._upload_local_file(chat_id, file_path, caption, metadata, file_name=file_name)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs: Any) -> SendResult:
        if not self._cfg.outbound_files:
            return await super().send_image_file(chat_id, image_path, caption=caption,
                                                 reply_to=reply_to, metadata=metadata, **kwargs)
        return await self._upload_local_file(chat_id, image_path, caption, metadata)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        if not self._cfg.outbound_files:
            return await super().send_voice(chat_id, audio_path, caption=caption,
                                            reply_to=reply_to, metadata=metadata, **kwargs)
        return await self._upload_local_file(chat_id, audio_path, caption, metadata)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        if not self._cfg.outbound_files:
            return await super().send_video(chat_id, video_path, caption=caption,
                                            reply_to=reply_to, metadata=metadata, **kwargs)
        return await self._upload_local_file(chat_id, video_path, caption, metadata)

    async def _upload_local_file(self, chat_id: str, path: str, caption: Optional[str],
                                 metadata: Optional[Dict[str, Any]], *,
                                 file_name: Optional[str] = None) -> SendResult:
        """Upload a local file into the room (into the reply's thread if any). The
        file's name and content are not logged, only the extension and the size.
        """
        import mimetypes

        self._clear_ack(chat_id, (metadata or {}).get("reply_to_message_id") or "")
        name = file_name or os.path.basename(path) or "file"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        ext = os.path.splitext(name)[1].lower() or "-"
        try:
            size = os.path.getsize(path)
            handle = open(path, "rb")  # noqa: SIM115 - closed by the with below
        except OSError as exc:
            logger.warning("Rocket.Chat: outbound file (%s) not readable: %s", ext, type(exc).__name__)
            return SendResult(success=False, error=f"file not readable: {type(exc).__name__}")
        try:
            with handle:
                # File object: aiohttp sends it in chunks and does not read it whole.
                answer = await self._rest.upload_file(
                    chat_id, handle, name, mime,
                    msg=self.format_message(caption) if caption else "",
                    tmid=self._reply_thread_id(metadata),
                )
        except RocketChatNetworkError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        if not is_successful(answer):
            error = (answer or {}).get("error") or "upload failed"
            logger.warning("Rocket.Chat: outbound file (%s, %d bytes) rejected by the server: %s",
                           ext, size, error)
            return SendResult(success=False, error=f"Rocket.Chat rejected the file: {error}")
        logger.info("Rocket.Chat: outbound file sent to room %s (%s, %d bytes)", chat_id, ext, size)
        return SendResult(success=True, message_id=posted_message_id(answer))

    # ------------------------------------------------------------------
    # Dangerous-command prompt with buttons
    # ------------------------------------------------------------------
    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        """Hermes's documented hook: its prompt text with accept and deny buttons (approval_buttons.py).

        An unsuccessful result makes core fall back to its plain-text ``/approve`` prompt, which is what
        should happen when the buttons are off, no choice is allowed, or the server refuses the attachment.
        """
        if not self._cfg.approval_buttons:
            return SendResult(success=False, error="Approval buttons are disabled")
        attachments = approval_attachments(
            prompt.actions, self._cfg.bot_username, self._cfg.allowed_builtin_commands
        )
        if not attachments:
            return SendResult(success=False, error="No approval choice is allowed over Rocket.Chat")
        text = self.format_message(prompt.text)
        fallback = user_text("approval_typed_fallback", bot=self._cfg.bot_username)
        tmid = self._reply_thread_id(prompt.metadata)
        try:
            answer = await self._rest.post_message(prompt.chat_id, f"{text}\n\n{fallback}", tmid, attachments=attachments)
        except RocketChatNetworkError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        if not is_successful(answer):
            return SendResult(success=False, error=str((answer or {}).get("error") or "refused"))
        message_id = posted_message_id(answer)
        remember_approval_card(prompt.session_key, prompt.chat_id, message_id, text, tmid)
        return SendResult(success=True, message_id=message_id)

    @staticmethod
    def _warn_if_memory_is_shared(cfg: RocketChatConfig) -> None:
        """One startup warning when Hermes's memory is shared by several people (config.shared_memory_risk)."""
        if not cfg.shared_memory_warning:
            return
        try:
            from hermes_cli.config import read_raw_config_readonly
            stores = shared_memory_risk((read_raw_config_readonly() or {}).get("memory"), cfg)
        except Exception:  # noqa: BLE001 - only a hint; an unreadable config must not stop the adapter
            return
        if stores:
            logger.warning(
                "Rocket.Chat: Hermes's built-in memory (%s) is on and several people can reach this bot, so "
                "what one person tells it can appear in another person's answer. Set memory.memory_enabled "
                "and memory.user_profile_enabled to false in config.yaml for a shared bot, or "
                "ROCKETCHAT_SHARED_MEMORY_WARNING=false to silence this.",
                ", ".join(stores),
            )

    @property
    def typed_command_prefix(self) -> str:  # type: ignore[override]
        """What Hermes tells people to type in its text prompts ("reply `{prefix}approve`").

        A typed ``/approve`` never leaves a Rocket.Chat client; with the mention it does, in rooms and DMs alike.
        """
        return f"@{self._cfg.bot_username} /"

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Hermes's documented hook: a slash-command confirmation (``/new``) with buttons.

        The buttons send the same text answers Hermes's own fallback prompt asks for, and core intercepts those
        for the session, so ``confirm_id`` is not needed. An unsuccessful result falls back to that prompt.
        """
        if not self._cfg.approval_buttons:
            return SendResult(success=False, error="Approval buttons are disabled")
        attachments = confirm_attachments(self._cfg.bot_username, self._cfg.allowed_builtin_commands)
        if not attachments:
            return SendResult(success=False, error="No confirmation answer is allowed over Rocket.Chat")
        text = self.format_message(confirm_body(message))
        fallback = user_text("confirm_typed_fallback", bot=self._cfg.bot_username)
        tmid = self._reply_thread_id(metadata)
        try:
            answer = await self._rest.post_message(chat_id, f"{text}\n\n{fallback}", tmid, attachments=attachments)
        except RocketChatNetworkError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        if not is_successful(answer):
            return SendResult(success=False, error=str((answer or {}).get("error") or "refused"))
        message_id = posted_message_id(answer)
        remember_confirm_card(session_key, chat_id, message_id, text, tmid)
        return SendResult(success=True, message_id=message_id)

    async def _retire_approval_card(self, session_key: str, answered_by: str) -> None:
        """Replace the session's oldest answered prompt with one that has no buttons and says who answered.

        A repost rather than an edit: ``chat.update`` drops attachments in the stored message, but a web client
        that has the room open keeps showing the old buttons. Cosmetic, so it never raises; if the bot may not
        delete, the edit is the fallback.
        """
        card = pop_approval_card(session_key)
        if card is None:
            return
        room_id, message_id, text, tmid = card
        answered = f"{text}\n\n{user_text('approval_answered', user=answered_by)}"
        try:
            if is_successful(await self._rest.delete_message(room_id, message_id)):
                await self._rest.post_message(room_id, answered, tmid)
            else:
                await self._rest.update_message(room_id, message_id, answered)
        except Exception:  # noqa: BLE001 - cosmetic, the answer itself already went through
            logger.warning("Rocket.Chat: could not retire approval prompt %s", message_id, exc_info=True)

    def _reply_thread_id(self, metadata: Optional[Dict[str, Any]]) -> str:
        """The Rocket.Chat thread a reply belongs in, or ``""`` (main channel).

        Hermes core sends ``metadata={"thread_id": <source.thread_id>}`` with the reply, an
        error message and a command reply alike. That is the turn's real Rocket.Chat thread
        (``tmid``), so it is used as it is: no table, no segment, and it survives a restart
        (the delivery ledger re-sends with the same metadata).
        """
        return str((metadata or {}).get("thread_id") or "")

    def format_message(self, content: str) -> str:
        """Strip (or wrap) a leaked reasoning preamble if enabled, then adapt the text to
        Rocket.Chat markdown (``rc_markdown.escape_lone_tildes``: a lone ``~`` would strike
        through the text up to the next one).

        Thin wrapper around ``reasoning_format.wrap_leaked_reasoning``: this method only
        supplies the configured switch, pattern, label and mode. The heuristic is documented
        in that module; none of it is Rocket.Chat-specific.
        """
        if not self._cfg.reasoning_leak_formatting_enabled:
            return escape_lone_tildes(content)
        formatted = wrap_leaked_reasoning(
            content,
            pattern=self._cfg.reasoning_leak_pattern,
            label=self._cfg.reasoning_leak_label,
            mode=self._cfg.reasoning_leak_mode,
        )
        # DEBUG-only, full-text logging of what this heuristic changed: length-only logs
        # cannot tell whether a truncated answer came from this module's "bridge" logic
        # (a documented risk in wrap_leaked_reasoning) or from the model. Gated behind
        # isEnabledFor so the repr() cost is paid only when DEBUG logging is on.
        if formatted != content and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "reasoning_leak_formatting (mode=%s) changed the outgoing message: "
                "%d of %d raw chars removed; raw=%r formatted=%r",
                self._cfg.reasoning_leak_mode,
                len(content) - len(formatted),
                len(content),
                content,
                formatted,
            )
        return escape_lone_tildes(formatted)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Replace an already-posted message's text via ``chat.update``.

        Implementing this is what actually enables streaming: the core's
        stream consumer progressively edits one message as tokens arrive,
        but when an adapter leaves ``edit_message`` at the base-class
        default it silently degrades to "buffer the entire answer, then
        post it once" — and ``gateway/run.py`` additionally throws away
        every tool-progress update, since each would otherwise become a
        separate permanent message. Both together are why a Rocket.Chat
        user used to stare at an empty channel for the whole agent turn.

        Text-only (``chat.update``); Rocket.Chat renders GFM pipe-tables in
        the text natively.
        """
        if not message_id:
            return SendResult(success=False, error="edit_message: missing message_id")

        formatted = self.format_message(content)
        # A streamed edit keeps the reference added at the first send.
        prefix = _shared_sent_prefixes().get(message_id)
        if prefix and formatted:
            formatted = prefix + formatted

        chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH) if formatted else []
        # A streamed preview shows the leading chunk only; the core's own
        # overflow handling opens continuation messages for any remainder.
        # Rocket.Chat rejects an empty `text`, so hold a single space until
        # the first real token lands.
        text = chunks[0] if chunks else " "

        try:
            data = await self._rest.update_message(chat_id, message_id, text)
        except RocketChatNetworkError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        if not is_successful(data):
            return SendResult(success=False, error="Failed to edit message")
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Remove a message via ``chat.delete``.

        Only used for best-effort cleanup of a streaming preview that a
        fresh final message superseded (see
        ``prefers_fresh_final_streaming``); without it the user would be
        left with a half-streamed preview sitting above the real answer.
        """
        if not message_id:
            return False
        try:
            data = await self._rest.delete_message(chat_id, message_id)
        except RocketChatNetworkError as exc:
            logger.warning("Rocket.Chat: streaming preview delete failed: %s", exc)
            return False
        return is_successful(data)

    def prefers_fresh_final_streaming(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Whether to ask the core for a fresh final message instead of an in-place edit.

        Governed by ``ROCKETCHAT_ALWAYS_FRESH_FINAL_STREAMING`` (default **false**; do not
        flip it without reading the rest of this docstring).

        The flag was tried as a fix for a different bug: a mid-stream `edit_message` failure
        abandons the streamed preview, which stays as a truncated fragment ending in the
        streaming cursor ("The user ▉") while the real answer arrives as a separate message.

        It made things worse. Forcing every finalize through this hook re-triggers a
        separate core defect in the fresh-final path: a finished answer can be delivered
        twice. Measured: a byte-identical duplicate about 3 s apart that the outbound dedup
        guard (`dedup.py`) did not catch, because the second delivery bypasses this adapter's
        `send()` check, so ``ROCKETCHAT_OUTBOUND_DEDUP_TTL_SECONDS`` offers no protection. It
        stays in place, off by default, in case a future core release fixes the duplicate. Do
        not re-enable it without re-testing that scenario.
        """
        if not content:
            return False
        return self._cfg.always_fresh_final_streaming

    async def send_whoami_reply(
        self, chat_id: str, user_id: str, username: str, *, tmid: str = ""
    ) -> bool:
        """Post the ``/whoami`` identity-resolution report for the CALLER only.

        Handled inside the adapter without an agent turn (a pure lookup, instant, nothing
        for a model to add); into the caller's thread when ``tmid`` is set. The identity
        comes ONLY from the sender authenticated by the Rocket.Chat server
        (``user_id`` -> ``users.info``), never from the message text.
        """
        user = await self._rest.users_info(user_id)
        emails = [
            str(entry.get("address") or "")
            for entry in ((user or {}).get("emails") or [])
            if entry.get("address")
        ]
        text = build_whoami_reply(rc_username=username, emails=emails)
        try:
            data = await self._rest.post_message(chat_id, text, tmid=tmid)
        except RocketChatNetworkError as exc:
            logger.warning("Rocket.Chat: whoami send failed: %s", exc)
            return False
        return is_successful(data)

    def _send_lock(self, dedup_room_key: str) -> asyncio.Lock:
        """Get or create a (bot, room) mutex for the outbound check + send + remember."""
        lock = self._send_locks.get(dedup_room_key)
        if lock is None:
            lock = self._send_locks[dedup_room_key] = asyncio.Lock()
        return lock


    async def _notify(self, chat_id: str, text: str, *, tmid: str = "") -> None:
        """Fire-and-forget informational post - warn-only, never raises.
        ``tmid`` puts it into the trigger's thread."""
        try:
            await self._rest.post_message(chat_id, text, tmid=tmid)
        except RocketChatNetworkError as exc:
            logger.warning("Rocket.Chat: notification send failed: %s", exc)


    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Native Rocket.Chat Realtime API typing indicator.

        Rocket.Chat's typing indicator is a `stream-notify-room` DDP method call, not a REST
        endpoint: event `<roomId>/user-activity`, params `[username, ["user-typing"], {}]` (an
        empty activity list stops it). Verified against the Rocket.Chat 8.7 server source
        (`streamRoom.allowWrite` accepts ONLY the `user-activity` event and silently rejects
        anything else) and live with a DDP listener. The try/except below keeps the posture of
        `_safe_react`: cosmetic feedback must never surface an error or abort a turn.

        Only meaningful with the DDP transport; the webhook transport has no persistent
        connection to carry it. Like Discord's 12 s loop and Telegram's 4 s loop in Hermes core,
        we start our OWN periodic re-send task here and cancel it in stop_typing()/disconnect():
        one task per room, idempotent.

        If the server ever answers `stream-notify-room` with "Method not found", the indicator
        is switched off for the rest of the process instead of re-sending a failing call every
        few seconds (see `ddp_method_supported`).
        """
        if self._cfg.transport != "ddp":
            return
        if not self.ddp_method_supported(_TYPING_DDP_METHOD):
            return
        if chat_id in self._typing_tasks:
            return

        async def _loop() -> None:
            try:
                # The loop stops if the server has signalled that the method does not
                # exist (see ddp_transport_mixin._ddp_notify).
                while self.ddp_method_supported(_TYPING_DDP_METHOD):
                    await self._ddp_notify_typing(chat_id, True)
                    await asyncio.sleep(_TYPING_REFRESH_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - cosmetic, never surfaces to the user
                logger.warning(
                    "Rocket.Chat: typing loop failed for %s", chat_id, exc_info=True
                )

        self._typing_tasks[chat_id] = asyncio.create_task(_loop())

    async def stop_typing(self, chat_id: str) -> None:
        """Cancel the typing-refresh loop and tell Rocket.Chat typing stopped.

        Safe to call even when no loop is running (e.g. webhook-only
        transport, or a turn that never called send_typing) — see
        send_typing()'s docstring for the transport gate and error posture.
        """
        task = self._typing_tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort
                pass
        if self._cfg.transport == "ddp" and self.ddp_method_supported(_TYPING_DDP_METHOD):
            await self._ddp_notify_typing(chat_id, False)

    async def _ddp_notify_typing(self, chat_id: str, is_typing: bool) -> None:
        try:
            await self._ddp_notify(
                _TYPING_DDP_METHOD,
                [
                    f"{chat_id}/user-activity",
                    self._bot_username,
                    ["user-typing"] if is_typing else [],
                    {},
                ],
            )
        except Exception:  # noqa: BLE001 - cosmetic, never surfaces to the user
            logger.warning(
                "Rocket.Chat: typing notify(%s, %s) failed", chat_id, is_typing, exc_info=True
            )

    async def _safe_react(self, msg_id: str, emoji: str, should_react: bool) -> None:
        """Add or remove an ack reaction, swallowing every error.

        A reaction is cosmetic feedback; a network hiccup on it must never surface to the user
        or abort a turn. Always call via ``asyncio.create_task``, fire-and-forget.
        """
        try:
            resp = await self._rest.react(msg_id, emoji, should_react=should_react)
            if not (resp or {}).get("success"):
                logger.warning(
                    "Rocket.Chat: react(%s, :%s:, should_react=%s) -> %s",
                    msg_id,
                    emoji,
                    should_react,
                    resp,
                )
        except Exception:  # noqa: BLE001 - deliberately total; a cosmetic
            # reaction must never surface to the user or abort a turn.
            logger.warning(
                "Rocket.Chat: react(%s, %s, should_react=%s) failed",
                msg_id,
                emoji,
                should_react,
                exc_info=True,
            )

    def _clear_ack(self, chat_id: str, trigger: str = "") -> None:
        """Remove the instant-ack reaction of the message this reply answers.

        Called from ``send()`` the moment the agent's reply posts. Tracked per message, because
        several people can be waiting in one room at once: ``trigger`` is the question's id when core
        names it (``reply_to``). Without one (a progress bubble, a status line) the reaction is removed
        only if a single message is waiting, since another asker's reaction must not go early.

        No error signal here: Hermes core surfaces failures itself (retry classification, delivery
        ledger), a failed turn almost always reaches the user as some text reply via `send()`, and there
        is no reliable "this turn is dead" hook to hang an error emoji on without risking a false
        positive.
        """
        waiting = self._pending_ack.get(chat_id)
        if not waiting:
            return
        if trigger:
            if trigger not in waiting:
                return
            msg_id = trigger
        elif len(waiting) == 1:
            msg_id = waiting[0]
        else:
            return
        waiting.remove(msg_id)
        if not waiting:
            del self._pending_ack[chat_id]
        if self._cfg.ack_reaction:
            asyncio.create_task(
                self._safe_react(msg_id, self._cfg.ack_reaction, False)
            )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Room name and kind, falling back to the raw id when unknown.

        The room-type lookup (``rooms.info``, then the legacy
        ``channels.info``/``groups.info`` pair) is handled inside
        ``rest_client.room_info`` — see the note there.
        """
        room = await self._rest.room_info(chat_id)
        if room is None:
            return {"name": chat_id, "type": "channel"}
        # A private group carries "t": "p"; a public channel "c".
        kind = "group" if room.get("t") == "p" else "channel"
        return {"name": room.get("name") or chat_id, "type": kind}

    # ------------------------------------------------------------------
    # Inbound: Rocket.Chat Outgoing Webhook receiver
    # ------------------------------------------------------------------





# ---------------------------------------------------------------------------
# is_connected probe
# ---------------------------------------------------------------------------


# The documented helper (ADDING_A_PLATFORM.md: "for env-only platforms use
# _shared.env_is_connected(...) instead of a hand-rolled get_env_value check"): it runs
# OUTSIDE the gateway too (``hermes platforms list``), where os.environ is not populated
# from the profile's .env. The same four required settings as
# RocketChatConfig.REQUIRED_FIELDS.
_is_connected = env_is_connected(
    "ROCKETCHAT_TOKEN",
    "ROCKETCHAT_URL",
    "ROCKETCHAT_USER_ID",
    "ROCKETCHAT_BOT_USERNAME",
)


# ---------------------------------------------------------------------------
# Plugin registration entry point
# ---------------------------------------------------------------------------


def _build_adapter(config):
    return RocketChatAdapter(config)


# The base-class members and ``MessageEvent`` fields the plugin relies on (overrides or
# calls). A Hermes release that renames or removes one would otherwise break silently:
# the override is not called, the call raises ``AttributeError``. The list changes
# together with tests/test_hermes_contract.py, which checks that the source really uses
# all of them.
_RELIED_BASE_MEMBERS = (
    "build_source", "connect", "delete_message", "disconnect", "edit_message",
    "format_message", "get_chat_info", "handle_message", "_mark_connected",
    "send", "send_document", "send_image_file", "send_typing", "send_video", "send_voice",
    # not in the plugin guide; documented in ADDING_A_PLATFORM.md or the base class
    "_source_session_key", "_send_exec_approval_prompt", "send_slash_confirm",
    "_notify_fatal_error", "prefers_fresh_final_streaming",
    "_set_fatal_error", "splits_long_messages", "stop_typing", "typed_command_prefix",
    "truncate_message",
)
_RELIED_EVENT_FIELDS = (
    "channel_context", "media_text_inlined", "media_types", "media_urls", "message_id",
    "message_type", "raw_message", "source", "text",
)


def check_hermes_contract() -> List[str]:
    """The startup self-check: the list of missing names (empty = fine). Logs once at
        ERROR level and does not stop the plugin (the gap may be in a feature the rest can
        work without).
    """
    import dataclasses

    missing = [f"BasePlatformAdapter.{name}" for name in _RELIED_BASE_MEMBERS
               if not hasattr(BasePlatformAdapter, name)]
    if dataclasses.is_dataclass(MessageEvent):
        fields = {f.name for f in dataclasses.fields(MessageEvent)}
        missing += [f"MessageEvent.{name}" for name in _RELIED_EVENT_FIELDS if name not in fields]
    if missing:
        logger.error(
            "Rocket.Chat: this Hermes version lacks names the plugin relies on: %s - "
            "check the plugin against the installed Hermes before using it",
            ", ".join(missing),
        )
    return missing


def _outbound_files_enabled() -> bool:
    """ROCKETCHAT_OUTBOUND_FILES at registration (the platform guidance is fixed then).
        The key is readable only from env, so the guidance and the upload see the same value.
    """
    return load_config({}, env=_ScopedEnv()).outbound_files


def register(ctx) -> None:
    """Plugin entry point - called by the Hermes plugin system."""
    check_hermes_contract()
    ctx.register_platform(
        name="rocketchat",
        label="Rocket.Chat",
        adapter_factory=_build_adapter,
        check_fn=check_rocketchat_requirements,
        validate_config=validate_rocketchat_config,
        is_connected=_is_connected,
        required_env=[
            "ROCKETCHAT_URL",
            "ROCKETCHAT_TOKEN",
            "ROCKETCHAT_USER_ID",
            "ROCKETCHAT_BOT_USERNAME",
        ],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        standalone_sender_fn=standalone_send,
        dashboard_description="Connect Hermes to Rocket.Chat rooms and direct messages.",
        dashboard_env_vars=(
            "ROCKETCHAT_URL",
            "ROCKETCHAT_TOKEN",
            "ROCKETCHAT_USER_ID",
            "ROCKETCHAT_BOT_USERNAME",
            "ROCKETCHAT_ALLOWED_USERS",
        ),
        allowed_users_env="ROCKETCHAT_ALLOWED_USERS",
        allow_all_env="ROCKETCHAT_ALLOW_ALL_USERS",
        # Hermes's cron ``deliver=rocketchat`` target (and the key ``/sethome`` writes);
        # without it cron's pre-check treats Rocket.Chat as an unknown platform.
        cron_deliver_env_var="ROCKETCHAT_HOME_CHANNEL",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🚀",
        # The plugin's general Rocket.Chat guidance for the system prompt (see
        # prompt_guidance.py). Core uses this as the default platform hint; config
        # ``platform_hints.rocketchat.append`` adds a deployment's own text AFTER it
        # (replace: replaces it).
        platform_hint=build_platform_hint(_outbound_files_enabled()),
    )
