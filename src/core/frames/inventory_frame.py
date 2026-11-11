from dataclasses import dataclass, field

from DBDofusUnity.datas.protos.non_obf.game.inventory_pb2 import (
    InventoryContentEvent,
    InventoryWeightEvent,
    KamasUpdateEvent,
    ObjectAddedEvent,
    ObjectDeletedEvent,
    ObjectModifiedEvent,
    ObjectMovementEvent,
    ObjectQuantityEvent,
    ObjectsAddedEvent,
    ObjectsDeletedEvent,
    ObjectsQuantityEvent,
)
from src.core import config
from src.core.frames.frame import Frame
from src.services.discord_notifier import DiscordNotifier


@dataclass
class InventoryFrame(Frame):
    discord_notifier: DiscordNotifier | None = None
    _low_kamas_alerted: bool = field(init=False, default=False)

    def __post_init__(self):
        self.event_manager.on(
            InventoryContentEvent,
            self.on_inventory_content_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectAddedEvent,
            self.on_object_added_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectsAddedEvent,
            self.on_objects_added_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectDeletedEvent,
            self.on_object_deleted_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectsDeletedEvent,
            self.on_objects_deleted_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectQuantityEvent,
            self.on_object_quantity_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectsQuantityEvent,
            self.on_objects_quantity_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectModifiedEvent,
            self.on_object_modified_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            KamasUpdateEvent,
            self.on_kamas_update_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            InventoryWeightEvent,
            self.on_inventory_weight_event,
            originator=self,
            priority=self.priority,
        )
        self.event_manager.on(
            ObjectMovementEvent,
            self.on_object_movement_event,
            originator=self,
            priority=self.priority,
        )

    def on_object_quantity_event(self, message: ObjectQuantityEvent):
        object_item = self.game_state.inventory.objects_by_uid.get(message.object.object_uid)
        if object_item is None:
            return
        object_item.item.quantity = message.object.quantity
        if config.DEBUG:
            self.inventory_signals.updated_object_item.emit(object_item)

    def on_inventory_content_event(self, msg: InventoryContentEvent):
        self.game_state.inventory.set_objects(list(msg.objects))
        self._set_kamas(msg.kamas)

    def on_object_added_event(self, msg: ObjectAddedEvent):
        self.game_state.inventory.add_object(msg.object)

    def on_objects_added_event(self, msg: ObjectsAddedEvent):
        self.game_state.inventory.add_objects(list(msg.objects))

    def on_object_deleted_event(self, msg: ObjectDeletedEvent):
        self.game_state.inventory.remove_object(msg.object_uid)

    def on_objects_deleted_event(self, msg: ObjectsDeletedEvent):
        for uid in msg.objects_uid:
            self.game_state.inventory.remove_object(uid)

    def on_kamas_update_event(self, msg: KamasUpdateEvent):
        self._set_kamas(msg.quantity)

    def _set_kamas(self, kamas: int) -> None:
        self.game_state.inventory.kamas = kamas
        self._maybe_alert_low_kamas(kamas)

    def _maybe_alert_low_kamas(self, kamas: int) -> None:
        threshold = config.LOW_KAMAS_ALERT_THRESHOLD
        recovery_threshold = max(config.LOW_KAMAS_RECOVERY_THRESHOLD, threshold)
        if kamas >= recovery_threshold:
            self._low_kamas_alerted = False
        if kamas >= threshold:
            return
        if self.discord_notifier is None or self._low_kamas_alerted:
            return

        self._low_kamas_alerted = True
        account_name = (
            self.game_state.player.character_name or self.game_state.player.login
        )
        self.logger.warning(
            "Low kamas on %s: %s (threshold %s, recovery %s); sending Discord alert.",
            account_name,
            kamas,
            threshold,
            recovery_threshold,
        )
        self.discord_notifier.send_low_kamas_alert(
            account_name=account_name,
            kamas=kamas,
            threshold=threshold,
        )

    def on_inventory_weight_event(self, message: InventoryWeightEvent):
        self.game_state.inventory.inventory_weight = message.inventory_weight
        self.game_state.inventory.weight_max = message.weight_max

    def on_objects_quantity_event(self, msg: ObjectsQuantityEvent):
        for object_with_quantity in msg.object:
            object_item = self.game_state.inventory.objects_by_uid.get(
                object_with_quantity.object_uid
            )
            if object_item is None:
                continue
            object_item.item.quantity = object_with_quantity.quantity
            if config.DEBUG:
                self.inventory_signals.updated_object_item.emit(object_item)

    def on_object_modified_event(self, msg: ObjectModifiedEvent):
        self.game_state.inventory.objects_by_uid[msg.object.item.uid] = msg.object
        if config.DEBUG:
            self.inventory_signals.updated_object_item.emit(msg.object)

    def on_object_movement_event(self, msg: ObjectMovementEvent):
        object_item = self.game_state.inventory.objects_by_uid.get(msg.object_uid)
        if object_item is None:
            return
        object_item.position = msg.position
