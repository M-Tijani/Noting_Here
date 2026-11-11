from qfluentwidgets import BodyLabel

from src.controller.settings import SettingsService
from src.core.config import GlobalSettings
from src.gui.pages.settings.settings_panel import SettingsPanel


class ServicesSettingsPanel(SettingsPanel):
    def __init__(self) -> None:
        super().__init__(
            "The Sonji key creates new SmailPro mailboxes. "
            "Previously saved mailboxes remain usable without this key.\n\n"
            "The Discord bot token and channel ID are used to send alerts "
            "when a player sends a private message to a running account."
        )
        self.summary = BodyLabel("", self)
        self.overview_layout.addWidget(self.summary)
        self.overview_layout.addSpacing(16)
        self.key = self.line("Sonji key")
        self.discord_token = self.line("Discord bot token")
        self.discord_channel = self.line("Discord channel ID")

    def reload(self) -> None:
        def render(settings: GlobalSettings) -> None:
            self.summary.setText(
                "Sonji: " + ("Configured" if SettingsService().sonji_api_key() else "Not configured")
                + (" (variable d’environnement)" if settings.sonji_api_key is None else "")
                + "\nDiscord: "
                + ("Configured" if SettingsService().discord_bot_token() else "Not configured")
                + (" (environment variable)" if settings.discord_bot_token is None else "")
            )
            self.key.setText(settings.sonji_api_key or "")
            self.key.setEnabled(settings.sonji_api_key is not None)
            self.discord_token.setText(settings.discord_bot_token or "")
            self.discord_channel.setText(str(settings.discord_channel_id or ""))

        self.perform(SettingsService().get, render)

    def save(self) -> None:
        key = self.key.text().strip()
        token = self.discord_token.text().strip() or None
        channel_text = self.discord_channel.text().strip()
        channel_id = int(channel_text) if channel_text else None
        self.perform(
            lambda: SettingsService().update_sonji_key(key),
            lambda _: self._save_discord(token, channel_id),
        )

    def _save_discord(self, token: str | None, channel_id: int | None) -> None:
        self.perform(
            lambda: SettingsService().update_discord_settings(token, channel_id),
            lambda _: self.reload(),
        )
