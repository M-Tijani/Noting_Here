from dataclasses import dataclass
from typing import Any

from ankama_launcher_emulator.proxy.proxy import (
    Proxy,
    WorkerAction,
)
from google.protobuf.message import Message
from PyQt6.QtCore import QMetaObject, Qt

from DBDofusUnity.datas.protos.non_obf.game.chat_pb2 import ChatChannelMessageRequest
from DBDofusUnity.datas.protos.non_obf.game.game_message_pb2 import Request
from DBDofusUnity.datas.protos.non_obf.game.gamemap_pb2 import MapChangeRequest
from src.consts import MESSAGES_WITH_UID
from src.core import config
from src.core.bot.bot import Bot
from src.protocol.protocol import decode_varint_size, encode_msg
from src.protocol.protocol_game import (
    get_game_msg,
    get_game_msg_info,
    get_obf_game_message_from_msg,
)
from src.services.debug_recorder.recorder import MessageSource
from src.services.play_recorder import (
    extract_client_action,
    extract_fight_id,
    extract_fight_lifecycle,
    extract_fight_outcome,
    serialize_attack_context,
)


@dataclass
class GameProxy(Proxy):
    bot: Bot

    def __post_init__(self):
        super().__post_init__()
        self.bot.event_manager.on_send_game_callback = self.send_msg
        self.bot.event_manager.request_disconnect_callback = self.close
        self.uid: int = 1

    def on_close(self) -> None:
        self.bot.cancel_frame_timers()
        self.bot.event_manager.on_send_game_callback = None
        if self.bot.event_manager.request_disconnect_callback == self.close:
            self.bot.event_manager.request_disconnect_callback = None

        QMetaObject.invokeMethod(
            self.bot.game_info_signals,
            "disconnected",
            Qt.ConnectionType.QueuedConnection,
        )

    def alter_msg_datas(self, msg_content_datas: bytes, msg_datas: bytes, from_server: bool) -> bytes | None:
        expected_uid = self.uid + 1
        root_msg_namespace, clear_sub_msg, _, uid = get_game_msg(
            msg_content_datas, False, from_server=from_server
        )
        if clear_sub_msg is None:
            return msg_datas

        clear_sub_altered_msg, was_altered = self.bot.event_manager.alter_msg(clear_sub_msg)
        if clear_sub_altered_msg is None:
            return None

        # Server->client packets are never re-encoded here: a modifier can only hide
        # them (the framework still consumes the original packet), not rewrite them.
        if from_server:
            return msg_datas

        if not was_altered and (uid is None or uid == -1 or uid == expected_uid):
            return msg_datas

        obf_info = get_obf_game_message_from_msg(root_msg_namespace, clear_sub_altered_msg, expected_uid)
        if obf_info is not None:
            game_msg, _ = obf_info
            return encode_msg(game_msg)

    def on_sent_msg_datas(self, msg_datas: bytes, was_send_from_proxy: bool, from_server: bool) -> None:
        size, pos = decode_varint_size(msg_datas)
        msg_content_datas = msg_datas[pos : pos + size]

        _, clear_sub_msg, obf_sub_msg, uid = get_game_msg(
            msg_content_datas, config.DEBUG and not was_send_from_proxy, from_server=from_server
        )
        if uid is not None and uid != -1:
            self.uid = uid

        source: MessageSource
        if from_server:
            source = "server"
        elif was_send_from_proxy:
            source = "framework_injected"
        else:
            source = "client_forwarded"

        self._record_play_trace(clear_sub_msg, obf_sub_msg, uid, from_server, source)

        recorder = self.bot.debug_recorder
        if recorder is not None:
            recorder.record_game_message(clear_sub_msg, obf_sub_msg, uid, from_server, source)

        if config.DEBUG and self.bot.msg_info_signals.capture_enabled:
            msg_infos = get_game_msg_info(
                clear_sub_msg,
                obf_sub_msg,
                uid,
                from_server,
            )
            self.bot.msg_info_signals.msg_info.emit(msg_infos, was_send_from_proxy)

        if clear_sub_msg is not None:
            self._log_mitm_message(clear_sub_msg, from_server, was_send_from_proxy)
            self.bot.event_manager.process_msg(clear_sub_msg)

    def _log_mitm_message(
        self,
        clear_sub_msg: Message,
        from_server: bool,
        was_send_from_proxy: bool,
    ) -> None:
        if from_server:
            return
        msg_name = clear_sub_msg.DESCRIPTOR.full_name
        if was_send_from_proxy:
            self.bot.logger.debug("MITM framework -> server: %s", msg_name)
        else:
            self.bot.logger.debug("MITM client -> server: %s", msg_name)
        if isinstance(clear_sub_msg, MapChangeRequest):
            self.bot.logger.debug(
                "MITM MapChangeRequest map_id=%s auto_pilot=%s (framework=%s)",
                clear_sub_msg.map_id,
                clear_sub_msg.auto_pilot,
                was_send_from_proxy,
            )
        elif isinstance(clear_sub_msg, ChatChannelMessageRequest):
            self.bot.logger.debug(
                "MITM ChatChannelMessageRequest channel=%s content=%r (framework=%s)",
                clear_sub_msg.channel,
                clear_sub_msg.content,
                was_send_from_proxy,
            )

    def _record_play_trace(
        self,
        clear_sub_msg: Message | None,
        obf_sub_msg: Message,
        uid: int | None,
        from_server: bool,
        source: MessageSource,
    ) -> None:
        recorder = self.bot.play_recorder
        if recorder is None:
            return
        recorder.record_message(clear_sub_msg, obf_sub_msg, uid, from_server, source)
        if clear_sub_msg is None:
            return

        in_fight = self.bot.game_state.fight.in_fight
        if from_server:
            outcome = extract_fight_outcome(
                clear_sub_msg,
                character_id=self.bot.game_state.player.character_id,
                turns=self.bot.game_state.fight.fight_turn,
            )
            if outcome is not None:
                recorder.record_outcome(outcome)
            lifecycle = extract_fight_lifecycle(clear_sub_msg, in_fight=in_fight)
            if lifecycle is not None:
                recorder.record_event(
                    lifecycle,
                    self._play_observation(),
                    self.bot.game_state.debug_snapshot(),
                    fight_id=extract_fight_id(clear_sub_msg),
                )
            return

        action = extract_client_action(clear_sub_msg)
        if action is None:
            return
        recorder.record_action(
            source,
            action,
            self._play_observation() if in_fight else None,
            context="fight" if in_fight else "world",
        )

    def _play_observation(self) -> dict[str, Any] | None:
        try:
            return serialize_attack_context(self.bot.game_state.get_attack_context_if_available())
        except Exception:
            self.bot.logger.exception("Could not build play recording observation")
            return None

    def send_msg(self, clear_sub_msg: Message) -> None:
        if clear_sub_msg.__class__ in MESSAGES_WITH_UID:
            uid = self.uid + 1
        else:
            uid = -1

        obf_info = get_obf_game_message_from_msg(Request.DESCRIPTOR.full_name, clear_sub_msg, uid)
        if obf_info is not None:
            obf_game_msg, _ = obf_info
            self.queue_worker_item.put((WorkerAction.SEND_SERVER, encode_msg(obf_game_msg), True, False))
