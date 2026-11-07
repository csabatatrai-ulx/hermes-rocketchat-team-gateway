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
    command_allowlist_key,
    is_command_allowed,
    is_skill_command_allowed,
    parse_builtin_command_name,
)
from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON  # noqa: E402


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

    def test_stop_passes_the_guard(self):
        self.assertEqual(blocked_builtin_command("/stop", DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}), "")


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


class TestBlockedBuiltinCommand(unittest.TestCase):
    """Default deny: every /word that is not allowed, known to Hermes or not."""

    def block(self, text, allowed=DEFAULT_ALLOWED_BUILTIN_COMMANDS, env=None):
        return blocked_builtin_command(text, allowed, env or {})

    def test_allowed_defaults_pass(self):
        for text in ("/new", "/reset", "/approve", "/deny", "/cancel", "/stop", "/btw x", "/context"):
            with self.subTest(text=text):
                self.assertEqual(self.block(text), "")

    def test_every_other_word_is_blocked(self):
        for text, expected in (
            ("/approvals off", "approvals"),
            ("/pause", "pause"),
            ("/yolo on", "yolo"),
            ("/YOLO on", "yolo"),
            ("/yolo@hermes on", "yolo"),
            ("/nosuchcommand", "nosuchcommand"),
            ("/plugin_cmd", "plugin_cmd"),
            ("/my-bundle", "my_bundle"),
            ("/deploy", "deploy"),
            ("/google-workspace do X", "google_workspace"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.block(text), expected)

    def test_an_empty_allowlist_blocks_the_defaults_too(self):
        self.assertEqual(self.block("/new", frozenset()), "new")

    def test_non_command_text_is_not_blocked(self):
        self.assertEqual(self.block("what's new?"), "")
        self.assertEqual(self.block("/usr/share/doc read it"), "")

    def test_aliases_map_to_their_key(self):
        self.assertEqual(self.block("/fork"), "branch")
        self.assertEqual(self.block("/set-home"), "sethome")
        self.assertEqual(self.block("/reload-mcp"), "reload_mcp")
        self.assertEqual(self.block("/reload_mcp"), "reload_mcp")
        self.assertEqual(self.block("/hb"), "heartbeat")

    def test_alias_follows_its_command(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_HEARTBEAT": "true"})
        self.assertEqual(self.block("/hb", allowed), "")
        self.assertEqual(self.block("/heartbeat", allowed), "")

    def test_reset_keeps_its_own_allowlist_entry(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_RESET": "false"})
        self.assertEqual(self.block("/reset", allowed), "reset")
        self.assertEqual(self.block("/new", allowed), "")

    def test_any_word_can_be_allowed_by_its_own_key(self):
        allowed = allowed_builtin_commands({"ROCKETCHAT_ALLOW_BUILTIN_DEPLOY": "true"})
        self.assertEqual(self.block("/deploy", allowed), "")

    def test_a_skill_is_allowed_by_its_skill_key(self):
        env = {"ROCKETCHAT_ALLOW_SKILL_GOOGLE_WORKSPACE": "true"}
        self.assertEqual(self.block("/google-workspace do X", env=env), "")
        self.assertEqual(self.block("/google_workspace do X", env=env), "")
        self.assertEqual(self.block("/other-skill", env=env), "other_skill")


class TestIsCommandAllowed(unittest.TestCase):
    def test_empty_word_is_not_allowed(self):
        # Hermes's parser gives no word for a file path: such a turn stays conversational.
        self.assertFalse(is_command_allowed("", DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}))

    def test_words(self):
        self.assertTrue(is_command_allowed("new", DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}))
        self.assertTrue(is_command_allowed("ctx", DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}))
        self.assertFalse(is_command_allowed("yolo", DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}))


class TestCommandAllowlistKey(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(command_allowlist_key("reset"), "reset")
        self.assertEqual(command_allowlist_key("hb"), "heartbeat")
        self.assertEqual(command_allowlist_key("reload-mcp"), "reload_mcp")
        self.assertEqual(command_allowlist_key("q"), "queue")
        self.assertEqual(command_allowlist_key("pause"), "pause")
        self.assertEqual(command_allowlist_key("nosuchcommand"), "nosuchcommand")


class TestBuiltinCommandAliases(unittest.TestCase):
    def test_no_alias_shadows_a_documented_key(self):
        for alias in BUILTIN_COMMAND_ALIASES:
            with self.subTest(alias=alias):
                self.assertNotIn(alias, BUILTIN_COMMANDS)


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
    """Against the real Hermes (tests only; the plugin itself does not import this): every name and alias of
    every command is blocked unless deliberately allowed, and the alias table agrees with Hermes.
    """

    def test_every_form_of_every_command_is_blocked_or_allowlisted(self):
        commands = _hermes_commands_module()
        if commands is None:
            self.skipTest("hermes_cli.commands not importable (set HERMES_AGENT_PATH)")
        checked = 0
        for cmd in commands.COMMAND_REGISTRY:
            for form in (cmd.name, *cmd.aliases):
                checked += 1
                with self.subTest(command=cmd.name, form=form):
                    key = command_allowlist_key(form)
                    blocked = blocked_builtin_command(f"/{form} x", DEFAULT_ALLOWED_BUILTIN_COMMANDS, {})
                    self.assertEqual(blocked, "" if key in DEFAULT_ALLOWED_BUILTIN_COMMANDS else key)
        self.assertGreater(checked, 60)

    def test_an_allowlisted_form_belongs_to_an_allowlisted_command(self):
        # An alias must not open a different command than the key suggests.
        commands = _hermes_commands_module()
        if commands is None:
            self.skipTest("hermes_cli.commands not importable (set HERMES_AGENT_PATH)")
        for cmd in commands.COMMAND_REGISTRY:
            for form in (cmd.name, *cmd.aliases):
                if command_allowlist_key(form) in DEFAULT_ALLOWED_BUILTIN_COMMANDS:
                    with self.subTest(form=form):
                        self.assertIn(cmd.name, DEFAULT_ALLOWED_BUILTIN_COMMANDS | {"new"})

    def test_the_alias_table_matches_hermes(self):
        commands = _hermes_commands_module()
        if commands is None:
            self.skipTest("hermes_cli.commands not importable (set HERMES_AGENT_PATH)")
        for alias, target in BUILTIN_COMMAND_ALIASES.items():
            cmd = commands.resolve_command(alias) or commands.resolve_command(alias.replace("_", "-"))
            if cmd is None:
                continue  # an alias another Hermes version has; it only names a key
            with self.subTest(alias=alias):
                self.assertEqual(cmd.name.replace("-", "_"), target)


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


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestPresetNamesAreHermesCommands(unittest.TestCase):
    """A preset name must be the canonical name of an installed Hermes command (a renamed key would leave the command silently blocked)."""

    PLUGIN_ONLY = {"always", "cancel"}  # answers to a prompt, parsed apart from the command registry

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
