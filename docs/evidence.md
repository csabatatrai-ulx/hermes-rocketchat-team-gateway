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

## Continuous integration

Every push runs the unit and contract tests and the catalog validator against two Hermes releases: the oldest the plugin supports and the newest tested live. The contract tests fail when a Hermes release removes or renames a member the adapter relies on, so a breaking change shows up in CI before it reaches a server.

## Not yet verified

- The Rocket.Chat mobile apps: the buttons follow the mobile app's source, but have not been clicked on a phone.
- Rocket.Chat 9.x: supported from the 9.0 API, not run live.

How to run the tests yourself is in [Development](development.md).
