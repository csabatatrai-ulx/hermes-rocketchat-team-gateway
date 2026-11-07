"""Inbound Outgoing Webhook reception and the shared inbound dispatch for the Rocket.Chat adapter.

WebhookInboundMixin handles receiving the inbound HTTP webhook (``ROCKETCHAT_TRANSPORT=webhook``, the fallback path) and
wiring it into the decision chain (``classify_inbound``). ``RocketChatAdapter`` inherits it together with
``DdpTransportMixin``.

Transport-agnostic: the default DDP path also calls the same ``_classify_inbound``/``_dispatch_inbound_decision`` pair,
with a webhook-shaped payload built from a DDP message, so the two transports' decisions and side effects cannot drift
apart.

Direct messages (see dm_session.py): before the decision, ``_classify_inbound`` uses ``rooms.info`` (cached) to decide
whether a ``t='d'`` room is a group DM (which runs as a room); a one-to-one DM reaches core as ``chat_type="dm"``. From a
DM only ids and counts enter the log, never text.

Sessions (see room_session.py) belong to Hermes core; ``thread_id`` is the real Rocket.Chat thread.
"""

from __future__ import annotations

import asyncio
import time
import logging
import os
from typing import Any, Optional

# A guarded import: a future Hermes refactor may move or rename this module. That must
# not break the whole adapter, but it must be loud.
try:
    from agent.skill_commands import resolve_skill_command_key as _resolve_skill_command_key
except ImportError:  # pragma: no cover - defensive, see comment above
    _resolve_skill_command_key = None

from .builtin_commands import (
    PERSISTENT_APPROVAL_KEY,
    CommandResolver,
    blocked_builtin_command,
    blocked_persistent_approval,
    blocked_skill_command,
    make_hermes_command_resolver,
)
from .dm_session import (
    bot_replied_since,
    is_group_direct_room,
    select_seed_messages,
    select_thread_seed,
)
from .history import (
    HISTORY_LIMIT_MAX,
    UnverifiedSender,
    build_channel_context,
    estimate_tokens,
    exclude_message,
    find_context_boundary,
    messages_before,
    pin_thread_root,
    select_context_window,
    should_augment_with_context,
    trim_messages_to_token_budget,
)
from .inbound import (
    HANDLE_BUILTIN_COMMAND_BLOCKED,
    HANDLE_DM_ONLY_NOTICE,
    HANDLE_MESSAGE,
    HANDLE_SKILL_COMMAND_BLOCKED,
    HANDLE_WHOAMI,
    IGNORE_DUPLICATE,
    IGNORE_NOT_ALLOWED,
    IGNORE_SENDER,
    IGNORE_STALE,
    REJECT_BAD_TOKEN,
    InboundDecision,
    classify_inbound,
    is_own_message,
    is_sender_allowed,
    parse_webhook_body,
    payload_room_type,
    rc_timestamp,
    rc_timestamp_seconds,
    secret_matches,
)
from .room_session import (
    BOUNDARY_LAST_REPLY,
    BOUNDARY_NONE,
    BOUNDARY_PREVIOUS_MENTION,
    find_previous_mention,
    select_pending_window,
)
from .asker import issue_asker
from .media import DownloadedMedia, download_attachments, extract_file_refs, message_type_name
from .reply_reference import ReplyTarget
from .shared_state import _shared_reply_targets, remember_direct_room_kind
from .scoped_env import ScopedEnv
from .user_texts import user_text
from .whoami import is_whoami_command
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)

# The installed Hermes's command resolver (hermes_command_resolver, below), loaded
# lazily once; ``None`` if the Hermes registry cannot be imported (then the guard fails
# closed and warns once). The list is a "not loaded yet" marker, because ``None`` itself
# is a valid result.
_HERMES_COMMAND_RESOLVER: list = []


def hermes_command_resolver() -> Optional[CommandResolver]:
    """The installed Hermes's command resolver, or ``None`` (with a warning) if ``hermes_cli.commands`` cannot be imported;
    then ``builtin_commands.blocked_builtin_command`` runs fail-closed.

    The Hermes imports happen here, lazily and defensively (``builtin_commands.py`` stays free of Hermes imports); a missing
    bundle or quick-command resolver only narrows the scope and does not disable the guard.
    """
    try:
        from hermes_cli.commands import is_gateway_known_command, resolve_command
    except Exception:  # noqa: BLE001 - any import failure: fail closed
        logger.warning(
            "Rocket.Chat: could not import hermes_cli.commands - the built-in command "
            "guard runs FAIL-CLOSED: every /word message that is not allowlisted "
            "(ROCKETCHAT_ALLOW_BUILTIN_<NAME>) and not an installed skill is blocked. "
            "Hermes' internal module layout changed - re-check the import path."
        )
        return None
    try:
        from agent.skill_bundles import resolve_bundle_command_key
    except Exception:  # noqa: BLE001
        resolve_bundle_command_key = None
    try:
        from hermes_cli.config import read_raw_config_readonly
    except Exception:  # noqa: BLE001
        read_raw_config_readonly = None

    def _quick_command_names() -> Any:
        if read_raw_config_readonly is None:
            return ()
        qc = (read_raw_config_readonly() or {}).get("quick_commands")
        return qc if isinstance(qc, dict) else ()

    return make_hermes_command_resolver(
        resolve_command,
        is_gateway_known_command=is_gateway_known_command,
        resolve_bundle_command_key=resolve_bundle_command_key,
        quick_command_names=_quick_command_names,
    )


def _hermes_command_resolver() -> Optional[CommandResolver]:
    """The cached :func:`hermes_command_resolver` (so the warning goes out once)."""
    if not _HERMES_COMMAND_RESOLVER:
        _HERMES_COMMAND_RESOLVER.append(hermes_command_resolver())
    return _HERMES_COMMAND_RESOLVER[0]


def _is_installed_skill(word: str) -> bool:
    return _resolve_skill_command_key is not None and _resolve_skill_command_key(word) is not None


def log_ignored_delivery(decision: InboundDecision) -> None:
    """Log a delivery dropped without a turn (both the webhook and DDP paths call this).

    * A repeated or too-old delivery: debug, not warning. Rocket.Chat regularly re-sends the same message because of a
      reaction, edit, pin or thread reply (the bot's own ack reaction included); that is expected, not an error.
    * A ``ROCKETCHAT_IGNORE_SENDERS`` sender and a DM from a sender who is not allowed: info, ids only; the message text
      never enters the log. The bot does not answer a refused DM (an answer would reveal that it is alive and that an
      allowlist exists); a refused mention in a room stays unlogged.
    """
    if decision.outcome == IGNORE_SENDER:
        logger.info(
            "Rocket.Chat: message from an ignored sender dropped "
            "(sender_id=%s, room=%s, message_id=%s)",
            decision.sender_id, decision.channel_id, decision.message_id,
        )
    elif decision.outcome == IGNORE_NOT_ALLOWED and decision.is_direct:
        logger.info(
            "Rocket.Chat: direct message from a sender not on the allowlist dropped "
            "(sender_id=%s, room=%s, message_id=%s)",
            decision.sender_id, decision.channel_id, decision.message_id,
        )
    elif decision.outcome == IGNORE_DUPLICATE:
        logger.debug(
            "Rocket.Chat: duplicate delivery ignored (message_id=%s)", decision.message_id
        )
    elif decision.outcome == IGNORE_STALE:
        logger.debug(
            "Rocket.Chat: message older than the inbound dedup retention ignored "
            "(message_id=%s)", decision.message_id,
        )




class WebhookInboundMixin:
    """Rocket.Chat Outgoing Webhook reception and decision chain dispatch."""

    def _log_ignored_delivery(self, decision: InboundDecision) -> None:
        """The DDP path's entry point to the module-level ``log_ignored_delivery``."""
        log_ignored_delivery(decision)

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "rocketchat"})

    async def _handle_outgoing_webhook(self, request: "web.Request") -> "web.Response":
        """Turn one webhook delivery into an agent turn or nothing.

        All of the decision-making — body parsing, secret check, self-filter,
        dedup, mention handling, allowlist, built-in-command detection, and
        crucially the ORDER of those — lives in ``inbound.classify_inbound``,
        where it is unit-tested. This method only carries out the decision.
        """
        from aiohttp import web

        decision = await self._classify_inbound(await request.read())

        if decision.outcome == REJECT_BAD_TOKEN:
            logger.warning("Rocket.Chat: rejected webhook with invalid/missing token")
        else:
            log_ignored_delivery(decision)

        if not decision.should_handle:
            body = (
                {"error": decision.reason}
                if decision.http_status >= 400
                else {"status": "ignored", "reason": decision.reason}
            )
            return web.json_response(body, status=decision.http_status)

        await self._dispatch_inbound_decision(decision)

        if decision.outcome == HANDLE_MESSAGE:
            return web.json_response(
                {"status": "accepted", "message_id": decision.message_id}
            )
        return web.json_response({"status": "handled", "reason": decision.reason})

    async def _classify_inbound(self, raw_body: bytes) -> InboundDecision:
        """Both transports' entry point: group DM recognition, then the decision.

        For a ``t='d'`` room (and only there) ``_is_group_direct`` first decides whether it has more than two participants; the
        decision itself is the pure ``classify_inbound``.
        """
        group_direct = await self._is_group_direct(raw_body)
        return self._classify_inbound_from_body(raw_body, group_direct=group_direct)

    async def _is_group_direct(self, raw_body: bytes) -> bool:
        """Whether the delivery's room is a DM of more than two participants (``rooms.info``).

        Asks only for an authenticated (matching webhook secret), non-self message from a ``t='d'`` room, once per room (the
        ``_direct_room_kinds`` cache keeps a successful answer; see shared_state.py). If the lookup does not decide (error,
        missing field), the room runs as a one-to-one DM, so a transient error does not silence the bot's DMs, and the next
        message asks again.
        """
        payload = parse_webhook_body(raw_body)
        if not payload or payload_room_type(payload) != "d":
            return False
        if not secret_matches(payload.get("token"), self._webhook_secret):
            return False
        if is_own_message(payload, self._user_id, self._bot_username):
            return False
        room_id = str(payload.get("channel_id", "") or "")
        if not room_id:
            return False
        cache = self._direct_room_kinds
        if room_id in cache:
            return cache[room_id]
        try:
            room = await self._rest.room_info(room_id)
        except Exception:  # noqa: BLE001 - fails soft, ld. docstring
            room = None
        is_group = is_group_direct_room(room)
        if is_group is None:
            logger.warning(
                "Rocket.Chat: could not tell whether direct room %s has more than two "
                "participants (rooms.info); handling it as a one-to-one DM", room_id,
            )
            return False
        remember_direct_room_kind(cache, room_id, is_group)
        if is_group:
            logger.info(
                "Rocket.Chat: direct room %s has more than two participants; "
                "handled as a room (mention required)", room_id,
            )
        return is_group

    def _classify_inbound_from_body(
        self, raw_body: bytes, *, group_direct: bool = False
    ) -> InboundDecision:
        """The ONE ``classify_inbound`` call site, shared by webhook AND DDP.

        Extracted so the two inbound transports cannot drift on which
        kwargs get passed. The webhook path calls this with the raw HTTP body; the DDP path calls
        it with a synthetic, same-shaped payload built from a DDP message
        object (``ddp_transport_mixin.ddp_message_to_webhook_payload``).
        """
        return classify_inbound(
            raw_body,
            bot_username=self._bot_username,
            bot_user_id=self._user_id,
            webhook_secret=self._webhook_secret,
            require_mention=self._require_mention,
            allowed_users=self._cfg.allowed_users,
            allow_all_users=self._cfg.allow_all_users,
            ignore_senders=self._cfg.ignore_senders,
            group_direct=group_direct,
            dm_only=self._cfg.dm_only,
            whoami_enabled=self._cfg.whoami_enabled,
            whoami_command=self._cfg.whoami_command,
            is_duplicate=self._inbound_dedup.is_duplicate,
            max_message_age_seconds=self._cfg.inbound_dedup_retention_hours * 3600.0,
            is_whoami_command=lambda text, command: is_whoami_command(text, command=command),
            blocked_builtin_command=lambda text: blocked_persistent_approval(
                text, self._cfg.allowed_builtin_commands
            ) or blocked_builtin_command(
                text,
                self._cfg.allowed_builtin_commands,
                resolve_command=_hermes_command_resolver(),
                is_skill_command=_is_installed_skill,
            ),
            blocked_skill_command=lambda text: blocked_skill_command(
                text, ScopedEnv(), resolve_skill_command_key=_resolve_skill_command_key
            ),
        )

    async def _dispatch_inbound_decision(self, decision: InboundDecision) -> None:
        """All SIDE EFFECTS for an already-classified decision, transport-agnostic.

        Kept separate so ``DdpTransportMixin`` can reuse the EXACT same outcome handling as
        the webhook path, instead of a second, drift-prone copy. Callers
        are responsible for the transport-specific response/ack shape (an
        HTTP ``web.json_response`` for the webhook, nothing for DDP) - this
        method only performs the awaits, it returns nothing.
        """
        # Command replies also go to the trigger's thread (``decision.thread_id``, "" in
        # the main channel), like the agent's answer.
        if decision.outcome == HANDLE_DM_ONLY_NOTICE:
            # ROCKETCHAT_DM_ONLY: one notice per room (per process), so an invitation or
            # repeated mention does not litter the room; the text reveals nothing about
            # the owner or the settings. The message text is not logged.
            noticed = self._dm_only_noticed_rooms
            logger.info(
                "Rocket.Chat: room mention ignored, DM-only bot "
                "(sender_id=%s, room=%s, message_id=%s, notice=%s)",
                decision.sender_id, decision.channel_id, decision.message_id,
                "no" if decision.channel_id in noticed else "yes",
            )
            if decision.channel_id and decision.channel_id not in noticed:
                noticed.add(decision.channel_id)
                await self._notify(
                    decision.channel_id, user_text("dm_only_notice"),
                    tmid=decision.thread_id,
                )
            return

        if decision.outcome == HANDLE_WHOAMI:
            await self.send_whoami_reply(
                decision.channel_id,
                decision.sender_id,
                decision.sender_username,
                tmid=decision.thread_id,
            )
            return

        if decision.outcome == HANDLE_BUILTIN_COMMAND_BLOCKED:
            # Deterministic, no agent turn - see builtin_commands.py for why
            # this exists and how to allow a specific command via .env.
            builtin_env_name = decision.builtin_command_name.upper().replace("-", "_")
            if decision.builtin_command_name == PERSISTENT_APPROVAL_KEY:
                # Persistent approval would write to the gateway's whole config, so it
                # is blocked on a shared bot
                # (builtin_commands.blocked_persistent_approval).
                await self._notify(
                    decision.channel_id,
                    user_text("persistent_approval_blocked"),
                    tmid=decision.thread_id,
                )
                return
            await self._notify(
                decision.channel_id,
                user_text(
                    "builtin_command_blocked",
                    command=decision.builtin_command_name,
                    env_key=f"ROCKETCHAT_ALLOW_BUILTIN_{builtin_env_name}",
                ),
                tmid=decision.thread_id,
            )
            return

        if decision.outcome == HANDLE_SKILL_COMMAND_BLOCKED:
            # Deterministic, no agent turn. A SEPARATE gap from the builtin-command one
            # above (see builtin_commands.py: skill commands are a different Hermes
            # registry).
            skill_env_name = decision.skill_command_name.upper().replace("-", "_")
            await self._notify(
                decision.channel_id,
                user_text(
                    "skill_command_blocked",
                    command=decision.skill_command_name,
                    env_key=f"ROCKETCHAT_ALLOW_SKILL_{skill_env_name}",
                ),
                tmid=decision.thread_id,
            )
            return

        # Fall-through: HANDLE_MESSAGE (a real agent turn).
        #
        # Instant acknowledgement reaction on the user's @mention: visible feedback long
        # before the LLM answer. Fire-and-forget: a failed reaction must never affect
        # the turn. send() removes it when the reply posts (self._pending_ack). Fired
        # HERE, before the deferred context fetch and turn, so the emoji lands promptly
        # however long the fetch takes.
        #
        # Reply reference: the question's data for send() (reply_to = this message id);
        # see reply_reference.py.
        if decision.message_id:
            _shared_reply_targets().remember(ReplyTarget(
                message_id=decision.message_id,
                room_id=decision.channel_id,
                room_name=decision.channel_name,
                room_type=payload_room_type(decision.payload or {}) or "c",
                username=decision.sender_username,
                is_direct=decision.is_direct,
            ))

        if self._cfg.ack_reaction and decision.message_id:
            self._pending_ack.setdefault(decision.channel_id, []).append(decision.message_id)
            asyncio.create_task(
                self._safe_react(decision.message_id, self._cfg.ack_reaction, True)
            )

        # The room-context fetch must not run synchronously here, before this handler
        # returns its 200. On a busy room that delays the webhook response, which delays
        # Rocket.Chat handing the sender's own message back into the room and can trip a
        # webhook retry: a duplicate delivery the inbound dedup then has to absorb. So
        # the 200 goes first and the fetch and turn run in a detached task.
        # handle_message() is fire-and-forget internally; the fetch was the only part
        # that blocked the response. The DDP path has no HTTP response, but the
        # principle (don't block the caller's event loop on a slow context fetch)
        # applies the same; see DdpTransportMixin._ddp_handle_message.
        asyncio.create_task(self._augment_and_handle(decision))

    def _unverified_sender_check(self) -> UnverifiedSender:
        """The condition of the block's ``[unverified sender]`` marker, or ``None``.

        Marks only when there is an allowlist (``ROCKETCHAT_ALLOWED_USERS`` and no ``ROCKETCHAT_ALLOW_ALL_USERS``): the line's
        author is not allowed (the same id/name rule as the inbound filter). Rendering never marks the bot's own line.
        """
        cfg = self._cfg
        if cfg.allow_all_users or not cfg.allowed_users:
            return None

        def _unverified(msg: dict) -> bool:
            author = (msg or {}).get("u") or {}
            return not is_sender_allowed(
                str(author.get("_id") or ""), str(author.get("username") or ""),
                cfg.allowed_users, False,
            )

        return _unverified

    async def _fetch_room_context(
        self,
        decision: InboundDecision,
        *,
        since_previous_mention: bool = True,
        unverified_sender: UnverifiedSender = None,
    ) -> list:
        """The context window's messages (chronological) in the trigger's scope.

        Scope (see history.py): for a thread trigger only the thread, with the root (``chat.getMessage``) as first line and the
        replies from ``chat.getThreadMessages``; otherwise the room's main channel (``channels``/``groups.history``/``dm.history``,
        ``showThreadMessages=false``, ``latest`` = the trigger's time, exclusive). Either way only messages before the trigger
        count, and the trigger also drops out by id.

        With ``since_previous_mention`` (main channel, per-person thread; room_session.py) it first looks for the asker's previous
        mention in the same fetch and scope; if there is one, the window is every message after that boundary (no lower bound or
        root pin, at most ``limit``, with the token budget); if not (or in a shared thread), the window since the bot's last reply.
        Writes one summary log line (numbers only, no text). On an error (the GETs return ``{}``) the window is empty: the turn
        runs without a block.
        """
        cfg = self._cfg
        limit = cfg.history_limit
        ts = (decision.payload or {}).get("timestamp")
        latest_epoch = rc_timestamp_seconds(ts)
        root = None
        if decision.thread_id:
            scope = "thread"
            root = await self._rest.get_message(decision.thread_id)
            replies = await self._rest.thread_messages(
                decision.thread_id, count=min(limit + 1, HISTORY_LIMIT_MAX)
            )
            fetched = ([root] if root else []) + replies
        else:
            scope = "room"
            # +1 without a time limit: the trigger itself may then be on the page.
            fetched = await self._rest.room_history(
                decision.channel_id,
                latest=rc_timestamp(latest_epoch) if latest_epoch is not None else "",
                count=limit if latest_epoch is not None else min(limit + 1, HISTORY_LIMIT_MAX),
                show_thread_messages=False,
            )
        candidates = messages_before(exclude_message(fetched, decision.message_id), latest_epoch)
        identity = dict(bot_user_id=self._user_id, bot_username=self._bot_username)
        mention = -1
        if since_previous_mention:
            mention = find_previous_mention(
                candidates,
                user_id=decision.sender_id,
                username=decision.sender_username,
                tmid=decision.thread_id,
                **identity,
            )
        if mention >= 0:
            boundary = BOUNDARY_PREVIOUS_MENTION
            window = select_pending_window(candidates, mention, limit=limit)
        else:
            boundary = (
                BOUNDARY_LAST_REPLY
                if find_context_boundary(candidates, **identity) >= 0
                else BOUNDARY_NONE
            )
            window = select_context_window(
                candidates, limit=limit, min_messages=cfg.history_min_messages, **identity
            )
            window = pin_thread_root(window, root, limit=limit)
        kept = trim_messages_to_token_budget(
            window, budget=cfg.history_token_budget, unverified_sender=unverified_sender,
            tz=cfg.timezone, **identity
        )
        logger.info(
            "Rocket.Chat: room context for %s (scope=%s): fetched %d, kept %d, "
            "boundary=%s, ~%d tokens (session=%s, limit=%d, min=%d, budget=%s)",
            decision.channel_id,
            scope,
            len(fetched),
            len(kept),
            boundary,
            estimate_tokens(
                build_channel_context(
                    "", kept, unverified_sender=unverified_sender, tz=cfg.timezone, **identity
                )
            ) if kept else 0,
            "per-user" if since_previous_mention else "shared-thread",
            limit,
            cfg.history_min_messages,
            cfg.history_token_budget or "off",
        )
        return kept

    async def _fetch_dm_seed(self, decision: InboundDecision) -> list:
        """The first-contact seed (``ROCKETCHAT_DM_SEED_MESSAGES``).

        A single ``dm.history`` page (``room_history``'s chain, at most 100 messages, before the trigger): if the bot has already
        answered in the fetched history, no seed (the session transcript is the context); otherwise at most the DM's last
        ``dm_seed_messages`` messages, in the room block's format and token budget. One summary log line, numbers only; empty on
        error.
        """
        cfg = self._cfg
        ts = (decision.payload or {}).get("timestamp")
        latest_epoch = rc_timestamp_seconds(ts)
        fetched = await self._rest.room_history(
            decision.channel_id,
            latest=rc_timestamp(latest_epoch) if latest_epoch is not None else "",
            count=HISTORY_LIMIT_MAX,
            show_thread_messages=False,
        )
        candidates = messages_before(exclude_message(fetched, decision.message_id), latest_epoch)
        identity = dict(bot_user_id=self._user_id, bot_username=self._bot_username)
        first_round = not bot_replied_since(candidates, None, **identity)
        kept: list = []
        if first_round:
            kept = select_seed_messages(candidates, count=cfg.dm_seed_messages, **identity)
            kept = trim_messages_to_token_budget(
                kept, budget=cfg.history_token_budget, tz=cfg.timezone, **identity
            )
        logger.info(
            "Rocket.Chat: DM seed for %s: fetched %d, first round=%s, kept %d",
            decision.channel_id,
            len(fetched),
            "yes" if first_round else "no",
            len(kept),
        )
        return kept

    async def _fetch_dm_thread_seed(self, decision: InboundDecision) -> list:
        """A DM thread's first turn (dm_session.py ``select_thread_seed``).

        The same fetch as a room thread: the root from ``chat.getMessage``, the replies from ``chat.getThreadMessages``, only those
        before the trigger. If the bot has written in the thread, no block. The boundary is stateless and computed within the
        fetched page: in a 1:1 DM thread the bot answers every turn, so its own message is on the fresh page. One summary log line,
        numbers only; fewer lines on error (``None`` / ``[]``).
        """
        cfg = self._cfg
        limit = cfg.history_limit
        latest_epoch = rc_timestamp_seconds((decision.payload or {}).get("timestamp"))
        root = await self._rest.get_message(decision.thread_id)
        replies = await self._rest.thread_messages(
            decision.thread_id, count=min(limit + 1, HISTORY_LIMIT_MAX)
        )
        candidates = messages_before(exclude_message(replies, decision.message_id), latest_epoch)
        identity = dict(bot_user_id=self._user_id, bot_username=self._bot_username)
        kept = select_thread_seed(root, candidates, limit=limit, **identity)
        kept = trim_messages_to_token_budget(
            kept, budget=cfg.history_token_budget, tz=cfg.timezone, **identity
        )
        logger.info(
            "Rocket.Chat: DM thread seed for %s: root=%s, replies %d, first round=%s, kept %d",
            decision.channel_id,
            "yes" if root else "no",
            len(replies),
            "no" if bot_replied_since(candidates, None, **identity) else "yes",
            len(kept),
        )
        return kept

    async def _download_trigger_files(self, decision: InboundDecision) -> DownloadedMedia:
        """The mentioning message's files in Hermes's media store (see media.py); an empty :class:`DownloadedMedia` without files.

        The DDP path's payload carries the file descriptions (``files`` key, even with an empty list); in webhook mode, where the
        key is missing, the gateway reads the message with ``chat.getMessage``. The download uses the bot's PAT; the size limit is
        Hermes's ``gateway.max_inbound_media_bytes``. On error the turn goes on without the file (with one log line).
        """
        payload = decision.payload or {}
        if "files" in payload:
            refs = extract_file_refs({"files": payload.get("files") or []})
        elif self._cfg.transport == "webhook" and decision.message_id:
            refs = extract_file_refs(await self._rest.get_message(decision.message_id) or {})
        else:
            refs = []
        if not refs:
            return DownloadedMedia([], [], [], [], "")
        # Public names of ``gateway.platforms.base`` (adding-platform-adapters.md,
        # "Public helpers for platform plugins": "media cache helpers"). A guarded
        # import: should a Hermes release move them, the turn runs on without the file
        # (one ERROR line) and is not lost.
        try:
            from gateway.platforms.base import cache_media_bytes_async, get_inbound_media_max_bytes
        except ImportError as exc:
            logger.error(
                "Rocket.Chat: Hermes media cache helpers not importable (%s) - "
                "%d attachment(s) skipped for room %s", exc, len(refs), decision.channel_id,
            )
            return DownloadedMedia([], [], [], [], "")

        media = await download_attachments(
            refs,
            download=self._rest.get_bytes,
            cache_media=cache_media_bytes_async,
            max_bytes=get_inbound_media_max_bytes(),
        )
        logger.info(
            "Rocket.Chat: %d of %d attachment(s) handed to Hermes for room %s (%s; %d inlined)",
            len(media.paths), len(refs), decision.channel_id, ", ".join(media.kinds) or "none",
            sum(media.text_inlined),
        )
        return media

    async def _augment_and_handle(self, decision: InboundDecision) -> None:
        """Fetch the room context, build the turn, run the agent.

        Split out of ``_handle_outgoing_webhook`` so the webhook 200 does NOT wait on the context fetch. Runs in a detached
        ``asyncio.create_task``: the HTTP response is already sent by the time this starts, so any error here is logged and
        swallowed (there is no caller left to fail back to).

        The context (the room_id line and any history block) goes in ``MessageEvent.channel_context``; ``text`` stays the bare trigger
        (see history.py): Hermes core appends it AFTER command recognition and the ``[username] `` prefix, with a ``[New message]``
        marker. So a forwarded ``/new`` still runs as a command.

        The turn is registered in ``_inflight_augment_rooms`` only while it is being PREPARED: from before the context fetch and the
        file download until ``handle_message()`` hands it to Hermes core. From there on core's own shutdown notice covers it
        (``gateway.shutdown.notice_*``, thread-aware, switchable with ``gateway_restart_notification``), so ``disconnect()`` only fills
        the gap before it.

        Sessions belong to Hermes core (see room_session.py and dm_session.py): ``chat_type="dm"`` in a DM, ``"channel"`` in a room, and
        ``thread_id`` is the real thread. In a room the block runs since the asker's previous mention (since the bot's last reply in a
        shared thread), with ``[unverified sender]`` markers if there is an allowlist; a DM has no block, only the room_id line (and the
        first-contact seed with ``ROCKETCHAT_DM_SEED_MESSAGES > 0``).
        """
        try:
            text = decision.text

            # Sessions belong to Hermes core. The plugin writes no segment:
            # ``chat_type="dm"`` in a DM, ``"channel"`` in a room, ``thread_id`` the
            # real Rocket.Chat thread (or ``None``). Core's documented settings decide
            # (``group_sessions_per_user``: per person in the main channel;
            # ``thread_sessions_per_user``: per person in a thread, otherwise a shared
            # thread session with a ``[name]`` prefix). A new session: ``/new``. The
            # reply's thread is ``thread_id`` itself.
            cfg = self._cfg
            source = self.build_source(
                chat_id=decision.channel_id,
                chat_name=decision.channel_name,
                chat_type="dm" if decision.is_direct else "channel",
                user_id=decision.sender_id,
                user_name=decision.sender_username,
                thread_id=decision.thread_id or None,
                message_id=decision.message_id,
            )
            # Core's documented key (ADDING_A_PLATFORM.md "Key patterns":
            # "_source_session_key … never the free build_session_key()"): it pins the
            # source's identity first and computes in the running profile's namespace,
            # so the key is exactly what core uses for the turn.
            session_key = self._source_session_key(source)
            # During preparation (context, file download) the shutdown notice is ours;
            # after handle_message() it is core's (see the docstring). The entry is
            # removed right after handle_message().
            self._inflight_augment_rooms[session_key] = (decision.channel_id, decision.thread_id)

            # The block's boundary (room_session.py): the asker's previous mention in
            # the main channel and a per-person thread (what came before is in their
            # session), the bot's last reply in a shared thread.
            shared_thread = bool(decision.thread_id) and not self.config.extra.get(
                "thread_sessions_per_user", False
            )
            # `[unverified sender]` in the room block (a 1:1 DM has no other sender).
            unverified_sender = None if decision.is_direct else self._unverified_sender_check()
            messages: list = []
            try:
                if should_augment_with_context(text):
                    if decision.is_direct:
                        if decision.thread_id:
                            if cfg.history_limit > 0:
                                messages = await self._fetch_dm_thread_seed(decision)
                        elif cfg.dm_seed_messages > 0:
                            messages = await self._fetch_dm_seed(decision)
                    elif cfg.history_limit > 0:
                        messages = await self._fetch_room_context(
                            decision,
                            since_previous_mention=not shared_thread,
                            unverified_sender=unverified_sender,
                        )
            except Exception as exc:  # noqa: BLE001 - the context is a nice-to-have
                # On an error (a timeout or a non-JSON answer on a GET that the REST
                # client does not swallow) the request goes without a block and does not
                # stay unanswered.
                messages = []
                logger.warning(
                    "Rocket.Chat: room context fetch failed for room %s: %s",
                    decision.channel_id, exc,
                )
            channel_context = build_channel_context(
                decision.channel_id,
                messages,
                bot_user_id=self._user_id,
                bot_username=self._bot_username,
                unverified_sender=unverified_sender,
                tz=cfg.timezone,
                # Asker attestation (asker.py): the trigger's sender and room, for a
                # Rocket.Chat history MCP server.
                asker=issue_asker(cfg.asker_key, decision.sender_id, decision.channel_id, time.time()),
            )

            media = await self._download_trigger_files(decision)
            event_text = text
            if media.injected_text:
                # The pattern of Hermes's Discord adapter: an inserted document goes IN
                # FRONT of the request (see media.py).
                event_text = f"{media.injected_text}\n\n{text}" if text else media.injected_text
            event = MessageEvent(
                text=event_text,
                message_type=getattr(MessageType, message_type_name(media.kinds)),
                source=source,
                raw_message=decision.payload,
                message_id=decision.message_id,
                media_urls=media.paths,
                media_types=media.media_types,
                media_text_inlined=media.text_inlined,
                channel_context=channel_context or None,
            )

            # handle_message() is fire-and-forget internally (spawns
            # background processing and returns before the agent turn
            # finishes).
            await self.handle_message(event)
        except Exception:
            logger.exception(
                "Rocket.Chat: background context/turn processing failed for room %s",
                decision.channel_id,
            )
        finally:
            # `session_key` only exists once source-building succeeded; an
            # earlier failure never registered anything. From here on the
            # turn is Hermes core's (or it failed): either way, ours is done.
            if "session_key" in locals():
                self._inflight_augment_rooms.pop(session_key, None)
