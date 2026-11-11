"""Slash commands for the Discord notification bot."""

import discord
from discord import app_commands
from discord.ext import commands

from src.discord_bot.bot import NotificationBot


def _authorize(interaction: discord.Interaction) -> bool:
    """Require the caller to be an admin or have the configured power role."""
    bot = interaction.client
    config = getattr(bot, "config", None)
    if config is None:
        return False
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.name == config.admin_role_name for role in interaction.user.roles)


class NotificationCog(commands.Cog):
    """Slash commands restricted to the configured admin role or administrators."""

    def __init__(self, bot: NotificationBot) -> None:
        self.bot = bot

    @app_commands.check(_authorize)
    @app_commands.command(name="ping", description="Check whether the bot is responsive")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! ({latency_ms} ms)", ephemeral=True)

    @app_commands.check(_authorize)
    @app_commands.command(name="status", description="Show the bot status and configuration")
    async def status(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Notification Bot Status",
            color=discord.Color.green() if self.bot.is_ready() else discord.Color.red(),
        )
        embed.add_field(name="Ready", value=str(self.bot.is_ready()), inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)} ms", inline=True)
        embed.add_field(
            name="Notification channel",
            value=f"<#{self.bot.config.notification_channel_id}>",
            inline=False,
        )
        embed.add_field(
            name="Admin role",
            value=self.bot.config.admin_role_name,
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.check(_authorize)
    @app_commands.command(name="notify", description="Send a notification to the configured channel")
    @app_commands.describe(message="The message to broadcast")
    async def notify(self, interaction: discord.Interaction, message: str) -> None:
        channel = self.bot.get_channel(self.bot.config.notification_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The configured notification channel could not be found.",
                ephemeral=True,
            )
            return

        await channel.send(message)
        await interaction.response.send_message(
            f"Notification sent to <#{self.bot.config.notification_channel_id}>.",
            ephemeral=True,
        )


async def setup(bot: NotificationBot) -> None:
    await bot.add_cog(NotificationCog(bot))
