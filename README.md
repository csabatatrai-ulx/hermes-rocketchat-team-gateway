# Rocket.Chat Team Gateway for Hermes Agent

A Rocket.Chat platform adapter for [Hermes Agent](https://hermes-agent.nousresearch.com/), built for rooms where many people share one bot. It does one job: carry conversations between Rocket.Chat and Hermes reliably. It registers no agent tools and holds no business logic, the same scope as Hermes's own Mattermost, Slack and Telegram adapters.

Supports **Rocket.Chat 8.7 – 9.x** from one code path. Requires **Hermes 0.21.6 or newer**.

## Why this adapter

**It keeps a shared room understandable.**

- Everyone gets their own session in a room. A thread is shared by its participants, or per person with Hermes's `thread_sessions_per_user`.
- Every mention carries what happened in the room since that person's previous mention, marked as context, with dates and UTC offsets. Nothing is stored; it is read from Rocket.Chat each time.
- Answers quote the question and mention the asker, so concurrent answers stay attributable. A thread mention is answered in the thread.
- A one-to-one DM needs no mention. A DM of three or more people follows room rules.

**It is safe by default in front of many users.**

- Every Hermes command is blocked over chat unless allowed. A new command in a future Hermes is blocked from day one, and if the command registry cannot be read the guard fails closed. Otherwise anyone in a room could run `/model`, `/yolo` or `/restart`.
- With `approvals.mode: manual`, a dangerous command waits for a click: accept and deny buttons appear on the prompt, and each click runs as the authenticated person who made it.
- Skill commands have their own allowlist, and persistent approvals that would write to the shared config are refused.
- A bot answers only in rooms it is a member of, so nobody can pull it into a channel with a mention.
- Lines from senders outside the allowlist are marked `[unverified sender]` in the room context, and a multi-line message cannot forge a context marker.
- An optional signed asker token tells a history MCP server who is really asking, without trusting the model. See [security](docs/security.md#asker-token).

**It stays correct when things go wrong.**

- Duplicate suppression in both directions survives a restart. A reaction or edit makes Rocket.Chat redeliver a message with the same id; that never starts a second turn.
- After a reconnect, missed mentions are replayed. After a restart, the bot says which messages it did not process, or answers them with `replay` mode.
- A provider failure becomes a short notice naming the model provider, not a raw stack of retries.
- Removed Rocket.Chat APIs are detected at runtime and deprecation headers are logged, so a server upgrade shows up in the log before it breaks.

## Quick start

### 1. Prepare Rocket.Chat

1. **Administration → Users → New**: create a user such as `hermes` with the roles `user` and `bot`, without admin or room owner rights.
2. Log in as the bot, open **Profile → Personal Access Tokens**, and create a token with **Ignore Two Factor Authentication** ticked. Copy the token and the user ID; they are shown once.
3. Invite the bot to the rooms it should work in: `/invite @hermes`. DMs need no setup.

### 2. Install

```bash
hermes plugins install rocketchat-team-gateway --enable
```

### 3. Configure

Run `hermes gateway setup` and choose Rocket.Chat. The wizard checks the credentials against the server before saving them. Or put the settings in `~/.hermes/.env` (`chmod 600`):

```dotenv
ROCKETCHAT_URL=https://chat.example.com
ROCKETCHAT_BOT_USERNAME=hermes
ROCKETCHAT_USER_ID=<bot user id>
ROCKETCHAT_TOKEN=<bot personal access token>
ROCKETCHAT_ALLOWED_USERS=<user id>,<user id>    # user IDs, not usernames
```

Decide who may talk to the bot: `ROCKETCHAT_ALLOWED_USERS`, Hermes pairing, or on a closed server `ROCKETCHAT_ALLOW_ALL_USERS=true`. Without one of them Hermes answers nobody.

**Before letting real users in, give the platform an explicit tool list.** A platform plugin cannot set this for you, and without it Hermes grants the platform its full core toolset, including the terminal and file writes.

```bash
hermes config set platform_toolsets.rocketchat '[my-mcp-server]'   # or [] for none
hermes config set approvals.mode manual   # a person answers every dangerous command
```

Hermes's default `smart` mode lets a helper model approve flagged commands, so no one is asked.

### 4. Start and check

```bash
hermes gateway restart
grep 'Rocket.Chat:' ~/.hermes/logs/agent.log
```

A healthy start logs:

```text
Rocket.Chat: authenticated as @hermes (<bot user id>) on https://chat.example.com
Rocket.Chat: DDP logged in (resume token)
Rocket.Chat: DDP subscribed to stream-room-messages
```

Then mention the bot in a room it belongs to, or send it a DM. `@hermes /whoami` answers instantly without a model call and shows who the bot thinks you are.

## What this plugin does on your system

| Behaviour | Details |
|---|---|
| Network | Connects only to `ROCKETCHAT_URL`: a long-lived WebSocket (`/websocket`) and REST calls. No telemetry. Redirects are not followed, so the token cannot reach another host. |
| Listening port | None by default. `ROCKETCHAT_TRANSPORT=webhook` opens an HTTP listener on `127.0.0.1:8645` and refuses to start without a shared secret. |
| Background work | One WebSocket connection with reconnect, a typing indicator loop during turns, debounced state writes. |
| Files on disk | `$HERMES_HOME/rocketchat-plugin-dedup/`: `inbound.json`, `outbound.json` (message ids and content hashes of sent replies), `ddp_cursor.json`, `ddp_status.json`. `ROCKETCHAT_DEDUP_STATE_DIR=off` keeps them in memory. |
| Files read | When the model sends `MEDIA:/path`, Hermes validates the path and the plugin uploads that file to the room. `ROCKETCHAT_OUTBOUND_FILES=false` turns this off. |
| Files received | Attachments on the mentioning message are downloaded with the bot's token into Hermes's media cache. |
| Credentials | Read through Hermes's profile-scoped secret reader. The plugin reads no credential files. |
| Logs | Message text from DMs is never logged, only ids and counts. |
| Shell commands, self-update | None. |

## Replacing the `rocketchat-platform` plugin

This is an independent implementation that uses the same Hermes platform name, `rocketchat`, so `platform_toolsets.rocketchat`, `deliver=rocketchat` and `ROCKETCHAT_URL`, `ROCKETCHAT_TOKEN`, `ROCKETCHAT_USER_ID` and `ROCKETCHAT_ALLOWED_USERS` keep working. Enable only one of the two; the last one registered wins. Add `ROCKETCHAT_BOT_USERNAME`, then restart the gateway.

Choose [`rocketchat-platform`](https://github.com/HalfbitStudio/hermes-plugin-rocketchat) if you want Rocket.Chat tools inside the agent. Choose this one if you want a transport-only adapter and a shared-room experience; pair it with an MCP server for room access.

## Documentation

| Document | Contents |
|---|---|
| [Configuration](docs/configuration.md) | every setting with its default |
| [Sessions and context](docs/sessions-and-context.md) | sessions, room context, threads, DMs |
| [Commands](docs/commands.md) | chat commands and the command guard |
| [Attachments](docs/attachments.md) | inbound and outbound files |
| [Security](docs/security.md) | threat model, asker token, recommended Hermes settings |
| [Deployment](docs/deployment.md) | recommended Hermes config, state, reverse proxy, containers |
| [Compatibility](docs/compatibility.md) | versions and the Rocket.Chat APIs used |
| [Architecture](docs/architecture.md) | module layout, delivery paths, design decisions |
| [Troubleshooting](docs/troubleshooting.md) | symptoms and fixes |
| [Development](docs/development.md) | running the tests |

## License

[MIT](LICENSE)
