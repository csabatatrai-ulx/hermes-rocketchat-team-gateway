"""Asker attestation: who is asking, and in which room.

A Rocket.Chat history MCP server does not know who is asking, and the model
supplies the room, so a room member (or an injected instruction) could ask
about a private room the bot belongs to but the person does not. The gateway
knows exactly who addressed the bot and where: per turn it issues a short-lived
signed attestation, the model passes it verbatim in the tool call and the
server verifies it. The secret is never in the model's context, so the
attestation cannot be forged.

Format (see docs/security.md for the verifier contract):
``v1.<user ID>.<room ID>.<expiry, unix seconds>.<signature>``, where the
signature is the HMAC-SHA256 of ``v1.<user ID>.<room ID>.<expiry>`` with the
shared key, base64url without ``=``, first 22 characters. Pure module (no
network, no Hermes imports).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

ASKER_TOKEN_VERSION = "v1"
# 30 minutes: a new attestation is issued every turn, so it only has to outlive one
# turn, including one waiting for an approval. An attestation that ends up in the chat
# by accident is usable for at most half an hour.
ASKER_TTL_SECONDS = 30 * 60
_SIG_CHARS = 22
_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
# The model sees this line and must pass its value verbatim.
ASKER_LINE_PREFIX = "[Rocket.Chat asker: "


def sign_asker(key: str, user_id: str, room_id: str, expires: int) -> str:
    """The attestation with the given expiry; ``""`` when there is no key or an
        id is not a Rocket.Chat identifier (the line is then omitted).
    """
    if not key or not _ID_RE.match(str(user_id or "")) or not _ID_RE.match(str(room_id or "")):
        return ""
    payload = f"{ASKER_TOKEN_VERSION}.{user_id}.{room_id}.{int(expires)}"
    digest = hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(digest).decode().rstrip("=")[:_SIG_CHARS]
    return f"{payload}.{signature}"


def issue_asker(key: str, user_id: str, room_id: str, now: float, ttl: int = ASKER_TTL_SECONDS) -> str:
    """The attestation for the current turn (expires at ``now + ttl``)."""
    return sign_asker(key, user_id, room_id, int(now) + int(ttl))


def asker_line(token: str) -> str:
    """The ``[Rocket.Chat asker: <attestation>]`` line, or ``""``."""
    return f"{ASKER_LINE_PREFIX}{token}]" if token else ""
