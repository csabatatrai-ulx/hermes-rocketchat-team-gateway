# Commands

## Answered by the plugin

| Command | Answer |
|---|---|
| `/whoami` | The sender's Rocket.Chat identity as authenticated by the server, without a model call. Read-only, shows only the caller's own data. |

## Hermes commands: default deny

Hermes's admin gating (`allow_admin_from`) is off by default, so without a guard anyone mentioning the bot could run `/model`, `/yolo on` or `/restart` with full Hermes privileges. The plugin blocks every `/word` the **installed** Hermes resolves as a command: its registry with all aliases, plugin commands, skill bundles and the operator's `quick_commands`.

A blocked command gets a reply naming the key that would allow it. Text Hermes does not know as a command passes through.

**Allowed by default:**

| Command | Why |
|---|---|
| `/new`, `/reset` | Start a new session; rooms use per-person sessions |
| `/approve`, `/deny` | Answer a pending prompt once |
| `/stop` | Stops only the sender's own run |
| `/btw`, `/context` | Read-only, about the sender's own session |

**Change one:**

```dotenv
ROCKETCHAT_ALLOW_BUILTIN_STATUS=true
ROCKETCHAT_ALLOW_BUILTIN_RESET=false
```

Use the canonical name with hyphens as underscores (`ROCKETCHAT_ALLOW_BUILTIN_RELOAD_MCP`). A key that matches no command logs a warning at startup.

**Presets:** `ROCKETCHAT_ALLOW_BUILTIN_PRESET=personal` adds session management, `/model`, `/bg`, `/sethome`, `/goal` and similar commands for a bot that answers one owner. It does not belong on a shared bot, where anyone could switch the model or browse sessions. `/yolo`, `/restart`, `/update`, `/approvals`, `/pause`, `/learn`, `/debug` and `/platform` are in no preset.

**If the registry cannot be imported,** the guard fails closed: every `/word` that is neither allowed nor an installed skill is blocked, and one warning is logged.

## Persistent approval

Answering a confirmation with "always", `/always`, `/remember` or `/approve always|session|permanent` writes to the gateway's whole `config.yaml`, so one room member would change it for everyone. These are refused unless `ROCKETCHAT_ALLOW_BUILTIN_ALWAYS=true`. A one-off `/approve` and `/deny` pass.

The plain-text check covers Hermes's English words. If you set `display.language`, Hermes also accepts that language's words as an approval answer; the slash-command forms stay covered. On a shared bot, keep `approvals` on and set Hermes's admin lists below.

## Skills

Installed skills (`/google-workspace`) use a separate Hermes registry with no admin check. They have their own allowlist with no defaults: `ROCKETCHAT_ALLOW_SKILL_<NAME>=true`.

## Second line of defence

Set Hermes's own lists as well, so Hermes refuses admin commands whatever reaches it. The two do not cover each other's scope.

```yaml
rocketchat:
  group_allow_admin_from: [<admin user id>]   # rooms
  allow_admin_from: [<admin user id>]         # one-to-one DMs
```

## Rocket.Chat clients and slash commands

Web and desktop clients handle unknown `/` commands themselves. Mention the bot first (`@hermes /new`), or enable `Message_AllowUnrecognizedSlashCommand` in Rocket.Chat.
