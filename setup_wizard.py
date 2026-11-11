"""``hermes gateway setup`` for Rocket.Chat.

Uses Hermes's documented wizard helpers (``hermes_cli.gateway_setup_wizard``, ``hermes_cli.setup``).
The credentials are checked against the server before they are saved, and the bot's username comes
from the server, so a typo shows up here rather than as a silent bot later.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .rest_client import RocketChatRestClient

REQUIRED_ENV = ("ROCKETCHAT_TEAM_URL", "ROCKETCHAT_TEAM_TOKEN", "ROCKETCHAT_TEAM_USER_ID", "ROCKETCHAT_TEAM_BOT_USERNAME")


async def fetch_identity(url: str, token: str, user_id: str) -> Optional[Dict[str, Any]]:
    """The account behind the credentials (``GET me``), or ``None`` if the server refuses them."""
    client = RocketChatRestClient(url, token, user_id)
    await client.open()
    try:
        me = await client.me()
    finally:
        await client.close()
    return me if me.get("username") else None


def interactive_setup() -> None:
    """Guide the user through connecting a Rocket.Chat bot account."""
    from hermes_cli.gateway_setup_wizard import (
        confirm_reconfigure,
        print_setup_header,
        prompt_csv,
        save_env_values,
    )
    from hermes_cli.cli_output import print_info, print_success, print_warning
    from hermes_cli.setup import prompt, prompt_yes_no

    print_setup_header("Rocket.Chat")
    if not confirm_reconfigure("Rocket.Chat", *REQUIRED_ENV):
        return
    print_info("Create a bot account first:")
    print_info("  1. Administration > Users > New: roles user and bot, no admin rights.")
    print_info("  2. Log in as the bot: Profile > Personal Access Tokens.")
    print_info("     Tick 'Ignore Two Factor Authentication', then copy the token and the user ID.")
    print_info("  3. Invite the bot to its rooms: /invite @<bot>")
    print()

    url = (prompt("Rocket.Chat server URL (e.g. https://chat.example.com)") or "").strip().rstrip("/")
    user_id = (prompt("Bot user ID") or "").strip()
    token = (prompt("Bot Personal Access Token", password=True) or "").strip()
    if not (url and user_id and token):
        print_warning("Setup cancelled: the URL, user ID and token are all required.")
        return

    identity = asyncio.run(fetch_identity(url, token, user_id))
    if identity is None:
        print_warning("The server refused these credentials, or it is not reachable.")
        if not prompt_yes_no("Save them anyway?", False):
            return
        username = (prompt("Bot username") or "").strip()
        if not username:
            return
    else:
        username = identity["username"]
        print_success(f"Authenticated as @{username}")
        if "bot" not in (identity.get("roles") or []):
            print_warning("This account has no bot role, so replies may hit the REST rate limit.")
    save_env_values(
        ROCKETCHAT_TEAM_URL=url,
        ROCKETCHAT_TEAM_USER_ID=user_id,
        ROCKETCHAT_TEAM_TOKEN=token,
        ROCKETCHAT_TEAM_BOT_USERNAME=username,
    )

    print()
    print_info("Who may talk to the bot? Enter Rocket.Chat user IDs, not usernames.")
    print_info("Without an allowlist, Hermes answers nobody unless you use pairing.")
    allowed = prompt_csv("Allowed user IDs (comma-separated, empty to skip)", "")
    if allowed:
        save_env_values(ROCKETCHAT_TEAM_ALLOWED_USERS=allowed)

    print()
    print_info("Home channel: the room ID for cron results (empty to skip).")
    home = (prompt("Home channel room ID") or "").strip()
    if home:
        save_env_values(ROCKETCHAT_TEAM_HOME_CHANNEL=home)

    print()
    print_warning("Before letting real users in, give this platform an explicit tool list:")
    print_info("  hermes config set platform_toolsets.rocketchat_team '[my-mcp-server]'")
    print_success("Rocket.Chat configured. Run: hermes gateway restart")
