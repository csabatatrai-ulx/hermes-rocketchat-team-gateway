"""Deciding what to do with an incoming Rocket.Chat message.

Both inbound transports end here: the Outgoing Webhook body, and the DDP
path's synthetic payload of the same shape
(``ddp_transport_mixin.ddp_message_to_webhook_payload``). The decision is
pure (no I/O), so it is testable without a real Rocket.Chat - and it is the
part that has produced the most live bugs:

* **The mention match must be bounded.** A plain substring match lets
  ``@Hermes-Beta`` also match the bot ``Hermes`` (its username is a prefix),
  so BOTH bots answered the same message. The negative lookahead is the fix,
  and it has tests instead of a comment.
* **Repeat deliveries are real.** The same ``message_id`` arrives more than
  once (a DDP ``changed`` event after a reaction/edit/thread reply, the
  reconnect catch-up, duplicate Outgoing-Webhook triggers), which without
  the dedup step means several agent turns for one user message.
* **The ORDER of the checks is load-bearing**, and easy to break by accident.
  The secret is verified before anything else is trusted; the self-filter
  runs first; a typed system message (``message_type``, the message's ``t``:
  a call start, a join, a rename) never starts a turn; the age guard then drops
  anything older than the dedup retention (a Rocket.Chat redelivery of a
  days-old message must never reach the agent, even after the cache has
  forgotten its id); the ignored-sender filter (``ROCKETCHAT_IGNORE_SENDERS``,
  the other bots: no bot-to-bot reply loop) runs after the mention/DM filter
  and before the allowlist; the dedup runs only AFTER the mention/DM,
  ignored-sender and allowlist filters (so ordinary room chatter, an ignored
  bot or a refused sender cannot push an old @mention's id out of the cache),
  but still before any command or agent turn; and every built-in command below
  is matched only after the mention has been stripped.
* **A one-to-one DM needs no mention; a group DM is a room.** Rocket.Chat
  stores a private conversation of more than two people as a ``t='d'`` room
  too. The caller looks that up (``rooms.info``, see dm_session.py) and
  passes ``group_direct=True``: then the room rules apply (mention required,
  fresh session, room context). ``InboundDecision.is_direct`` is true only
  for a one-to-one DM.
* **A thread reply carries its thread.** The payload's ``tmid`` (Outgoing
  Webhook and DDP alike) becomes ``InboundDecision.thread_id``: the room
  context is then read from that thread only, and the reply goes back into
  it (see history.py).

Everything here is a pure function over the parsed payload, with the one
side-effecting step (the dedup cache) injected as a callable.
"""

from __future__ import annotations

import hmac
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Pattern

# What the caller should do with a delivery. Grouped by the HTTP status they
# map to, because Rocket.Chat's own behaviour differs: a 4xx makes it log an
# error, a 200 does not.
REJECT_BAD_BODY = "reject_bad_body"      # 400 — unparseable
REJECT_BAD_TOKEN = "reject_bad_token"    # 401 — wrong/missing shared secret
IGNORE_SELF = "ignore_self"              # 200 — our own bot's message
IGNORE_SYSTEM_MESSAGE = "ignore_system_message"  # 200 — a typed Rocket.Chat system message (``t``), e.g. a call start
IGNORE_DUPLICATE = "ignore_duplicate"    # 200 — same message_id seen already
IGNORE_STALE = "ignore_stale"            # 200 — older than the dedup retention window
IGNORE_NO_MENTION = "ignore_no_mention"  # 200 — not addressed to this bot
IGNORE_NOT_ALLOWED = "ignore_not_allowed"  # 200 — sender not on the allowlist
IGNORE_SENDER = "ignore_sender"          # 200 — sender on ROCKETCHAT_IGNORE_SENDERS (e.g. another bot)
HANDLE_DM_ONLY_NOTICE = "handle_dm_only_notice"  # 200 — ROCKETCHAT_DM_ONLY: a room mention, no agent turn
HANDLE_WHOAMI = "handle_whoami"          # 200 — identity-resolution report, no agent turn
HANDLE_BUILTIN_COMMAND_BLOCKED = "handle_builtin_command_blocked"  # 200 — recognised Hermes command, not on the allowlist, no agent turn
HANDLE_SKILL_COMMAND_BLOCKED = "handle_skill_command_blocked"  # 200 — recognised Hermes skill, not on the allowlist, no agent turn
HANDLE_MESSAGE = "handle_message"        # 200 — run an agent turn

# The short machine-readable reason strings sent back in the HTTP body. Kept
# next to the outcomes so the two can't drift apart.
_REASONS = {
    REJECT_BAD_BODY: "cannot parse body",
    REJECT_BAD_TOKEN: "invalid token",
    IGNORE_SELF: "self",
    IGNORE_SYSTEM_MESSAGE: "system message",
    IGNORE_DUPLICATE: "duplicate",
    IGNORE_STALE: "stale",
    IGNORE_NO_MENTION: "no mention",
    IGNORE_NOT_ALLOWED: "not allowed",
    IGNORE_SENDER: "ignored sender",
    HANDLE_DM_ONLY_NOTICE: "dm_only",
    HANDLE_WHOAMI: "whoami",
    HANDLE_BUILTIN_COMMAND_BLOCKED: "builtin_command_blocked",
    HANDLE_SKILL_COMMAND_BLOCKED: "skill_command_blocked",
    HANDLE_MESSAGE: "accepted",
}

_HTTP_STATUS = {REJECT_BAD_BODY: 400, REJECT_BAD_TOKEN: 401}


@dataclass(frozen=True)
class InboundDecision:
    """What to do, plus everything the caller needs to do it."""

    outcome: str
    text: str = ""
    message_id: str = ""
    channel_id: str = ""
    channel_name: str = ""
    sender_id: str = ""
    sender_username: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    # The thread root's id (the payload's ``tmid``) if the trigger is a thread reply,
    # otherwise "". This decides the context's scope (only the thread) and where the
    # reply goes (``chat.postMessage`` ``tmid``).
    thread_id: str = ""
    # Whether this is a one-to-one DM (see dm_session.py): a ``t='d'`` room with no more
    # than two participants (``classify_inbound`` ``group_direct``). This decides the
    # ``chat_type`` (``"dm"``) and the log line of a refused DM.
    is_direct: bool = False
    # Populated only for HANDLE_BUILTIN_COMMAND_BLOCKED - the recognised
    # Hermes command word (without the leading "/"), so the caller can name
    # it in the reply without re-parsing ``text``.
    builtin_command_name: str = ""
    # Populated only for HANDLE_SKILL_COMMAND_BLOCKED - the recognised
    # Hermes skill slug (without the leading "/"), same reasoning as
    # builtin_command_name above but for the separate skill-command gap
    # (see builtin_commands.py's module docstring).
    skill_command_name: str = ""

    @property
    def reason(self) -> str:
        return _REASONS.get(self.outcome, self.outcome)

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS.get(self.outcome, 200)

    @property
    def should_handle(self) -> bool:
        return self.outcome in (
            HANDLE_DM_ONLY_NOTICE,
            HANDLE_WHOAMI,
            HANDLE_BUILTIN_COMMAND_BLOCKED,
            HANDLE_SKILL_COMMAND_BLOCKED,
            HANDLE_MESSAGE,
        )


def payload_room_type(payload: Dict[str, Any]) -> str:
    """The Rocket.Chat room type of an inbound payload, or ``""``.

    DDP path: always carries an explicit ``room_type`` key (from the bot's
    own subscription list). Outgoing Webhook path: RC sends no room type, but
    a direct message is the only room kind WITHOUT a name, so the webhook
    payload has no ``channel_name`` exactly for DMs (verified live against
    RC 8.7: the key is absent) - that is how the ``@<bot>`` DM integration
    is recognised.
    """
    if "room_type" in payload:
        return str(payload.get("room_type") or "")
    return "" if payload.get("channel_name") else "d"


def rc_timestamp_seconds(ts: Any) -> Optional[float]:
    """A Rocket.Chat timestamp in epoch seconds, or ``None``.

    Accepts both shapes: DDP/EJSON ``{"$date": <ms>}`` and the REST/Outgoing Webhook ISO-8601 string
    (``"2026-10-05T08:00:00.000Z"``). A missing or unparseable value -> ``None`` (the caller then does
    NOT drop the message: a format change must not silence the bot).
    """
    if isinstance(ts, dict):
        ts = ts.get("$date")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return ts / 1000.0
    if isinstance(ts, str) and ts.strip():
        try:
            moment = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:  # Rocket.Chat sends UTC ("Z")
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def rc_timestamp(epoch_seconds: float) -> str:
    """Rocket.Chat REST ``oldest``/``latest`` format (ISO-8601 UTC, ms).

    The DDP catch-up and the context fetch's ``latest`` limit pass this too (from the trigger's ``ts``,
    see :func:`rc_timestamp_seconds`).
    """
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def is_stale_message(
    ts: Any, *, now: float, max_age_seconds: Optional[float]
) -> bool:
    """True if the message's Rocket.Chat timestamp is older than ``max_age_seconds``.

    An unknown or missing timestamp, or a disabled (``None``/``<=0``) limit, gives False: when in doubt
    the message passes.
    """
    if not max_age_seconds or max_age_seconds <= 0:
        return False
    sent_at = rc_timestamp_seconds(ts)
    if sent_at is None:
        return False
    return now - sent_at > max_age_seconds


def build_mention_pattern(bot_username: str) -> Pattern[str]:
    """A bounded, case-insensitive ``@username`` matcher.

    The negative lookahead is the entire point: without it the pattern for bot
    ``Hermes`` also matches the text ``@Hermes-Beta``, because the username
    is a prefix of the other bot's — and then both bots answer the same
    message. Excluded continuation characters are the ones Rocket.Chat allows
    in a username (letters, digits, underscore, hyphen, dot).
    """
    return re.compile(
        re.escape(f"@{bot_username}") + r"(?![A-Za-z0-9_.\-])", re.IGNORECASE
    )


def has_mention(text: str, pattern: Pattern[str]) -> bool:
    return bool(text and pattern.search(text))


def strip_mention(text: str, pattern: Pattern[str]) -> str:
    """Remove the bot's mention and surrounding whitespace from the message."""
    return pattern.sub("", text or "").strip()


def parse_webhook_body(raw_body: bytes) -> Optional[Dict[str, Any]]:
    """Parse a webhook body, JSON first, form-encoded second.

    Rocket.Chat sends JSON, but a form-encoded body is accepted as a fallback
    so a hand-rolled `curl` test (or a future Rocket.Chat change) does not look
    like a broken integration. Returns ``None`` when neither works.
    """
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        try:
            parsed = dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
        except Exception:
            return None
        # An EMPTY result from the form-encoded fallback means the body was not
        # form-encoded either (``parse_qsl`` never fails, it just yields nothing), so an
        # empty or junk body must not be mistaken for a valid empty payload. A JSON body
        # of literally "{}" is different: that parsed, so it is returned as-is.
        if not parsed:
            return None
    return parsed if isinstance(parsed, dict) else None


def secret_matches(payload_token: Any, expected_secret: str) -> bool:
    """Constant-time comparison of the Outgoing Webhook's shared secret.

    An unset ``expected_secret`` accepts everything. That is reachable only on the DDP path (where
    ``ddp_transport_mixin`` sets the payload's ``token`` itself and the secret carries no
    authentication): ``transport=webhook`` with an empty secret does not even start (fail closed, see
    ``config.py`` ``missing_required``). ``compare_digest`` rather than ``==`` so the check does not
    leak the secret through timing.
    """
    if not expected_secret:
        return True
    return hmac.compare_digest(str(payload_token or ""), expected_secret)


def is_own_message(payload: Dict[str, Any], bot_user_id: str, bot_username: str) -> bool:
    """Whether this delivery is the bot's own post (would cause a reply loop).

    Three independent signals, because any one of them can be missing from a
    given payload: Rocket.Chat's ``bot`` flag, the user id, and the username.
    """
    if payload.get("bot"):
        return True
    sender_id = str(payload.get("user_id", "") or "")
    sender_username = str(payload.get("user_name", "") or "")
    if bot_user_id and sender_id == bot_user_id:
        return True
    return bool(bot_username) and sender_username.lower() == bot_username.lower()


def is_ignored_sender(
    sender_id: str, sender_username: str, ignore_senders: Optional[List[str]]
) -> bool:
    """Whether the sender is on the ``ROCKETCHAT_IGNORE_SENDERS`` list.

    An item is a user id (exact match) or a username (case-insensitive). Empty list = nobody.
    """
    if not ignore_senders:
        return False
    uname = (sender_username or "").lower()
    for entry in ignore_senders:
        if sender_id and entry == sender_id:
            return True
        if uname and entry.lower() == uname:
            return True
    return False


def is_sender_allowed(
    sender_id: str,
    sender_username: str,
    allowed_users: Optional[List[str]],
    allow_all_users: bool,
) -> bool:
    """Allowlist check; an EMPTY allowlist allows everyone.

    That is deliberate and matches the PoC default: the gate is opt-in. Both
    the id and the username are accepted so an operator can write whichever is
    to hand.
    """
    if allow_all_users or not allowed_users:
        return True
    return sender_id in allowed_users or sender_username in allowed_users


def classify_inbound(
    raw_body: bytes,
    *,
    bot_username: str,
    bot_user_id: str,
    webhook_secret: str = "",
    require_mention: bool = True,
    allowed_users: Optional[List[str]] = None,
    allow_all_users: bool = False,
    ignore_senders: Optional[List[str]] = None,
    group_direct: bool = False,
    dm_only: bool = False,
    whoami_enabled: bool = True,
    whoami_command: str = "/whoami",
    is_duplicate: Optional[Callable[[str], bool]] = None,
    is_whoami_command: Optional[Callable[[str, str], bool]] = None,
    blocked_builtin_command: Optional[Callable[[str], str]] = None,
    blocked_skill_command: Optional[Callable[[str], str]] = None,
    max_message_age_seconds: Optional[float] = None,
    now_fn: Callable[[], float] = time.time,
) -> InboundDecision:
    """Decide what a delivery means. See the module docstring on ordering.

    ``is_duplicate`` receives the already-namespaced cache key and is the only
    side-effecting step (it records what it is asked about).
    ``max_message_age_seconds`` (the inbound dedup retention) enables the age
    guard on the payload's ``timestamp``; ``now_fn`` is injectable for tests.
    ``group_direct``: the caller found the payload's ``t='d'`` room to have
    more than two participants (``rooms.info``) - it is then handled as a room.
    ``dm_only`` (``ROCKETCHAT_DM_ONLY``): a room delivery that passed the
    mention and ignored-sender filters becomes :data:`HANDLE_DM_ONLY_NOTICE`
    (no agent turn, no dedup entry), whoever the sender is.
    """
    payload = parse_webhook_body(raw_body)
    if payload is None:
        return InboundDecision(REJECT_BAD_BODY)

    if not secret_matches(payload.get("token"), webhook_secret):
        return InboundDecision(REJECT_BAD_TOKEN, payload=payload)

    sender_id = str(payload.get("user_id", "") or "")
    sender_username = str(payload.get("user_name", "") or "")
    message_id = str(payload.get("message_id", "") or "")
    channel_id = str(payload.get("channel_id", "") or "")
    channel_name = str(payload.get("channel_name", "") or "")
    thread_id = str(payload.get("tmid", "") or "")
    # A DM is addressed to the bot by definition - no @mention needed there
    # (the usual chat-bot convention). See payload_room_type for how both
    # transports tell a DM apart; a group DM counts as a room.
    is_direct = payload_room_type(payload) == "d" and not group_direct

    def decision(
        outcome: str,
        text: str = "",
        *,
        builtin_command_name: str = "",
        skill_command_name: str = "",
    ) -> InboundDecision:
        return InboundDecision(
            outcome,
            text=text,
            message_id=message_id,
            channel_id=channel_id,
            channel_name=channel_name,
            sender_id=sender_id,
            sender_username=sender_username,
            payload=payload,
            thread_id=thread_id,
            is_direct=is_direct,
            builtin_command_name=builtin_command_name,
            skill_command_name=skill_command_name,
        )

    # Before the dedup, so the bot's own posts never occupy cache entries.
    if is_own_message(payload, bot_user_id, bot_username):
        return decision(IGNORE_SELF)

    # A system message: a message with a ``t`` field (``videoconf`` call start, ``uj``
    # join, ``r`` rename) is not chat and starts no turn; the room context skips it the
    # same way (history.is_system_event). The type stays in the decision's payload, so a
    # later type-specific handler can hook in at this one place.
    if str(payload.get("message_type", "") or ""):
        return decision(IGNORE_SYSTEM_MESSAGE)

    # Age limit: a message older than the retention window never reaches the agent.
    # Rocket.Chat re-sends even a days-old message with the same id (reaction, edit,
    # pin, thread reply, reconnect catch-up), and the dedup cache may have forgotten it.
    if is_stale_message(
        payload.get("timestamp"), now=now_fn(), max_age_seconds=max_message_age_seconds
    ):
        return decision(IGNORE_STALE)

    raw_text = str(payload.get("text", "") or "")
    pattern = build_mention_pattern(bot_username)
    mentioned = has_mention(raw_text, pattern)

    if require_mention and not mentioned and not is_direct:
        return decision(IGNORE_NO_MENTION)

    # A listed sender's message (another bot) never starts a turn, in rooms or DMs, so a
    # DM between two bots cannot spin into an endless reply loop (messages gateways send
    # over REST carry no Rocket.Chat ``bot`` flag). After the mention filter so such a
    # bot's room chatter is not logged; before the allowlist and the dedup, so it uses
    # no dedup entry.
    if is_ignored_sender(sender_id, sender_username, ignore_senders):
        return decision(IGNORE_SENDER)

    # ROCKETCHAT_DM_ONLY: never a turn in a room (group DMs included). BEFORE the
    # allowlist: the owner gets this answer too when mentioning the bot in a room;
    # before the dedup, since no turn runs. After the mention filter, so room chatter
    # causes no notice.
    if dm_only and not is_direct:
        return decision(HANDLE_DM_ONLY_NOTICE)

    if not is_sender_allowed(sender_id, sender_username, allowed_users, allow_all_users):
        return decision(IGNORE_NOT_ALLOWED)

    # Only a message that passed the filters (addressed to the bot, from an allowed
    # sender) enters the cache; room chatter does not use it up (see "ORDER" in the
    # module docstring). Still BEFORE every command and agent turn, so a repeat delivery
    # runs nothing again.
    if is_duplicate is not None and message_id:
        if is_duplicate(f"{bot_username}:{message_id}"):
            return decision(IGNORE_DUPLICATE)

    text = strip_mention(raw_text, pattern) if mentioned else raw_text.strip()

    # Only after stripping the mention: the user typed "@Bot /whoami", so the
    # command is only recognisable once the mention is gone. The resolved identity comes from
    # `decision.sender_id`/`sender_username`, already carried on every
    # InboundDecision - no extra field needed here.
    if whoami_enabled and channel_id and is_whoami_command is not None:
        if is_whoami_command(text, whoami_command):
            return decision(HANDLE_WHOAMI, text=text)

    # LAST check before the HANDLE_MESSAGE fallback, deliberately: this is the ONLY
    # thing standing between an arbitrary Rocket.Chat sender and Hermes's privileged
    # commands (/model, /yolo, /restart, ...), since Hermes's admin-vs-user gating is
    # off unless configured in config.yaml; see builtin_commands.py.
    # ``blocked_builtin_command`` not being injected means this check is skipped
    # entirely, but a caller that DOES inject it is expected to fail closed for anything
    # not on its allowlist.
    if blocked_builtin_command is not None:
        command_name = blocked_builtin_command(text)
        if command_name:
            return decision(
                HANDLE_BUILTIN_COMMAND_BLOCKED,
                text=text,
                builtin_command_name=command_name,
            )

    # SAME reasoning/position as the builtin-command check above, but for
    # the SEPARATE skill-command registry (see builtin_commands.py's module
    # docstring: skill commands are dispatched via a completely different
    # mechanism that Hermes' own admin gate never checks either, so this is
    # not redundant with the check above - a skill is not in Hermes'
    # COMMAND_REGISTRY, so the check above lets it through to here).
    if blocked_skill_command is not None:
        skill_name = blocked_skill_command(text)
        if skill_name:
            return decision(
                HANDLE_SKILL_COMMAND_BLOCKED,
                text=text,
                skill_command_name=skill_name,
            )

    return decision(HANDLE_MESSAGE, text=text)
