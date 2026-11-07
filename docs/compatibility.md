# Compatibility

## Tested

The unit suite and a live end-to-end run pass on Hermes 0.22.6 and on the official Hermes 0.23.0 container image, against Rocket.Chat 8.9.1. The live run covers mentions, threads, DMs, group DMs, the command guard, files in both directions, concurrent askers, approval buttons clicked in the web client, a restart and a network outage.

## Hermes

`requires_hermes: ">=0.21.6"`. The adapter uses only surfaces listed in Hermes's platform guide, with the exceptions below, which the test suite tracks in `tests/test_hermes_contract.py`.

At startup the adapter checks that every base-class member it relies on exists, and logs one ERROR naming any that are missing.

### Internal imports

`hermes plugins validate` reports these as warnings. They exist because the command guard must resolve commands exactly as the installed Hermes does, and Hermes has no public way to ask. Each one is imported defensively, so a moved module cannot break the adapter.

| Import | Purpose | If it fails |
|---|---|---|
| `hermes_cli.commands.resolve_command`, `is_gateway_known_command` | Is this `/word` a Hermes command? | Guard fails closed |
| `agent.skill_bundles.resolve_bundle_command_key` | Is it a skill bundle? | Fewer known commands; unknown `/word`s still pass to Hermes |
| `agent.skill_commands.resolve_skill_command_key` | Is it a skill command? | The skill allowlist is inactive; unknown `/word`s pass to Hermes. Check this after a Hermes upgrade |

## Rocket.Chat

**Supported: 8.7 – 9.x, from one code path.** No version branches: a DDP "Method not found" answer marks the method unsupported for the process, REST falls back along an endpoint chain, and `x-deprecation-*` headers are logged once per endpoint at WARNING.

| API | Used for |
|---|---|
| `X-Auth-Token` + `X-User-Id` (Personal Access Token) | All REST calls |
| `GET me`, `permissions.listAll`, `settings.public` (`Site_Url`) | Startup checks and quote links |
| `POST chat.postMessage`, `chat.update`, `chat.delete`, `chat.react` | Replies, streamed edits, ack reaction |
| `GET rooms.info`, fallback `channels.info` → `groups.info` | Room name, group DM detection |
| `GET channels.history` → `groups.history` → `dm.history` | Room context, catch-up |
| `GET chat.getMessage`, `chat.getThreadMessages` | Thread context, webhook mode files |
| `GET subscriptions.get`, `users.info` | Member rooms, `/whoami` |
| `GET /file-upload/<id>/<name>` | Inbound attachments |
| `POST rooms.media/<rid>` → `rooms.mediaConfirm/<rid>/<fileId>` | File upload. One-step `rooms.upload` answers 404 on 8.8 |
| WebSocket `/websocket`, DDP `login {resume}`, `stream-room-messages` `__my_messages__` | Inbound |
| DDP `stream-notify-room` `<rid>/user-activity` | Typing indicator |
| Outgoing webhook, no script | Inbound fallback |
| Message attachment `actions` (`type: button`, `msg_in_chat_window`) | Approval buttons. Marked deprecated in Rocket.Chat's typings but present in 9.0; the typed commands are the fallback |

Replies always go over REST, because 9.0 removes the DDP `sendMessage` and `updateMessage` methods. `rooms.history` is deliberately unused: it exists only from 8.9.

The API list was compared against 9.0's source and deprecation list before a 9.0 release candidate existed. To surface anything deprecated before 9.0 on a test server, set `ROCKET_CHAT_DEPRECATION_THROW_ERRORS_FOR_VERSIONS_UNDER=9.0.0` there.

**Remaining risk:** DDP has no REST replacement for inbound messages. Rocket.Chat has announced no removal date; the webhook transport is the fallback.
