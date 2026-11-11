# Configuration

Every setting is a `ROCKETCHAT_TEAM_*` environment variable, normally in `~/.hermes/.env`. The plugin's Hermes platform id is `rocketchat_team`: it names its `config.yaml` sections (`platforms.rocketchat_team`, `platform_toolsets.rocketchat_team`, …) and the cron target `deliver=rocketchat_team`. Before 2.0 they were `ROCKETCHAT_*` and `rocketchat`; see [Upgrading from 1.x](getting-started.md#upgrading-from-1x).

## Rules

- **Precedence** follows Hermes's documented env-over-YAML rule: environment variable, then the platform's `extra` mapping in `config.yaml` (`platforms.rocketchat_team.extra.require_mention: false`, lower case, no prefix), then the default.
- **Environment only:** `ROCKETCHAT_TEAM_ALLOW_BUILTIN_*`, `ROCKETCHAT_TEAM_ALLOW_SKILL_*` and `ROCKETCHAT_TEAM_OUTBOUND_FILES`.
- **An empty value** counts as unset.
- **On/off values** are `true/1/yes/on` and `false/0/no/off`. Anything else keeps the setting's own default and logs a warning naming the key, so a typo can never flip a switch. Hermes core reads `ROCKETCHAT_TEAM_ALLOW_ALL_USERS` too and only accepts `true/1/yes` there.
- Settings are read at startup. Run `hermes gateway restart` after a change.

## Connection and identity

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_URL` | required | Server base URL, no path, the final address without redirects |
| `ROCKETCHAT_TEAM_TOKEN` | required | The bot's Personal Access Token |
| `ROCKETCHAT_TEAM_USER_ID` | required | The bot's user ID |
| `ROCKETCHAT_TEAM_BOT_USERNAME` | required | The bot's username, used for mention detection and self-filtering |
| `ROCKETCHAT_TEAM_TRANSPORT` | `ddp` | Inbound path: `ddp` or `webhook` |
| `ROCKETCHAT_TEAM_WS_HEARTBEAT_SECONDS` | `30` | DDP keepalive, 5–300; other values become 30; only `0` disables it |

## Who may talk to the bot

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_ALLOWED_USERS` | empty | Comma-separated user IDs. Without this, pairing or `ALLOW_ALL_USERS`, Hermes answers nobody |
| `ROCKETCHAT_TEAM_ALLOW_ALL_USERS` | `false` | Anyone may use the bot |
| `ROCKETCHAT_TEAM_REQUIRE_MENTION` | `true` | Rooms need an @mention (never a 1:1 DM) |
| `ROCKETCHAT_TEAM_IGNORE_SENDERS` | empty | Usernames or user IDs that never start a turn, such as other bots |
| `ROCKETCHAT_TEAM_DM_ONLY` | `false` | Work only in 1:1 DMs; a room mention gets one notice per room and never a turn |
| `ROCKETCHAT_TEAM_ALLOW_CONTEXT_REFERENCES` | `false` | Let Hermes expand `@file:`, `@folder:`, `@git:`, `@url:`, `@diff` and `@staged` in the asker's own message. Off by default, because anyone who can reach the bot could attach files from its workspace. The room context and inserted documents stay inert either way. See [Security](security.md#context-references) |

## Conversation

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_HOME_CHANNEL` | empty | Hermes home channel room ID: target of the greeting and `deliver=rocketchat_team` cron jobs, also from a process separate from the gateway. `/sethome` writes it |
| `ROCKETCHAT_TEAM_STARTUP_GREETING` | `true` | Greet the home channel on a cold start |
| `ROCKETCHAT_TEAM_REPLY_QUOTE` | `true` | In rooms, quote the question in the first answer message |
| `ROCKETCHAT_TEAM_REPLY_MENTION` | `true` | In rooms, mention the asker in the first answer message |
| `ROCKETCHAT_TEAM_ACK_REACTION` | `eyes` | Emoji on the mentioning message while the agent works; `none`/`off` disables it |
| `ROCKETCHAT_TEAM_WHOAMI_ENABLED` | `true` | Answer `/whoami` |
| `ROCKETCHAT_TEAM_WHOAMI_COMMAND` | `/whoami` | Its command word |
| `ROCKETCHAT_TEAM_OUTBOUND_FILES` | `true` | Upload files the agent sends with `MEDIA:/path` |
| `ROCKETCHAT_TEAM_APPROVAL_BUTTONS` | `true` | Accept and deny buttons on a dangerous-command prompt |
| `ROCKETCHAT_TEAM_SHARED_MEMORY_WARNING` | `true` | A startup warning when Hermes's built-in memory is on although several people can reach the bot. Only a log line |

## Room context

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_HISTORY_LIMIT` | `50` | Most messages in the context block; `0` disables it, above `100` becomes `100` |
| `ROCKETCHAT_TEAM_HISTORY_MIN_MESSAGES` | `20` | Least messages when the block runs since the bot's last reply, at most the limit |
| `ROCKETCHAT_TEAM_HISTORY_TOKEN_BUDGET` | `0` | Block size cap in estimated tokens, oldest lines dropped first; `0` = no cap |
| `ROCKETCHAT_TEAM_DM_SEED_MESSAGES` | `0` | On first contact in a 1:1 DM, at most this many earlier DM messages as a block; at most `100` |
| `ROCKETCHAT_TEAM_TIMEZONE` | gateway's local zone | IANA zone for context timestamps. Set it when the gateway runs in UTC and users do not |

## Commands

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_ALLOW_BUILTIN_<NAME>` | see [commands](commands.md) | Allow (`true`) or block (`false`) one Hermes command |
| `ROCKETCHAT_TEAM_ALLOW_BUILTIN_PRESET` | `default` | `default`, or `personal` for a single-owner bot |
| `ROCKETCHAT_TEAM_ALLOW_SKILL_<NAME>` | all off | Allow one Hermes skill command |

## Reliability

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_DDP_CATCHUP_MAX_SECONDS` | `600` | Longest outage replayed after a reconnect; `0` disables it |
| `ROCKETCHAT_TEAM_STARTUP_CATCHUP_MODE` | `notice` | Messages sent while the bot was down: `notice` (one "please resend" note per room or thread), `replay` (answer them up to the cap, a note for the rest), `off` |
| `ROCKETCHAT_TEAM_STARTUP_CATCHUP_MAX_SECONDS` | `600` | How far back a fresh start looks; `0` disables it |
| `ROCKETCHAT_TEAM_STARTUP_CATCHUP_MAX_DISPATCHES` | `10` | Most turns one start launches in `replay` mode, at least 1 |
| `ROCKETCHAT_TEAM_INBOUND_DEDUP_RETENTION_HOURS` | `72` | How long a handled message id is remembered; also the age limit for inbound messages |
| `ROCKETCHAT_TEAM_OUTBOUND_DEDUP_TTL_SECONDS` | `10` | Identical content to the same place within this window goes out once; `0` disables it |
| `ROCKETCHAT_TEAM_DEDUP_STATE_DIR` | `$HERMES_HOME/rocketchat-plugin-dedup` | Restart-surviving state; `off`/`none` keeps it in memory |

## Webhook transport

Use this only when DDP is impossible. The listener must be reachable from Rocket.Chat.

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_WEBHOOK_SECRET` | empty | Required with `ROCKETCHAT_TEAM_TRANSPORT=webhook`; the platform refuses to start without it |
| `ROCKETCHAT_TEAM_WEBHOOK_HOST` | `127.0.0.1` | Listener address |
| `ROCKETCHAT_TEAM_WEBHOOK_PORT` | `8645` | Listener port |

On Rocket.Chat create two integrations under **Administration → Integrations → New → Outgoing**, event *Message Sent*, URL `http://<hermes-host>:8645/rocketchat`, the same secret as *Token*:

- one for rooms: *Channel* lists the rooms, *Trigger Words* is `@<bot username>`;
- one for DMs: *Channel* is `@<bot username>`, no trigger word.

## Model-specific

| Key | Default | Meaning |
|---|---|---|
| `ROCKETCHAT_TEAM_ASKER_KEY` | empty | Key shared with a history MCP server, at least 32 characters. See [security](security.md#asker-token) |
| `ROCKETCHAT_TEAM_REASONING_LEAK_FORMATTING` | `false` | Clean reasoning a model leaks in front of its answer as plain text. Enable it only for a model that does this: an ordinary answer starting "Based on ..." would be a false positive |
| `ROCKETCHAT_TEAM_REASONING_LEAK_MODE` | `drop` | `drop` removes it, `wrap` keeps it as a quoted aside |
| `ROCKETCHAT_TEAM_REASONING_LEAK_PATTERN` | built in | Regular expression for a lead-in |
| `ROCKETCHAT_TEAM_REASONING_LEAK_LABEL` | `💭 _thinking_` | First line of the aside in `wrap` mode |
| `ROCKETCHAT_TEAM_ALWAYS_FRESH_FINAL_STREAMING` | `false` | Leave off. It re-triggers a duplicate final delivery in Hermes core that the outbound guard cannot catch |
