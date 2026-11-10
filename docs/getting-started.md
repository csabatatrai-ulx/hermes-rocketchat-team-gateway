# Install on your server

Supports **Rocket.Chat 8.7 – 9.x** and **Hermes Agent 0.22.3 or newer**.

## 1. Prepare Rocket.Chat

1. **Administration → Users → New:** create a user such as `hermes` with the roles `user` and `bot`, without admin or room owner rights.
2. Log in as the bot, open **Profile → Personal Access Tokens**, and create a token with **Ignore Two Factor Authentication** ticked. Copy the token and the user ID; they are shown once.
3. Invite the bot to the rooms it should work in: `/invite @hermes`. DMs need no setup.

## 2. Install the plugin

=== "From the Hermes catalog"

    ```bash
    hermes plugins install rocketchat-team-gateway --enable
    ```

=== "From the repository"

    Until the catalog entry is merged:

    ```bash
    hermes plugins install csabatatrai-ulx/hermes-rocketchat-team-gateway --enable
    ```

## 3. Configure

Run `hermes gateway setup` and choose Rocket.Chat. The wizard checks the credentials against the server before saving them. Or put the settings in `~/.hermes/.env` (`chmod 600`):

```dotenv
ROCKETCHAT_URL=https://chat.example.com
ROCKETCHAT_BOT_USERNAME=hermes
ROCKETCHAT_USER_ID=<bot user id>
ROCKETCHAT_TOKEN=<bot personal access token>
ROCKETCHAT_ALLOWED_USERS=<user id>,<user id>    # user IDs, not usernames
```

Decide who may talk to the bot: `ROCKETCHAT_ALLOWED_USERS`, Hermes pairing, or on a closed server `ROCKETCHAT_ALLOW_ALL_USERS=true`. Without one of them Hermes answers nobody. Every setting is listed in [Configuration](configuration.md).

## 4. Make it safe for a shared bot

!!! warning "Do this before letting real users in"
    A platform plugin cannot set Hermes's configuration for you. Without an explicit tool list, Hermes grants the platform its full core toolset, including the terminal and file writes.

```bash
hermes config set platform_toolsets.rocketchat '[my-mcp-server]'   # or [] for none
hermes config set approvals.mode manual   # a person answers every dangerous command
hermes config set memory.memory_enabled false        # one memory would be shared by everyone
hermes config set memory.user_profile_enabled false
```

Hermes's default `smart` approval mode lets a helper model approve flagged commands, so no one is asked. The reasons behind each setting are in [Security](security.md).

`@file:`, `@url:` and the other Hermes `@` references in messages are kept inert by default, so a room member cannot attach files from the bot's workspace. For a bot only you use, `ROCKETCHAT_ALLOW_CONTEXT_REFERENCES=true` turns this off ([why](security.md#context-references)).

## 5. Start and check

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

Then mention the bot in a room it belongs to, or send it a DM. `@hermes /whoami` answers instantly without a model call and shows who the bot thinks you are. If something does not work, see [Troubleshooting](troubleshooting.md).

## How it differs from `rocketchat-platform`

The Hermes plugin catalog already has a Rocket.Chat plugin, [`rocketchat-platform`](https://github.com/HalfbitStudio/hermes-plugin-rocketchat). The two solve different problems: that one gives the agent Rocket.Chat tools; this one makes a room that many people share with one bot safe and understandable. Compared with `rocketchat-platform` 1.5.2:

| | `rocketchat-platform` | Rocket.Chat Team Gateway |
|---|---|---|
| Agent tools for Rocket.Chat (post, search, history, channels) | ✓ ten tools | – by design; pair it with an MCP server |
| Context a mention carries | earlier messages of the thread, for a mention in a thread | what happened in the room since the asker's previous mention |
| Who an answer is for | a plain reply, or a thread with `ROCKETCHAT_REPLY_MODE=thread` | each answer quotes its question and mentions its asker |
| Hermes commands over chat | passed to Hermes | refused unless an administrator allows each one |
| Dangerous commands | Hermes's text prompt | approval buttons that only the asker can use |
| After a restart or an outage | reconnects | answers or reports the mentions it missed; no duplicate turns, even across restarts |

Both register the Hermes platform name `rocketchat`, so enable only one; the last one registered wins. Switching keeps `ROCKETCHAT_URL`, `ROCKETCHAT_TOKEN`, `ROCKETCHAT_USER_ID`, `ROCKETCHAT_ALLOWED_USERS`, `platform_toolsets.rocketchat` and `deliver=rocketchat` working; add `ROCKETCHAT_BOT_USERNAME` and restart the gateway.
