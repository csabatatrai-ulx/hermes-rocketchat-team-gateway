# Security

Rocket.Chat authorises every API call as the bot, not as the person who asked. The adapter is the layer that keeps a shared bot from acting on behalf of the wrong person.

## What the adapter enforces

| Risk | Control |
|---|---|
| Anyone runs privileged Hermes commands | [Default-deny command guard](commands.md) |
| One member changes approvals for everyone | Persistent approval refused |
| Bot pulled into a channel by a mention | Only rooms it is a member of |
| Bots answering each other forever | `ROCKETCHAT_IGNORE_SENDERS`; own messages filtered |
| A prefix match wakes the wrong bot | Bounded mention match (`@hermes` does not match `@hermes-beta`) |
| Forged markers in a message | Continuation lines indented; column 0 is the gateway's |
| Old message re-triggers after a reaction | Time-based dedup and age limit |
| Unauthenticated webhook | Refuses to start without a secret; constant-time compare; loopback by default |
| Token sent elsewhere | Redirects not followed |
| Secrets across profiles | Profile-scoped reader; state under each profile's `HERMES_HOME` |

## What it does not do

The adapter cannot set Hermes's configuration. These are yours:

- **Tools.** Without `platform_toolsets.rocketchat`, Hermes gives the platform its whole core toolset. Always set an explicit list on a shared bot.
- **Approvals.** Hermes's default `approvals.mode: smart` lets a helper model approve flagged commands without asking anyone. Use `manual` on a shared bot.
- **Memory.** Memory is per Hermes home, so a shared bot shares it among all users unless disabled.
- **Prompt injection.** Room context is marked as context, but it is still text a model reads. Give the bot only the tools its users may trigger, and keep approvals on for consequential ones.

Use a dedicated bot account with minimal room memberships, never an administrator token.

## Asker token

A history MCP server does not know who is asking, and the model supplies the room. A room member, or text injected into a message, could ask about a private room the bot belongs to and the person does not. The gateway knows who addressed the bot and where.

With `ROCKETCHAT_ASKER_KEY` set, each turn carries this line after the room ID:

```text
[Rocket.Chat asker: v1.<user id>.<room id>.<expiry>.<signature>]
```

The signature is the HMAC-SHA256 of `v1.<user id>.<room id>.<expiry>` with the shared key, base64url without padding, first 22 characters. The token lasts 30 minutes and is reissued every turn. The key is never in the model's context, so a forged or altered token fails verification.

The model passes the token verbatim as a tool argument. A verifying MCP server should return only what that user may read: rooms the user and bot share, and the user's own DM with the bot. The MCP server is not part of this plugin.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
