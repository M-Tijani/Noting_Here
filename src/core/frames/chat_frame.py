from dataclasses import dataclass, field

from DBDofusUnity.datas.protos.non_obf.game.chat_pb2 import (
    Channel,
    ChatChannelMessageEvent,
    ChatPrivateMessageRequest,
)

from src.core.frames.frame import Frame
from src.services.discord_notifier import DiscordNotifier


@dataclass
class ChatFrame(Frame):
    discord_notifier: DiscordNotifier
    _auto_replied_senders: set[str] = field(default_factory=lambda: set(), init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.event_manager.on(
            ChatChannelMessageEvent,
            self.on_channel_message,
            originator=self,
            priority=self.priority,
        )

    def on_channel_message(self, msg: ChatChannelMessageEvent) -> None:
        self.logger.info(
            "ChatFrame received channel=%s sender=%s sender_id=%s content=%r",
            msg.channel,
            msg.sender_name,
            msg.sender_character_id,
            msg.content,
        )
        if msg.channel != Channel.PRIVATE:
            return

        sender = msg.sender_name or "Unknown"
        content = (msg.content or "").strip()
        player_character_id = self.game_state.player.character_id
        player_character_name = self.game_state.player.character_name
        player_login = self.game_state.player.login
        account_name = player_character_name or player_login

        if self.discord_notifier.is_configured():
            self.logger.info(
                "Private message from %s to %s; sending Discord alert.",
                sender,
                account_name,
            )
            self.discord_notifier.send_private_message_alert(
                account_name=account_name,
                sender=sender,
                content=content,
            )
        else:
            self.logger.debug(
                "Private message received but Discord notifier is not configured."
            )

        is_self = self._is_own_message(
            msg.sender_character_id,
            sender,
            player_character_id,
            player_character_name,
            player_login,
        )
        if is_self:
            self.logger.info(
                "Ignoring own echoed PM (sender=%s sender_id=%s player_id=%s).",
                sender,
                msg.sender_character_id,
                player_character_id,
            )
            return

        if self._has_auto_replied(sender):
            self.logger.info(
                "Already auto-replied to %s once; skipping.", sender
            )
            return

        self.logger.info(
            "Auto-replying to %s (id=%s) with 'yo' and 'cv'.",
            sender,
            msg.sender_character_id,
        )
        self._mark_auto_replied(sender)
        self._send_private_message("yo", sender)
        self.run_timer((0.5, 1.0), lambda: self._send_private_message("cv", sender))

    def _is_own_message(
        self,
        sender_character_id: int,
        sender_name: str,
        player_character_id: int,
        player_character_name: str,
        player_login: str,
    ) -> bool:
        if sender_character_id != 0 and sender_character_id == player_character_id:
            return True
        if player_character_name and sender_name == player_character_name:
            return True
        if player_login and sender_name == player_login:
            return True
        return False

    def _has_auto_replied(self, sender: str) -> bool:
        return sender in self._auto_replied_senders

    def _mark_auto_replied(self, sender: str) -> None:
        self._auto_replied_senders.add(sender)

    def _send_private_message(self, content: str, recipient_name: str) -> None:
        request = ChatPrivateMessageRequest(
            content=content,
            name=recipient_name,
        )
        self.logger.debug(
            "Sending ChatPrivateMessageRequest to=%r content=%r",
            recipient_name,
            content,
        )
        if self.event_manager.on_send_game_callback is None:
            self.logger.error(
                "Cannot send private message: on_send_game_callback is not set. "
                "Is the game proxy/client connected?"
            )
            return
        self.event_manager.send(request)

