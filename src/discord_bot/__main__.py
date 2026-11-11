"""Entry point to start or test the Discord notification bot."""

import argparse
import asyncio
import email.message
import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import discord

from src.discord_bot.bot import NotificationBot
from src.discord_bot.config import DiscordBotConfig, load_discord_bot_config

DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/M-Tijani/notification-bot, 1.0)"


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _send_test_notification(config: DiscordBotConfig, message: str) -> None:
    """Post a one-off test message to the configured notification channel."""
    url = f"{DISCORD_API_BASE}/channels/{config.notification_channel_id}/messages"
    headers = {
        "Authorization": f"Bot {config.bot_token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload: dict[str, Any] = {
        "embeds": [
            {
                "title": "Test notification",
                "description": message,
                "color": 0x2ECC71,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "fields": [
                    {"name": "Channel", "value": f"<#{config.notification_channel_id}>", "inline": True},
                    {"name": "Admin role", "value": config.admin_role_name, "inline": True},
                ],
            }
        ]
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        status_code = response.getcode()
        if status_code >= 400:
            raise urllib.error.HTTPError(url, status_code, "Discord API error", email.message.Message(), None)


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    if exc.fp is None:
        return ""
    try:
        return exc.read().decode("utf-8", errors="replace")
    finally:
        exc.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Discord notification bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a test notification to the configured channel and exit",
    )
    parser.add_argument(
        "--message",
        default="The Discord notification bot is configured correctly.",
        help="Custom message for --test",
    )
    args = parser.parse_args()

    _configure_logging()
    logger = logging.getLogger(__name__)
    try:
        config = load_discord_bot_config()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    if args.test:
        try:
            _send_test_notification(config, args.message)
        except urllib.error.HTTPError as exc:
            body = _read_http_error_body(exc)
            logger.error("Discord API returned HTTP %s: %s", exc.code, body)
            return 1
        except urllib.error.URLError as exc:
            logger.error("Failed to reach Discord: %s", exc)
            return 1
        logger.info("Test notification sent to channel %s.", config.notification_channel_id)
        return 0

    logger.info(
        "Starting notification bot (application_id=%s, channel_id=%s, admin_role=%s)",
        config.application_id,
        config.notification_channel_id,
        config.admin_role_name,
    )
    bot = NotificationBot(config)
    try:
        async with bot:
            await bot.start(config.bot_token)
    except discord.HTTPException as exc:
        if exc.status == 401:
            logger.error(
                "Discord login failed (401 Unauthorized). Check that DISCORD_BOT_TOKEN "
                "is the bot token (not the public key or application ID) and that it is still valid."
            )
        else:
            logger.error("Discord HTTP error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
