# Security

Rocket.Chat authorises every API call as the bot, not as the person who asked. The adapter is the layer that keeps a shared bot from acting on behalf of the wrong person.

## What the adapter enforces

| Risk | Control |
|---|---|
| Anyone runs privileged Hermes commands | [Default-deny command guard](commands.md) |
| One member changes approvals for everyone | Persistent approval refused |
| Bot pulled into a channel by a mention | Only rooms it is a member of |
| Bots answering each other forever | `ROCKETCHAT_TEAM_IGNORE_SENDERS`; own messages filtered |
| A prefix match wakes the wrong bot | Bounded mention match (`@hermes` does not match `@hermes-beta`) |
| Forged markers in a message | Continuation lines indented; column 0 is the gateway's |
| A member attaches files from the bot's workspace with `@file:` or makes it fetch a URL with `@url:` | [`@` references kept inert](#context-references); `ROCKETCHAT_TEAM_ALLOW_CONTEXT_REFERENCES=true` turns it off |
| Old message re-triggers after a reaction | Time-based dedup and age limit |
| Unauthenticated webhook | Refuses to start without a secret; constant-time compare; loopback by default |
| Token sent elsewhere | Redirects not followed |
| Secrets across profiles | Profile-scoped reader; state under each profile's `HERMES_HOME` |

## What it does not do

The adapter cannot set Hermes's configuration. These are yours:

- **Tools.** Without `platform_toolsets.rocketchat_team`, Hermes gives the platform its whole core toolset. Always set an explicit list on a shared bot.
- **Approvals.** Hermes's default `approvals.mode: smart` lets a helper model approve flagged commands without asking anyone. Use `manual` on a shared bot.
- **What counts as dangerous.** Hermes decides which commands need an approval, from a list of patterns. The list does not cover every deletion: in Hermes 0.23.0, `rm` of an absolute path, a recursive `rm`, `find -delete` and `xargs rm` ask, but a plain `rm` of a relative path or glob (`cd /srv/app && rm logs/*`) does not (reported upstream as [#151542](https://github.com/NousResearch/hermes-agent/issues/151542)). Treat approvals as a safety net, not a sandbox: on a shared bot, give it only the tools it needs, and run its terminal in a container or a dedicated account that can only reach what the bot may change.
- **Memory.** Hermes keeps one memory per Hermes home, and it is on by default, so on a shared bot what one person tells it can surface in another person's answer. Turn off `memory.memory_enabled` and `memory.user_profile_enabled` for a shared bot. The adapter logs a warning at startup when they are on and more than one person can reach the bot (`ROCKETCHAT_TEAM_SHARED_MEMORY_WARNING=false` silences it).
- **Prompt injection.** Room context is marked as context, but it is still text a model reads. Give the bot only the tools its users may trigger, and keep approvals on for consequential ones.

Use a dedicated bot account with minimal room memberships, never an administrator token.

## Context references

Hermes expands `@file:`, `@folder:`, `@git:`, `@url:`, `@diff` and `@staged` in every message it receives from a chat platform, before the model runs. It reads the file, lists the folder, runs the git command or fetches the page, and appends the result to the turn. On a terminal that is your own shortcut. In a shared room it means anyone who can reach the bot could attach a file from the bot's workspace, or make the bot's host fetch an address on your internal network, with one message. This is not a tool call, so no tool list, approval or `pre_tool_call` plugin sees it. Hermes blocks only credential files (`.env`, `auth.json`, `~/.ssh`).

Hermes also expands references inside the room context block. A reference hidden in an earlier message, even one from an unverified sender, would be expanded when someone else mentions the bot.

**What the adapter does.** Before a message reaches Hermes, the adapter puts an invisible zero-width space after the `@` of each such reference, in three places: the asker's message, the room context, and the text of an inserted document. Hermes then reads the reference as plain text. What people see in Rocket.Chat does not change. If the bot has a file or web tool, the model can still open the file or page through it, and then your tool list and approvals apply.

Verified live on Hermes 0.23.0: without this, a room member's `@file:` attached the bot's `config.yaml` and listed its `memories/` folder ([evidence](evidence.md#context-references)).

**How to turn it off.** Set `ROCKETCHAT_TEAM_ALLOW_CONTEXT_REFERENCES=true` in `~/.hermes/.env` and restart the gateway. References in the asker's own message are then expanded as Hermes does by default. The room context and inserted documents stay inert either way, because neither is the asker's request. The adapter logs a warning at startup while the switch is on. Turn it on only for a bot that just you, or people who may read its workspace, can reach.

## Asker token

A history MCP server does not know who is asking, and the model supplies the room. A room member, or text injected into a message, could ask about a private room the bot belongs to and the person does not. The gateway knows who addressed the bot and where.

With `ROCKETCHAT_TEAM_ASKER_KEY` set, each turn carries this line after the room ID:

```text
[Rocket.Chat asker: v1.<user id>.<room id>.<expiry>.<signature>]
```

The signature is the HMAC-SHA256 of `v1.<user id>.<room id>.<expiry>` with the shared key, base64url without padding, first 22 characters. The token lasts 30 minutes and is reissued every turn. The key is never in the model's context, so a forged or altered token fails verification.

The model passes the token verbatim as a tool argument. A verifying MCP server should return only what that user may read: rooms the user and bot share, and the user's own DM with the bot. The MCP server is not part of this plugin.

## Reporting a vulnerability

See [SECURITY.md](https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway/blob/main/SECURITY.md).
