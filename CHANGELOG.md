# Changelog

## 1.0.0

First public release.

- Rocket.Chat platform adapter over the realtime API (DDP), with an outgoing webhook as an alternative transport
- Per-person sessions in shared rooms, with room context since the asker's previous mention
- Replies that quote the question, mention the asker and stay in the thread
- Default-deny guard for Hermes commands over chat, with no dependency on Hermes internals
- Buttons on dangerous-command prompts and on the `/new` confirmation, sent as the person who clicked, two per row so their labels stay whole on a phone
- Commands typed as `@bot /new` or `!new`, since Rocket.Chat clients swallow a bare `/new`
- Duplicate suppression that survives a restart, catch-up after a reconnect, and a notice or replay after a restart
- `hermes gateway setup` wizard and out-of-process cron delivery
- Inbound and outbound files
- Supports Rocket.Chat 8.7 - 9.x and Hermes 0.22.3 or newer
