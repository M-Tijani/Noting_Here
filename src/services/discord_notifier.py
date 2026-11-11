import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.services.logging_utils.contextual_logger import ContextualLogger

DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/M-Tijani/notification-bot, 1.0)"
MAX_EMBED_DESCRIPTION_LENGTH = 4096
MAX_EMBED_FIELD_VALUE_LENGTH = 1024


@dataclass
class DiscordNotifier(ContextualLogger):
    """Send alerts to a Discord channel using a bot token."""

    bot_token: str | None
    channel_id: int | None

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.channel_id)

    def _read_http_error_body(self, exc: urllib.error.HTTPError) -> str:
        if exc.fp is None:
            return ""
        try:
            return exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()

    def send_private_message_alert(self, account_name: str, sender: str, content: str) -> None:
        if not self.is_configured():
            self.logger.debug("Discord notifier is not configured; skipping private message alert.")
            return
        thread = threading.Thread(
            target=self._send_private_message_alert,
            args=(account_name, sender, content),
            name="discord-pm-alert",
            daemon=True,
        )
        thread.start()

    def send_low_kamas_alert(self, account_name: str, kamas: int, threshold: int) -> None:
        if not self.is_configured():
            self.logger.debug("Discord notifier is not configured; skipping low kamas alert.")
            return
        thread = threading.Thread(
            target=self._send_low_kamas_alert,
            args=(account_name, kamas, threshold),
            name="discord-low-kamas-alert",
            daemon=True,
        )
        thread.start()

    def _send_private_message_alert(self, account_name: str, sender: str, content: str) -> None:
        safe_content = content[:MAX_EMBED_FIELD_VALUE_LENGTH] if content else "*empty message*"
        description = f"**{sender}** sent a private message to **{account_name}**."
        if len(description) > MAX_EMBED_DESCRIPTION_LENGTH:
            description = description[:MAX_EMBED_DESCRIPTION_LENGTH]
        self._post_embed(
            {
                "title": "Private message received",
                "description": description,
                "color": 0xE74C3C,
                "fields": [
                    {"name": "Sender", "value": sender or "*unknown*", "inline": True},
                    {"name": "Account", "value": account_name or "*unknown*", "inline": True},
                    {"name": "Message", "value": safe_content, "inline": False},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _send_low_kamas_alert(self, account_name: str, kamas: int, threshold: int) -> None:
        self._post_embed(
            {
                "title": "Low kamas alert",
                "description": (
                    f"**{account_name}** is low on kamas and may stop farming. "
                    "Recharge the account before the Lua script fails."
                ),
                "color": 0xE67E22,
                "fields": [
                    {"name": "Account", "value": account_name or "*unknown*", "inline": True},
                    {"name": "Kamas", "value": f"{kamas:,}", "inline": True},
                    {"name": "Threshold", "value": f"{threshold:,}", "inline": True},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _post_embed(self, embed: dict[str, Any]) -> None:
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload: dict[str, Any] = {"embeds": [embed]}
        try:
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                status_code = response.getcode()
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            response_body = self._read_http_error_body(exc)
        except urllib.error.URLError:
            self.logger.exception("Discord alert request failed.")
            return

        if status_code in {429, 502, 503, 504}:
            self.logger.warning(
                "Discord alert rate-limited or unavailable (HTTP %s); message dropped.",
                status_code,
            )
        elif status_code >= 400:
            self.logger.warning(
                "Discord alert failed (HTTP %s): %s",
                status_code,
                response_body[:200],
            )
