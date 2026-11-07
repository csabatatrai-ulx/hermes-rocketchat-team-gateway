# Deployment

## Recommended Hermes settings for a team bot

Hermes defaults fit one person at a terminal. A plugin cannot change them, so set these in `config.yaml`.

```yaml
platform_toolsets:
  rocketchat:            # explicit: only what this bot needs
    - my-mcp-server
display:
  busy_input_mode: queue # a second message waits instead of interrupting
  platforms:
    rocketchat:
      tool_progress: new
      interim_assistant_messages: false
      long_running_notifications: false   # the typing indicator is the signal
      cleanup_progress: true
      show_reasoning: false
approvals:
  mode: manual                            # a person answers every dangerous command, see below
thread_sessions_per_user: true            # per-person sessions in threads too
memory:
  memory_enabled: false                   # shared across all users otherwise
  user_profile_enabled: false
rocketchat:
  group_allow_admin_from: [<admin user id>]
  allow_admin_from: [<admin user id>]
```

**Set `approvals.mode: manual` on a shared bot.** Hermes's default, `smart`, lets a helper model approve a flagged command it considers safe, and then no person is asked and no button appears. In testing it approved an `rm -rf` of a directory by itself. Use `smart` only if you accept that decision being made by a model.

For deployment-specific tool guidance, use `platform_hints.rocketchat.append` rather than changing the plugin.

## A personal assistant

The same plugin runs as one person's assistant, by configuration only:

```dotenv
ROCKETCHAT_DM_ONLY=true
ROCKETCHAT_ALLOWED_USERS=<owner user id>
ROCKETCHAT_ALLOW_BUILTIN_PRESET=personal
ROCKETCHAT_STARTUP_GREETING=false
ROCKETCHAT_HOME_CHANNEL=<owner's DM room id>
```

## Rocket.Chat side

- The bot needs the `user` and `bot` roles. The `bot` role carries `api-bypass-rate-limit`; without it, replies and streamed edits run into HTTP 429, and the plugin warns at startup.
- Set the server's **Site URL** correctly. Quote cards render only from links on that address.
- Enable `Message_AllowUnrecognizedSlashCommand` if people type Hermes commands without mentioning the bot.

## State

| What | Where | Keep across restarts? |
|---|---|---|
| `inbound.json` | `$HERMES_HOME/rocketchat-plugin-dedup/` | Yes: otherwise a reaction on an old message can start a new answer |
| `outbound.json` | same | Preferably (10-second window) |
| `ddp_cursor.json` | same | Yes: otherwise a restart cannot report what it missed |
| `ddp_status.json` | same | No, rewritten at start |
| Hermes sessions | `$HERMES_HOME/` | Yes |

## Reverse proxy

Inbound is a long-lived WebSocket at `/websocket`. Keep the proxy from closing it:

```nginx
location /websocket {
    proxy_pass http://rocketchat;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

## Containers

`hermes gateway` does not exit when a platform fails to start, so a container looks healthy. Use the plugin's status file for readiness. Its `state` value moves through `connecting`, `connected`, `reconnecting`, `auth_failed` and `stopped`.

```bash
grep -q '"connected"' "$HERMES_HOME/rocketchat-plugin-dedup/ddp_status.json"
```

Keep `$HERMES_HOME` on a persistent volume. Hermes's media cache lives there too, so size it for a day of attachments. Without a shared secret store, write `.env` from your secret manager at startup.
