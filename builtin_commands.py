"""Guarding Hermes's built-in slash commands on the Rocket.Chat platform.

Hermes's admin-vs-user slash-command gating (``allow_admin_from`` in
``config.yaml``) is off by default, and this adapter forwards any text that is
not its own command (``/whoami``) to the agent turn unchanged. Without a guard,
any Rocket.Chat sender who mentions the bot with ``/model ...``, ``/yolo on`` or
``/restart`` would run that command with full Hermes privileges.

**Default deny, explicit allowlist.** Every ``/word`` that the INSTALLED Hermes
resolves to a command - its ``COMMAND_REGISTRY`` with every alias
(``hermes_cli.commands.resolve_command``), plugin commands
(``is_gateway_known_command``), skill bundles and the operator's
``quick_commands`` - is blocked unless its allowlist key is on
(``ROCKETCHAT_ALLOW_BUILTIN_<NAME>``, defaults:
:data:`DEFAULT_ALLOWED_BUILTIN_COMMANDS`). A future Hermes command is therefore
blocked from day one, with no change here. The Hermes imports live in
``webhook_mixin.py``; this module stays free of them. If the Hermes registry
cannot be imported, the guard FAILS CLOSED: every ``/word`` that is neither
allowlisted nor an installed skill is blocked (one warning is logged).

:data:`BUILTIN_COMMANDS` is documentation (risk and rationale per command) and
the alias fallback of the fail-closed mode; it does not decide what is blocked.

Defence in depth: also set Hermes's ``group_allow_admin_from`` (rooms) and
``allow_admin_from`` (one-to-one DMs) in ``config.yaml``, so Hermes itself refuses
admin commands to non-admins whatever reaches it. The two lists do not cover each
other's scope.

**Skill commands are a separate gap.** Installed skills (``/computer-use``,
``/google-workspace``, ...) are dispatched through a different registry from
``COMMAND_REGISTRY``, with no ``allow_admin_from`` check. See
``blocked_skill_command`` for the separate check (its own
``ROCKETCHAT_ALLOW_SKILL_<NAME>`` allowlist, no defaults).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Documentation only: this does not decide what is blocked (see the module docstring).
# name -> (risk, one-line rationale). "name" is the canonical command word Hermes
# dispatches on; aliases (/reset for /new) get their own entry since Hermes dispatches
# on either word. Whether a command starts allowed lives only in
# DEFAULT_ALLOWED_BUILTIN_COMMANDS.
BUILTIN_COMMANDS: Dict[str, Tuple[str, str]] = {
    "new": ("medium", "session reset"),
    "reset": ("medium", "alias of /new"),
    "status": ("low", "info only (session/model/tokens); a good candidate for debugging from chat"),
    "help": ("low", "built-in help text; harmless"),
    "whoami": ("low", "the sender's own permission level; harmless"),
    "version": ("low", "Hermes version and build info"),
    "profile": ("low", "active profile name and home; info only; in the personal preset"),
    "reasoning": ("low", "reasoning effort for the session (--global writes config.yaml); in the personal preset"),
    "fast": ("low", "fast mode on/off"),
    "voice": ("low", "TTS on/off"),
    "footer": ("low", "run footer (model, context %) on/off; writes config.yaml; in the personal preset"),
    "personality": ("low", "predefined personality; writes config.yaml; in the personal preset"),
    "codex_runtime": ("low", "Codex/OpenAI runtime toggle; provider-specific"),
    "bundles": ("low", "list of installed skill bundles; info"),
    "insights": ("low", "usage statistics (sessions, tokens, tools); info only; in the personal preset"),
    "kanban": ("low", "multi-profile kanban board"),
    "reload_skills": ("low", "~/.hermes/skills/ re-scan; admin-level"),
    "topic": ("low", "Telegram DM topic-session mode; not relevant on Rocket.Chat"),
    "start": ("low", "Telegram bot-launch ping; not relevant on Rocket.Chat"),
    "retry": ("low", "resend the last message; convenience"),
    "title": ("low", "set the session title; cosmetic"),
    "branch": ("low", "fork the session"),
    "compress": ("low", "manual context compression"),
    "subgoal": ("low", "extra criteria of the active /goal; in the personal preset"),
    "agents": ("low", "active agents and background tasks (alias: /tasks); info only; in the personal preset"),
    "queue": ("low", "queue a prompt after this turn"),
    "btw": ("low", "side question in the sender's own conversation, not written to the session; on by default"),
    "context": ("low", "how full the sender's own session context is; info only; on by default"),
    "commands": ("low", "browse the command catalog; mostly noise in a live chat room"),
    "deny": ("low", "deny a pending dangerous command; the pair of /approve; on by default"),
    "undo": ("medium", "roll back N user turns; a surprising session effect if anyone can call it"),
    "rollback": ("medium", "filesystem checkpoint restore; surprising effect"),
    "stop": ("low", "stop the sender's own running answer (per-user session in rooms); on by default"),
    "bg": ("medium", "prompt in a separate background session; in the personal preset"),
    "steer": ("medium", "steer a running turn after the next tool call; disruptive in a shared room; in the personal preset"),
    "goal": ("medium", "long-lived goal over several turns, with a judge model; in the personal preset"),
    "moa": ("medium", "Mixture-of-Agents preset for one turn"),
    "sethome": ("medium", "target of proactive notifications; admin-level setting"),
    "resume": ("medium", "resume an earlier named session; risk of mixing sessions"),
    "sessions": ("medium", "browse the session list; can reveal other users' sessions"),
    "memory": ("medium", "approve pending memory writes; anyone could manipulate the bot's memory"),
    "suggestions": ("medium", "manage suggested automations; admin-level"),
    "blueprint": ("medium", "automation from a blueprint; admin-level"),
    "curator": ("medium", "control background skill maintenance; admin-level"),
    "reload_mcp": ("medium", "reload MCP server connections; admin-level, surprising side effect"),
    "usage": ("medium", "token usage and limits; info, but its 'reset' subcommand changes state"),
    "topup": ("low", "Nous balance and billing; provider-specific"),
    "login": ("low", "Nous account login; provider-specific"),
    "diff": ("low", "git changes in the working directory"),
    "init": ("low", "generate AGENTS.md from a repository"),
    "egress": ("low", "Docker egress-proxy status"),
    "discard": ("low", "drop a turn lost in a restart so the queue moves on; in the personal preset"),
    "heartbeat": ("medium", "repeating prompt while idle; pauses after a restart until the owner's first message; in the personal preset"),
    "loop": ("medium", "repeat a prompt at intervals (--times N); pauses after a restart until the owner's first message; in the personal preset"),
    "plan": ("low", "markdown plan under .hermes/plans/, without executing it; in the personal preset"),
    "review": ("medium", "an independent sub-agent reviews the previous work (async, extra model calls); in the personal preset"),
    "refine": ("medium", "lessons of the conversation into memory or a skill; in the personal preset"),
    "approvals": ("high", "KEEP BLOCKED: sets the dangerous-command approval mode (e.g. off); not even for the owner"),
    "pause": ("high", "KEEP BLOCKED: pauses all work of the whole gateway; the owner could lock themselves out"),
    # Allowed on purpose. Hermes's destructive_slash_confirm gate can only confirm
    # commands that are already allowed on this list (/new, /reset, /undo, /clear);
    # /yolo, /restart etc. never get that far because they are blocked here first,
    # before Hermes sees the text. The blast radius of /approve is therefore exactly the
    # commands already enabled. Without it, the confirmation prompt of /new could never
    # be resolved over Rocket.Chat.
    "approve": ("low", "needed to resolve the confirmation prompt of /new; see the comment above"),
    "model": ("high", "KEEP BLOCKED: anyone could switch the bot's model (disruptive, costs money)"),
    "yolo": ("high", "KEEP BLOCKED: would turn off the dangerous-command approval gate"),
    "learn": ("high", "KEEP BLOCKED: would create a durable skill from arbitrary chat content (prompt-injection surface)"),
    "restart": ("high", "KEEP BLOCKED: denial of service (anyone could restart the whole gateway)"),
    "platform": ("high", "KEEP BLOCKED: could pause or stop this very integration"),
    "update": ("high", "KEEP BLOCKED: anyone could start Hermes's self-update"),
    "debug": ("high", "KEEP BLOCKED: would generate a debug report uploaded to a third party"),
    # The "Always Approve" answer to Hermes's /new confirmation (/always, /remember, the
    # plain word "always") writes ``approvals.destructive_slash_confirm: false`` into
    # the gateway's whole config.yaml, so one room member would remove the confirmation
    # of everyone else's /new. /approve always|session|permanent follows the same
    # pattern. Hermes parses these answers separately from its command registry, so they
    # have their own guard: :func:`blocked_persistent_approval`.
    "always": ("high", "persistent approval (/always, /approve always|session); affects everyone on a shared bot; in the personal preset"),
}

# The effective default: EVERY command is blocked except /new (and its alias /reset),
# /approve (needed to resolve the confirmation prompt of /new, see above) and its pair
# /deny (otherwise a pending dangerous command could only be approved over Rocket.Chat,
# never denied), /stop (stops only the sender's own run; in a shared thread, that
# thread's session, like /new), /btw and /context (read-only, and they touch only the
# asker's own session). This is this plugin's own decision, not a Hermes default.
DEFAULT_ALLOWED_BUILTIN_COMMANDS: FrozenSet[str] = frozenset({"new", "reset", "approve", "deny", "stop", "btw", "context"})

# Command presets (ROCKETCHAT_ALLOW_BUILTIN_PRESET). A preset EXTENDS the default set;
# the per-command ROCKETCHAT_ALLOW_BUILTIN_<NAME> key always overrides it, so one
# command can be taken out of a preset. "personal" is for an owner's own Hermes that
# answers only them (DM-only, one allowed user): Rocket.Chat then does what the owner's
# Telegram does: switching and resuming sessions, changing the model, approvals,
# /sethome (cron delivery target). It does NOT belong on a shared, multi-user bot, where
# anyone could switch the model or browse other people's sessions. The most dangerous
# commands (/yolo, /restart, /update, /platform, /debug, /learn, /approvals, /pause) are
# in no preset; they need an explicit per-command key.
BUILTIN_COMMAND_PRESETS: Dict[str, FrozenSet[str]] = {
    "default": frozenset(),
    "personal": frozenset({
        "sessions", "resume", "title", "branch", "retry", "undo", "compress",
        "stop", "status", "usage", "queue", "help", "commands", "model",
        "bg", "sethome", "version", "always", "btw", "context",
    
        "steer", "agents", "goal", "subgoal", "personality", "reasoning", "footer",
        "profile", "insights", "discard", "heartbeat", "loop", "plan", "review", "refine",
    }),
}
DEFAULT_BUILTIN_COMMAND_PRESET = "default"

# The same two sets as config.py's TRUE_VALUES/FALSE_VALUES (separate here because of a
# circular import; tests/test_config.py checks they match). A typo can never flip a
# command's own default: only a recognised value overrides it.
_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}

_ALLOW_BUILTIN_PREFIX = "ROCKETCHAT_ALLOW_BUILTIN_"
_PRESET_KEY = "ROCKETCHAT_ALLOW_BUILTIN_PRESET"


def builtin_command_preset(env: Mapping[str, str]) -> str:
    """The ``ROCKETCHAT_ALLOW_BUILTIN_PRESET`` preset name; an unknown value ->
        ``default``, with a warning (a typo can never open up more).
    """
    raw = str(env.get(_PRESET_KEY, "") or "").strip().lower()
    if not raw:
        return DEFAULT_BUILTIN_COMMAND_PRESET
    if raw not in BUILTIN_COMMAND_PRESETS:
        logger.warning(
            "Rocket.Chat: unknown %s=%r, using %r (known: %s)",
            _PRESET_KEY, raw, DEFAULT_BUILTIN_COMMAND_PRESET,
            ", ".join(sorted(BUILTIN_COMMAND_PRESETS)),
        )
        return DEFAULT_BUILTIN_COMMAND_PRESET
    return raw


def _allowlist_key(name: str) -> str:
    """Env-var-compatible key: lower case, no ``/``, hyphen -> underscore."""
    return str(name or "").strip().lower().lstrip("/").replace("-", "_")


def allowed_builtin_commands(env: Mapping[str, str]) -> FrozenSet[str]:
    """Which command allowlist keys may pass through to Hermes unmodified.

    Any ``ROCKETCHAT_ALLOW_BUILTIN_<NAME>`` counts, not only those documented in
    :data:`BUILTIN_COMMANDS`, so a newer Hermes command (blocked by default) can be
    enabled the same way. A command's own default is the union of the default set
    and the ``ROCKETCHAT_ALLOW_BUILTIN_PRESET`` preset; the per-command key overrides
    it in both directions.
    """
    defaults = set(DEFAULT_ALLOWED_BUILTIN_COMMANDS) | set(
        BUILTIN_COMMAND_PRESETS[builtin_command_preset(env)]
    )
    names = set(BUILTIN_COMMANDS) | defaults
    for key in env:
        if key == _PRESET_KEY:
            continue
        if key.startswith(_ALLOW_BUILTIN_PREFIX) and len(key) > len(_ALLOW_BUILTIN_PREFIX):
            names.add(_allowlist_key(key[len(_ALLOW_BUILTIN_PREFIX):]))
    allowed = set()
    for name in names:
        raw = env.get(f"{_ALLOW_BUILTIN_PREFIX}{name.upper()}")
        default = name in defaults
        value = str(raw).strip().lower() if raw is not None else ""
        if value in _TRUE_VALUES:
            is_allowed = True
        elif value in _FALSE_VALUES:
            is_allowed = False
        else:
            is_allowed = default
        if is_allowed:
            allowed.add(name)
    return frozenset(allowed)


# The alias fallback of the fail-closed mode only (no importable Hermes registry).
# Hermes dispatches a command by the canonical ``COMMAND_REGISTRY`` name (hyphenated:
# ``reload-mcp``, ``codex-runtime``) AND by every alias. The keys above use underscores
# (they must work as env var names) and list only some aliases, so the key lookup first
# normalises hyphen -> underscore and then maps through this table to the canonical key.
# With a live Hermes its own ``resolve_command`` resolves the name (see
# :func:`make_hermes_command_resolver`), so a new alias cannot stay unguarded. /reset
# deliberately stays its own entry (it has its own env var), not an alias here.
BUILTIN_COMMAND_ALIASES: Dict[str, str] = {
    "fork": "branch",
    "compact": "compress",
    "tasks": "agents",
    "q": "queue",
    "set_home": "sethome",
    "suggest": "suggestions",
    "bp": "blueprint",
    "v": "version",
}


def parse_builtin_command_name(text: str) -> str:
    """The lowercase command word if ``text`` starts with ``/word...``, else ''.

    Cuts the same way as Hermes's ``MessageEvent.get_command()``: drops the part
    after ``@`` in ``/command@botname`` (otherwise ``/yolo@bot`` would bypass the
    guard while Hermes ran it as ``yolo``), and does not treat a word containing
    ``/`` (a file path) as a command.
    """
    stripped = text.strip()
    if not stripped.startswith("/") or len(stripped) < 2:
        return ""
    rest = stripped[1:].split(None, 1)
    if not rest:
        return ""
    word = rest[0].lower().split("@", 1)[0]
    return "" if "/" in word else word


def canonical_builtin_command_name(name: str) -> str:
    """The :data:`BUILTIN_COMMANDS` key that ``name`` (canonical name, hyphenated
        form or static alias) resolves to, or ``""``. Searches the documented list only
        (the alias fallback of the fail-closed mode).
    """
    key = _allowlist_key(name)
    key = BUILTIN_COMMAND_ALIASES.get(key, key)
    return key if key in BUILTIN_COMMANDS else ""


# The typed command word -> Hermes's canonical command name, or None/"" when Hermes does
# not know it as a command.
CommandResolver = Callable[[str], Optional[str]]


def command_allowlist_key(word: str, canonical: str = "") -> str:
    """The allowlist key (``ROCKETCHAT_ALLOW_BUILTIN_<KEY>``) for a typed word.

    If the typed form has its own documented entry (``reset``), that decides;
    otherwise the canonical name Hermes resolved (``/fork`` -> ``branch``, ``/hb`` ->
    ``heartbeat``); without a Hermes registry the static alias table, and finally
    the typed word itself.
    """
    typed = _allowlist_key(word)
    if typed in BUILTIN_COMMANDS:
        return typed
    if canonical:
        return _allowlist_key(canonical)
    return BUILTIN_COMMAND_ALIASES.get(typed, typed)


def blocked_builtin_command(
    text: str,
    allowed: FrozenSet[str],
    *,
    resolve_command: Optional[CommandResolver] = None,
    is_skill_command: Optional[Callable[[str], bool]] = None,
) -> str:
    """The allowlist key if ``text`` is a ``/word`` command that must NOT reach
    Hermes - otherwise ``""``.

    ``resolve_command`` (ld. :func:`make_hermes_command_resolver`) given:
    every word the installed Hermes resolves to a command is blocked unless
    its key is in ``allowed``; a word Hermes does not know (e.g.
    ``/nosuchcommand``) passes - Hermes answers it with its own
    "unknown command" notice, no command runs. A resolver that raises
    counts as "is a command" (fail closed).

    ``resolve_command`` is None (the Hermes registry is not importable):
    FAIL CLOSED - every ``/word`` that is neither allowlisted nor an
    installed skill (``is_skill_command``; those go to
    :func:`blocked_skill_command` and its own allowlist) is blocked.

    The returned key is what ``ROCKETCHAT_ALLOW_BUILTIN_<KEY>`` uses
    (e.g. ``/reload-mcp`` and ``/fork`` -> ``reload_mcp``/``branch``).
    """
    word = parse_builtin_command_name(text)
    if not word:
        return ""
    canonical = ""
    if resolve_command is not None:
        try:
            canonical = resolve_command(word) or ""
        except Exception:  # noqa: BLE001 - when in doubt it counts as a command
            logger.warning(
                "Rocket.Chat: Hermes command resolution failed for /%s - blocking it",
                word, exc_info=True,
            )
            canonical = word
        if not canonical:
            return ""
    elif is_skill_command is not None:
        try:
            if is_skill_command(word):
                return ""  # the skill guard decides (its own allowlist)
        except Exception:  # noqa: BLE001 - when in doubt, block here
            pass
    key = command_allowlist_key(word, canonical)
    return "" if key in allowed else key


# Hermes's answers that grant persistent approval (``_SLASH_CONFIRM_*_CHOICES``,
# ``_PLAINTEXT_APPROVAL_WORDS``, ``_APPROVE_CHOICE_BY_ARG`` in Hermes core). The plugin
# cannot see whether a confirmation is pending, so these are always intercepted: a
# standalone "always" message has no other meaning.
_PERSISTENT_APPROVAL_COMMANDS = frozenset({"always", "remember"})
_PERSISTENT_APPROVE_ARGS = frozenset({"always", "session", "permanent", "permanently"})
_PERSISTENT_APPROVAL_REPLIES = frozenset({
    "always", "always approve", "approve always", "remember",
    "session", "approve session", "session approve",
})
PERSISTENT_APPROVAL_KEY = "always"


def unknown_allowed_commands(
    allowed: FrozenSet[str], resolve_command: Optional[CommandResolver]
) -> Tuple[str, ...]:
    """The allowed names the installed Hermes does not know as commands.

    A Hermes rename or a mistyped ``ROCKETCHAT_ALLOW_BUILTIN_<NAME>`` key would
    otherwise be silently ineffective: the command stays blocked. The startup warning
    reports this against the running Hermes, not only in the tests. Persistent approval
    (``always``) is its own key, not a Hermes command. Empty without a resolver
    (fail-closed mode, which warns separately).
    """
    if resolve_command is None:
        return ()
    return tuple(sorted(
        name for name in allowed
        if name != PERSISTENT_APPROVAL_KEY and resolve_command(name) is None
    ))


def blocked_persistent_approval(text: str, allowed: FrozenSet[str]) -> str:
    """``"always"`` if ``text`` would grant persistent approval and that is not
    enabled (``ROCKETCHAT_ALLOW_BUILTIN_ALWAYS``), else ``""``.

    Intercepts ``/always``, ``/remember`` and ``/approve always|session|permanent``
    (``/word@bot`` and ``!word`` too), and the standalone text answer ("always" etc.).
    A one-off ``/approve`` and ``/deny`` pass.
    """
    if PERSISTENT_APPROVAL_KEY in allowed:
        return ""
    stripped = (text or "").strip()
    word = parse_builtin_command_name(stripped)
    if not word and stripped.startswith("!"):
        word = parse_builtin_command_name("/" + stripped[1:])
    if word in _PERSISTENT_APPROVAL_COMMANDS:
        return PERSISTENT_APPROVAL_KEY
    parts = stripped.split()
    if word == "approve" and len(parts) > 1 and parts[1].lower() in _PERSISTENT_APPROVE_ARGS:
        return PERSISTENT_APPROVAL_KEY
    reply = " ".join(stripped.lstrip("!/").lower().split())
    return PERSISTENT_APPROVAL_KEY if reply in _PERSISTENT_APPROVAL_REPLIES else ""


def make_hermes_command_resolver(
    resolve_command: Callable[[str], Any],
    *,
    is_gateway_known_command: Optional[Callable[[str], bool]] = None,
    resolve_bundle_command_key: Optional[Callable[[str], Optional[str]]] = None,
    quick_command_names: Optional[Callable[[], Any]] = None,
) -> CommandResolver:
    """The :data:`CommandResolver` built from Hermes's own resolvers (injected, so
    the module stays testable without Hermes).

    Order and scope follow Hermes's dispatch: ``COMMAND_REGISTRY`` name or alias
    (``resolve_command``; also tries an underscore form as hyphenated, as Hermes does
    for plugin, skill and bundle commands), plugin command
    (``is_gateway_known_command``), skill bundle and the operator's ``quick_commands``
    (their ``exec`` type runs a shell command). Returns the typed word's canonical
    name or ``None``.
    """

    def _resolve(word: str) -> Optional[str]:
        forms = [word] if "_" not in word else [word, word.replace("_", "-")]
        for form in forms:
            cmd = resolve_command(form)
            if cmd is not None:
                return str(getattr(cmd, "name", "") or form)
        if is_gateway_known_command is not None and any(
            is_gateway_known_command(form) for form in forms
        ):
            return word
        if resolve_bundle_command_key is not None and resolve_bundle_command_key(word):
            return word
        if quick_command_names is not None:
            names = quick_command_names() or ()
            if word in {str(name).strip().lower() for name in names}:
                return word
        return None

    return _resolve


def is_skill_command_allowed(name: str, env: Mapping[str, str]) -> bool:
    """Whether the installed skill ``name`` (e.g. ``"google-workspace"``) may
    run via Rocket.Chat.

    Deliberately FAIL-CLOSED with NO per-skill default (unlike
    :data:`DEFAULT_ALLOWED_BUILTIN_COMMANDS`, there is no skill with an
    established Rocket.Chat use case today) - every skill is blocked until
    its matching ``ROCKETCHAT_ALLOW_SKILL_<NAME>`` env var is explicitly set
    to a recognised true value. Hyphens in the skill slug become
    underscores in the env var name (env vars cannot contain hyphens).
    """
    raw = env.get(f"ROCKETCHAT_ALLOW_SKILL_{name.upper().replace('-', '_')}")
    value = str(raw).strip().lower() if raw is not None else ""
    return value in _TRUE_VALUES


def blocked_skill_command(
    text: str,
    env: Mapping[str, str],
    *,
    resolve_skill_command_key: Optional[Callable[[str], Optional[str]]],
) -> str:
    """The skill slug if ``text`` invokes an INSTALLED-but-not-allowed Hermes
    skill - otherwise ``""``.

    ``resolve_skill_command_key`` is injected (Hermes'
    ``agent.skill_commands.resolve_skill_command_key``) so this module stays
    import-free of Hermes internals, same reasoning as every other injected
    callable in this codebase (``is_whoami_command``, ...) - it is ``None`` in this plugin's own unit
    tests and in any environment where that Hermes-core module cannot be
    imported, in which case this check is a no-op (fails to the CALLER's
    own judgment about whether that is acceptable - see adapter.py, which
    logs a warning when it cannot import it, rather than silently
    pretending the gap is covered).

    Membership is checked by asking Hermes itself whether ``name`` resolves
    to a REAL, currently-installed skill (not a static dict here) - this
    list changes whenever a skill is installed/removed, so a hand-maintained
    snapshot would drift.
    """
    if resolve_skill_command_key is None:
        return ""
    name = parse_builtin_command_name(text)
    if not name:
        return ""
    if resolve_skill_command_key(name) is None:
        return ""  # not an installed skill at all - not this function's concern
    if is_skill_command_allowed(name, env):
        return ""
    return name
