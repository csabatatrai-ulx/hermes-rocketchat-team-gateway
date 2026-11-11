# Commands

## Answered by the plugin

| Command | Answer |
|---|---|
| `/whoami` | The sender's Rocket.Chat identity as authenticated by the server, without a model call. Read-only, shows only the caller's own data. |

## Hermes commands: default deny

Hermes's admin gating (`allow_admin_from`) is off by default, so without a guard anyone mentioning the bot could run `/model`, `/yolo on` or `/restart` with full Hermes privileges. The plugin refuses **every `/word` that is not on its allowlist**, without asking Hermes what counts as a command. So a command, alias, plugin command, skill or quick command added later is refused from day one, and no Hermes change can open the guard.

A refused word gets a reply naming the key that would allow it, with no model call. For a word the plugin does not document, such as the start of `/api returns 500, why?`, the reply also says to resend it without the leading slash and names the skill key. A word containing another slash (`/etc/hosts`) is a path, not a command, and goes to the model.

As a second layer, a turn that Hermes itself would parse as a non-allowed command is marked `allow_gateway_control=False`, so any difference between the two parsers leaves the text as conversation.

**Allowed by default:**

| Command | Why |
|---|---|
| `/new`, `/reset` | Start a new session; rooms use per-person sessions |
| `/approve`, `/deny`, `/cancel` | Answer a pending prompt once |
| `/stop` | Stops only the sender's own run |
| `/btw`, `/context` | Read-only, about the sender's own session |

**Change one:**

```dotenv
ROCKETCHAT_TEAM_ALLOW_BUILTIN_STATUS=true
ROCKETCHAT_TEAM_ALLOW_BUILTIN_RESET=false
```

Use the name with hyphens as underscores (`ROCKETCHAT_TEAM_ALLOW_BUILTIN_RELOAD_MCP`). Common aliases follow their command (`/hb` follows `HEARTBEAT`). An alias the plugin does not list stays refused until it gets its own key, and a mistyped key leaves the command refused.

**Presets:** `ROCKETCHAT_TEAM_ALLOW_BUILTIN_PRESET=personal` adds session management, `/model`, `/bg`, `/sethome`, `/goal` and similar commands for a bot that answers one owner. It does not belong on a shared bot, where anyone could switch the model or browse sessions: each person who wants a personal assistant needs their own Hermes instance and bot account (see [Personal assistant](personal-assistant.md)). `/yolo`, `/restart`, `/update`, `/approvals`, `/pause`, `/learn`, `/debug` and `/platform` are in no preset.

The `personal` preset adds: `/sessions`, `/resume`, `/title`, `/branch`, `/retry`, `/undo`, `/compress`, `/discard`, `/status`, `/usage`, `/queue`, `/bg`, `/help`, `/commands`, `/version`, `/profile`, `/model`, `/reasoning`, `/personality`, `/footer`, `/insights`, `/always`, `/sethome`, `/heartbeat`, `/loop`, `/steer`, `/agents`, `/goal`, `/subgoal`, `/plan`, `/review` and `/refine`.

## Approving a dangerous command

When the agent wants to run something dangerous and Hermes's `approvals.mode` calls for a person (see [deployment](deployment.md)), the prompt carries **Accept** and **Deny** buttons, and the typed commands always work too.

A button sends `@bot /approve` or `@bot /deny` as the person who clicked it, the same message they could type. So the same checks apply: the allowlist, the command guard, Hermes's admin lists and the session. In a room, someone else's click reaches their own session, where nothing is pending. In a shared thread any participant can answer, as with the typed command.

The prompt is Hermes's own text, unchanged: the whole command up to Hermes's 3,000-character card limit, with its tabs and spaces, in a code block. A longer command is cut by Hermes itself, and Hermes then treats it as not fully shown. So what Accept runs is what the card shows.

Only choices the guard would allow get a button, so "always" is not offered unless `ROCKETCHAT_TEAM_ALLOW_BUILTIN_ALWAYS=true`. `ROCKETCHAT_TEAM_APPROVAL_BUTTONS=false` returns to Hermes's plain-text prompt.

## Confirming `/new`

Hermes asks for confirmation before `/new` and similar commands. The prompt carries **Approve Once** and **Cancel** buttons that work the same way, and typed `/approve` and `/cancel` always work too. "Always Approve" gets a button only with `ROCKETCHAT_TEAM_ALLOW_BUILTIN_ALWAYS=true`. `ROCKETCHAT_TEAM_APPROVAL_BUTTONS=false` returns to Hermes's plain-text prompt.

## Persistent approval

Answering a confirmation with "always", `/always`, `/remember` or `/approve always|session|permanent` writes to the gateway's whole `config.yaml`, so one room member would change it for everyone. These are refused unless `ROCKETCHAT_TEAM_ALLOW_BUILTIN_ALWAYS=true`. A one-off `/approve` and `/deny` pass.

The plain-text check covers Hermes's English words. If you set `display.language`, Hermes also accepts that language's words as an approval answer; the slash-command forms stay covered. On a shared bot, keep `approvals` on and set Hermes's admin lists below.

## Skills

Skills (`/google-workspace`) are refused like any other word, and have their own allowlist with no defaults: `ROCKETCHAT_TEAM_ALLOW_SKILL_<NAME>=true`. This matters because Hermes applies no admin check to skills.

## Second line of defence

Set Hermes's own lists as well, so Hermes refuses admin commands whatever reaches it. The two do not cover each other's scope.

```yaml
rocketchat_team:
  group_allow_admin_from: [<admin user id>]   # rooms
  allow_admin_from: [<admin user id>]         # one-to-one DMs
```

## Typing a command

Rocket.Chat clients treat a message that starts with `/` as one of their own commands and do not send an unknown one. Three forms work:

| Form | Where |
|---|---|
| `@hermes /new` | Rooms and DMs. Hermes's own text prompts show this form. |
| `!new` | DMs, and after the mention in rooms (`@hermes !new`), as on Hermes's Slack and Matrix adapters. Only allowed or documented commands are rewritten, so "!important" stays text. |
| `/new` | Only with `Message_AllowUnrecognizedSlashCommand` enabled in Rocket.Chat |

Rocket.Chat's own `/help`, `/status` and `/topic` share a name with Hermes commands. They run as Rocket.Chat commands unless the bot is mentioned first.
