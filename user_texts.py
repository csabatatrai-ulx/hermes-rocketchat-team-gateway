"""Every chat text the plugin itself writes (everything not written by the model).

Keeping them in one module makes them easy to review and keeps wording out of
the transport code. Texts use ``str.format`` placeholders.

The model-facing guidance lives in ``prompt_guidance.py``.
"""

from __future__ import annotations

TEXTS = {
    # Cold-start greeting in the home channel (adapter.connect).
    "startup_greeting": "Hi everyone!",
    # ROCKETCHAT_DM_ONLY: one-time notice on a mention in a room.
    "dm_only_notice": (
        "ℹ️ _This is a personal bot. It only works in direct messages (DM) and does not answer in this room._"
    ),
    # Command guard rejections (webhook_mixin._dispatch_inbound_decision).
    "persistent_approval_blocked": (
        "Persistent approval (\"always\", `/always`, `/approve always`) is not enabled on this bot, "
        "because it would affect everyone else too. Approve once: `/approve`, deny: `/deny`."
    ),
    "builtin_command_blocked": (
        "The `/{command}` command is not enabled over Rocket.Chat. If you need it, "
        "enable it with `{env_key}=true` in .env, then restart the Hermes gateway."
    ),
    "skill_command_blocked": (
        "The `/{command}` skill is not enabled over Rocket.Chat. If you need it, "
        "enable it with `{env_key}=true` in .env, then restart the Hermes gateway."
    ),
    # Model endpoint failures (adapter.send, classify_model_endpoint_failure).
    "model_endpoint_auth": (
        "_⚠️ The model provider rejected the request (authentication error), so I cannot "
        "answer right now. Please tell the operator._"
    ),
    "model_endpoint_unavailable": (
        "_⚠️ The model provider is not reachable right now, so I cannot answer. Please try again later._"
    ),
    "model_endpoint_detail": "{message}\n\n_(Detail: {detail})_",
    # Posted to the room of an interrupted turn on shutdown (adapter._notify_inflight_shutdown).
    "shutdown_notice": (
        "_⚠️ The bot is restarting while preparing the answer — please ask again in a few seconds._"
    ),
    # Startup check for messages that were never processed (ddp_transport_mixin).
    "startup_catchup_skipped_one": "_⚠️ I restarted and did not process this message. Please send it again._",
    "startup_catchup_skipped_many": (
        "_⚠️ I restarted and did not process {count} messages. Please send them again._"
    ),
    # /whoami (whoami.build_whoami_reply).
    "whoami_user": "**RC user:** `{username}`",
    "whoami_emails": "**E-mail(s):** {emails}",
    "whoami_no_email": "(no known e-mail address)",
    "whoami_source": "_Source: your login as authenticated by the Rocket.Chat server._",
}


def user_text(key: str, **values: str) -> str:
    """The text for ``key``, with ``values`` substituted."""
    text = TEXTS[key]
    return text.format(**values) if values else text
