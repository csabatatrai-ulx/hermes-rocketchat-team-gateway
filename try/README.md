# Try it locally

A local Rocket.Chat, Hermes Agent and this plugin, wired together by Docker Compose. A bot, two people (alice and bob) and a room (#hermes-demo) are created for you. Everything listens on 127.0.0.1 only.

You need Docker with Compose. The first start downloads Rocket.Chat, MongoDB and Hermes images, so it takes a few minutes.

## Start

From the repository root:

```bash
docker compose -f try/compose.yaml up -d
python3 try/check.py
```

It starts with a **scripted demo model**: no account, no API key. It is not AI; it answers with fixed text, and asks to run a terminal command when you ask it to delete something. Everything around the model is real: the plugin, Hermes, Rocket.Chat, the command guard and the approval buttons. `try/check.py` checks them end to end (7 checks, about 15 seconds; Python 3, standard library only) and exits non-zero if any fails.

### A real model

Sign in to Nous Portal (the free tier is enough); it replaces the demo model:

```bash
docker compose -f try/compose.yaml exec -u hermes hermes hermes portal
docker compose -f try/compose.yaml restart hermes
```

Or use any OpenAI-compatible endpoint from the first start (`down -v` first if the stack already ran):

```bash
LLM_API_KEY=... LLM_BASE_URL=https://api.example.com/v1 LLM_MODEL=model-name docker compose -f try/compose.yaml up -d
```

The model needs tool calling for the approval demo. `try/check.py` is for the demo model only.

## Use

Open http://localhost:3000 and sign in as **alice**, password **hermes-demo**. Sign in as **bob** in a private window to be the second person.

| Try this in #hermes-demo | What happens |
|---|---|
| `@hermes when is the release?` as alice and bob at once | Each gets their own answer, quoting the question |
| `@hermes please delete the old build` (demo model) or `@hermes run rm -rf /tmp/old-build with the terminal tool` (real model) | A prompt with buttons; only the asker's click runs it |
| `@hermes /yolo on` | Refused, with no model call |
| `@hermes !new` | A confirmation with buttons |
| A direct message to hermes | No mention needed |

The terminal tool runs inside the Hermes container only.

## Stop

```bash
docker compose -f try/compose.yaml down -v    # -v also deletes the demo data
```

## Notes

- Another port: `TRY_PORT=3300 docker compose -f try/compose.yaml up -d`, then open http://localhost:3300 (and `TRY_PORT=3300 python3 try/check.py`). The demo model's port is `TRY_DEMO_PORT` (default 3001).
- The plugin is mounted read-only from this checkout, so local changes are live after `restart hermes`.
- The demo passwords and settings are for this local stack only. For a real server, follow the main [README](../README.md#quick-start).
