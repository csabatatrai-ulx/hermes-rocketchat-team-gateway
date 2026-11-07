"""Buttons on Hermes's dangerous-command prompt and slash-command confirmation.

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


def confirm_attachments(bot_username: str, allowed_commands: FrozenSet[str]) -> List[Dict[str, Any]]:
    """The buttons of a slash-command confirmation (``/new``): the same rule, only allowed answers get one.

    "Always" would switch the confirmation off for everyone on the gateway, so it needs
    ``ROCKETCHAT_ALLOW_BUILTIN_ALWAYS``.
    """
    buttons = [
        {
            "type": "button",
            "text": label,
            "msg": f"@{bot_username} /{command}",
            "msg_in_chat_window": True,
            "msg_processing_type": "sendMessage",
        }
        for label, command in (("Approve Once", "approve"), ("Always Approve", "always"), ("Cancel", "cancel"))
        if command in allowed_commands
    ]
    return [{"button_alignment": "horizontal", "actions": buttons}] if buttons else []


def confirm_body(message: str) -> str:
    """Hermes's confirmation text without its list of typed answers, which the buttons replace.

    That list would also offer "Always Approve", which is refused by default. In another Hermes language the
    words differ and the text stays whole, which is only longer.
    """
    head = message.split("\n\nChoose:", 1)[0]
    return "\n".join(line for line in head.splitlines() if "Text fallback" not in line).rstrip()


def is_approval_answer(text: str) -> bool:
    """Whether the (mention-stripped) text answers a pending prompt: ``/approve``, ``/deny``, ``/cancel``, ``/always``."""
    words = (text or "").strip().lower().split()
    return bool(words) and words[0].split("@", 1)[0] in ("/approve", "/deny", "/cancel", "/always")
