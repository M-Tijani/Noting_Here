"""Configuration for the Discord notification bot."""

import os
from typing import Self

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from src.utils.project_paths import ENV_PATH

load_dotenv(ENV_PATH)


class DiscordBotConfig(BaseModel):
    """Settings required to run the Discord notification bot.

    Values are read from environment variables. Strings are stripped and empty
    values are treated as missing so that the Pydantic validation produces a
    clear error when a required secret is not provided.
    """

    bot_token: str = Field(description="Discord bot token")
    application_id: int = Field(description="Discord application (client) ID")
    public_key: str = Field(description="Discord application public key")
    notification_channel_id: int = Field(description="Channel ID where notifications are posted")
    admin_role_name: str = Field(default="power", description="Role name granted admin access")
    command_guild_id: int | None = Field(
        default=None, description="Optional guild ID to sync slash commands to instantly"
    )

    @field_validator("bot_token", "public_key", "admin_role_name", mode="before")
    @classmethod
    def _strip_strings(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return value

    @field_validator("application_id", "notification_channel_id", "command_guild_id", mode="before")
    @classmethod
    def _coerce_int(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, int):
            return value
        return int(str(value))

    @model_validator(mode="after")
    def _ensure_required_present(self) -> Self:
        required: dict[str, object] = {
            "bot_token": self.bot_token,
            "application_id": self.application_id,
            "public_key": self.public_key,
            "notification_channel_id": self.notification_channel_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required settings: {', '.join(missing)}")
        return self


def load_discord_bot_config() -> DiscordBotConfig:
    """Load and validate Discord bot configuration from the environment.

    Raises:
        ValueError: When a required setting is missing or invalid, with a
            message that lists every failure.
    """
    try:
        return DiscordBotConfig(
            bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
            application_id=_parse_int(os.getenv("DISCORD_APPLICATION_ID")),
            public_key=os.getenv("DISCORD_PUBLIC_KEY", ""),
            notification_channel_id=_parse_int(os.getenv("DISCORD_NOTIFICATION_CHANNEL_ID")),
            admin_role_name=os.getenv("DISCORD_ADMIN_ROLE_NAME", "power"),
            command_guild_id=_parse_optional_int(os.getenv("DISCORD_COMMAND_GUILD_ID")),
        )
    except ValidationError as exc:
        missing = [f"  - {error['loc'][0]}" for error in exc.errors() if error.get("loc")]
        raise ValueError(
            f"Discord bot configuration is incomplete. Add the missing values to {ENV_PATH}:\n"
            + "\n".join(missing)
        ) from exc


def _parse_int(value: str | None) -> int:
    """Return an int from a string, or 0 when the value is missing or blank.

    The 0 sentinel is rejected by the configuration validator for required IDs.
    """
    if value is None or value.strip() == "":
        return 0
    return int(value)


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)
