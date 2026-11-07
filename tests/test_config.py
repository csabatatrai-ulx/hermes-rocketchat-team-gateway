"""Unit tests for config.py: precedence, coercion and defaults.

The environment is passed in as a plain dict, never mutated: that is the whole reason this module exists
separately from the adapter.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (  # noqa: E402
    DEFAULT_WEBHOOK_HOST,
    DEFAULT_WEBHOOK_PORT,
    RocketChatConfig,
    FALSE_VALUES,
    TRUE_VALUES,
    as_bool,
    as_float,
    as_int,
    as_list,
    _norm_reaction,
    _normalize_transport,
    load_config,
)
from whoami import DEFAULT_WHOAMI_COMMAND  # noqa: E402
from reasoning_format import (  # noqa: E402
    DEFAULT_REASONING_LEAD_PATTERN,
    DEFAULT_REASONING_LEAK_LABEL,
)
from history import (  # noqa: E402
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_HISTORY_MIN_MESSAGES,
)
from builtin_commands import DEFAULT_ALLOWED_BUILTIN_COMMANDS  # noqa: E402

MINIMAL_ENV = {
    "ROCKETCHAT_URL": "http://localhost:3000",
    "ROCKETCHAT_TOKEN": "tok",
    "ROCKETCHAT_USER_ID": "uid",
    "ROCKETCHAT_BOT_USERNAME": "Hermes",
}


class TestAsBool(unittest.TestCase):
    def test_empty_uses_default(self):
        self.assertTrue(as_bool("", default=True))
        self.assertFalse(as_bool("", default=False))
        self.assertTrue(as_bool(None, default=True))
        self.assertFalse(as_bool(None, default=False))

    def test_false_values(self):
        for raw in ("false", "FALSE", " False ", "0", "no", "NO", "off", "Off"):
            self.assertFalse(as_bool(raw, default=True), raw)

    def test_true_values(self):
        for raw in ("true", "1", "yes", "on", " ON ", "Yes"):
            self.assertTrue(as_bool(raw, default=False), raw)

    def test_unrecognised_value_keeps_default_and_warns(self):
        # A typo can never flip a switch: in both directions the default stays and the
        # warning names the key.
        with self.assertLogs("config", level="WARNING") as logs:
            self.assertTrue(as_bool("flase", default=True, name="ROCKETCHAT_X"))
            self.assertFalse(as_bool("yse", default=False, name="ROCKETCHAT_Y"))
        self.assertIn("ROCKETCHAT_X", logs.output[0])
        self.assertIn("flase", logs.output[0])
        self.assertIn("ROCKETCHAT_Y", logs.output[1])

    def test_same_value_sets_as_builtin_command_allowlist(self):
        # builtin_commands.py keeps its own set (circular import); the two must not
        # differ.
        import builtin_commands

        self.assertEqual(set(TRUE_VALUES), builtin_commands._TRUE_VALUES)
        self.assertEqual(set(FALSE_VALUES), builtin_commands._FALSE_VALUES)


class TestLoadConfigFlags(unittest.TestCase):
    def test_off_disables_a_default_true_flag(self):
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_REQUIRE_MENTION="off"))
        self.assertFalse(cfg.require_mention)

    def test_off_keeps_allow_all_users_disabled(self):
        # "off" must never switch this on.
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_ALLOW_ALL_USERS="off"))
        self.assertFalse(cfg.allow_all_users)

    def test_typo_on_allow_all_users_stays_disabled(self):
        with self.assertLogs("config", level="WARNING") as logs:
            cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_ALLOW_ALL_USERS="ture"))
        self.assertFalse(cfg.allow_all_users)
        self.assertIn("ROCKETCHAT_ALLOW_ALL_USERS", "".join(logs.output))

    def test_dm_only_and_startup_greeting_defaults(self):
        # For a personal Hermes (DM only, home channel without a greeting).
        cfg = load_config({}, dict(MINIMAL_ENV))
        self.assertFalse(cfg.dm_only)
        self.assertTrue(cfg.startup_greeting)
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_DM_ONLY="true",
                                   ROCKETCHAT_STARTUP_GREETING="false"))
        self.assertTrue(cfg.dm_only)
        self.assertFalse(cfg.startup_greeting)

    def test_on_enables_a_default_false_flag(self):
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_ALLOW_ALL_USERS="on"))
        self.assertTrue(cfg.allow_all_users)

    def test_extra_bool_value_is_accepted(self):
        # The harness's extra mapping can also hold a real bool from YAML.
        cfg = load_config({"allow_all_users": True}, dict(MINIMAL_ENV))
        self.assertTrue(cfg.allow_all_users)
        cfg = load_config({"require_mention": False}, dict(MINIMAL_ENV))
        self.assertFalse(cfg.require_mention)


class TestInboundDedupRetention(unittest.TestCase):
    def test_default(self):
        self.assertEqual(load_config({}, dict(MINIMAL_ENV)).inbound_dedup_retention_hours, 72)

    def test_env_value(self):
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS="24"))
        self.assertEqual(cfg.inbound_dedup_retention_hours, 24)
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS="0.5"))
        self.assertEqual(cfg.inbound_dedup_retention_hours, 0.5)

    def test_invalid_or_non_positive_falls_back_to_default(self):
        for raw in ("0", "-5", "abc", "nan", "inf"):
            with self.assertLogs("config", level="WARNING"):
                cfg = load_config(
                    {}, dict(MINIMAL_ENV, ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS=raw)
                )
            self.assertEqual(cfg.inbound_dedup_retention_hours, 72, raw)


class TestAsInt(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(as_int("8646", default=1), 8646)
        self.assertEqual(as_int(" 42 ", default=1), 42)

    def test_falls_back_on_garbage(self):
        # A typo in one numeric var must not stop the integration from starting.
        for raw in ("", "abc", None, "8.5.1"):
            self.assertEqual(as_int(raw, default=7), 7, raw)


class TestAsFloat(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(as_float("2.5", default=1.0), 2.5)

    def test_falls_back_on_garbage(self):
        self.assertEqual(as_float("not a number", default=10.0), 10.0)


class TestAsList(unittest.TestCase):
    def test_splits_and_trims(self):
        self.assertEqual(as_list(" a , b ,c "), ["a", "b", "c"])

    def test_drops_blanks(self):
        self.assertEqual(as_list("a,,b,"), ["a", "b"])

    def test_empty(self):
        self.assertEqual(as_list(""), [])
        self.assertEqual(as_list(None), [])


class TestNormReaction(unittest.TestCase):
    def test_strips_colons_and_space(self):
        self.assertEqual(_norm_reaction(":eyes:"), "eyes")
        self.assertEqual(_norm_reaction("  eyes  "), "eyes")
        self.assertEqual(_norm_reaction("white_check_mark"), "white_check_mark")

    def test_disabled_values_become_empty(self):
        for raw in ("", "  ", None, "none", "off", "OFF", "false", "0", ":none:"):
            self.assertEqual(_norm_reaction(raw), "", raw)

    def test_default_env_value(self):
        cfg = load_config({}, {"ROCKETCHAT_ACK_REACTION": ":+1:"})
        self.assertEqual(cfg.ack_reaction, "+1")
        cfg_off = load_config({}, {"ROCKETCHAT_ACK_REACTION": "off"})
        self.assertEqual(cfg_off.ack_reaction, "")
        cfg_default = load_config({}, {})
        self.assertEqual(cfg_default.ack_reaction, "eyes")


class TestPrecedence(unittest.TestCase):
    # Env over YAML: Hermes's documented rule, as on every Hermes platform (see
    # config.py's module docstring).
    def test_env_is_used_when_extra_is_absent(self):
        cfg = load_config({}, MINIMAL_ENV)
        self.assertEqual(cfg.base_url, "http://localhost:3000")
        self.assertEqual(cfg.bot_username, "Hermes")

    def test_env_wins_over_extra(self):
        cfg = load_config({"bot_username": "Hermes-Beta"}, MINIMAL_ENV)
        self.assertEqual(cfg.bot_username, "Hermes")

    def test_extra_is_used_when_env_is_absent_or_empty(self):
        # An .env may hold empty values to mean "unset"; an empty
        # string must not shadow the YAML value.
        env = {k: v for k, v in MINIMAL_ENV.items() if k != "ROCKETCHAT_BOT_USERNAME"}
        self.assertEqual(load_config({"bot_username": "Hermes-Beta"}, env).bot_username, "Hermes-Beta")
        env["ROCKETCHAT_BOT_USERNAME"] = ""
        self.assertEqual(load_config({"bot_username": "Hermes-Beta"}, env).bot_username, "Hermes-Beta")

    def test_empty_extra_falls_through_to_env(self):
        cfg = load_config({"bot_username": ""}, MINIMAL_ENV)
        self.assertEqual(cfg.bot_username, "Hermes")

    def test_empty_env_falls_through_to_default(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_WEBHOOK_HOST="")
        self.assertEqual(load_config({}, env).webhook_host, DEFAULT_WEBHOOK_HOST)

    def test_token_argument_is_the_yaml_level_fallback(self):
        # The harness carries the YAML token on the config object, not in
        # extra; the env key still wins (env > YAML).
        env = {k: v for k, v in MINIMAL_ENV.items() if k != "ROCKETCHAT_TOKEN"}
        self.assertEqual(load_config({}, env, token="from-harness").token, "from-harness")
        cfg = load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_TOKEN="from-env"), token="from-harness")
        self.assertEqual(cfg.token, "from-env")

    def test_token_falls_back_to_env(self):
        self.assertEqual(load_config({}, MINIMAL_ENV, token="").token, "tok")




class TestDefaults(unittest.TestCase):
    def test_all_defaults_with_minimal_env(self):
        cfg = load_config({}, MINIMAL_ENV)
        self.assertEqual(cfg.webhook_host, DEFAULT_WEBHOOK_HOST)
        self.assertEqual(cfg.webhook_port, DEFAULT_WEBHOOK_PORT)
        self.assertTrue(cfg.require_mention)
        self.assertFalse(cfg.allow_all_users)
        self.assertEqual(cfg.allowed_users, [])
        self.assertFalse(cfg.reasoning_leak_formatting_enabled)
        self.assertEqual(cfg.reasoning_leak_pattern, DEFAULT_REASONING_LEAD_PATTERN)
        self.assertEqual(cfg.reasoning_leak_label, DEFAULT_REASONING_LEAK_LABEL)
        self.assertEqual(cfg.outbound_dedup_ttl_seconds, 10.0)
        self.assertFalse(cfg.always_fresh_final_streaming)
        self.assertEqual(cfg.history_limit, DEFAULT_HISTORY_LIMIT)
        self.assertEqual(cfg.history_min_messages, DEFAULT_HISTORY_MIN_MESSAGES)
        self.assertEqual(cfg.history_token_budget, 0)

    def test_url_trailing_slash_is_stripped(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_URL="http://localhost:3000///")
        self.assertEqual(load_config({}, env).base_url, "http://localhost:3000")


class TestTransportConfig(unittest.TestCase):
    """ROCKETCHAT_TRANSPORT; see RocketChatConfig.transport."""

    def test_webhook_is_accepted(self):
        self.assertEqual(_normalize_transport("webhook"), "webhook")

    def test_ddp_is_accepted(self):
        self.assertEqual(_normalize_transport("ddp"), "ddp")

    def test_case_insensitive(self):
        self.assertEqual(_normalize_transport("WEBHOOK"), "webhook")

    def test_unset_defaults_to_ddp(self):
        self.assertEqual(_normalize_transport(""), "ddp")
        self.assertEqual(_normalize_transport(None), "ddp")

    def test_removed_both_value_now_falls_back_to_ddp(self):
        self.assertEqual(_normalize_transport("both"), "ddp")

    def test_load_config_defaults_to_ddp(self):
        self.assertEqual(load_config({}, MINIMAL_ENV).transport, "ddp")

    def test_load_config_reads_rocketchat_transport(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_TRANSPORT="webhook")
        self.assertEqual(load_config({}, env).transport, "webhook")


class TestDeploymentSettings(unittest.TestCase):
    """Non-default values an operator is expected to set (see docs/configuration.md)."""

    def test_dedup_can_be_disabled(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_OUTBOUND_DEDUP_TTL_SECONDS="0")
        self.assertEqual(load_config({}, env).outbound_dedup_ttl_seconds, 0.0)

    def test_always_fresh_final_streaming_can_be_enabled(self):
        # Off by default (see config.py comment) - explicit opt-in only.
        env = dict(MINIMAL_ENV, ROCKETCHAT_ALWAYS_FRESH_FINAL_STREAMING="true")
        self.assertTrue(load_config({}, env).always_fresh_final_streaming)


class TestDedupStateDirConfig(unittest.TestCase):
    """Restart-surviving dedup persistence; see dedup.py."""

    def test_unset_computes_default_under_hermes_home(self):
        env = dict(MINIMAL_ENV, HERMES_HOME="/home/x/.hermes/profiles/claude")
        cfg = load_config({}, env)
        self.assertEqual(
            cfg.dedup_state_dir,
            "/home/x/.hermes/profiles/claude/rocketchat-plugin-dedup",
        )

    def test_unset_falls_back_to_tilde_hermes_without_hermes_home(self):
        env = dict(MINIMAL_ENV)
        env.pop("HERMES_HOME", None)
        cfg = load_config({}, env)
        self.assertEqual(
            cfg.dedup_state_dir,
            os.path.expanduser("~/.hermes/rocketchat-plugin-dedup"),
        )

    def test_off_word_disables_it(self):
        for value in ("off", "none", "OFF", "disabled", "false", "0"):
            env = dict(MINIMAL_ENV, ROCKETCHAT_DEDUP_STATE_DIR=value)
            self.assertEqual(load_config({}, env).dedup_state_dir, "", msg=value)

    def test_explicit_value_used_verbatim(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_DEDUP_STATE_DIR="/custom/dedup-dir")
        self.assertEqual(load_config({}, env).dedup_state_dir, "/custom/dedup-dir")


class TestDdpCatchupConfig(unittest.TestCase):
    def test_default_is_ten_minutes(self):
        self.assertEqual(load_config({}, MINIMAL_ENV).ddp_catchup_max_seconds, 600)

    def test_zero_disables(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_DDP_CATCHUP_MAX_SECONDS="0")
        self.assertEqual(load_config({}, env).ddp_catchup_max_seconds, 0)

    def test_custom_value(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_DDP_CATCHUP_MAX_SECONDS="120")
        self.assertEqual(load_config({}, env).ddp_catchup_max_seconds, 120)


class TestWhoamiConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config({}, MINIMAL_ENV)
        self.assertTrue(cfg.whoami_enabled)
        self.assertEqual(cfg.whoami_command, DEFAULT_WHOAMI_COMMAND)

    def test_can_be_disabled(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_WHOAMI_ENABLED="false")
        self.assertFalse(load_config({}, env).whoami_enabled)

    def test_custom_command(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_WHOAMI_COMMAND="/ki-vagyok")
        self.assertEqual(load_config({}, env).whoami_command, "/ki-vagyok")


class TestHistoryConfig(unittest.TestCase):
    """The room-context keys."""

    def cfg(self, **env):
        return load_config({}, dict(MINIMAL_ENV, **env))

    def test_defaults(self):
        cfg = self.cfg()
        self.assertEqual((cfg.history_limit, cfg.history_min_messages), (50, 20))
        self.assertEqual(cfg.history_token_budget, 0)

    def test_limit_zero_disables(self):
        self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_LIMIT="0").history_limit, 0)
        self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_LIMIT="0").history_min_messages, 0)

    def test_limit_is_capped_at_100(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_LIMIT="500").history_limit, 100)

    def test_bad_or_negative_limit_keeps_the_default(self):
        for raw in ("sok", "-3"):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_LIMIT=raw).history_limit, 50)

    def test_min_messages_is_at_most_the_limit(self):
        cfg = self.cfg(ROCKETCHAT_HISTORY_LIMIT="10", ROCKETCHAT_HISTORY_MIN_MESSAGES="30")
        self.assertEqual((cfg.history_limit, cfg.history_min_messages), (10, 10))
        self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_MIN_MESSAGES="0").history_min_messages, 0)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_MIN_MESSAGES="x").history_min_messages, 20)

    def test_env_wins_over_extra(self):
        cfg = load_config({"history_limit": "5"}, dict(MINIMAL_ENV, ROCKETCHAT_HISTORY_LIMIT="40"))
        self.assertEqual(cfg.history_limit, 40)

    def test_token_budget(self):
        self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_TOKEN_BUDGET="5000").history_token_budget, 5000)
        self.assertEqual(self.cfg(ROCKETCHAT_HISTORY_TOKEN_BUDGET="-1").history_token_budget, 0)


class TestBuiltinCommandsConfig(unittest.TestCase):
    """Confirms load_config() actually wires builtin_commands.py end to end -
    see test_builtin_commands.py for the allowlist logic itself."""

    def test_default_allows_only_new_and_reset(self):
        self.assertEqual(
            load_config({}, MINIMAL_ENV).allowed_builtin_commands,
            DEFAULT_ALLOWED_BUILTIN_COMMANDS,
        )

    def test_env_flag_reaches_the_config_object(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_ALLOW_BUILTIN_STATUS="true")
        self.assertIn("status", load_config({}, env).allowed_builtin_commands)

    def test_high_risk_command_stays_off_through_the_full_pipeline(self):
        self.assertNotIn("yolo", load_config({}, MINIMAL_ENV).allowed_builtin_commands)


class TestRequiredFields(unittest.TestCase):
    def test_complete_config(self):
        cfg = load_config({}, MINIMAL_ENV)
        self.assertTrue(cfg.is_complete)
        self.assertEqual(cfg.missing_required(), [])

    def test_names_what_is_missing(self):
        # "not configured" without saying WHICH field costs someone an hour.
        cfg = load_config({}, {"ROCKETCHAT_URL": "http://x"})
        self.assertEqual(cfg.missing_required(), ["token", "user_id", "bot_username"])

    def test_empty_config_is_incomplete(self):
        self.assertFalse(load_config({}, {}).is_complete)

    def test_webhook_transport_requires_the_secret(self):
        # FAIL CLOSED: without a secret the webhook listener would accept anyone.
        env = dict(MINIMAL_ENV, ROCKETCHAT_TRANSPORT="webhook")
        cfg = load_config({}, env)
        self.assertEqual(cfg.missing_required(), ["webhook_secret"])
        self.assertFalse(cfg.is_complete)

    def test_webhook_transport_with_a_secret_is_complete(self):
        env = dict(
            MINIMAL_ENV, ROCKETCHAT_TRANSPORT="webhook", ROCKETCHAT_WEBHOOK_SECRET="s3cret"
        )
        self.assertTrue(load_config({}, env).is_complete)

    def test_ddp_transport_does_not_need_the_secret(self):
        env = dict(MINIMAL_ENV, ROCKETCHAT_TRANSPORT="ddp")
        self.assertEqual(load_config({}, env).missing_required(), [])


class TestImmutability(unittest.TestCase):
    def test_config_is_frozen(self):
        # A setting changing mid-process would be a bug: the gateway re-reads
        # configuration by restarting.
        cfg = load_config({}, MINIMAL_ENV)
        with self.assertRaises(Exception):
            cfg.bot_username = "something else"  # type: ignore[misc]

    def test_defaults_are_not_shared_between_instances(self):
        a = RocketChatConfig()
        b = RocketChatConfig()
        self.assertIsNot(a.allowed_users, b.allowed_users)


class TestOutboundFilesSwitch(unittest.TestCase):
    # Env only (the guidance sees the same value at registration).
    def test_default_on_env_off_extra_ignored(self):
        self.assertTrue(load_config({}, MINIMAL_ENV).outbound_files)
        self.assertFalse(load_config({}, dict(MINIMAL_ENV, ROCKETCHAT_OUTBOUND_FILES="false")).outbound_files)
        self.assertTrue(load_config({"outbound_files": "false"}, MINIMAL_ENV).outbound_files)


if __name__ == "__main__":
    unittest.main()


class StartupCatchupConfigTests(unittest.TestCase):
    """The startup check."""

    def test_defaults(self):
        cfg = load_config({}, dict(MINIMAL_ENV))
        self.assertEqual(cfg.startup_catchup_mode, "notice")
        self.assertEqual(cfg.startup_catchup_max_seconds, 600)
        self.assertEqual(cfg.startup_catchup_max_dispatches, 10)
        
    def test_values_from_env(self):
        cfg = load_config({}, dict(
            MINIMAL_ENV, ROCKETCHAT_STARTUP_CATCHUP_MODE="Replay",
            ROCKETCHAT_STARTUP_CATCHUP_MAX_SECONDS="120",
            ROCKETCHAT_STARTUP_CATCHUP_MAX_DISPATCHES="3",
        ))
        self.assertEqual(
            (cfg.startup_catchup_mode, cfg.startup_catchup_max_seconds,
             cfg.startup_catchup_max_dispatches),
            ("replay", 120, 3),
        )

    def test_unknown_mode_falls_back_with_a_warning(self):
        with self.assertLogs("config", level="WARNING") as logs:
            cfg = load_config({}, dict(
                MINIMAL_ENV, ROCKETCHAT_STARTUP_CATCHUP_MODE="always",
            ))
        self.assertEqual(cfg.startup_catchup_mode, "notice")
        self.assertEqual(len(logs.output), 1)

    def test_negative_window_disables_and_dispatch_cap_is_at_least_one(self):
        cfg = load_config({}, dict(
            MINIMAL_ENV, ROCKETCHAT_STARTUP_CATCHUP_MAX_SECONDS="-5",
            ROCKETCHAT_STARTUP_CATCHUP_MAX_DISPATCHES="0",
        ))
        self.assertEqual((cfg.startup_catchup_max_seconds, cfg.startup_catchup_max_dispatches), (0, 1))
