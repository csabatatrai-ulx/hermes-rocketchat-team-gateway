# Personal assistant

The same plugin can run Hermes as one person's assistant in their Rocket.Chat DMs: it answers only its owner, only in a one-to-one DM, and opens the session, model and scheduling commands that a shared team bot must not expose. Nothing in the code changes; it is configuration only.

!!! warning "One Hermes instance and one bot account per person"
    A Hermes instance holds one owner's sessions, memory, model choice and cron jobs, and the `personal` preset lets its user switch models, browse and resume sessions and choose where reminders are delivered. If several people want an assistant, run a separate Hermes instance (its own `HERMES_HOME`, container or profile) with its own Rocket.Chat bot account for each of them. Never use the `personal` preset on a bot that more than one person can talk to.

## What the owner gets

| Area | What works |
|---|---|
| Conversation | Plain messages in the DM, no mention needed; answers stream in |
| Memory | Hermes remembers facts and preferences across sessions (memory is safe to enable: only the owner talks to it) |
| Sessions | `/sessions`, `/resume`, `/title`, `/branch`, `/undo`, `/retry`, `/compress`, `/new` |
| Model and run | `/model`, `/reasoning`, `/personality`, `/bg`, `/queue`, `/steer`, `/usage`, `/status` |
| Reminders and goals | Ask in plain words ("remind me in an hour to…"); `/sethome` makes the DM the delivery target; `/loop`, `/goal`, `/plan` |
| Approvals | Dangerous commands still wait for the owner's click, as on a team bot |

The full list of the preset is in [Commands and approvals](commands.md). Type commands as `!status` in the DM, or `@<bot> /status`; a Rocket.Chat client does not send an unknown `/status` on its own.

## Set it up

### 1. A bot account for this person

In Rocket.Chat, **Administration → Users → New**: create a user such as `bob-assistant` with the roles `user` and `bot`. Log in as it, open **Profile → Personal Access Tokens** and create a token with **Ignore Two Factor Authentication** ticked. Copy the token and the user ID.

You also need the owner's user ID: open the owner under **Administration → Users**; the ID is the last part of the page address.

### 2. Its own Hermes

Give the assistant its own Hermes home: a separate container with its own data volume, a separate `HERMES_HOME`, or a Hermes profile. Install and enable the plugin there as in [Install on your server](getting-started.md).

### 3. `.env`

```dotenv
ROCKETCHAT_TEAM_URL=https://chat.example.com
ROCKETCHAT_TEAM_BOT_USERNAME=bob-assistant
ROCKETCHAT_TEAM_USER_ID=<bot user id>
ROCKETCHAT_TEAM_TOKEN=<bot personal access token>
ROCKETCHAT_TEAM_ALLOWED_USERS=<owner user id>      # the owner alone
ROCKETCHAT_TEAM_DM_ONLY=true                       # DMs only; a room mention gets one short notice
ROCKETCHAT_TEAM_ALLOW_BUILTIN_PRESET=personal      # the owner's commands
ROCKETCHAT_TEAM_STARTUP_GREETING=false
ROCKETCHAT_TEAM_TIMEZONE=Europe/Budapest           # the owner's zone, for reminders and timestamps
```

### 4. `config.yaml`

```yaml
memory:
  memory_enabled: true          # one owner, so their memory is theirs alone
  user_profile_enabled: true
approvals:
  mode: manual                  # the owner clicks to allow a dangerous command
streaming:
  enabled: true                 # optional: answers appear as they are written
  transport: edit
```

### 5. Start and point reminders at the DM

Start the gateway, open a DM with the bot as the owner and send `!sethome`. Hermes stores the DM as its home channel, and reminders and other scheduled jobs are delivered there. You can also set `ROCKETCHAT_TEAM_HOME_CHANNEL` to the DM's room ID in `.env`.

## Several assistants on one server

Each assistant is its own Hermes process with its own data, bot account and `.env`. When several run on one host with host networking, give each its own port for Hermes's built-in API server, or turn it off where it is not used:

```yaml
platforms:
  api_server:
    enabled: false
```

The assistants share nothing but the Rocket.Chat server. If they use the same model provider account, they also share its rate limits; a busy free tier shows up in `!usage`.

## What stays closed

`/yolo`, `/restart`, `/update`, `/approvals`, `/pause`, `/learn`, `/debug` and `/platform` are in no preset: they skip approvals, restart or update the gateway, or change its configuration. If the owner really needs one, enable it alone with its own key, for example `ROCKETCHAT_TEAM_ALLOW_BUILTIN_DEBUG=true`, and restart the gateway. Any command can also be taken out of the preset with `ROCKETCHAT_TEAM_ALLOW_BUILTIN_<NAME>=false`.

## Check it works

As the owner, in the DM:

1. Ask a question without a mention: the assistant answers.
2. `!status` and `!model`: session and model information, no model call.
3. "Remember that my standup is at 9:30." Then `!new` and ask when the standup is: it remembers.
4. `!sethome`, then "Remind me in one minute to stretch.": the reminder arrives in the DM about a minute later.
5. `!yolo on`: refused.

As anyone else:

6. A DM to the assistant gets no answer.
7. A mention in a room the bot belongs to gets one short notice that it works in DMs only.

These checks, run against a real model, are in [Testing and evidence](evidence.md).

## Good to know

- Hermes shows short progress lines while it works (for example, which tool it is using). For a quieter DM, see the `display` settings in [Deployment](deployment.md).
- Reminders are delivered on Hermes's scheduler tick, so a "one minute" reminder can arrive up to a minute later.
- Everything about the plugin's security applies unchanged: see [Security](security.md).
