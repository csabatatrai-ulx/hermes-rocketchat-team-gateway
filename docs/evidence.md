# Testing and evidence

Every number below comes from a run, not from a claim. The live runs used Rocket.Chat 8.9.1, the official Hermes 0.23.0 image and Chrome driving the web client.

| What was checked | Result |
|---|---|
| Unit and contract tests in CI, on Hermes 0.22.3 (oldest supported) and 0.23.0 | 750+ pass on both |
| `hermes plugins validate --install-deps` | Passes, no private core imports |
| End-to-end scenarios: mentions, threads, DMs, files both ways, concurrent askers, the command guard | 39 / 39 |
| Approval buttons clicked in a room and a thread, including another person's click doing nothing | 16 / 16 |
| `/new` confirmation buttons | 10 / 10 |
| Approval button in a one-to-one DM | 4 / 4 |
| Gateway restart and network outage, with catch-up | 14 / 14 |
| Cron delivery through the running gateway, and with the gateway stopped | 2 / 2 |
| Refused commands such as `/yolo`, `/tools`, `/cron` and unknown `/words` | 0 model calls |
| Real model (Nous Portal): room context, identity, threads, long answers, approval | Pass |
| Personal assistant, a second Hermes with the `personal` preset and a real model: DM questions, memory across sessions, preset commands, `/sethome` and a delivered reminder, `/yolo` refused, other people and rooms ignored | 19 / 19 |
| Fully local: the same real-model checks against a self-hosted open-weight model on vLLM, no cloud model service (team bot: room context, identity, threads, long answers, approval; personal assistant: all of the above) | 11 / 11 and 19 / 19 |

## How these were measured

- **Unit and contract tests** run in [CI](https://github.com/csabatatrai-ulx/hermes-rocketchat-team-gateway/actions/workflows/ci.yml) on every push. The contract tests import the Hermes release under test and check each member the adapter relies on, so a renamed or removed interface fails the build.
- **Live runs** use a real Rocket.Chat 8.9.1 in Docker (with MongoDB and NATS), the official Hermes Agent image and this plugin exactly as committed. Test users post through the Rocket.Chat REST API, and every button is clicked by Chrome in the Rocket.Chat web client, signed in as the person who clicks.
- **Each check asserts an outcome you could see yourself**: the answer arrives in the right room or thread and mentions its asker; a dangerous command's file is still on disk after another person's click and gone only after the asker's; a refused command produces no request at the model endpoint (a recording stub model counts them).
- **Restarts and outages** are real: the gateway container is stopped, or its connection to Rocket.Chat is cut, while people keep writing, then started again.
- **Real-model runs** use Nous Portal, and again a self-hosted model served by vLLM on an on-premises GPU. The personal assistant run uses a second Hermes instance with its own bot account.
- **The demo video** is recorded from such a run; nothing in it is mocked.

The live-run scripts depend on a local test server and are not in this repository; [Try it locally](try.md) sets up the same kind of environment for you to repeat any of it by hand.

Building it also turned up two Hermes gateway bugs, which the author reported upstream: [#148433](https://github.com/NousResearch/hermes-agent/issues/148433) (a shutdown during a redirected turn overwrites an already streamed reply) and [#149169](https://github.com/NousResearch/hermes-agent/issues/149169) (`/loop` and `/heartbeat` stay silent after a gateway restart until the chat gets a plain message).

## Continuous integration

Every push runs the unit and contract tests and the catalog validator against two Hermes releases: the oldest the plugin supports and the newest tested live. The contract tests fail when a Hermes release removes or renames a member the adapter relies on, so a breaking change shows up in CI before it reaches a server.

## Not yet verified

- The Rocket.Chat mobile apps: the buttons follow the mobile app's source, but have not been clicked on a phone.
- Rocket.Chat 9.x: supported from the 9.0 API, not run live.

How to run the tests yourself is in [Development](development.md).
