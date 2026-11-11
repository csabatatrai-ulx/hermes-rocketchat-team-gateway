# Attachments

## Inbound

Files on the mentioning message are downloaded with the bot's token and passed to Hermes in `MessageEvent.media_urls`, the same way as Hermes's own adapters. Hermes then gives images to the model or its vision helper and transcribes audio.

- A vision-capable model or helper is needed for images.
- `gateway.max_inbound_media_bytes` limits size. Rocket.Chat states the size up front, so an oversized file is not downloaded.
- A small text file (text extension, `text/*`, or entirely valid UTF-8, up to 100 KB) is inserted into the request. Larger or binary documents go as a path.
- Rocket.Chat's generated thumbnails are skipped, so an image arrives once.
- A failed or refused file is reported to the model, so it does not describe an earlier file or guess. The turn continues.
- Files from earlier messages are not fetched; the context block only names them.

## Outbound

When the model writes `MEDIA:/absolute/path`, Hermes core validates the path and calls the adapter's `send_document`, `send_image_file`, `send_voice` or `send_video`. The adapter uploads with `rooms.media` and `rooms.mediaConfirm`, into the reply's thread. Only the extension and size are logged.

`ROCKETCHAT_TEAM_OUTBOUND_FILES=false` disables this and removes the tag from the platform guidance. Rocket.Chat's own settings still apply: upload enabled, allowed types and size. A refusal is reported to the user with the server's reason.

`send_image` (a URL) is not implemented, on purpose: Rocket.Chat previews links itself, and downloading a foreign URL would be an SSRF surface.
