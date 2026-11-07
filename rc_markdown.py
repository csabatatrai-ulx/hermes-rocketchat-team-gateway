"""Adapt outgoing text to Rocket.Chat's markdown dialect.

Rocket.Chat's message parser (8.x) also strikes through text between SINGLE
tildes (``~text~``), even across spaces. Hermes and models use ``~`` to mean
"approximately" (``/context`` prints ``~ 2,276 tokens ~ 1.7%``), so the part
between the two marks rendered struck through. Rocket.Chat shows ``\\~`` as a
plain ``~``; standard strikethrough (``~~text~~``) works unchanged.

So a lone tilde (not part of ``~~``) becomes ``\\~``, outside code blocks,
inline code and URLs (where the backslash would show or break the link). A
code block not closed yet (while streaming) is protected to the end of the
text. Hermes's Slack adapter translates to its platform's dialect the same way
(``format_message``).

Pure module (no network, no ``gateway.*`` import).
"""

from __future__ import annotations

import re

# Protected spans where nothing is rewritten: code block (an unclosed one runs to the
# end of the text), inline code, URL (including inside angle brackets).
_PROTECTED = re.compile(r"```[\s\S]*?(?:```|\Z)|`[^`\n]+`|<?https?://[^\s<>)]+>?")
# A tilde with no tilde or backslash before it and no tilde after it.
_LONE_TILDE = re.compile(r"(?<![~\\])~(?!~)")


def escape_lone_tildes(text: str) -> str:
    """Replace lone ``~`` with ``\\~`` outside the protected spans."""
    if "~" not in (text or ""):
        return text
    out, pos = [], 0
    for match in _PROTECTED.finditer(text):
        out.append(_LONE_TILDE.sub(r"\\~", text[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_LONE_TILDE.sub(r"\\~", text[pos:]))
    return "".join(out)
