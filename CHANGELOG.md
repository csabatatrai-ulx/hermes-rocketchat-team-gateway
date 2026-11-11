# Changelog

## 2.0.0

Breaking: the plugin now has its own platform id and settings, so it no longer clashes with the `rocketchat-platform` catalog plugin, which registers `rocketchat` and reads `ROCKETCHAT_*`. Requested in the catalog review.

- Platform id `rocketchat_team` (was `rocketchat`): config sections `platforms.rocketchat_team`, `platform_toolsets.rocketchat_team`, `platform_hints.rocketchat_team`, `display.platforms.rocketchat_team`, the top-level `rocketchat_team:` admin lists, and the cron target `deliver=rocketchat_team`
- Settings renamed from `ROCKETCHAT_*` to `ROCKETCHAT_TEAM_*` (for example `ROCKETCHAT_TEAM_URL`, `ROCKETCHAT_TEAM_TOKEN`); the old names are no longer read
- When only old `ROCKETCHAT_*` keys are set, startup logs an error that lists each one with its new name
- The dedup state folder comes from Hermes's own `get_hermes_home()`, so each profile keeps its own folder under a multiplexed gateway
- Sessions start fresh after the upgrade, because the session keys contain the platform id

See "Upgrading from 1.x" in the README.

## 1.0.0

First public release.

- Rocket.Chat platform adapter over the realtime API (DDP), with an outgoing webhook as an alternative transport
- Per-person sessions in shared rooms, with room context since the asker's previous mention
- Replies that quote the question, mention the asker and stay in the thread
- Default-deny guard for Hermes commands over chat, with no dependency on Hermes internals
- Hermes's `@file:`, `@folder:`, `@git:`, `@url:`, `@diff` and `@staged` references kept inert in messages, room context and inserted documents, so a room member cannot attach files from the bot's workspace; `ROCKETCHAT_ALLOW_CONTEXT_REFERENCES=true` keeps them live in the asker's own message
- Buttons on dangerous-command prompts and on the `/new` confirmation, sent as the person who clicked, two per row so their labels stay whole on a phone
- Commands typed as `@bot /new` or `!new`, since Rocket.Chat clients swallow a bare `/new`
- Duplicate suppression that survives a restart, catch-up after a reconnect, and a notice or replay after a restart
- `hermes gateway setup` wizard and out-of-process cron delivery
- Inbound and outbound files
- A try-it stack (`try/`): Rocket.Chat, Hermes and the plugin in one Compose file, with a scripted demo model so no account or key is needed, and `try/check.py`, seven end-to-end checks
- Supports Rocket.Chat 8.7 - 9.x and Hermes 0.22.3 or newer
