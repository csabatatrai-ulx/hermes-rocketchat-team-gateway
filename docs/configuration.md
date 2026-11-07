# Configuration

Every setting is a `ROCKETCHAT_*` environment variable, normally in `~/.hermes/.env`.

## Rules

- **Precedence** follows Hermes's documented env-over-YAML rule: environment variable, then the platform's `extra` mapping in `config.yaml` (`platforms.rocketchat.extra.require_mention: false`, lower case, no prefix), then the default.
- **Environment only:** `ROCKETCHAT_ALLOW_BUILTIN_*`, `ROCKETCHAT_ALLOW_SKILL_*` and `ROCKETCHAT_OUTBOUND_FILES`.
- **An empty value** counts as unset.
- **On/off values** are `true/1/yes/on` and `false/0/no/off`. Anything else keeps the setting's own default and logs a warning naming the key, so a typo can never flip a switch. Hermes core reads `ROCKETCHAT_ALLOW_ALL_USERS` too and only accepts `true/1/yes` there.
- Settings are read at startup. Run `hermes gateway restart` after a change.

## Connection and identity

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_URL` | required | Server base URL, no path, the final address without redirects |
| `ROCKETCHAT_TOKEN` | required | The bot's Personal Access Token |
| `ROCKETCHAT_USER_ID` | required | The bot's user ID |
| `ROCKETCHAT_BOT_USERNAME` | required | The bot's username, used for mention detection and self-filtering |
| `ROCKETCHAT_TRANSPORT` | `ddp` | Inbound path: `ddp` or `webhook` |
| `ROCKETCHAT_WS_HEARTBEAT_SECONDS` | `30` | DDP keepalive, 5–300; other values become 30; only `0` disables it |

## Who may talk to the bot

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_ALLOWED_USERS` | empty | Comma-separated user IDs. Without this, pairing or `ALLOW_ALL_USERS`, Hermes answers nobody |
| `ROCKETCHAT_ALLOW_ALL_USERS` | `false` | Anyone may use the bot |
| `ROCKETCHAT_REQUIRE_MENTION` | `true` | Rooms need an @mention (never a 1:1 DM) |
| `ROCKETCHAT_IGNORE_SENDERS` | empty | Usernames or user IDs that never start a turn, such as other bots |
| `ROCKETCHAT_DM_ONLY` | `false` | Work only in 1:1 DMs; a room mention gets one notice per room and never a turn |

## Conversation

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_HOME_CHANNEL` | empty | Hermes home channel room ID: target of the greeting and `deliver=rocketchat` cron jobs, also from a process separate from the gateway. `/sethome` writes it |
| `ROCKETCHAT_STARTUP_GREETING` | `true` | Greet the home channel on a cold start |
| `ROCKETCHAT_REPLY_QUOTE` | `true` | In rooms, quote the question in the first answer message |
| `ROCKETCHAT_REPLY_MENTION` | `true` | In rooms, mention the asker in the first answer message |
| `ROCKETCHAT_ACK_REACTION` | `eyes` | Emoji on the mentioning message while the agent works; `none`/`off` disables it |
| `ROCKETCHAT_WHOAMI_ENABLED` | `true` | Answer `/whoami` |
| `ROCKETCHAT_WHOAMI_COMMAND` | `/whoami` | Its command word |
| `ROCKETCHAT_OUTBOUND_FILES` | `true` | Upload files the agent sends with `MEDIA:/path` |

## Room context

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_HISTORY_LIMIT` | `50` | Most messages in the context block; `0` disables it, above `100` becomes `100` |
| `ROCKETCHAT_HISTORY_MIN_MESSAGES` | `20` | Least messages when the block runs since the bot's last reply, at most the limit |
| `ROCKETCHAT_HISTORY_TOKEN_BUDGET` | `0` | Block size cap in estimated tokens, oldest lines dropped first; `0` = no cap |
| `ROCKETCHAT_DM_SEED_MESSAGES` | `0` | On first contact in a 1:1 DM, at most this many earlier DM messages as a block; at most `100` |
| `ROCKETCHAT_TIMEZONE` | gateway's local zone | IANA zone for context timestamps. Set it when the gateway runs in UTC and users do not |

## Commands

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_ALLOW_BUILTIN_<NAME>` | see [commands](commands.md) | Allow (`true`) or block (`false`) one Hermes command |
| `ROCKETCHAT_ALLOW_BUILTIN_PRESET` | `default` | `default`, or `personal` for a single-owner bot |
| `ROCKETCHAT_ALLOW_SKILL_<NAME>` | all off | Allow one Hermes skill command |

## Reliability

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_DDP_CATCHUP_MAX_SECONDS` | `600` | Longest outage replayed after a reconnect; `0` disables it |
| `ROCKETCHAT_STARTUP_CATCHUP_MODE` | `notice` | Messages sent while the bot was down: `notice` (one "please resend" note per room or thread), `replay` (answer them up to the cap, a note for the rest), `off` |
| `ROCKETCHAT_STARTUP_CATCHUP_MAX_SECONDS` | `600` | How far back a fresh start looks; `0` disables it |
| `ROCKETCHAT_STARTUP_CATCHUP_MAX_DISPATCHES` | `10` | Most turns one start launches in `replay` mode, at least 1 |
| `ROCKETCHAT_INBOUND_DEDUP_RETENTION_HOURS` | `72` | How long a handled message id is remembered; also the age limit for inbound messages |
| `ROCKETCHAT_OUTBOUND_DEDUP_TTL_SECONDS` | `10` | Identical content to the same place within this window goes out once; `0` disables it |
| `ROCKETCHAT_DEDUP_STATE_DIR` | `$HERMES_HOME/rocketchat-plugin-dedup` | Restart-surviving state; `off`/`none` keeps it in memory |

## Webhook transport

Use this only when DDP is impossible. The listener must be reachable from Rocket.Chat.

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_WEBHOOK_SECRET` | empty | Required with `ROCKETCHAT_TRANSPORT=webhook`; the platform refuses to start without it |
| `ROCKETCHAT_WEBHOOK_HOST` | `127.0.0.1` | Listener address |
| `ROCKETCHAT_WEBHOOK_PORT` | `8645` | Listener port |

On Rocket.Chat create two integrations under **Administration → Integrations → New → Outgoing**, event *Message Sent*, URL `http://<hermes-host>:8645/rocketchat`, the same secret as *Token*:

- one for rooms: *Channel* lists the rooms, *Trigger Words* is `@<bot username>`;
- one for DMs: *Channel* is `@<bot username>`, no trigger word.

## Model-specific

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_ASKER_KEY` | empty | Key shared with a history MCP server, at least 32 characters. See [security](security.md#asker-token) |
| `ROCKETCHAT_REASONING_LEAK_FORMATTING` | `false` | Clean reasoning a model leaks in front of its answer as plain text. Enable it only for a model that does this: an ordinary answer starting "Based on ..." would be a false positive |
| `ROCKETCHAT_REASONING_LEAK_MODE` | `drop` | `drop` removes it, `wrap` keeps it as a quoted aside |
| `ROCKETCHAT_REASONING_LEAK_PATTERN` | built in | Regular expression for a lead-in |
| `ROCKETCHAT_REASONING_LEAK_LABEL` | `💭 _thinking_` | First line of the aside in `wrap` mode |
| `ROCKETCHAT_ALWAYS_FRESH_FINAL_STREAMING` | `false` | Leave off. It re-triggers a duplicate final delivery in Hermes core that the outbound guard cannot catch |
