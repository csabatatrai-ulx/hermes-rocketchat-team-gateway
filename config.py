"""Reading this integration's configuration, in one place.

Every knob arrives as a string from one of two sources: the harness's
per-platform ``extra`` mapping, or an environment variable. Two properties make
this worth its own module:

* **Testable without the environment.** ``load_config()`` takes the mappings
  as arguments, so precedence and coercion can be asserted against plain dicts.
* **One place to see the whole surface.** The knobs are documented in
  ``docs/configuration.md`` and ``plugin.yaml``; a single dataclass makes it
  obvious when those drift apart.

Precedence is environment → ``extra`` → default, and an EMPTY value counts as
absent. Env over YAML is Hermes's documented rule for platform settings
(``gateway/platforms/ADDING_A_PLATFORM.md``: "env > YAML"). The empty rule is
deliberate: an ``.env`` may hold ``ROCKETCHAT_HOME_CHANNEL=`` (empty) to mean "no
greeting room", and a blank string must not be mistaken for a deliberate choice
of some other default.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

try:  # loaded as part of the plugin package (the normal runtime path)
    from .reasoning_format import (
        DEFAULT_REASONING_LEAD_PATTERN,
        DEFAULT_REASONING_LEAK_LABEL,
        DEFAULT_REASONING_LEAK_MODE,
    )
    from .history import (
        DEFAULT_HISTORY_LIMIT,
        DEFAULT_HISTORY_MIN_MESSAGES,
        DEFAULT_HISTORY_TOKEN_BUDGET,
        HISTORY_LIMIT_MAX,
    )
    from .builtin_commands import DEFAULT_ALLOWED_BUILTIN_COMMANDS, allowed_builtin_commands
    from .whoami import DEFAULT_WHOAMI_COMMAND
    from .dedup import DEFAULT_INBOUND_DEDUP_RETENTION_HOURS
    from .dm_session import DEFAULT_DM_SEED_MESSAGES
except ImportError:  # imported as a bare module by the unit tests
    from reasoning_format import (
        DEFAULT_REASONING_LEAD_PATTERN,
        DEFAULT_REASONING_LEAK_LABEL,
        DEFAULT_REASONING_LEAK_MODE,
    )
    from history import (
        DEFAULT_HISTORY_LIMIT,
        DEFAULT_HISTORY_MIN_MESSAGES,
        DEFAULT_HISTORY_TOKEN_BUDGET,
        HISTORY_LIMIT_MAX,
    )
    from builtin_commands import DEFAULT_ALLOWED_BUILTIN_COMMANDS, allowed_builtin_commands
    from whoami import DEFAULT_WHOAMI_COMMAND
    from dedup import DEFAULT_INBOUND_DEDUP_RETENTION_HOURS
    from dm_session import DEFAULT_DM_SEED_MESSAGES

MAX_MESSAGE_LENGTH = 4000
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8645
DEFAULT_OUTBOUND_DEDUP_TTL_SECONDS = 10.0
# Name of the subdirectory for restart-surviving dedup state (see dedup.py and
# shared_state.py).
DEFAULT_DEDUP_STATE_DIR_NAME = "rocketchat-plugin-dedup"


def _is_set(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


# Recognised on/off values (case and surrounding space do not matter). Anything in
# neither set (a typo like "flase") gets the switch's OWN default with a warning, so
# that a typo can never flip a switch in either direction:
# ROCKETCHAT_ALLOW_ALL_USERS=off must never mean "on".
TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off"})

# The env var names, paired with the `extra` keys they can be overridden by.
# The naming convention, applied by ``_pick``: ROCKETCHAT_FOO_BAR <->
# extra["foo_bar"].
_ENV_PREFIX = "ROCKETCHAT_"


def _pick(
    key: str,
    extra: Mapping[str, Any],
    env: Mapping[str, str],
    default: str = "",
) -> str:
    """First non-empty of ``ROCKETCHAT_<KEY>``, ``extra[key]``, ``default``.

        Env over YAML, Hermes's documented rule (see the module docstring).
    """
    for candidate in (env.get(_ENV_PREFIX + key.upper(), ""), extra.get(key, "")):
        text = "" if candidate is None else str(candidate)
        if text != "":
            return text
    return default


def as_bool(raw: str, *, default: bool, name: str = "") -> bool:
    """Coerce a configuration string to a flag.

    Empty or missing -> ``default``; ``TRUE_VALUES`` -> True; ``FALSE_VALUES`` ->
    False; anything else -> ``default`` plus a warning naming the key (``name``) and
    the rejected value (see the comment above ``TRUE_VALUES``).
    """
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    logging.getLogger(__name__).warning(
        "Rocket.Chat: %s=%r is not a recognised boolean (expected one of %s / %s), "
        "using the default (%s)",
        name or "flag", raw, "|".join(sorted(TRUE_VALUES)), "|".join(sorted(FALSE_VALUES)),
        "true" if default else "false",
    )
    return default


def _flag(key: str, extra: Mapping[str, Any], env: Mapping[str, str], default: bool) -> bool:
    """``as_bool(_pick(key, ...))``, with ``ROCKETCHAT_<KEY>`` named in the warning."""
    return as_bool(_pick(key, extra, env), default=default, name=_ENV_PREFIX + key.upper())


def _choice(
    key: str, extra: Mapping[str, Any], env: Mapping[str, str], allowed: Tuple[str, ...], default: str
) -> str:
    """An enumerated value (lower-cased); empty -> the default, unknown -> the
        default with a warning (like ``as_bool``).
    """
    raw = _pick(key, extra, env)
    value = raw.strip().lower()
    if not value:
        return default
    if value in allowed:
        return value
    logging.getLogger(__name__).warning(
        "Rocket.Chat: %s=%r is not one of %s, using %s",
        _ENV_PREFIX + key.upper(), raw, "|".join(allowed), default,
    )
    return default


def as_int(raw: str, *, default: int) -> int:
    """Coerce to int, falling back to ``default`` on anything unparseable.

    A typo in one numeric env var should not stop the whole integration from
    starting - the feature it belongs to degrades to its default instead.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def as_float(raw: str, *, default: float) -> float:
    """Coerce to float, falling back to ``default`` (same reasoning as ``as_int``)."""
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _normalize_history_limit(raw: str) -> int:
    """Upper bound of the room-context window: 0 = off, at most 100.

    Unparseable or negative -> the default (50), above 100 -> 100; both with a warning
    (a typo must not silently switch the feature off).
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_HISTORY_LIMIT
    value = as_int(text, default=-1)
    if value < 0:
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_HISTORY_LIMIT=%r is not a non-negative integer, "
            "using the default (%d)", raw, DEFAULT_HISTORY_LIMIT,
        )
        return DEFAULT_HISTORY_LIMIT
    if value > HISTORY_LIMIT_MAX:
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_HISTORY_LIMIT=%r is above the maximum, using %d",
            raw, HISTORY_LIMIT_MAX,
        )
        return HISTORY_LIMIT_MAX
    return value


def _normalize_history_min_messages(raw: str, limit: int) -> int:
    """Lower bound of the window: unparseable or negative -> 20, and at most ``limit``."""
    text = str(raw or "").strip()
    value = as_int(text, default=-1) if text else DEFAULT_HISTORY_MIN_MESSAGES
    if value < 0:
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_HISTORY_MIN_MESSAGES=%r is not a non-negative "
            "integer, using the default (%d)", raw, DEFAULT_HISTORY_MIN_MESSAGES,
        )
        value = DEFAULT_HISTORY_MIN_MESSAGES
    return min(value, limit)


def _normalize_timezone(raw: str) -> Optional[tzinfo]:
    """The time zone of room-context timestamps.

    An IANA name (``Europe/Berlin``); empty -> ``None``, meaning the gateway process's
    local zone. An unknown or invalid name -> also the local zone, with a warning.

    Lines carry their UTC offset because a model otherwise reads a bare UTC time as
    local time.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, OSError
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_TIMEZONE=%r is not a valid IANA time zone, "
            "using the local time zone", raw,
        )
        return None


def _normalize_dm_seed_messages(raw: str) -> int:
    """First-contact seed size in a DM (messages): 0 = none, at most 100.

    Unparseable or negative -> 0, above 100 -> 100; both with a warning (like
    ``_normalize_history_limit``).
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_DM_SEED_MESSAGES
    value = as_int(text, default=-1)
    if value < 0:
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_DM_SEED_MESSAGES=%r is not a non-negative integer, "
            "using the default (%d)", raw, DEFAULT_DM_SEED_MESSAGES,
        )
        return DEFAULT_DM_SEED_MESSAGES
    if value > HISTORY_LIMIT_MAX:
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_DM_SEED_MESSAGES=%r is above the maximum, using %d",
            raw, HISTORY_LIMIT_MAX,
        )
        return HISTORY_LIMIT_MAX
    return value


_VALID_TRANSPORTS = ("webhook", "ddp")


def _normalize_transport(raw: str) -> str:
    """Fall back to ``"ddp"`` for anything unrecognised, with a warning."""
    value = (raw or "ddp").strip().lower()
    if value not in _VALID_TRANSPORTS:
        logging.getLogger(__name__).warning(
            "Rocket.Chat: ROCKETCHAT_TRANSPORT=%r not in %s, falling back to "
            "'ddp'", raw, _VALID_TRANSPORTS,
        )
        return "ddp"
    return value


DEFAULT_WS_HEARTBEAT_SECONDS = 30
_MIN_WS_HEARTBEAT_SECONDS = 5
_MAX_WS_HEARTBEAT_SECONDS = 300


def _normalize_ws_heartbeat_seconds(raw: str) -> int:
    """5-300 s, default 30 - only the literal "0" disables the guard; an
    out-of-range or invalid value falls back to 30 (no clamping, no warning).

    A DDP WebSocket opened with no keepalive (``heartbeat=None``) raises
    nothing when the connection goes half-open: the read loop blocks
    forever, the reconnect loop in ``_ddp_connect_and_listen`` never fires,
    and inbound messages stop PERMANENTLY while outbound REST keeps working
    and the process looks healthy - no logged error either (confirmed
    upstream: HalfbitStudio/hermes-plugin-rocketchat commit 989a04c, "arm
    WebSocket keepalive to end silent inbound death"). Invalid/out-of-range
    values keep the safe default rather than silently disabling the guard -
    only an explicit "0" opts out.
    """
    text = str(raw or "").strip()
    if text == "0":
        return 0
    value = as_int(text, default=DEFAULT_WS_HEARTBEAT_SECONDS)
    if value < _MIN_WS_HEARTBEAT_SECONDS or value > _MAX_WS_HEARTBEAT_SECONDS:
        return DEFAULT_WS_HEARTBEAT_SECONDS
    return value


# DDP catch-up after a reconnect (see ddp_transport_mixin._ddp_catch_up):
# at most this many seconds of the outage are replayed from REST history, so
# a long outage does not make the bot answer hours-old mentions. 0 disables.
DEFAULT_DDP_CATCHUP_MAX_SECONDS = 600

# On a fresh start: read back what arrived while the process was down, from the last
# seen message (durable cursor, ``ddp_cursor.json`` in the dedup folder), at most this
# many seconds back; 0 disables it. See ddp_transport_mixin ``_ddp_catch_up``.
DEFAULT_STARTUP_CATCHUP_MAX_SECONDS = 600
# What to do with read-back messages addressed to the bot:
#
# ``notice`` (default) does not process them; one note per room or thread asks the
# sender to resend (no late answers, no repeated tool calls).
#
# ``replay`` hands at most ``startup_catchup_max_dispatches`` to Hermes along the same
# path as a live message, with a note for the rest (the pattern of Hermes's Discord
# ``missed_message_backfill``, also opt-in there).
#
# ``off`` does nothing.
STARTUP_CATCHUP_MODES = ("notice", "replay", "off")
DEFAULT_STARTUP_CATCHUP_MODE = "notice"
# In ``replay`` mode one start launches at most this many turns (the default of Hermes's
# Discord ``max_dispatches``).
DEFAULT_STARTUP_CATCHUP_MAX_DISPATCHES = 10



def as_list(raw: str) -> List[str]:
    """Split a comma-separated setting, dropping blanks and surrounding space."""
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _norm_reaction(raw: str) -> str:
    """An emoji reaction shortname with colons/space stripped; "" (disabled)
    for a blank/``none``/``off`` setting."""
    name = str(raw or "").strip().strip(":").strip()
    return "" if name.lower() in ("", "none", "off", "false", "0") else name


def _positive_float(key: str, raw: str, default: float) -> float:
    """A positive number, otherwise (missing, unparseable, ``<= 0``) ``default``.

    An invalid value gets a warning (a missing one does not).
    """
    text = str(raw or "").strip()
    if not text:
        return default
    value = as_float(text, default=-1.0)
    if value <= 0 or value != value or value == float("inf"):
        logging.getLogger(__name__).warning(
            "Rocket.Chat: %s%s=%r must be a positive number, using the default (%s)",
            _ENV_PREFIX, key.upper(), raw, default,
        )
        return default
    return value


_DEDUP_STATE_DIR_OFF_VALUES = ("none", "off", "false", "0", "disabled")


def _resolve_hermes_state_dir(env: Mapping[str, str]) -> str:
    """The gateway's own state root (``HERMES_HOME``, default ``~/.hermes``).

    Each Hermes PROFILE sets its own ``HERMES_HOME`` (``~/.hermes/profiles/<name>``),
    so reading it here (rather than hardcoding ``~/.hermes``) keeps the persisted
    dedup state inside the profile that owns it: two profiles never share or clobber
    one file.
    """
    home = str(env.get("HERMES_HOME") or "").strip()
    return os.path.expanduser(home) if home else os.path.expanduser("~/.hermes")


def _resolve_dedup_state_dir(raw: str, env: Mapping[str, str]) -> str:
    """The restart-surviving dedup persistence directory, or ``""`` (disabled).

    Unlike every other setting in this module, an UNSET/EMPTY value here does NOT
    mean "disabled"; it means "use the computed default" (HERMES_HOME +
    ``DEFAULT_DEDUP_STATE_DIR_NAME``). Because ``_pick`` treats "unset" and "set to
    empty" identically, the operator's escape hatch is a literal off-word instead,
    the same convention ``_norm_reaction`` uses for ``ack_reaction``.
    """
    value = str(raw or "").strip()
    if value.lower() in _DEDUP_STATE_DIR_OFF_VALUES:
        return ""
    return os.path.expanduser(value) if value else os.path.join(
        _resolve_hermes_state_dir(env), DEFAULT_DEDUP_STATE_DIR_NAME
    )


@dataclass(frozen=True)
class RocketChatConfig:
    """Everything this integration reads from configuration, already coerced.

    Frozen because a setting changing mid-process would be a bug, not a
    feature: the gateway re-reads configuration by restarting.
    """

    # --- connection (all four are required) ---
    base_url: str = ""
    token: str = ""
    user_id: str = ""
    bot_username: str = ""
    # The Hermes "home channel" (room id): target of the startup greeting (when
    # ``startup_greeting``) and of ``deliver=rocketchat`` cron jobs
    # (``cron_deliver_env_var``). Hermes's ``/sethome`` writes this key.
    home_channel: str = ""
    # ROCKETCHAT_STARTUP_GREETING: separate switch so the home channel can be a cron
    # target without a greeting (an owner's DM on a personal bot).
    startup_greeting: bool = True

    # ROCKETCHAT_ASKER_KEY: the asker attestation's key, shared with a Rocket.Chat
    # history MCP server (asker.py). Empty = no attestation line in the context.
    asker_key: str = ""

    # --- inbound listener ---
    webhook_secret: str = ""
    webhook_host: str = DEFAULT_WEBHOOK_HOST
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    require_mention: bool = True
    # ROCKETCHAT_DM_ONLY: work only in 1:1 DMs. A mention in a room (or group DM) starts
    # no turn and loads no room context; the bot says once per room that it is DM-only.
    # For a personal Hermes running with its owner's full rights, where anyone's message
    # in a shared room would run the owner's agent.
    dm_only: bool = False
    # Reply reference in rooms (reply_reference.py): quote of the question and @mention.
    # Neither goes on an answer in a 1:1 DM.
    reply_quote: bool = True
    reply_mention: bool = True
    # ROCKETCHAT_OUTBOUND_FILES: the plugin uploads a file the model sends with a
    # ``MEDIA:/path`` tag into the room (Hermes's documented
    # ``send_document``/``send_image_file``/``send_voice``/``send_video`` overrides, see
    # adapter.py). When off, Hermes's default stays (core's "cannot send this" notice)
    # and the platform guidance does not offer the tag.
    outbound_files: bool = True
    # ROCKETCHAT_APPROVAL_BUTTONS: accept and deny buttons on a dangerous-command prompt
    # (approval_buttons.py). When off, Hermes sends its plain-text /approve prompt.
    approval_buttons: bool = True
    # A startup warning when Hermes's built-in memory is on although several people can reach the bot
    # (shared_memory_risk). Only a log line; it changes nothing.
    shared_memory_warning: bool = True
    # ROCKETCHAT_TRANSPORT (see ddp_transport_mixin.py): "ddp" (default: inbound only
    # through the Realtime API, the webhook HTTP server does not start) or "webhook"
    # (Outgoing Webhook HTTP listener, the fallback path). Either-or: the two cannot run
    # together, that would process messages twice. An invalid value falls back to "ddp"
    # with a warning.
    transport: str = "ddp"

    # DDP WebSocket keepalive interval, seconds - see
    # _normalize_ws_heartbeat_seconds()'s docstring for why this defaults
    # ON (silent-inbound-death prevention), not a purely optional tuning knob.
    ws_heartbeat_seconds: int = DEFAULT_WS_HEARTBEAT_SECONDS
    ddp_catchup_max_seconds: int = DEFAULT_DDP_CATCHUP_MAX_SECONDS
    startup_catchup_max_seconds: int = DEFAULT_STARTUP_CATCHUP_MAX_SECONDS
    startup_catchup_max_dispatches: int = DEFAULT_STARTUP_CATCHUP_MAX_DISPATCHES
    startup_catchup_mode: str = DEFAULT_STARTUP_CATCHUP_MODE

    allowed_users: List[str] = field(default_factory=list)
    allow_all_users: bool = False
    # ROCKETCHAT_IGNORE_SENDERS: usernames or user ids whose messages never start a turn
    # (rooms and DMs), typically the other bots, against bot-to-bot reply loops. Runs
    # before the allowlist and the dedup (see inbound.py ``is_ignored_sender``).
    ignore_senders: List[str] = field(default_factory=list)


    # --- leaked-reasoning blockquote heuristic ---
    reasoning_leak_formatting_enabled: bool = False
    reasoning_leak_pattern: str = DEFAULT_REASONING_LEAD_PATTERN
    reasoning_leak_label: str = DEFAULT_REASONING_LEAK_LABEL
    reasoning_leak_mode: str = DEFAULT_REASONING_LEAK_MODE

    # --- /whoami (whoami.py) ---
    whoami_enabled: bool = True
    whoami_command: str = DEFAULT_WHOAMI_COMMAND

    # --- room context on mention (history.py) --- Upper bound of the window (0 = off,
    # at most 100), its lower bound (at most history_limit) and the optional token
    # budget (0 = none).
    history_limit: int = DEFAULT_HISTORY_LIMIT
    history_min_messages: int = DEFAULT_HISTORY_MIN_MESSAGES
    history_token_budget: int = DEFAULT_HISTORY_TOKEN_BUDGET

    # Sessions belong to Hermes core (room_session.py, dm_session.py). Time zone of the
    # block's timestamps (ROCKETCHAT_TIMEZONE, IANA name); None = the process's local
    # zone.
    timezone: Optional[tzinfo] = None

    # --- direct messages (dm_session.py) --- First-contact seed (0 = none), used while
    # the bot has not answered in the DM yet.
    dm_seed_messages: int = DEFAULT_DM_SEED_MESSAGES

    # --- instant acknowledgement reaction --- Emoji shortname the bot puts on the
    # user's @mention the moment a message is accepted for an agent turn: visible "seen
    # you" feedback long before the LLM answer. `adapter._resolve_ack()` (called from
    # `send()`) removes it (or replaces it with ":x:" on failure) the moment the reply
    # is ready. Empty string = disabled. Deliberately a single static reaction: every
    # reaction change reorders Rocket.Chat's message list, which looks like a jump in a
    # live channel.
    ack_reaction: str = "eyes"

    # --- allowlist of Hermes's built-in / commands (builtin_commands.py) ---
    allowed_builtin_commands: FrozenSet[str] = field(
        default_factory=lambda: DEFAULT_ALLOWED_BUILTIN_COMMANDS
    )

    # --- duplicate suppression ---
    outbound_dedup_ttl_seconds: float = DEFAULT_OUTBOUND_DEDUP_TTL_SECONDS
    # Inbound dedup retention in hours (see dedup.py), also the age limit: an older
    # Rocket.Chat message never reaches the agent. ``<= 0`` or invalid -> the default.
    inbound_dedup_retention_hours: float = DEFAULT_INBOUND_DEDUP_RETENTION_HOURS
    # State directory for restart-surviving dedup; the file names (inbound.json,
    # outbound.json) are built on it in shared_state.py. EMPTY in the dataclass default
    # because the real default is computed from HERMES_HOME (see
    # _resolve_dedup_state_dir). "" here only means that without load_config() (a test's
    # direct constructor call) the feature stays off, like the dedup classes' own
    # persist_path=None default.
    dedup_state_dir: str = ""

    # --- streaming finalize strategy --- Default FALSE: see
    # prefers_fresh_final_streaming() in adapter.py for why turning this on made things
    # worse (byte-identical duplicate delivery the outbound dedup guard did not catch).
    always_fresh_final_streaming: bool = False

    REQUIRED_FIELDS = ("base_url", "token", "user_id", "bot_username")

    def missing_required(self) -> List[str]:
        """Which connection settings are still unset.

        Returned as a list rather than a bool so the caller can name them in the log:
        "not configured" without saying WHICH of the four is missing is the kind of
        message that costs someone an hour.

        ``webhook_secret`` is required only with ``transport == "webhook"`` (fail closed):
        without it the listener would accept any POST from anyone who can reach it, as
        any Rocket.Chat user. DDP does not use the secret.
        """
        missing = [name for name in self.REQUIRED_FIELDS if not getattr(self, name)]
        if self.transport == "webhook" and not self.webhook_secret:
            missing.append("webhook_secret")
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_required()


def load_config(
    extra: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    token: str = "",
) -> RocketChatConfig:
    """Build a :class:`RocketChatConfig` from ``extra`` + ``env``.

    ``token`` is passed separately because the harness carries it on the
    platform config object itself (``config.token``) rather than in ``extra``;
    it still falls back to ``ROCKETCHAT_TOKEN``.
    """
    extra = extra or {}
    env = env if env is not None else os.environ
    history_limit = _normalize_history_limit(_pick("history_limit", extra, env))

    return RocketChatConfig(
        base_url=_pick("url", extra, env).rstrip("/"),
        # Env over YAML: PlatformConfig.token (``platforms.rocketchat.token``) is the
        # YAML level, after the env key and ``extra``.
        token=_pick("token", extra, env) or token,
        user_id=_pick("user_id", extra, env),
        bot_username=_pick("bot_username", extra, env),
        home_channel=_pick("home_channel", extra, env),
        asker_key=_pick("asker_key", extra, env),
        webhook_secret=_pick("webhook_secret", extra, env),
        webhook_host=_pick("webhook_host", extra, env, DEFAULT_WEBHOOK_HOST),
        webhook_port=as_int(
            _pick("webhook_port", extra, env), default=DEFAULT_WEBHOOK_PORT
        ),
        require_mention=_flag("require_mention", extra, env, True),
        transport=_normalize_transport(_pick("transport", extra, env, "ddp")),
        ws_heartbeat_seconds=_normalize_ws_heartbeat_seconds(
            _pick(
                "ws_heartbeat_seconds", extra, env, str(DEFAULT_WS_HEARTBEAT_SECONDS)
            )
        ),
        ddp_catchup_max_seconds=max(
            0,
            as_int(
                _pick("ddp_catchup_max_seconds", extra, env),
                default=DEFAULT_DDP_CATCHUP_MAX_SECONDS,
            ),
        ),
        startup_catchup_max_seconds=max(
            0,
            as_int(
                _pick("startup_catchup_max_seconds", extra, env),
                default=DEFAULT_STARTUP_CATCHUP_MAX_SECONDS,
            ),
        ),
        startup_catchup_max_dispatches=max(
            1,
            as_int(
                _pick("startup_catchup_max_dispatches", extra, env),
                default=DEFAULT_STARTUP_CATCHUP_MAX_DISPATCHES,
            ),
        ),
        startup_catchup_mode=_choice(
            "startup_catchup_mode", extra, env, STARTUP_CATCHUP_MODES, DEFAULT_STARTUP_CATCHUP_MODE
        ),
        allowed_users=as_list(_pick("allowed_users", extra, env)),
        allow_all_users=_flag("allow_all_users", extra, env, False),
        dm_only=_flag("dm_only", extra, env, False),
        reply_quote=_flag("reply_quote", extra, env, True),
        reply_mention=_flag("reply_mention", extra, env, True),
        # Env only (like ROCKETCHAT_ALLOW_BUILTIN_*): the platform guidance is fixed at
        # registration, when the ``config.yaml`` ``extra`` is not readable yet, so both
        # always see the same value.
        approval_buttons=_flag("approval_buttons", extra, env, True),
        shared_memory_warning=_flag("shared_memory_warning", extra, env, True),
        outbound_files=as_bool(
            env.get("ROCKETCHAT_OUTBOUND_FILES", ""), default=True, name="ROCKETCHAT_OUTBOUND_FILES"
        ),
        startup_greeting=_flag("startup_greeting", extra, env, True),
        ignore_senders=as_list(_pick("ignore_senders", extra, env)),
        reasoning_leak_formatting_enabled=_flag("reasoning_leak_formatting", extra, env, False),
        reasoning_leak_pattern=_pick(
            "reasoning_leak_pattern", extra, env, DEFAULT_REASONING_LEAD_PATTERN
        ),
        reasoning_leak_label=_pick(
            "reasoning_leak_label", extra, env, DEFAULT_REASONING_LEAK_LABEL
        ),
        reasoning_leak_mode=(_pick("reasoning_leak_mode", extra, env, DEFAULT_REASONING_LEAK_MODE).strip().lower() or DEFAULT_REASONING_LEAK_MODE),
        whoami_enabled=_flag("whoami_enabled", extra, env, True),
        whoami_command=_pick("whoami_command", extra, env, DEFAULT_WHOAMI_COMMAND),
        history_limit=history_limit,
        history_min_messages=_normalize_history_min_messages(
            _pick("history_min_messages", extra, env), history_limit
        ),
        history_token_budget=max(
            0,
            as_int(
                _pick("history_token_budget", extra, env),
                default=DEFAULT_HISTORY_TOKEN_BUDGET,
            ),
        ),
        timezone=_normalize_timezone(_pick("timezone", extra, env)),
        dm_seed_messages=_normalize_dm_seed_messages(_pick("dm_seed_messages", extra, env)),
        ack_reaction=_norm_reaction(_pick("ack_reaction", extra, env, "eyes")),
        allowed_builtin_commands=allowed_builtin_commands(env),
        outbound_dedup_ttl_seconds=as_float(
            _pick("outbound_dedup_ttl_seconds", extra, env),
            default=DEFAULT_OUTBOUND_DEDUP_TTL_SECONDS,
        ),
        inbound_dedup_retention_hours=_positive_float(
            "inbound_dedup_retention_hours",
            _pick("inbound_dedup_retention_hours", extra, env),
            DEFAULT_INBOUND_DEDUP_RETENTION_HOURS,
        ),
        dedup_state_dir=_resolve_dedup_state_dir(_pick("dedup_state_dir", extra, env), env),
        always_fresh_final_streaming=_flag("always_fresh_final_streaming", extra, env, False),
    )


def shared_memory_risk(memory_section: Any, cfg: "RocketChatConfig") -> Tuple[str, ...]:
    """Hermes's built-in memory stores that are on while more than one person can reach the bot.

    Hermes keeps one memory per Hermes home, and both stores default to on, so on a shared bot what one person
    tells it can surface in another person's answer. Empty when the bot answers a single person only (DM-only
    with one allowed user) or both stores are off.
    """
    single_person = cfg.dm_only and len(cfg.allowed_users) == 1 and not cfg.allow_all_users
    if single_person:
        return ()
    section = memory_section if isinstance(memory_section, Mapping) else {}
    return tuple(
        key for key in ("memory_enabled", "user_profile_enabled")
        if as_bool(section.get(key) if section.get(key) is not None else "", default=True)
    )
