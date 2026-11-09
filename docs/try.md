# Try it locally

A local Rocket.Chat, Hermes Agent and this plugin, wired together by Docker Compose. A bot, two people (`alice` and `bob`) and a room (`#hermes-demo`) are created for you. Everything listens on `127.0.0.1` only.

!!! info "What you need"
    Docker with Compose. The first start downloads the Rocket.Chat, MongoDB and Hermes images, so it takes a few minutes.

## Start

From a checkout of the repository:

```bash
git clone https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway
cd hermes-rocketchat-team-gateway
docker compose -f try/compose.yaml up -d
python3 try/check.py
```

It starts with a **scripted demo model**: no account, no API key. It is not AI; it answers with fixed text, and asks to run a terminal command when you ask it to delete something. Everything around the model is real: the plugin, Hermes, Rocket.Chat, the command guard and the approval buttons.

`try/check.py` checks them end to end and exits non-zero if any check fails (Python 3, standard library only, about 15 seconds):

```text
PASS  a mention gets an answer that quotes the question and mentions the asker
PASS  two people at once: each answer quotes its own question and mentions its own asker
PASS  /yolo is refused over chat, without reaching the model
PASS  a dangerous command waits for approval, with buttons on the prompt
PASS  another person's approval runs nothing
PASS  the asker's approval runs the command
PASS  a direct message needs no mention

7 / 7 passed
```

Then choose a real model:

=== "Nous Portal (free tier)"

    ```bash
    # sign in to Nous Portal and pick a model; it replaces the demo model
    docker compose -f try/compose.yaml exec -u hermes hermes hermes portal
    docker compose -f try/compose.yaml restart hermes
    ```

=== "Any OpenAI-compatible endpoint"

    Start the stack with the endpoint instead (`down -v` first if it already ran):

    ```bash
    LLM_API_KEY=... LLM_BASE_URL=https://api.example.com/v1 LLM_MODEL=model-name \
      docker compose -f try/compose.yaml up -d
    ```

    The model needs tool calling for the approval demo.

## Use

Open **http://localhost:3000** and sign in as `alice`, password `hermes-demo`. Sign in as `bob` in a private window to be the second person.

| Try this in `#hermes-demo` | What happens |
|---|---|
| `@hermes when is the release?` as alice and bob at once | Each gets their own answer, quoting the question |
| `@hermes please delete the old build` (demo model) or `@hermes run rm -rf /tmp/old-build with the terminal tool` (real model) | A prompt with buttons; only the asker's click runs it |
| `@hermes /yolo on` | Refused, with no model call |
| `@hermes !new` | A confirmation with buttons |
| A direct message to `hermes` | No mention needed |

The terminal tool runs inside the Hermes container only.

## Stop

```bash
docker compose -f try/compose.yaml down -v    # -v also deletes the demo data
```

## Notes

- **Another port:** `TRY_PORT=3300 docker compose -f try/compose.yaml up -d`, then open http://localhost:3300.
- **Your own changes:** the plugin is mounted read-only from the checkout, so a change is live after `docker compose -f try/compose.yaml restart hermes`.
- **Not for production:** the demo passwords and settings are for this local stack only. For a real server, see [Install on your server](getting-started.md).
