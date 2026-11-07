"""The ``/whoami`` command: report the caller's own Rocket.Chat identity.

Deliberately small and read-only: it shows exactly what the bot sees about
the CALLER - the Rocket.Chat server-authenticated sender (``u._id`` ->
``users.info``), never anything taken from the message text - without
touching any other user's data. Useful as a diagnostic ("who does the bot
think I am?"); it carries no permission logic.

Same split as ``history.py``: this module is pure (no HTTP), the adapter
gathers the inputs (``users.info``) and calls ``build_whoami_reply`` with the
already-fetched values.
"""

from __future__ import annotations

from typing import List, Optional

try:  # as part of the plugin package (the normal runtime path)
    from .user_texts import user_text
except ImportError:  # unit tests import it as a bare module (see config.py)
    from user_texts import user_text

DEFAULT_WHOAMI_COMMAND = "/whoami"


def is_whoami_command(text: str, *, command: str = DEFAULT_WHOAMI_COMMAND) -> bool:
    """True when ``text`` is exactly the whoami command.

    Strict, whole-message, case-insensitive match: a prefix match would
    swallow a real question that happens to start with "/whoami".
    """
    if not text or not command:
        return False
    return text.strip().lower() == command.strip().lower()


def build_whoami_reply(
    *, rc_username: str, emails: Optional[List[str]] = None
) -> str:
    """Plain-text report of the caller's Rocket.Chat identity (``user_texts.py``)."""
    emails = emails or []
    email_text = ", ".join(emails) if emails else user_text("whoami_no_email")
    return "\n".join(
        [
            user_text("whoami_user", username=rc_username),
            user_text("whoami_emails", emails=email_text),
            user_text("whoami_source"),
        ]
    )
