# Sessions and context

## Sessions belong to Hermes

The adapter hands every turn to Hermes with `chat_type="dm"` for a one-to-one DM and `"channel"` otherwise, and the real Rocket.Chat thread (`tmid`) or nothing as `thread_id`. Hermes's documented settings decide the rest, as on its own platforms.

| Where | Session | Setting |
|---|---|---|
| Room, main channel | One per person | `group_sessions_per_user` (default on) |
| Thread | Shared, with a `[name]` prefix | `thread_sessions_per_user: true` makes it per person (recommended for a team bot) |
| 1:1 DM | One continuous session | |
| DM thread | Its own session | |

A session lasts until `/new`. The adapter keeps no session state and no daily switch.

## Room context

Each mention in a room receives a block of recent messages, marked as context only:

```text
[Rocket.Chat room_id: GENERAL]
[Recent chat messages - context only, not requests]
[2026-10-06 11:58 +02:00] alice: Is the report ready?
[2026-10-06 11:59 +02:00] hermes (you): Yes, I uploaded it.

[New message]
What is the status?
```

The block travels in `MessageEvent.channel_context`, so `text` stays the bare trigger and Hermes's command recognition keeps working.

**Where the block starts:**

- Main channel and per-person thread: the asker's previous mention in that scope. What came before is already in their session. `/new` or `/reset` ends the search, since the session restarted. Other `/commands` are skipped.
- Without a previous mention, and in a shared thread: the bot's last reply, with at least `ROCKETCHAT_TEAM_HISTORY_MIN_MESSAGES` lines.

**What it holds:**

- Only messages before the trigger. A thread mention sees that thread, root first.
- System messages and Hermes's progress bubbles are left out.
- Attachments appear as `[file: name (type)]`, without content.
- Continuation lines of a multi-line message are indented, so column 0 holds only lines the gateway wrote and nobody can forge a marker or a sender.
- With an allowlist, lines from senders outside it are marked `[unverified sender]`.

Nothing is cached. The window is read from Rocket.Chat per mention, so it is correct after a restart. If the read fails, the turn runs without a block and logs one warning.

## Replies

A thread mention is answered in that thread, including core's error and restart notices. In a room the first answer message quotes the question and mentions the asker. Progress bubbles and further chunks of a long answer do not.

Enabling "also send to channel" on a thread reply does not move it out of the thread: it belongs to the thread's session.

## Direct messages

A one-to-one DM needs no mention and gets no room block. `ROCKETCHAT_TEAM_DM_SEED_MESSAGES` optionally gives a first contact the earlier DM messages.

A thread in a DM starts empty. On its first turn, while the bot has not written in the thread, it receives the root and replies so far. The root is usually the bot's own message that the user replied to.

A `t='d'` room of more than two people follows room rules. The adapter asks `rooms.info` once per room, and if that fails it treats the room as a 1:1 DM and asks again on the next message.

DM text is never written to the log, only ids and counts. A DM from someone not allowed gets no answer, because an answer would reveal that the bot is alive.
