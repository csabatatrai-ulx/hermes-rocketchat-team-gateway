"""Accept and deny buttons on Hermes's dangerous-command prompt.

A Rocket.Chat attachment button sends a preset message as the person who clicked it. So a button here
sends exactly what that person could type (``@bot /approve``, ``@bot /deny``), and the click takes the
normal inbound path: the server authenticates the sender, then the allowlist, the command guard, Hermes's
own admin lists and session key all apply unchanged. There is no separate authorization to get wrong. In
a room each person has their own session, so someone else's click reaches a session with nothing pending.

The attachment type is marked deprecated in Rocket.Chat's typings although it is still present in 9.0, and
not every client may render it, so the prompt always names the typed commands too.

Pure module (no network, no ``gateway.*`` import).
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Tuple

# Hermes's approval choice -> (the command word, its arguments, the allowlist key the guard checks).
_CHOICES = {
    "once": ("approve", "", "approve"),
    "session": ("approve", " session", "always"),
    "always": ("approve", " always", "always"),
    "deny": ("deny", "", "deny"),
}


def approval_attachments(
    actions: Iterable[Tuple[str, str, str]],
    bot_username: str,
    allowed_commands: FrozenSet[str],
) -> List[Dict[str, Any]]:
    """The attachment carrying one button per offered choice, or ``[]`` when none is allowed.

    ``actions`` are Hermes's ``(label, choice, style)`` rows. A choice the command guard would refuse gets no
    button, so persistent approval is not offered unless ``ROCKETCHAT_ALLOW_BUILTIN_ALWAYS`` is on. The mention
    keeps the click away from the client's own slash-command handling and makes it work in rooms and DMs alike.
    """
    buttons = []
    for label, choice, _style in actions:
        known = _CHOICES.get(choice)
        if known is None or known[2] not in allowed_commands:
            continue
        command, arguments, _key = known
        buttons.append({
            "type": "button",
            "text": label,
            "msg": f"@{bot_username} /{command}{arguments}",
            "msg_in_chat_window": True,
            "msg_processing_type": "sendMessage",
        })
    return [{"button_alignment": "horizontal", "actions": buttons}] if buttons else []


def is_approval_answer(text: str) -> bool:
    """Whether the (mention-stripped) text answers a pending approval: ``/approve ...`` or ``/deny ...``."""
    words = (text or "").strip().lower().split()
    return bool(words) and words[0].split("@", 1)[0] in ("/approve", "/deny")
