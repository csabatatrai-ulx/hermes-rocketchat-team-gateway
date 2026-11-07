"""Optional cleanup of reasoning that a model leaks into its answer.

Some OpenAI-compatible backends return chain-of-thought as plain text in front
of the reply instead of inside ``<think>`` tags or a reasoning field, so
Hermes's think-stripper never sees it. This layer is off by default and
enabled with ``ROCKETCHAT_REASONING_LEAK_FORMATTING=true``; it should stay off
for models that do not leak, because an ordinary answer starting "Based on ..."
would be a false positive.

If the first paragraph reads like self-narrated reasoning and a real answer
follows, ``drop`` mode removes the preamble and ``wrap`` mode renders it as a
``> 💭`` aside. The output is standard ``>`` markdown, nothing here is specific
to Rocket.Chat. The switch and the pattern are supplied by the caller (the
``ROCKETCHAT_REASONING_LEAK_*`` settings).
"""

from __future__ import annotations

import re

# A reasonable default, not a universal truth about how models leak reasoning. Override
# it with ROCKETCHAT_REASONING_LEAK_PATTERN when a backend's leak style does not match.
DEFAULT_REASONING_LEAD_PATTERN = (
    r"^(the user|i should|i need to|let me|thinking process|"
    r"analyz(e|ing)|based on|the message|this (is|appears))"
)

DEFAULT_REASONING_LEAK_LABEL = "💭 _thinking_"

# What to do with a detected leaked-reasoning preamble:
#
# "drop" removes it and keeps only the real answer. The internal monologue adds nothing,
# and it would otherwise be re-ingested as room history, where the bot then treats its
# own past guesses as fact.
#
# "wrap" keeps it as a "> 💭" aside. Safer against a false positive eating a real answer,
# but noisier.
DEFAULT_REASONING_LEAK_MODE = "drop"


def _render(lead_in: str, rest: str, *, label: str, mode: str) -> str:
    if mode == "drop":
        return rest.strip()
    quoted = "\n".join(f"> {line}" for line in lead_in.splitlines())
    return f"> {label}\n{quoted}\n\n{rest}"


def wrap_leaked_reasoning(
    content: str, *, pattern: str, label: str, mode: str = DEFAULT_REASONING_LEAK_MODE
) -> str:
    """Strip or wrap a leaked reasoning preamble.

    ``mode`` is ``"drop"`` (remove the preamble, keep only the answer) or
    ``"wrap"`` (render it as a ``> 💭`` blockquote aside). Either way the
    guards below guarantee a non-empty real answer always survives - a
    message that is ENTIRELY leaked reasoning (no answer after it) is left
    untouched, since dropping it would post nothing.

    The separator between the leaked reasoning and the real answer is
    inconsistent in practice - sometimes a blank line, sometimes a single
    line break. Prefer splitting on the blank line (keeps a multi-line
    reasoning block together); fall back to the first single line break
    when there's no blank line at all.

    A leak is not always confined to the FIRST paragraph: a backend may leak
    reasoning across SEVERAL blank-line-separated paragraphs (e.g. "The user
    is asking ..." / "Looking at the transcript, the key points were: 1. ...
    9. ..." / "I should provide a brief summary ...") before the real answer
    starts. This consumes further LEADING paragraphs into the same preamble
    as long as they still look like reasoning, with one concession: a single
    paragraph that does NOT itself match ``pattern`` (a plain recap or list)
    is still swallowed as a "bridge" IF the paragraph right after it matches
    ``pattern`` again. The LAST paragraph is never consumed - there must
    always be something left to treat as the real answer.

    Leaves anything that doesn't match ``pattern`` untouched - this is a
    heuristic, not an exhaustive parser.
    """
    if "\n\n" not in content:
        lead_in, _, rest = content.partition("\n")
        lead_in = lead_in.strip()
        rest = rest.strip()
        if rest and re.match(pattern, lead_in, re.IGNORECASE):
            return _render(lead_in, rest, label=label, mode=mode)
        return content

    paragraphs = content.split("\n\n")
    if not re.match(pattern, paragraphs[0].strip(), re.IGNORECASE):
        return content

    def matches(index: int) -> bool:
        return bool(re.match(pattern, paragraphs[index].strip(), re.IGNORECASE))

    consumed = 1
    i = 1
    while i < len(paragraphs) - 1:
        if matches(i):
            consumed = i + 1
            i += 1
            continue
        if i + 1 < len(paragraphs) - 1 and matches(i + 1):
            consumed = i + 2
            i += 2
            continue
        break

    lead_in = "\n\n".join(p.strip() for p in paragraphs[:consumed])
    rest = "\n\n".join(p.strip() for p in paragraphs[consumed:]).strip()
    if not rest:
        return content
    return _render(lead_in, rest, label=label, mode=mode)
