import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builtin_commands import (  # noqa: E402
    BUILTIN_COMMAND_ALIASES,
    BUILTIN_COMMAND_PRESETS,
    BUILTIN_COMMANDS,
    DEFAULT_ALLOWED_BUILTIN_COMMANDS,
    PERSISTENT_APPROVAL_KEY,
    allowed_builtin_commands,
    blocked_builtin_command,
    blocked_persistent_approval,
    blocked_skill_command,
    canonical_builtin_command_name,
    command_allowlist_key,
    is_skill_command_allowed,
    make_hermes_command_resolver,
    parse_builtin_command_name,
    unknown_allowed_commands,
)
from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON  # noqa: E402


class _FakeCommandDef:
    def __init__(self, name, aliases=()):
        self.name = name
        self.aliases = tuple(aliases)


# A small registry imitating Hermes for the tests without Hermes, with the two most
# serious commands an allowlist must catch.
_FAKE_REGISTRY = [
    _FakeCommandDef("new", ("reset",)),
    _FakeCommandDef("approve"),
    _FakeCommandDef("approvals"),
    _FakeCommandDef("pause"),
    _FakeCommandDef("heartbeat", ("hb",)),
    _FakeCommandDef("branch", ("fork",)),
    _FakeCommandDef("reload-mcp", ("reload_mcp",)),
    _FakeCommandDef("sethome", ("set-home",)),
    _FakeCommandDef("yolo"),
    _FakeCommandDef("status"),
]
_FAKE_LOOKUP = {k: c for c in _FAKE_REGISTRY for k in (c.name, *c.aliases)}


def _fake_resolver(**kwargs):
    return make_hermes_command_resolver(
        lambda name: _FAKE_LOOKUP.get(name.lower().lstrip("/")), **kwargs
    )


class TestParseBuiltinCommandName(unittest.TestCase):
    def test_plain_command(self):
        self.assertEqual(parse_builtin_command_name("/new"), "new")

    def test_command_with_arguments(self):
        self.assertEqual(parse_builtin_command_name("/model claude-opus-5"), "model")

    def test_case_insensitive(self):
        self.assertEqual(parse_builtin_command_name("/YOLO on"), "yolo")

    def test_surrounding_whitespace_ignored(self):
        self.assertEqual(parse_builtin_command_name("   /restart  "), "restart")

    def test_non_command_text_yields_empty(self):
        self.assertEqual(parse_builtin_command_name("what's new?"), "")

    def test_empty_text_yields_empty(self):
        self.assertEqual(parse_builtin_command_name(""), "")

    def test_bare_slash_yields_empty(self):
        self.assertEqual(parse_builtin_command_name("/"), "")

    def test_bot_suffix_is_stripped_like_hermes_get_command(self):
        # Hermes's MessageEvent.get_command() cuts "/cmd@bot" to "cmd"; without this
        # "/yolo@bot" would bypass the guard.
        self.assertEqual(parse_builtin_command_name("/YOLO@hermes on"), "yolo")

    def test_word_with_slash_is_not_a_command(self):
        # A file path (Hermes does not treat it as a command either). Deliberately not a
        # system file: Hermes's plugin install scan treats the system password file's
        # path as CRITICAL even in tests and blocks `hermes plugins install`.
        self.assertEqual(parse_builtin_command_name("/usr/share/doc"), "")


class TestBuiltinCommandPresets(unittest.TestCase):
    """ROCKETCHAT_ALLOW_BUILTIN_PRESET: the preset of a personal Hermes."""

    def test_deny_is_allowed_by_default(self):
        # The pair of /approve: without it a pending dangerous command could only be
        # approved over Rocket.Chat, never denied.
        self.assertIn("deny", allowed_builtin_commands({}))

    def test_personal_preset_opens_the_session_commands(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"})
        for name in ("sessions", "resume", "title", "branch", "model", "sethome", "status"):
            self.assertIn(name, allowed)
        self.assertTrue(DEFAULT_ALLOWED_BUILTIN_COMMANDS <= allowed)

    def test_btw_and_context_are_allowed_by_default(self):
        # /btw does not write to the session, /context only reads, and both touch only
        # the asker's own session, so they are on by default on a shared bot too.
        for env in ({}, {"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"}):
            allowed = allowed_builtin_commands(env)
            for name in ("btw", "context"):
                self.assertIn(name, allowed)

    def test_personal_preset_opens_the_terminal_like_commands_but_default_does_not(self):
        personal = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"})
        default = allowed_builtin_commands({})
        for name in ("steer", "agents", "goal", "subgoal", "personality", "reasoning", "footer", "profile",
                     "insights", "discard", "heartbeat", "loop", "plan", "review", "refine", "bg"):
            self.assertIn(name, personal)
            self.assertNotIn(name, default)

    def test_personal_preset_keeps_the_meaningless_in_a_pod_commands_closed(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"})
        for name in ("diff", "init", "rollback", "egress", "codex_runtime", "login", "topup", "fast", "update"):
            self.assertNotIn(name, allowed)

    def test_personal_preset_never_opens_the_most_dangerous_commands(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"})
        for name in ("yolo", "restart", "update", "platform", "debug", "learn", "approvals", "pause"):
            self.assertNotIn(name, allowed)

    def test_per_command_key_overrides_the_preset_both_ways(self):
        allowed = allowed_builtin_commands({
            "ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal",
            "ROCKETCHAT_ALLOW_BUILTIN_MODEL": "false",
            "ROCKETCHAT_ALLOW_BUILTIN_YOLO": "true",
        })
        self.assertNotIn("model", allowed)
        self.assertIn("yolo", allowed)

    def test_unknown_preset_falls_back_to_default_with_a_warning(self):
        with self.assertLogs("builtin_commands", level="WARNING"):
            allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "everything"})
        self.assertEqual(allowed, DEFAULT_ALLOWED_BUILTIN_COMMANDS)

    def test_preset_key_is_not_a_command_named_preset(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"})
        self.assertNotIn("preset", allowed)


class TestStopAllowedByDefault(unittest.TestCase):
    """/stop is on by default (it stops only the sender's own run)."""

    def test_stop_is_in_the_default_set(self):
        self.assertIn("stop", allowed_builtin_commands({}))

    def test_stop_can_still_be_switched_off(self):
        self.assertNotIn("stop", allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_STOP": "false"}))

    def test_stop_passes_the_fail_closed_guard(self):
        self.assertEqual(blocked_builtin_command("/stop", DEFAULT_ALLOWED_BUILTIN_COMMANDS), "")


class TestBlockedPersistentApproval(unittest.TestCase):
    """Persistent approval writes the gateway's whole config, so it is off by default."""

    def test_persistent_forms_are_blocked_by_default(self):
        for text in (
            "/always", "/remember", "/always@hermes", "!always",
            "/approve always", "/approve session", "/APPROVE Permanent", "/approve permanently",
            "always", "  Always  Approve ", "approve always", "session",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    blocked_persistent_approval(text, DEFAULT_ALLOWED_BUILTIN_COMMANDS),
                    PERSISTENT_APPROVAL_KEY,
                )

    def test_one_off_answers_and_normal_text_pass(self):
        for text in ("/approve", "/approve once", "/deny", "/new", "/stop",
                     "always remember to log time", "who wrote about the session?", ""):
            with self.subTest(text=text):
                self.assertEqual(blocked_persistent_approval(text, DEFAULT_ALLOWED_BUILTIN_COMMANDS), "")

    def test_allow_key_and_personal_preset_open_it(self):
        for env in ({"ROCKETCHAT_ALLOW_BUILTIN_ALWAYS": "true"},
                    {"ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal"}):
            with self.subTest(env=env):
                self.assertEqual(blocked_persistent_approval("/always", allowed_builtin_commands(env)), "")

    def test_personal_preset_can_close_it_again(self):
        allowed = allowed_builtin_commands({
            "ROCKETCHAT_ALLOW_BUILTIN_PRESET": "personal",
            "ROCKETCHAT_ALLOW_BUILTIN_ALWAYS": "false",
        })
        self.assertEqual(blocked_persistent_approval("/approve always", allowed), PERSISTENT_APPROVAL_KEY)


class TestAllowedBuiltinCommands(unittest.TestCase):
    def test_default_is_new_and_reset_only(self):
        self.assertEqual(allowed_builtin_commands({}), DEFAULT_ALLOWED_BUILTIN_COMMANDS)
        self.assertIn("new", allowed_builtin_commands({}))
        self.assertIn("reset", allowed_builtin_commands({}))

    def test_high_risk_command_stays_off_by_default(self):
        allowed = allowed_builtin_commands({})
        for name in ("model", "yolo", "restart", "platform", "update", "debug", "learn"):
            self.assertNotIn(name, allowed)

    def test_approve_is_allowed_by_default(self):
        # Exception to the high-risk-stays-off rule: /approve can only ever
        # resolve a confirmation for a command already on THIS allowlist
        # (currently just /new/reset) - see builtin_commands.py's comment on
        # "approve" for why that bounds its blast radius.
        self.assertIn("approve", allowed_builtin_commands({}))

    def test_env_flag_turns_a_command_on(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_STATUS": "true"})
        self.assertIn("status", allowed)
        self.assertIn("new", allowed)  # the default is not lost by adding another

    def test_env_flag_turns_new_off(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_NEW": "false"})
        self.assertNotIn("new", allowed)

    def test_typo_value_does_not_enable_a_default_off_command(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_MODEL": "yse"})
        self.assertNotIn("model", allowed)  # falls back to its own default (off), not "on"

    def test_typo_value_does_not_disable_a_default_on_command(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_NEW": "flase"})
        self.assertIn("new", allowed)  # falls back to its own default (on), not "off"

    def test_only_recognised_true_values_enable_a_command(self):
        for value in ("true", "1", "yes", "on", "TRUE", "On"):
            self.assertIn(
                "status",
                allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_STATUS": value}),
                msg=f"value={value!r} should enable it",
            )

    def test_only_recognised_false_values_disable_a_command(self):
        for value in ("false", "0", "no", "off", "FALSE", "Off"):
            self.assertNotIn(
                "new",
                allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_NEW": value}),
                msg=f"value={value!r} should disable it",
            )

    def test_every_command_has_a_default_entry(self):
        for name in BUILTIN_COMMANDS:
            self.assertIsInstance(name, str)
            self.assertTrue(name)


class TestBlockedBuiltinCommandWithRegistry(unittest.TestCase):
    """Default deny: EVERYTHING Hermes knows as a command."""

    def block(self, text, allowed=DEFAULT_ALLOWED_BUILTIN_COMMANDS, **kwargs):
        return blocked_builtin_command(
            text, allowed, resolve_command=_fake_resolver(**kwargs)
        )

    def test_allowed_defaults_pass(self):
        for text in ("/new", "/reset", "/approve"):
            with self.subTest(text=text):
                self.assertEqual(self.block(text), "")

    def test_commands_missing_from_the_old_static_list_are_blocked(self):
        # Commands a static list would miss.
        self.assertEqual(self.block("/approvals off"), "approvals")
        self.assertEqual(self.block("/pause"), "pause")
        self.assertEqual(self.block("/hb"), "heartbeat")

    def test_disallowed_known_command_is_blocked(self):
        self.assertEqual(self.block("/yolo on", frozenset()), "yolo")

    def test_bot_suffix_does_not_bypass_the_guard(self):
        self.assertEqual(self.block("/yolo@hermes on"), "yolo")

    def test_unknown_word_passes(self):
        # Hermes does not know it as a command: Hermes gives its own "unknown command"
        # answer, nothing runs.
        self.assertEqual(self.block("/nosuchcommand"), "")

    def test_non_command_text_is_not_blocked(self):
        self.assertEqual(self.block("what's new?"), "")

    def test_aliases_map_to_the_canonical_key(self):
        self.assertEqual(self.block("/fork"), "branch")
        self.assertEqual(self.block("/set-home"), "sethome")
        self.assertEqual(self.block("/reload-mcp"), "reload_mcp")
        self.assertEqual(self.block("/reload_mcp"), "reload_mcp")

    def test_alias_follows_its_canonical_allow_setting(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_HEARTBEAT": "true"})
        self.assertEqual(self.block("/hb", allowed), "")
        self.assertEqual(self.block("/heartbeat", allowed), "")

    def test_reset_keeps_its_own_allowlist_entry(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_RESET": "false"})
        self.assertEqual(self.block("/reset", allowed), "reset")
        self.assertEqual(self.block("/new", allowed), "")

    def test_a_command_not_documented_here_can_still_be_allowed(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_PAUSE": "true"})
        self.assertEqual(self.block("/pause", allowed), "")

    def test_plugin_bundle_and_quick_commands_are_commands_too(self):
        kwargs = dict(
            is_gateway_known_command=lambda name: name == "plugin-cmd",
            resolve_bundle_command_key=lambda name: "/b" if name == "my-bundle" else None,
            quick_command_names=lambda: {"deploy": {"type": "exec"}},
        )
        self.assertEqual(self.block("/plugin_cmd", **kwargs), "plugin_cmd")
        self.assertEqual(self.block("/my-bundle", **kwargs), "my_bundle")
        self.assertEqual(self.block("/deploy", **kwargs), "deploy")
        self.assertEqual(self.block("/semmi", **kwargs), "")

    def test_a_failing_resolver_fails_closed(self):
        def boom(_word):
            raise RuntimeError("hermes refactor")

        with self.assertLogs("builtin_commands", level="WARNING"):
            self.assertEqual(
                blocked_builtin_command("/anything", frozenset(), resolve_command=boom),
                "anything",
            )


class TestBlockedBuiltinCommandFailClosed(unittest.TestCase):
    """Without the Hermes registry: every /word not allowed is blocked, except skills."""

    def test_allowed_defaults_pass(self):
        self.assertEqual(blocked_builtin_command("/new", DEFAULT_ALLOWED_BUILTIN_COMMANDS), "")
        self.assertEqual(blocked_builtin_command("/reset", DEFAULT_ALLOWED_BUILTIN_COMMANDS), "")

    def test_every_other_word_is_blocked(self):
        for text, expected in (
            ("/approvals off", "approvals"),
            ("/pause", "pause"),
            ("/nosuchcommand", "nosuchcommand"),
            ("/yolo@hermes on", "yolo"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    blocked_builtin_command(text, DEFAULT_ALLOWED_BUILTIN_COMMANDS), expected
                )

    def test_static_aliases_still_map_to_their_key(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_VERSION": "true"})
        self.assertEqual(blocked_builtin_command("/v", allowed), "")
        self.assertEqual(blocked_builtin_command("/fork", frozenset()), "branch")
        self.assertEqual(blocked_builtin_command("/set-home", frozenset()), "sethome")

    def test_installed_skills_are_left_to_the_skill_guard(self):
        is_skill = lambda word: word == "google-workspace"  # noqa: E731
        self.assertEqual(
            blocked_builtin_command("/google-workspace x", frozenset(), is_skill_command=is_skill),
            "",
        )
        self.assertEqual(
            blocked_builtin_command("/yolo", frozenset(), is_skill_command=is_skill), "yolo"
        )

    def test_non_command_text_is_not_blocked(self):
        self.assertEqual(blocked_builtin_command("what's new?", frozenset()), "")
        self.assertEqual(blocked_builtin_command("/usr/share/doc read it", frozenset()), "")


class TestCommandAllowlistKey(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(command_allowlist_key("reset", "new"), "reset")
        self.assertEqual(command_allowlist_key("hb", "heartbeat"), "heartbeat")
        self.assertEqual(command_allowlist_key("reload-mcp", "reload-mcp"), "reload_mcp")
        self.assertEqual(command_allowlist_key("q"), "queue")
        self.assertEqual(command_allowlist_key("pause"), "pause")


class TestBuiltinCommandAliases(unittest.TestCase):
    def test_every_alias_targets_a_guarded_key(self):
        for alias, target in BUILTIN_COMMAND_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, BUILTIN_COMMANDS)
                self.assertNotIn(alias, BUILTIN_COMMANDS)

    def test_unknown_word_has_no_canonical_name(self):
        self.assertEqual(canonical_builtin_command_name("nosuchcommand"), "")


def _hermes_commands_module():
    """The real ``hermes_cli.commands`` when the Hermes source is available, otherwise None."""
    candidate = os.environ.get("HERMES_AGENT_PATH") or os.path.expanduser(
        "~/.hermes/hermes-agent"
    )
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)
    try:
        import hermes_cli.commands as commands
    except Exception:  # noqa: BLE001 - no Hermes source: the test skips
        return None
    return commands


class TestAgainstHermesRegistry(unittest.TestCase):
    """Drift guard against the real Hermes: every name and alias of every chat-reachable command (not cli_only, or
    gateway-reachable through a config gate) is blocked or explicitly allowed, so a new command from a Hermes upgrade
    cannot stay silently unguarded.
    """

    def test_every_chat_reachable_command_is_blocked_or_allowlisted(self):
        commands = _hermes_commands_module()
        if commands is None:
            self.skipTest("hermes_cli.commands not importable (set HERMES_AGENT_PATH)")
        resolver = make_hermes_command_resolver(
            commands.resolve_command,
            is_gateway_known_command=commands.is_gateway_known_command,
        )
        checked = 0
        for cmd in commands.COMMAND_REGISTRY:
            if cmd.cli_only and not cmd.gateway_config_gate:
                continue
            for form in (cmd.name, *cmd.aliases):
                checked += 1
                with self.subTest(command=cmd.name, form=form):
                    blocked = blocked_builtin_command(
                        f"/{form} x", DEFAULT_ALLOWED_BUILTIN_COMMANDS, resolve_command=resolver
                    )
                    key = command_allowlist_key(form, cmd.name)
                    if key in DEFAULT_ALLOWED_BUILTIN_COMMANDS:
                        self.assertEqual(blocked, "")
                    else:
                        self.assertEqual(blocked, key)
        self.assertGreater(checked, 60)

    def test_the_audit_findings_are_blocked_by_default(self):
        commands = _hermes_commands_module()
        if commands is None:
            self.skipTest("hermes_cli.commands not importable (set HERMES_AGENT_PATH)")
        resolver = make_hermes_command_resolver(commands.resolve_command)
        # /btw and /context (/ctx) are on by default on purpose (they only read, about
        # one's own session); the point is that everything else stays blocked by
        # default.
        for word in ("approvals", "pause", "save", "bg", "heartbeat", "hb",
                     "refine", "review", "loop", "proactive", "plan", "egress",
                     "diff", "busy", "init", "topup"):
            with self.subTest(word=word):
                if commands.resolve_command(word) is None:
                    self.skipTest(f"/{word} is not in this Hermes version")
                self.assertTrue(
                    blocked_builtin_command(
                        f"/{word}", DEFAULT_ALLOWED_BUILTIN_COMMANDS, resolve_command=resolver
                    )
                )


def _resolver(known):
    """A fake resolve_skill_command_key: recognises names in ``known``."""

    def _resolve(name):
        key = f"/{name}"
        return key if key in known else None

    return _resolve


class TestIsSkillCommandAllowed(unittest.TestCase):
    def test_default_is_blocked(self):
        # Unlike builtin commands, NO skill has an established default-on
        # use case - see the module docstring on why this is fail-closed
        # with no DEFAULT_ALLOWED equivalent.
        self.assertFalse(is_skill_command_allowed("google-workspace", {}))

    def test_env_flag_turns_a_skill_on(self):
        self.assertTrue(
            is_skill_command_allowed(
                "google-workspace", {"ROCKETCHAT_ALLOW_SKILL_GOOGLE_WORKSPACE": "true"}
            )
        )

    def test_hyphens_map_to_underscores_in_the_env_var_name(self):
        self.assertTrue(
            is_skill_command_allowed(
                "node-inspect-debugger",
                {"ROCKETCHAT_ALLOW_SKILL_NODE_INSPECT_DEBUGGER": "true"},
            )
        )

    def test_typo_value_does_not_enable_it(self):
        self.assertFalse(
            is_skill_command_allowed(
                "computer-use", {"ROCKETCHAT_ALLOW_SKILL_COMPUTER_USE": "yse"}
            )
        )


class TestBlockedSkillCommand(unittest.TestCase):
    def test_installed_but_not_allowed_skill_is_blocked(self):
        resolver = _resolver({"/google-workspace"})
        self.assertEqual(
            blocked_skill_command(
                "/google-workspace do X", {}, resolve_skill_command_key=resolver
            ),
            "google-workspace",
        )

    def test_installed_and_allowed_skill_is_not_blocked(self):
        resolver = _resolver({"/google-workspace"})
        env = {"ROCKETCHAT_ALLOW_SKILL_GOOGLE_WORKSPACE": "true"}
        self.assertEqual(
            blocked_skill_command("/google-workspace do X", env, resolve_skill_command_key=resolver),
            "",
        )

    def test_word_that_is_not_an_installed_skill_is_not_blocked(self):
        # Not a real skill - blocking it would be a false positive, same
        # reasoning as blocked_builtin_command's unknown-word case.
        resolver = _resolver({"/google-workspace"})
        self.assertEqual(
            blocked_skill_command("/nosuchskill", {}, resolve_skill_command_key=resolver), ""
        )

    def test_non_command_text_is_not_blocked(self):
        resolver = _resolver({"/google-workspace"})
        self.assertEqual(
            blocked_skill_command("what's new?", {}, resolve_skill_command_key=resolver), ""
        )

    def test_no_resolver_injected_means_no_op(self):
        # e.g. this plugin's own standalone unit tests, or a Hermes install
        # where the internal module could not be imported (adapter.py logs
        # a warning in that case - see its __init__) - this function itself
        # must not raise or silently pretend to have checked anything.
        self.assertEqual(
            blocked_skill_command("/google-workspace do X", {}, resolve_skill_command_key=None),
            "",
        )


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestPresetNamesAreHermesCommands(unittest.TestCase):
    """A preset name must be the canonical name of an installed Hermes command (a renamed key would leave the command silently blocked)."""

    PLUGIN_ONLY = {"always"}  # persistent approval has its own guard; it is not a Hermes command

    def test_every_preset_name_is_a_canonical_hermes_command(self):
        from hermes_cli.commands import resolve_command
        names = set(DEFAULT_ALLOWED_BUILTIN_COMMANDS).union(*BUILTIN_COMMAND_PRESETS.values()) - self.PLUGIN_ONLY
        for name in sorted(names):
            with self.subTest(name=name):
                command = resolve_command(name.replace("_", "-")) or resolve_command(name)
                self.assertIsNotNone(command, f"{name}: no such Hermes command")
                canonical = command.name.replace("-", "_")
                self.assertTrue(name == canonical or name in {a.replace("-", "_") for a in command.aliases},
                                f"{name}: a Hermes neve {command.name}")


class TestUnknownAllowedCommands(unittest.TestCase):
    """A renamed or mistyped allowed name warns at startup instead of silently blocking."""

    @staticmethod
    def resolve(word):
        return {"bg": "bg", "new": "new", "reset": "new", "reload-mcp": "reload-mcp"}.get(word.replace("_", "-"))

    def test_names_hermes_does_not_know_are_reported(self):
        allowed = frozenset({"bg", "background", "new", "reset", "reload_mcp", "always", "modle"})
        self.assertEqual(unknown_allowed_commands(allowed, self.resolve), ("background", "modle"))

    def test_without_a_resolver_nothing_is_reported(self):
        # fail-closed mode: loading the resolver has already warned about that
        self.assertEqual(unknown_allowed_commands(frozenset({"background"}), None), ())
