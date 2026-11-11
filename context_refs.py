"""Keep Hermes's ``@`` context references inert in chat text.

Hermes core expands ``@file:``, ``@folder:``, ``@git:``, ``@url:``, ``@diff`` and ``@staged``
(plus any ``@<kind>:`` a plugin registers) in every inbound gateway turn, before the model runs:
it reads the file, lists the folder, runs the git command or fetches the page and appends the
result to the turn. On a terminal that is the user's own convenience. In a shared room it lets
anyone who can reach the bot attach files from the bot's workspace (its Hermes home, with the
config and memory) or make the bot's host fetch a URL, without a tool call, so no toolset limit,
approval or ``pre_tool_call`` hook sees it. Core expands the room context block too, so a
reference hidden in an earlier message, even one from an unverified sender, is expanded when
someone else mentions the bot.

The adapter therefore puts a zero-width space after the ``@`` of every such reference before the
text reaches core (the trick core itself uses for ``@everyone``). The text people see in
Rocket.Chat is unchanged; the model reads the reference as plain text and can still open the
file or page through a tool, where the operator's limits and approvals apply.

``ROCKETCHAT_ALLOW_CONTEXT_REFERENCES=true`` keeps references in the asker's own message live.
The room context and inserted documents stay inert either way: neither is the asker's request.
"""
import re

# Mirrors core's REFERENCE_PATTERN and its plugin fallback (agent/context_references.py): the
# same left boundary, then a built-in bare kind or any ``<kind>:`` followed by a value. Matching
# a little more than core (any letters after ``@`` and a colon) is the safe direction.
_REFERENCE_START = re.compile(
    r"(?<![\w/])@(?=(?:diff|staged)\b|[A-Za-z][A-Za-z0-9_-]*:\S)"
)
_ZERO_WIDTH_SPACE = "\u200b"


def neutralize_context_references(text: str) -> str:
    """``text`` with a zero-width space after the ``@`` of each context reference."""
    if not text or "@" not in text:
        return text
    return _REFERENCE_START.sub("@" + _ZERO_WIDTH_SPACE, text)
