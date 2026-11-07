"""Unit tests for inbound.py: mention matching and webhook filtering.

Two live bugs are pinned down here as named regression cases:

* ``@Hermes`` must NOT match ``@Hermes-Beta`` (both bots answered one message)
* a repeat delivery of the same ``message_id`` must be recognised
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inbound import (  # noqa: E402
    HANDLE_BUILTIN_COMMAND_BLOCKED,
    HANDLE_DM_ONLY_NOTICE,
    HANDLE_MESSAGE,
    IGNORE_DUPLICATE,
    IGNORE_NO_MENTION,
    IGNORE_NOT_ALLOWED,
    IGNORE_SELF,
    IGNORE_SYSTEM_MESSAGE,
    IGNORE_STALE,
    REJECT_BAD_BODY,
    REJECT_BAD_TOKEN,
    build_mention_pattern,
    classify_inbound,
    has_mention,
    is_own_message,
    is_sender_allowed,
    parse_webhook_body,
    secret_matches,
    strip_mention,
)
from builtin_commands import (  # noqa: E402
    DEFAULT_ALLOWED_BUILTIN_COMMANDS,
    bang_to_slash,
    blocked_builtin_command,
)

BOT = "Hermes"
BOT_UID = "botuid"
_MISSING = object()  # "no timestamp key" marker for the age-limit tests


def body(**overrides) -> bytes:
    """A realistic Outgoing Webhook payload, overridable per test."""
    payload = {
        "token": "",
        "message_id": "m1",
        "channel_id": "room1",
        "channel_name": "general",
        "user_id": "user1",
        "user_name": "alice",
        "text": f"@{BOT} hello!",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class TestMentionPattern(unittest.TestCase):
    def test_plain_mention_matches(self):
        self.assertTrue(has_mention("@Hermes hello", build_mention_pattern("Hermes")))

    def test_case_insensitive(self):
        self.assertTrue(has_mention("@hermes hello", build_mention_pattern("Hermes")))

    def test_mention_anywhere_in_the_message(self):
        self.assertTrue(has_mention("hello @Hermes how are you?", build_mention_pattern("Hermes")))

    def test_REGRESSION_hermes_does_not_match_hermes_claude(self):
        # The live bug: "Hermes" is a prefix of "Hermes-Beta", so a substring
        # match made BOTH bots answer the same message.
        self.assertFalse(
            has_mention("@Hermes-Beta hello", build_mention_pattern("Hermes"))
        )

    def test_the_longer_bot_still_matches_its_own_mention(self):
        self.assertTrue(
            has_mention("@Hermes-Beta hello", build_mention_pattern("Hermes-Beta"))
        )

    def test_other_username_characters_also_block_the_match(self):
        for suffix in ("2", "_test", ".bot", "-x"):
            self.assertFalse(
                has_mention(f"@Hermes{suffix} hello", build_mention_pattern("Hermes")),
                suffix,
            )

    def test_punctuation_after_the_mention_still_matches(self):
        for suffix in (",", ":", "!", " ", ""):
            self.assertTrue(
                has_mention(f"@Hermes{suffix}", build_mention_pattern("Hermes")), suffix
            )

    def test_no_mention_in_plain_text(self):
        self.assertFalse(has_mention("hello everyone", build_mention_pattern("Hermes")))

    def test_empty_text(self):
        self.assertFalse(has_mention("", build_mention_pattern("Hermes")))

    def test_regex_metacharacters_in_the_username_are_escaped(self):
        # A username is operator-supplied; an unescaped "." would match any char.
        self.assertFalse(has_mention("@HermesX", build_mention_pattern("Hermes.")))


class TestStripMention(unittest.TestCase):
    def test_removes_the_mention_and_trims(self):
        pattern = build_mention_pattern("Hermes")
        self.assertEqual(strip_mention("@Hermes  hello!", pattern), "hello!")

    def test_removes_a_mid_sentence_mention(self):
        pattern = build_mention_pattern("Hermes")
        self.assertEqual(strip_mention("hello @Hermes how are you?", pattern), "hello  how are you?")

    def test_text_without_mention_is_only_trimmed(self):
        self.assertEqual(strip_mention("  hello  ", build_mention_pattern("Hermes")), "hello")

    def test_mention_only_message_becomes_empty(self):
        self.assertEqual(strip_mention("@Hermes", build_mention_pattern("Hermes")), "")


class TestParseWebhookBody(unittest.TestCase):
    def test_json(self):
        self.assertEqual(parse_webhook_body(b'{"a": 1}'), {"a": 1})

    def test_form_encoded_fallback(self):
        # Accepted so a hand-rolled curl test doesn't look like a broken
        # integration.
        self.assertEqual(parse_webhook_body(b"a=1&b=2"), {"a": "1", "b": "2"})

    def test_garbage(self):
        self.assertIsNone(parse_webhook_body(b"\xff\xfe not text"))

    def test_json_array_is_rejected(self):
        # A list would break every payload.get() downstream.
        self.assertIsNone(parse_webhook_body(b"[1, 2]"))

    def test_empty_body(self):
        # parse_qsl never fails, it just yields nothing - so an empty result
        # from the fallback must not be mistaken for a valid empty payload.
        self.assertIsNone(parse_webhook_body(b""))

    def test_explicit_empty_json_object_is_kept(self):
        # Different from an empty body: this one actually parsed. 
        self.assertEqual(parse_webhook_body(b"{}"), {})


class TestSecretMatches(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(secret_matches("secret", "secret"))

    def test_not_matching(self):
        self.assertFalse(secret_matches("other", "secret"))

    def test_missing_token(self):
        self.assertFalse(secret_matches(None, "secret"))
        self.assertFalse(secret_matches("", "secret"))

    def test_no_configured_secret_accepts_anything(self):
        # Documented "no secret configured" mode, not an oversight.
        self.assertTrue(secret_matches("anything", ""))
        self.assertTrue(secret_matches(None, ""))

    def test_different_lengths_do_not_raise(self):
        # compare_digest needs equal-length inputs for its timing guarantee;
        # it must not blow up on a short/long mismatch.
        self.assertFalse(secret_matches("short", "much-longer-secret"))


class TestIsOwnMessage(unittest.TestCase):
    def test_bot_flag(self):
        self.assertTrue(is_own_message({"bot": True}, BOT_UID, BOT))

    def test_matching_user_id(self):
        self.assertTrue(is_own_message({"user_id": BOT_UID}, BOT_UID, BOT))

    def test_matching_username_case_insensitively(self):
        self.assertTrue(is_own_message({"user_name": "hermes"}, BOT_UID, BOT))

    def test_someone_else(self):
        self.assertFalse(
            is_own_message({"user_id": "u1", "user_name": "alice"}, BOT_UID, BOT)
        )

    def test_the_other_bot_is_not_us(self):
        self.assertFalse(is_own_message({"user_name": "Hermes-Beta"}, BOT_UID, BOT))


class TestIsSenderAllowed(unittest.TestCase):
    def test_empty_allowlist_allows_everyone(self):
        self.assertTrue(is_sender_allowed("u1", "someone", [], False))
        self.assertTrue(is_sender_allowed("u1", "someone", None, False))

    def test_allow_all_overrides_the_list(self):
        self.assertTrue(is_sender_allowed("u1", "someone", ["other"], True))

    def test_id_on_the_list(self):
        self.assertTrue(is_sender_allowed("u1", "someone", ["u1"], False))

    def test_username_on_the_list(self):
        self.assertTrue(is_sender_allowed("u1", "someone", ["someone"], False))

    def test_not_on_the_list(self):
        self.assertFalse(is_sender_allowed("u1", "someone", ["other"], False))


class TestClassifyInboundDmOnly(unittest.TestCase):
    """ROCKETCHAT_DM_ONLY: never a turn in a room."""

    def classify(self, raw=None, **kwargs):
        kwargs.setdefault("bot_username", BOT)
        kwargs.setdefault("bot_user_id", BOT_UID)
        kwargs.setdefault("dm_only", True)
        return classify_inbound(raw if raw is not None else body(), **kwargs)

    def test_room_mention_becomes_a_notice_not_a_turn(self):
        d = self.classify(body(room_type="c"))
        self.assertEqual(d.outcome, HANDLE_DM_ONLY_NOTICE)
        self.assertTrue(d.should_handle)
        self.assertEqual(d.text, "")

    def test_direct_message_still_runs_a_turn(self):
        d = self.classify(body(text="hi", room_type="d"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)

    def test_group_direct_counts_as_a_room(self):
        d = self.classify(body(text="hi", room_type="d"), group_direct=True)
        self.assertEqual(d.outcome, IGNORE_NO_MENTION)
        d = self.classify(body(text=f"@{BOT} hello", room_type="d"), group_direct=True)
        self.assertEqual(d.outcome, HANDLE_DM_ONLY_NOTICE)

    def test_room_chatter_without_mention_stays_silent(self):
        d = self.classify(body(text="hello everyone", room_type="c"))
        self.assertEqual(d.outcome, IGNORE_NO_MENTION)

    def test_notice_comes_before_the_allowlist_and_the_dedup(self):
        seen = []
        d = self.classify(
            body(room_type="c"),
            allowed_users=["owner"],
            is_duplicate=lambda key: seen.append(key) or False,
        )
        self.assertEqual(d.outcome, HANDLE_DM_ONLY_NOTICE)
        self.assertEqual(seen, [])

    def test_off_by_default(self):
        d = classify_inbound(body(room_type="c"), bot_username=BOT, bot_user_id=BOT_UID)
        self.assertEqual(d.outcome, HANDLE_MESSAGE)


class TestClassifyInbound(unittest.TestCase):
    def classify(self, raw=None, **kwargs):
        kwargs.setdefault("bot_username", BOT)
        kwargs.setdefault("bot_user_id", BOT_UID)
        return classify_inbound(raw if raw is not None else body(), **kwargs)

    def test_normal_mention_is_handled(self):
        d = self.classify()
        self.assertEqual(d.outcome, HANDLE_MESSAGE)
        self.assertEqual(d.text, "hello!")
        self.assertEqual(d.message_id, "m1")
        self.assertEqual(d.channel_id, "room1")
        self.assertEqual(d.channel_name, "general")
        self.assertEqual(d.sender_username, "alice")
        self.assertTrue(d.should_handle)
        self.assertEqual(d.http_status, 200)
        self.assertEqual(d.thread_id, "")

    def test_unparseable_body(self):
        d = self.classify(b"\xff not json")
        self.assertEqual(d.outcome, REJECT_BAD_BODY)
        self.assertEqual(d.http_status, 400)
        self.assertFalse(d.should_handle)

    def test_wrong_secret(self):
        d = self.classify(body(token="wrong"), webhook_secret="secret")
        self.assertEqual(d.outcome, REJECT_BAD_TOKEN)
        self.assertEqual(d.http_status, 401)

    def test_right_secret(self):
        d = self.classify(body(token="secret"), webhook_secret="secret")
        self.assertEqual(d.outcome, HANDLE_MESSAGE)

    def test_own_message(self):
        d = self.classify(body(user_id=BOT_UID))
        self.assertEqual(d.outcome, IGNORE_SELF)

    def test_system_message_starts_no_turn(self):
        # A call's ``videoconf`` system message started a turn with the text "Conference
        # Call". A typed message starts no turn, in a DM or with a mention, and does not
        # enter the dedup.
        seen = []
        for kind in ("videoconf", "uj", "au", "r"):
            for raw in (body(message_type=kind, room_type="d", channel_name=""),
                        body(message_type=kind)):
                d = self.classify(raw, is_duplicate=lambda key: seen.append(key) or False)
                self.assertEqual(d.outcome, IGNORE_SYSTEM_MESSAGE, kind)
                self.assertFalse(d.should_handle)
                self.assertEqual(d.payload["message_type"], kind)
        self.assertEqual(seen, [])

    def test_empty_message_type_is_ordinary_chat(self):
        self.assertEqual(self.classify(body(message_type="")).outcome, HANDLE_MESSAGE)

    def test_no_mention_is_ignored(self):
        d = self.classify(body(text="hello everyone"))
        self.assertEqual(d.outcome, IGNORE_NO_MENTION)

    def test_dm_without_mention_is_handled(self):
        # A DM is addressed to the bot by definition (DDP roomType "d").
        d = self.classify(body(text="who am I?", room_type="d"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)
        self.assertEqual(d.text, "who am I?")

    def test_dm_with_mention_still_strips_it(self):
        d = self.classify(body(text=f"@{BOT} who am I?", room_type="d"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)
        self.assertEqual(d.text, "who am I?")

    def test_webhook_dm_without_mention_is_handled(self):
        # Outgoing Webhook: no room type, and a DM has no name -> RC omits
        # channel_name entirely (verified live).
        payload = json.loads(body(text="who am I?"))
        del payload["channel_name"]
        d = self.classify(json.dumps(payload).encode("utf-8"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)

    def test_channel_without_mention_still_ignored(self):
        d = self.classify(body(text="hello everyone", room_type="c"))
        self.assertEqual(d.outcome, IGNORE_NO_MENTION)

    def test_no_mention_is_handled_when_not_required(self):
        d = self.classify(body(text="hello everyone"), require_mention=False)
        self.assertEqual(d.outcome, HANDLE_MESSAGE)
        self.assertEqual(d.text, "hello everyone")

    def test_REGRESSION_other_bots_mention_is_ignored(self):
        # The live bug, end to end: a message addressed to @Hermes-Beta must
        # not reach the @Hermes bot.
        d = self.classify(body(text="@Hermes-Beta hello"))
        self.assertEqual(d.outcome, IGNORE_NO_MENTION)

    def test_sender_not_allowed(self):
        d = self.classify(allowed_users=["someoneelse"])
        self.assertEqual(d.outcome, IGNORE_NOT_ALLOWED)

class TestClassifyInboundBuiltinCommand(unittest.TestCase):
    """The allowlist guard (see builtin_commands.py): without it, ANY sender could run a privileged Hermes command through this adapter."""

    def classify(self, raw=None, allowed=None, **kwargs):
        kwargs.setdefault("bot_username", BOT)
        kwargs.setdefault("bot_user_id", BOT_UID)
        effective_allowed = DEFAULT_ALLOWED_BUILTIN_COMMANDS if allowed is None else allowed
        kwargs.setdefault(
            "blocked_builtin_command",
            lambda text: blocked_builtin_command(text, effective_allowed, {}),
        )
        return classify_inbound(raw if raw is not None else body(), **kwargs)

    def test_default_allowed_command_reaches_the_model(self):
        d = self.classify(body(text=f"@{BOT} /new"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)
        self.assertEqual(d.text, "/new")

    def test_non_allowlisted_builtin_command_is_blocked(self):
        d = self.classify(body(text=f"@{BOT} /yolo on"))
        self.assertEqual(d.outcome, HANDLE_BUILTIN_COMMAND_BLOCKED)
        self.assertEqual(d.builtin_command_name, "yolo")
        self.assertTrue(d.should_handle)
        self.assertEqual(d.http_status, 200)

    def test_explicitly_allowed_command_reaches_the_model(self):
        d = self.classify(body(text=f"@{BOT} /status"), allowed=frozenset({"status"}))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)

    def test_unrecognised_slash_text_is_blocked(self):
        d = self.classify(body(text=f"@{BOT} /nosuchcommand something"))
        self.assertEqual(d.outcome, HANDLE_BUILTIN_COMMAND_BLOCKED)
        self.assertEqual(d.builtin_command_name, "nosuchcommand")

    def test_a_typed_bang_command_is_guarded_like_a_slash(self):
        typed = lambda text: bang_to_slash(text, DEFAULT_ALLOWED_BUILTIN_COMMANDS, {})  # noqa: E731
        d = self.classify(body(text=f"@{BOT} !yolo on"), typed_command=typed)
        self.assertEqual(d.outcome, HANDLE_BUILTIN_COMMAND_BLOCKED)
        d = self.classify(body(text=f"@{BOT} !new"), typed_command=typed)
        self.assertEqual((d.outcome, d.text), (HANDLE_MESSAGE, "/new"))
        d = self.classify(body(text=f"@{BOT} !important"), typed_command=typed)
        self.assertEqual((d.outcome, d.text), (HANDLE_MESSAGE, "!important"))

    def test_plain_text_is_never_blocked(self):
        d = self.classify(body(text=f"@{BOT} what's the status?"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)

    def test_no_guard_injected_falls_through_unchanged(self):
        # Backward-compat: a caller that does not inject
        # ``blocked_builtin_command`` (e.g. an older test) must behave
        # exactly as before this feature existed.
        d = classify_inbound(
            body(text=f"@{BOT} /yolo on"),
            bot_username=BOT,
            bot_user_id=BOT_UID,
        )
        self.assertEqual(d.outcome, HANDLE_MESSAGE)

    def test_missing_allowlist_fails_closed_not_open(self):
        # allowed_builtin_commands=None (injected guard present, but no
        # allowlist given) must behave as an EMPTY allowlist, not as
        # "allow everything" - see the comment above the check in inbound.py.
        d = self.classify(body(text=f"@{BOT} /new"), allowed=frozenset())
        self.assertEqual(d.outcome, HANDLE_BUILTIN_COMMAND_BLOCKED)


class TestClassifyInboundThread(unittest.TestCase):
    """The payload's ``tmid`` is the decision's ``thread_id``."""

    def classify(self, raw=None, **kwargs):
        kwargs.setdefault("bot_username", BOT)
        kwargs.setdefault("bot_user_id", BOT_UID)
        return classify_inbound(raw if raw is not None else body(), **kwargs)

    def test_webhook_tmid_becomes_the_thread_id(self):
        d = self.classify(body(tmid="root-1"))
        self.assertEqual(d.outcome, HANDLE_MESSAGE)
        self.assertEqual(d.thread_id, "root-1")

    def test_main_channel_has_no_thread_id(self):
        self.assertEqual(self.classify(body(tmid="")).thread_id, "")
        self.assertEqual(self.classify().thread_id, "")

    def test_commands_carry_the_thread_too(self):
        d = self.classify(
            body(text=f"@{BOT} /whoami", tmid="root-1"),
            is_whoami_command=lambda text, command: text == command,
        )
        self.assertEqual(d.thread_id, "root-1")
        d = self.classify(
            body(text=f"@{BOT} /model x", tmid="root-1"),
            blocked_builtin_command=lambda text: blocked_builtin_command(
                text, DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}
            ),
        )
        self.assertEqual(d.outcome, HANDLE_BUILTIN_COMMAND_BLOCKED)
        self.assertEqual(d.thread_id, "root-1")


class TestRemovedHistoryCommand(unittest.TestCase):
    """The gateway has no /history command of its own; the text goes on to the Hermes command guard."""

    def classify(self, text, **kwargs):
        return classify_inbound(
            body(text=f"@{BOT} {text}"), bot_username=BOT, bot_user_id=BOT_UID, **kwargs
        )

    def test_history_forms_are_ordinary_text_without_the_guard(self):
        for text in ("/history status", "/history on", "/history off question"):
            d = self.classify(text)
            self.assertEqual(d.outcome, HANDLE_MESSAGE, text)
            self.assertEqual(d.text, text)

    def test_history_status_is_refused_by_the_command_guard(self):
        d = self.classify(
            "/history status",
            blocked_builtin_command=lambda text: blocked_builtin_command(
                text, DEFAULT_ALLOWED_BUILTIN_COMMANDS, {}
            ),
        )
        self.assertEqual(d.outcome, HANDLE_BUILTIN_COMMAND_BLOCKED)
        self.assertEqual(d.builtin_command_name, "history")


class TestClassifyInboundDedup(unittest.TestCase):
    """The dedup step, and its position in the chain."""

    def setUp(self):
        self.seen = set()

        def is_duplicate(key):
            if key in self.seen:
                return True
            self.seen.add(key)
            return False

        self.is_duplicate = is_duplicate

    def classify(self, raw=None, **kwargs):
        return classify_inbound(
            raw if raw is not None else body(),
            bot_username=BOT,
            bot_user_id=BOT_UID,
            is_duplicate=self.is_duplicate,
            **kwargs,
        )

    def test_first_delivery_is_handled(self):
        self.assertEqual(self.classify().outcome, HANDLE_MESSAGE)

    def test_second_delivery_of_the_same_id_is_a_duplicate(self):
        self.classify()
        self.assertEqual(self.classify().outcome, IGNORE_DUPLICATE)

    def test_a_different_message_id_is_not_a_duplicate(self):
        self.classify()
        self.assertEqual(self.classify(body(message_id="m2")).outcome, HANDLE_MESSAGE)

    def test_the_key_is_namespaced_per_bot(self):
        # One Rocket.Chat message reaches every bot; an un-namespaced key would
        # let the first bot's delivery mask the second bot's.
        self.classify()
        self.assertIn(f"{BOT}:m1", self.seen)

    def test_own_messages_do_not_consume_cache_entries(self):
        # The self-filter runs BEFORE the dedup on purpose.
        self.classify(body(user_id=BOT_UID))
        self.assertEqual(self.seen, set())

    def test_room_chatter_does_not_consume_cache_entries(self):
        # The dedup runs AFTER the mention/DM and allowlist filters, so room chatter
        # does not push out an old @mention's id.
        first = self.classify(body(text="hello everyone"))
        self.assertEqual(first.outcome, IGNORE_NO_MENTION)
        self.assertEqual(self.seen, set())

    def test_a_not_allowed_sender_does_not_consume_cache_entries(self):
        d = self.classify(allowed_users=["valaki-mas"])
        self.assertEqual(d.outcome, IGNORE_NOT_ALLOWED)
        self.assertEqual(self.seen, set())

    def test_thread_root_redelivered_for_a_reply_count_is_a_duplicate(self):
        # A thread reply makes the root arrive again as a `changed` event (tcount/tlm):
        # the dedup drops it; a thread mention is a new message with its own id.
        self.classify(body(message_id="root-1"))
        again = self.classify(body(message_id="root-1", tcount=1, tlm="2026-10-06T10:00:00.000Z"))
        self.assertEqual(again.outcome, IGNORE_DUPLICATE)
        reply = self.classify(body(message_id="reply-1", tmid="root-1"))
        self.assertEqual(reply.outcome, HANDLE_MESSAGE)
        self.assertEqual(reply.thread_id, "root-1")

    def test_a_redelivered_command_is_a_duplicate_too(self):
        # The dedup runs before EVERY command: a resend caused by a reaction does not
        # re-run /whoami.
        self.classify(body(text=f"@{BOT} /whoami"))
        self.assertIn(f"{BOT}:m1", self.seen)
        self.assertEqual(
            self.classify(body(text=f"@{BOT} /whoami")).outcome, IGNORE_DUPLICATE
        )

    def test_a_bad_secret_is_rejected_before_the_cache_is_touched(self):
        self.classify(body(token="wrong"), webhook_secret="secret")
        self.assertEqual(self.seen, set())

    def test_missing_message_id_is_never_a_duplicate(self):
        self.assertEqual(self.classify(body(message_id="")).outcome, HANDLE_MESSAGE)
        self.assertEqual(self.classify(body(message_id="")).outcome, HANDLE_MESSAGE)


class TestClassifyInboundAgeGuard(unittest.TestCase):
    """The age limit: a message older than the retention window never reaches the agent."""

    NOW = 1_800_000_000.0  # pinned clock
    RETENTION = 72 * 3600.0

    def setUp(self):
        self.seen = set()

    def is_duplicate(self, key):
        if key in self.seen:
            return True
        self.seen.add(key)
        return False

    def classify(self, timestamp, **kwargs):
        payload = {} if timestamp is _MISSING else {"timestamp": timestamp}
        return classify_inbound(
            body(**payload),
            bot_username=BOT,
            bot_user_id=BOT_UID,
            is_duplicate=self.is_duplicate,
            max_message_age_seconds=self.RETENTION,
            now_fn=lambda: self.NOW,
            **kwargs,
        )

    def iso(self, epoch):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def test_old_iso_timestamp_is_stale_and_not_recorded(self):
        d = self.classify(self.iso(self.NOW - self.RETENTION - 60))
        self.assertEqual(d.outcome, IGNORE_STALE)
        self.assertEqual(self.seen, set())

    def test_old_ddp_timestamp_is_stale(self):
        stale_ms = (self.NOW - 5 * 24 * 3600) * 1000
        self.assertEqual(self.classify({"$date": stale_ms}).outcome, IGNORE_STALE)

    def test_stale_even_when_the_cache_has_forgotten_the_id(self):
        # The cache is empty (forgotten); the age limit protects regardless.
        self.seen.clear()
        d = self.classify(self.iso(self.NOW - 4 * 24 * 3600))
        self.assertEqual(d.outcome, IGNORE_STALE)

    def test_recent_message_is_handled(self):
        self.assertEqual(self.classify(self.iso(self.NOW - 3600)).outcome, HANDLE_MESSAGE)
        # The same id with a fresh DDP timestamp: the age limit passes it, the dedup
        # catches it.
        self.assertEqual(
            self.classify({"$date": (self.NOW - 60) * 1000}).outcome, IGNORE_DUPLICATE
        )

    def test_missing_or_unparseable_timestamp_never_drops_the_message(self):
        for ts in (_MISSING, None, "", "not-a-date", {"$date": "x"}, 12):
            self.seen.clear()
            self.assertEqual(self.classify(ts).outcome, HANDLE_MESSAGE, ts)

    def test_disabled_without_a_max_age(self):
        d = classify_inbound(
            body(timestamp=self.iso(self.NOW - 30 * 24 * 3600)),
            bot_username=BOT,
            bot_user_id=BOT_UID,
            now_fn=lambda: self.NOW,
        )
        self.assertEqual(d.outcome, HANDLE_MESSAGE)


class TestRcTimestampSeconds(unittest.TestCase):
    def test_both_shapes(self):
        from inbound import rc_timestamp_seconds

        self.assertEqual(rc_timestamp_seconds({"$date": 1_700_000_000_000}), 1_700_000_000.0)
        self.assertEqual(rc_timestamp_seconds("2023-11-14T22:13:20.000Z"), 1_700_000_000.0)
        self.assertEqual(rc_timestamp_seconds("2023-11-14T22:13:20"), 1_700_000_000.0)
        for bad in (None, "", "x", {"$date": None}, {"$date": True}, [], 5):
            self.assertIsNone(rc_timestamp_seconds(bad), bad)


class TestReasonStrings(unittest.TestCase):
    def test_every_outcome_has_a_reason(self):
        for outcome in (
            REJECT_BAD_BODY,
            REJECT_BAD_TOKEN,
            IGNORE_SELF,
            IGNORE_DUPLICATE,
            IGNORE_STALE,
            IGNORE_NO_MENTION,
            IGNORE_NOT_ALLOWED,
            HANDLE_MESSAGE,
        ):
            from inbound import InboundDecision

            self.assertTrue(InboundDecision(outcome).reason, outcome)




class TestRcTimestamp(unittest.TestCase):
    """inbound.rc_timestamp: the REST ``latest``/``oldest`` shape."""

    def test_round_trips_with_rc_timestamp_seconds(self):
        from inbound import rc_timestamp, rc_timestamp_seconds

        self.assertEqual(rc_timestamp(rc_timestamp_seconds({"$date": 1791280805123})), "2026-10-06T10:00:05.123Z")
        self.assertEqual(rc_timestamp_seconds(rc_timestamp(1791280805.5)), 1791280805.5)


if __name__ == "__main__":
    unittest.main()
