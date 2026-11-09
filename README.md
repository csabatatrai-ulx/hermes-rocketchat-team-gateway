![Rocket.Chat Team Gateway for Hermes Agent. One AI bot. A whole team. Zero chaos. Developed at ULX. Artwork: Hermes, the messenger, connecting chat messages across a starry sky.](docs/images/banner.webp)

# Rocket.Chat Team Gateway for Hermes Agent

[![CI](https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Hermes Agent 0.22.3+](https://img.shields.io/badge/Hermes%20Agent-0.22.3%2B-6d28d9)](https://hermes-agent.nousresearch.com/)
[![Dependencies: aiohttp only, also used by Hermes's own messaging adapters](https://img.shields.io/badge/dependencies-aiohttp%20only-2e7d32)](requirements.txt)

**Bringing Hermes Agent into the enterprise team chat.** Rocket.Chat is the open source, self-hosted chat that organizations choose when their conversations must stay on their own servers. This adapter makes Hermes a safe colleague there: one bot for a whole team, default-deny commands, and approval buttons so a person decides on dangerous actions.

A Rocket.Chat platform adapter for [Hermes Agent](https://hermes-agent.nousresearch.com/), built for rooms where many people share one bot. It does one job: carry conversations between Rocket.Chat and Hermes reliably. It registers no agent tools of its own and holds no business logic, the same scope as Hermes's own Mattermost, Slack and Telegram adapters. In Rocket.Chat the agent still has [every tool Hermes has](#tools-everything-hermes-has-none-of-its-own): terminal, files, web, code, and any MCP server.

Supports **Rocket.Chat 8.7 – 9.x** from one code path. Requires **Hermes 0.22.3 or newer**.

- **Standalone, as [Hermes asks of third-party integrations](https://github.com/NousResearch/hermes-agent/pull/4637#issuecomment-4950466375).** Rocket.Chat support is a long-standing request ([#3725](https://github.com/NousResearch/hermes-agent/issues/3725), [#94576](https://github.com/NousResearch/hermes-agent/issues/94576)), and the in-tree adapter for it was closed under that policy. This one lives in its own repository and changes nothing in Hermes core, so keeping up with Rocket.Chat and Hermes releases is this project's job, not the Hermes maintainers'. It passes `hermes plugins validate` with no private core imports, and a contract test covers the Hermes interfaces it relies on.
- **Built for shared rooms.** Each person gets their own session and room context, Hermes commands are blocked over chat unless allowed, and only the person who asked can approve their dangerous command.
- **Tested live.** 750+ unit tests in CI against the oldest supported and the newest Hermes release, plus end-to-end runs with the official Hermes 0.23.0 image against Rocket.Chat 8.9.1, including a restart, a network outage and real clicks on the approval buttons.

Demo video (1:29): two people in a shared room, the approval buttons and the personal assistant.

https://github.com/user-attachments/assets/3a8107bc-dd71-4a02-9eed-7d476e32b55a

### ChatOps: a fix that waits for a human

Demo video (0:49):

https://github.com/user-attachments/assets/13bf2138-55c0-4fbd-9e7d-f0fc7ff10732

A simulated outage in a sandbox: the service, its logs and the alert are staged; what Hermes does, the commands it runs and the approvals are not.

## Try it in a few minutes

No Rocket.Chat server, no account, no API key? One Compose file starts Rocket.Chat, Hermes Agent and this plugin locally, with a bot, two people and a room ready, and a scripted demo model so nothing needs signing up:

```bash
git clone https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway && cd hermes-rocketchat-team-gateway
docker compose -f try/compose.yaml up -d
python3 try/check.py    # 7 end-to-end checks: answers, two askers, the command guard, approval buttons, DMs
```

Open **http://localhost:3000**, sign in as `alice` / `hermes-demo`, and say `@hermes hi` in **#hermes-demo**. The demo model is not AI: it answers with fixed text, so what you see is the plugin itself. For a real model, sign in to Nous Portal (free tier) or use any OpenAI-compatible endpoint: [try/README.md](try/README.md).

![A dangerous command waits for a click. Another person's click runs nothing; the click of the person who asked runs it.](docs/images/approval-buttons.gif)

## Why this adapter

**It keeps a shared room understandable.**

- Everyone gets their own session in a room. A thread is shared by its participants, or per person with Hermes's `thread_sessions_per_user`.
- Every mention carries what happened in the room since that person's previous mention, marked as context, with dates and UTC offsets. Nothing is stored; it is read from Rocket.Chat each time.
- Answers quote the question and mention the asker, so concurrent answers stay attributable. A thread mention is answered in the thread.
- A one-to-one DM needs no mention. A DM of three or more people follows room rules.

![Two people ask the bot at the same moment. Each answer quotes its question and mentions the asker, and both draw on what was said in the room.](docs/images/shared-room.png)

**It guards a room with many users.**

- Every `/word` is refused over chat unless allowed, without asking Hermes what is a command. A new command in a future Hermes is refused from day one, and no Hermes change can open the guard. Otherwise anyone in a room could run `/model`, `/yolo` or `/restart`.
- With `approvals.mode: manual`, a dangerous command waits for a click: accept and deny buttons appear on the prompt, and each click runs as the authenticated person who made it. Confirming `/new` works the same way. Which commands count as dangerous is Hermes's call, and its list has gaps (a relative-path `rm` is not flagged, [#151542](https://github.com/NousResearch/hermes-agent/issues/151542)); see [what approvals cover](docs/security.md#what-it-does-not-do).
- Skill commands have their own allowlist, and persistent approvals that would write to the shared config are refused.
- A bot answers only in rooms it is a member of, so nobody can pull it into a channel with a mention.
- Lines from senders outside the allowlist are marked `[unverified sender]` in the room context, and a multi-line message cannot forge a context marker.
- An optional signed asker token tells a history MCP server who is really asking, without trusting the model. See [security](docs/security.md#asker-token).

**It stays correct when things go wrong.**

- Duplicate suppression in both directions survives a restart. A reaction or edit makes Rocket.Chat redeliver a message with the same id; that never starts a second turn.
- After a reconnect, missed mentions are replayed. After a restart, the bot says which messages it did not process, or answers them with `replay` mode.
- A provider failure becomes a short notice naming the model provider, not a raw stack of retries.
- Removed Rocket.Chat APIs are detected at runtime and deprecation headers are logged, so a server upgrade shows up in the log before it breaks.

**It also works as a personal assistant.**

The same plugin can serve one person in their DMs, by configuration only: answer only the owner, only in DMs, and open the session, model and scheduling commands that a shared bot must not expose. The most dangerous commands (`/yolo`, `/restart`, `/update` and a few more) stay closed even then, unless enabled one by one.

> [!IMPORTANT]
> **A personal assistant needs its own Hermes instance and bot account per person.** One Hermes has one owner's sessions, memory, model and cron jobs. Never use the `personal` preset on a bot that several people can talk to.

```dotenv
ROCKETCHAT_DM_ONLY=true
ROCKETCHAT_ALLOWED_USERS=<owner user id>
ROCKETCHAT_ALLOW_BUILTIN_PRESET=personal
```

Step by step, with the settings and a checklist: [personal assistant](docs/personal-assistant.md).

## Talking to the bot

| | In a room or thread | In a one-to-one DM |
|---|---|---|
| A question | `@hermes when is the release?` | `when is the release?` |
| A Hermes command | `@hermes /new` or `@hermes !new` | `!new` or `@hermes /new` |

Rocket.Chat clients treat a message that starts with `/` as one of their own commands and never send an unknown one, so a bare `/new` does not reach the bot. Mention it first, or use `!new` as on Hermes's Slack and Matrix adapters. Hermes's own prompts already show the form that works, such as `@hermes /approve`. Only allowed commands run; see [commands](docs/commands.md).

## Evidence

Every number below comes from a run, not from a claim. The live runs used Rocket.Chat 8.9.1, the official Hermes 0.23.0 image and Chrome driving the web client.

| What was checked | Result |
|---|---|
| Unit and contract tests in CI, on Hermes 0.22.3 (oldest supported) and 0.23.0 | 770 pass on both |
| `hermes plugins validate --install-deps` | Passes, no private core imports |
| End-to-end scenarios: mentions, threads, DMs, files both ways, concurrent askers, the command guard | 39 / 39 |
| Approval buttons clicked in a room and a thread, including another person's click doing nothing | 16 / 16 |
| `/new` confirmation buttons | 10 / 10 |
| Approval button in a one-to-one DM | 4 / 4 |
| Gateway restart and network outage, with catch-up | 14 / 14 |
| Cron delivery through the running gateway, and with the gateway stopped | 2 / 2 |
| Refused commands such as `/yolo`, `/tools`, `/cron` and unknown `/words` | 0 model calls |
| Real model (Nous Portal): room context, identity, threads, long answers, approval | Pass |
| Personal assistant, a second Hermes with the `personal` preset and a real model: DM questions, memory across sessions, preset commands, `/sethome` and a delivered reminder, `/yolo` refused, other people and rooms ignored | 19 / 19 |
| Fully local: the same real-model checks against a self-hosted open-weight model on vLLM, no cloud model service (team bot: room context, identity, threads, long answers, approval; personal assistant: all of the above) | 11 / 11 and 19 / 19 |

### How these were measured

- **Unit and contract tests** run in [CI](https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway/actions/workflows/ci.yml) on every push. The contract tests import the Hermes release under test and check each member the adapter relies on, so a renamed or removed interface fails the build.
- **Live runs** use a real Rocket.Chat 8.9.1 in Docker (with MongoDB and NATS), the official Hermes Agent image and this plugin exactly as committed. Test users post through the Rocket.Chat REST API, and every button is clicked by Chrome in the Rocket.Chat web client, signed in as the person who clicks.
- **Each check asserts an outcome you could see yourself**: the answer arrives in the right room or thread and mentions its asker; a dangerous command's file is still on disk after another person's click and gone only after the asker's; a refused command produces no request at the model endpoint (a recording stub model counts them).
- **Restarts and outages** are real: the gateway container is stopped, or its connection to Rocket.Chat is cut, while people keep writing, then started again.
- **Real-model runs** use Nous Portal, and again a self-hosted model served by vLLM on an on-premises GPU. The personal assistant run uses a second Hermes instance with its own bot account.
- **The demo video** is recorded from such a run; nothing in it is mocked. The ChatOps clip is a real run too, against a simulated service in a sandbox.

You can repeat the core of it yourself: `python3 try/check.py` runs seven of these checks against the [try-it stack](try/README.md) with its scripted demo model, no account or key needed. The full live-run scripts depend on a local test server and are not in this repository.

Building it also turned up Hermes bugs, which the author reported upstream:

- [#148433](https://github.com/NousResearch/hermes-agent/issues/148433): a shutdown during a redirected turn overwrites an already streamed reply.
- [#149169](https://github.com/NousResearch/hermes-agent/issues/149169): `/loop` and `/heartbeat` stay silent after a gateway restart until the chat gets a plain message.
- [#151542](https://github.com/NousResearch/hermes-agent/issues/151542): a relative-path `rm` never asks for approval, while the same deletion with an absolute path does.
- [#148988](https://github.com/NousResearch/hermes-agent/issues/148988), with a fix proposed in [#149038](https://github.com/NousResearch/hermes-agent/pull/149038): session previews in `/resume` and `/sessions` showed an adapter's context line instead of the person's message; found because this adapter gives every turn room context.

## How it works

### System context

![C4 system context: team members chat in their own Rocket.Chat; Hermes Agent with this plugin receives mentions, calls the model provider and optional MCP servers, and answers in Rocket.Chat; an administrator configures it.](docs/images/c4-context.svg)

The C4 container and component views, and the Structurizr DSL they come from, are in [architecture](docs/architecture.md#c4-model).

### Message flow

```mermaid
flowchart LR
    RC["<b>Rocket.Chat</b><br/>rooms, threads, DMs"]

    subgraph Plugin["This plugin: no agent tools"]
        direction TB
        Decide["<b>Decide</b><br/>mention, allowlist,<br/>dedup, command guard"]
        Build["<b>Build the turn</b><br/>room context,<br/>attachments"]
        Send["<b>Send</b><br/>duplicate guard, quote,<br/>approval buttons"]
        Standalone["<b>Standalone send</b><br/>REST only"]
        Decide --> Build
    end

    subgraph Hermes["Hermes Agent"]
        direction TB
        Core["<b>Gateway core</b><br/>sessions, approvals,<br/>agent and tools"]
        Cron["<b>Cron</b><br/>no gateway running"]
    end

    RC -- "DDP or webhook" --> Decide
    Build -- "MessageEvent" --> Core
    Core -- "reply" --> Send
    Send -- "REST" --> RC
    Cron --> Standalone
    Standalone -- "REST" --> RC
```

The plugin only carries messages. Sessions, the agent and approvals stay in Hermes, and room access for the agent belongs in an MCP server. See [architecture](docs/architecture.md).

### Tools: everything Hermes has, none of its own

No tools of its own does not mean a bot without tools. In Rocket.Chat the agent uses every tool Hermes gives it: the terminal, files, web search, code execution and the tools of any MCP server you connect, with Hermes's approvals in front of the dangerous ones. You choose which, per platform, with `platform_toolsets.rocketchat`. The plugin only carries the conversation, on purpose:

- **The same scope as Hermes's own adapters.** Slack, Mattermost and Telegram register no tools either, and Hermes's platform guide asks platform plugins not to give the agent its own way to send messages.
- **A small surface.** A transport has one job; a bug in it cannot turn into a tool that reads or posts where it should not.
- **Capabilities where they belong.** A tool lives in Hermes or in an MCP server, where it works from every channel, not only Rocket.Chat, and is permissioned and audited on its own.

Rocket.Chat itself works the same way: to let the agent search a room, read its history or post elsewhere, connect an MCP server. Rocket.Chat 8.8 and later ships a native one (Enterprise plans with the AI add-on; alpha), and community servers exist:

```bash
hermes mcp add rocketchat --url https://chat.example.com/api/v1/mcp   # authentication: see Rocket.Chat's MCP documentation
hermes config set platform_toolsets.rocketchat '[terminal, rocketchat]'   # the tools this bot may use
```

With the optional signed asker token, such a server knows who is really asking. See [security](docs/security.md#asker-token).

### A question and its answer

```mermaid
sequenceDiagram
    autonumber
    actor A as Alice
    participant RC as Rocket.Chat
    participant P as Plugin
    participant H as Hermes core
    A->>RC: @hermes when is the release?
    RC-->>P: DDP stream-room-messages
    P->>P: classify_inbound: mention, allowlist, dedup, command guard
    P->>RC: react with eyes (acknowledged)
    P->>RC: REST history since Alice's previous mention
    P->>H: MessageEvent: text, room context, files, session key
    Note over H: session, model, tools, approvals
    H->>P: send(reply, reply_to)
    P->>P: record in the outbound duplicate guard
    P->>RC: REST postMessage: quote of the question, @alice
    P->>RC: remove the eyes reaction
    RC-->>A: the answer, in the thread if asked in one
```

### A dangerous command and its buttons

Each person has their own session in a room, so a click only reaches the session of the person who clicked.

```mermaid
sequenceDiagram
    autonumber
    actor A as Alice
    actor B as Bob
    participant RC as Rocket.Chat
    participant P as Plugin
    participant H as Hermes core
    A->>RC: @hermes delete the old build
    RC-->>P: mention
    P->>H: MessageEvent (Alice's session)
    H->>P: _send_exec_approval_prompt
    P->>RC: card with Allow Once / Deny buttons
    B->>RC: clicks Allow Once
    RC-->>P: "@hermes /approve" from Bob
    P->>H: /approve in Bob's session
    H->>P: No pending command to approve
    P->>RC: reply to Bob, nothing runs
    A->>RC: clicks Allow Once
    RC-->>P: "@hermes /approve" from Alice
    P->>H: /approve in Alice's session
    Note over H: the command runs
    P->>RC: replace the card: "Answered by @alice"
    H->>P: result
    P->>RC: answer to Alice
```

## Quick start

### 1. Prepare Rocket.Chat

1. **Administration → Users → New**: create a user such as `hermes` with the roles `user` and `bot`, without admin or room owner rights.
2. Log in as the bot, open **Profile → Personal Access Tokens**, and create a token with **Ignore Two Factor Authentication** ticked. Copy the token and the user ID; they are shown once.
3. Invite the bot to the rooms it should work in: `/invite @hermes`. DMs need no setup.

### 2. Install

```bash
hermes plugins install rocketchat-team-gateway --enable
```

Until the [catalog entry](https://github.com/NousResearch/hermes-agent/pulls?q=rocketchat-team-gateway) is merged, install it straight from this repository:

```bash
hermes plugins install csabatatrai-ulx/hermes-rocketchat-team-gateway --enable
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
hermes config set memory.memory_enabled false        # one memory would be shared by everyone
hermes config set memory.user_profile_enabled false
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

## Security model

| Question | Answer |
|---|---|
| Who can talk to the bot? | People in the rooms the bot is a member of, narrowed by `ROCKETCHAT_ALLOWED_USERS`. A mention cannot pull it into another room. |
| Who can run a Hermes command over chat? | Nobody, unless that command is allowed. Every `/word` is refused by default. |
| Who can approve a dangerous command? | Only the person who asked. Each click runs as the authenticated person who made it. |
| What does the model see from other people? | The room since the asker's previous mention, marked as context. Senders outside the allowlist are marked `[unverified sender]`. |
| What is up to your Hermes configuration? | Which tools the bot has, the approval mode, which commands count as dangerous, and memory. See [what the adapter does not do](docs/security.md#what-it-does-not-do). |

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

## Acknowledgements

[![ULX: enterprise open source IT](docs/images/ulx-logo-small.png)](https://ulx.hu/)

Developed by **Csaba A. Tátrai** ([LinkedIn](https://www.linkedin.com/in/csabatatrai-datascientist/)) at [Ulx Kft.](https://ulx.hu/), an enterprise open source IT company, and released as open source with its permission.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
