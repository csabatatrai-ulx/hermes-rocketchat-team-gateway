# Architecture

## Scope

This plugin is the wire between Hermes and Rocket.Chat, and nothing else. Registering no tools is a design decision that matches Hermes's own messaging adapters, none of which registers agent tools. Hermes's platform guide also says plugins "must not register an equivalent model surface that lets the agent initiate outbound messages on its own". Capabilities such as searching a room belong to tools the agent reaches over MCP, where they work from every channel and can be permissioned separately.

## Message flow

```text
                ┌─ DDP WebSocket (default) ─┐
Rocket.Chat ────┤                           ├─► classify_inbound ─► command guard / dedup
                └─ Outgoing Webhook ────────┘           │
                                                        ▼
                                  room context + attachments ─► MessageEvent ─► Hermes core
                                                                                    │
Rocket.Chat ◄── REST (send, edit, upload, react) ◄── duplicate guard ◄── adapter.send ◄┘
```

Both inbound paths build the same payload shape and end in the same pure decision function, so their behaviour cannot drift apart.

## Modules

| Module | Responsibility | Depends on Hermes |
|---|---|---|
| `adapter.py` | `BasePlatformAdapter` implementation, `register()` | Yes |
| `ddp_transport_mixin.py` | Realtime API connection, reconnect, catch-up | Yes |
| `webhook_mixin.py` | Webhook listener, shared dispatch, building the turn | Yes |
| `scoped_env.py` | Profile-scoped environment | Yes |
| `setup_wizard.py` | `hermes gateway setup` | Yes |
| `standalone.py` | Delivery from cron without a live adapter | Yes |
| `inbound.py` | The decision chain, pure | No |
| `builtin_commands.py` | Command guard | No (resolvers injected) |
| `history.py`, `room_session.py`, `dm_session.py` | Context windows and boundaries | No |
| `media.py` | Attachment handling | No |
| `dedup.py`, `shared_state.py` | Duplicate suppression, process-wide state | No |
| `rest_client.py` | REST API | No |
| `config.py` | All settings in one dataclass | No |
| `prompt_guidance.py`, `user_texts.py` | Text for the model, text for users | No |
| `reply_reference.py`, `rc_markdown.py`, `reasoning_format.py`, `asker.py`, `whoami.py` | Small pure helpers | No |

`tests/test_agnostic_boundary.py` fails the build if a Hermes-independent module imports Hermes.

## Decisions worth knowing

- **The order of checks in `classify_inbound` matters.** The dedup runs after the mention and allowlist filters, so room chatter cannot push an old mention out of the cache, and before any command or turn, so a redelivery runs nothing again.
- **One DDP subscription, not one per room.** Rocket.Chat limits subscriptions per connection, and a per-room design failed at about 40 rooms. Messages from non-member rooms are dropped.
- **Sessions are Hermes's.** The plugin passes the real thread and chat type and keeps no session logic.
- **Context stays in `channel_context`,** never in `text`, so command recognition keeps working.
- **State is process-wide.** Hermes can create a short-lived second adapter instance for a concurrent send; per-instance caches and locks would not protect against that case.
- **Outbound dedup records before sending.** If the POST fails after the server created the message, a retry must not post it twice. An explicit refusal removes the record, so a retry really sends.
- **POST retries live in the REST client,** because Hermes core does not retry its primary response delivery.
- **No signal handlers.** `disconnect()` is the documented shutdown hook; `signal.signal` would replace core's handler.
