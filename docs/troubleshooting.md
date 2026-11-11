# Troubleshooting

The plugin logs at INFO to Hermes's `agent.log`; the console shows WARNING and above. Filter with `grep 'Rocket.Chat:' ~/.hermes/logs/agent.log`.

| Symptom | Cause and fix |
|---|---|
| `Rocket.Chat: not configured, missing: …` | A required setting is unset; the log names it |
| `Rocket.Chat: failed to authenticate` | Wrong URL, user ID or token. Check with `curl -H "X-Auth-Token: TOKEN" -H "X-User-Id: ID" https://chat.example.com/api/v1/me` |
| `totp-required` on REST calls | The token was created without "Ignore Two Factor Authentication". Create a new one |
| The bot does not answer | Is it a member of the room? Do `ROCKETCHAT_TEAM_ALLOWED_USERS` hold user IDs, not usernames? Run `@bot /whoami` and check the startup log for import errors |
| A command does nothing in the web client | The client handled it. Mention the bot first: `@hermes /new` |
| `The /x command is not enabled over Rocket.Chat` | The guard works as designed. See [commands](commands.md) |
| "The model provider rejected the request" | Authentication with the provider failed, the bot is fine. The log has `model endpoint problem (kind=auth)`. Check the API key and `hermes config show` |
| "The model provider is not reachable" | Endpoint down, timeout, quota or wrong base URL; `kind=unavailable` in the log |
| The WebSocket reconnects often | Usually a proxy's idle timeout; see [deployment](deployment.md#reverse-proxy) |
| HTTP 429 in the log | The bot lacks `api-bypass-rate-limit`; give it the `bot` role |
| The quote card is missing | Site URL on the server does not match the address clients use |
| Files do not upload | Rocket.Chat's upload setting, allowed types or size limit. The reply names the server's reason |
