"""Discord notification bot client."""

import logging

import discord
from discord.ext import commands

from src.discord_bot.config import DiscordBotConfig

logger = logging.getLogger(__name__)


class NotificationBot(commands.Bot):
    """A lightweight Discord bot that posts notifications to a configured channel.

    The bot exposes slash commands restricted to members with the configured
    admin role or the ``Administrator`` permission. It also auto-replies to
    configured trigger words with a sequence of messages.
    """

    def __init__(self, config: DiscordBotConfig) -> None:
        self.config = config
        intents = discord.Intents.default()
        # Message content is required to read trigger words for auto-replies.
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=config.application_id,
        )

    async def setup_hook(self) -> None:
        # Load the notification command cog.
        await self.load_extension("src.discord_bot.commands")

        # Sync commands to a single guild when provided, otherwise use global sync.
        guild_id = self.config.command_guild_id
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        if self.user is None:
            return
        logger.info("Notification bot ready: %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        await self.process_commands(message)
        if message.author.bot:
            return
        if message.content.strip().lower() != "yo":
            return
        # Send the two-part auto-reply. Exceptions are logged by discord.py.
        await message.channel.send("yo")
        await message.channel.send("cv")
