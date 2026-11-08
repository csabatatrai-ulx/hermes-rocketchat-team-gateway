# Try it locally

A local Rocket.Chat, Hermes Agent and this plugin, wired together by Docker Compose. A bot, two people (alice and bob) and a room (#hermes-demo) are created for you. Everything listens on 127.0.0.1 only.

You need Docker with Compose. The first start downloads Rocket.Chat, MongoDB and Hermes images, so it takes a few minutes.

## Start

From the repository root:

```bash
docker compose -f try/compose.yaml up -d
docker compose -f try/compose.yaml exec -u hermes hermes hermes portal   # sign in to Nous Portal and pick a model
docker compose -f try/compose.yaml restart hermes
```

Nous Portal's free tier is enough. To use any other OpenAI-compatible endpoint instead, skip the last two commands and start with:

```bash
LLM_API_KEY=... LLM_BASE_URL=https://api.example.com/v1 LLM_MODEL=model-name docker compose -f try/compose.yaml up -d
```

The model needs tool calling for the approval demo.

## Use

Open http://localhost:3000 and sign in as **alice**, password **hermes-demo**. Sign in as **bob** in a private window to be the second person.

| Try this in #hermes-demo | What happens |
|---|---|
| `@hermes when is the release?` as alice and bob at once | Each gets their own answer, quoting the question |
| `@hermes run rm -rf /tmp/old-build with the terminal tool` | A prompt with buttons; only the asker's click runs it |
| `@hermes /yolo on` | Refused, with no model call |
| `@hermes !new` | A confirmation with buttons |
| A direct message to hermes | No mention needed |

The terminal tool runs inside the Hermes container only.

## Stop

```bash
docker compose -f try/compose.yaml down -v    # -v also deletes the demo data
```

## Notes

- Another port: `TRY_PORT=3300 docker compose -f try/compose.yaml up -d`, then open http://localhost:3300.
- The plugin is mounted read-only from this checkout, so local changes are live after `restart hermes`.
- The demo passwords and settings are for this local stack only. For a real server, follow the main [README](../README.md#quick-start).
