from dataclasses import dataclass, field
from enum import StrEnum, auto
from functools import partial

from DBDofusUnity.datas.protos.non_obf.game.basic_pb2 import (
    TextInformationEvent,
)
from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import (
    ObjectItemInventory,
)
from DBDofusUnity.datas.protos.non_obf.game.exchange_pb2 import (
    ExchangeObjectMoveRequest,
)
from DBDofusUnity.datas.protos.non_obf.game.guild_chest_pb2 import (
    GuildChestCurrentListenersAddEvent,
    GuildChestTabSelectRequest,
)
from DBDofusUnity.datas.protos.non_obf.game.inventory_pb2 import (
    InventoryWeightEvent,
    StorageInventoryContentEvent,
)
from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.game_constants.effect_action import EffectActionEnum
from DBDofusUnity.dofus_unity_reader.game_constants.guild import UNBOUNDED_CHEST_TAB_NUMBER
from DBDofusUnity.dofus_unity_reader.game_constants.inventory_position import (
    CharacterInventoryPositionEnum,
)
from DBDofusUnity.dofus_unity_reader.game_constants.item import ItemTypeEnum
from src.core.behaviors.interactives.interactive_behavior import InteractiveBehavior
from src.core.behaviors.movements.auto_trip.auto_trip_smart_behavior import (
    AutoTripSmartBehavior,
)
from src.core.behaviors.recovery_behavior import RecoverableBehavior
from src.core.behaviors.storage.enter_chests.enter_guild_chest_behavior import (
    EnterGuildChestBehavior,
)
from src.core.engine.items.item_formatter import format_item_name
from src.core.engine.movements.map.path_finding.path_finding import Pathfinding
from src.services.human_timings import HumanTimingsService

STORAGE_EVENT_TIMEOUT_SECONDS = 8.0


class UnloadInGuildChestError(StrEnum):
    NOTHING_TO_UNLOAD_WHILE_FULL = auto()


@dataclass
class UnloadInGuildChestBehavior(RecoverableBehavior):
    interactive_behavior: InteractiveBehavior
    path_finding: Pathfinding
    auto_trip_world_behavior: AutoTripSmartBehavior
    enter_guild_chest_behavior: EnterGuildChestBehavior

    object_to_unload_on_tab: list[tuple[int, list[ObjectItemInventory]]] = field(
        init=False, default_factory=list[tuple[int, list[ObjectItemInventory]]]
    )
    _tab_switch_generation: int = field(init=False, default=0)
    _deposit_generation: int = field(init=False, default=0)

    def run(self, unload_item_id_by_tab: dict[int, set[int]]) -> None:
        self.init_recovery_listeners()
        self.ensure_free_to_act(
            lambda: self.start_guild_chest_unload(unload_item_id_by_tab=unload_item_id_by_tab)
        )

    def start_guild_chest_unload(self, unload_item_id_by_tab: dict[int, set[int]]) -> None:
        bag_position = CharacterInventoryPositionEnum.InventoryPositionNotEquiped.value
        object_by_gid_in_inventory: dict[int, ObjectItemInventory] = {}
        for object in self.game_state.inventory.objects_by_uid.values():
            if object.position != bag_position:
                continue
            if any(effect.action == EffectActionEnum.LINKED_TO_CHARACTER for effect in object.item.effects):
                continue
            item = DataReader().item_by_id.get(object.item.gid)
            if item is None or item.realWeight == 0 or item.typeId == ItemTypeEnum.REKLOOT:
                continue
            object_by_gid_in_inventory[object.item.gid] = object
        self.logger.info(f"Inventory contains {len(object_by_gid_in_inventory)} unique bag items")

        self.object_to_unload_on_tab = self._group_inventory_items_by_tab(
            object_by_gid_in_inventory, unload_item_id_by_tab
        )

        total_items = sum(len(objects) for _, objects in self.object_to_unload_on_tab)
        tabs_summary = ", ".join(
            [f"Tab {tab}: {len(objects)} items" for tab, objects in self.object_to_unload_on_tab]
        )
        self.logger.info(
            f"Will unload {total_items} items across {len(self.object_to_unload_on_tab)} tabs ({tabs_summary})"
        )

        if len(self.object_to_unload_on_tab) == 0:
            if self.game_state.inventory.is_full_pods:
                self.logger.warning(
                    "No guild chest items to unload while pods are full; bank fallback may be needed"
                )
                return self.finish(UnloadInGuildChestError.NOTHING_TO_UNLOAD_WHILE_FULL)
            return self.finish()

        self.enter_guild_chest_behavior.start(
            callback=self.on_enter_guild_chest_behavior_finished, parent=self
        )

    def _group_inventory_items_by_tab(
        self,
        object_by_gid_in_inventory: dict[int, ObjectItemInventory],
        unload_item_id_by_tab: dict[int, set[int]],
    ) -> list[tuple[int, list[ObjectItemInventory]]]:
        """Group every bankable inventory item by its target guild-chest tab.

        Known items use the mapping provided by ``unload_item_id_by_tab``.
        Items already stored in the guild chest go back to their existing tab.
        Everything else lands in the first available tab that is not reserved
        for a known category (defaults to tab 4 when it exists).
        """
        from src.core.states.guild_chest_state import TAB_BY_GID

        object_to_unload_on_tab: list[tuple[int, list[ObjectItemInventory]]] = []
        objects_by_tab: dict[int, list[ObjectItemInventory]] = {}
        gids_treated: set[int] = set()

        # 1. Items with an explicit tab mapping (resources, planks, drops, ...)
        for tab, gids in unload_item_id_by_tab.items():
            for gid in gids:
                object = object_by_gid_in_inventory.get(gid)
                if object is None or gid in gids_treated:
                    continue
                gids_treated.add(gid)
                objects_by_tab.setdefault(tab, []).append(object)

        # 2. Items already present in the guild chest go back to their current tab.
        storage = self.game_state.guild_chest.storage
        for gid, object in list(object_by_gid_in_inventory.items()):
            if gid in gids_treated:
                continue
            existing_tab = storage.get_tab_for_gid(gid)
            if existing_tab is not None:
                gids_treated.add(gid)
                objects_by_tab.setdefault(existing_tab, []).append(object)

        # 3. Everything else goes to a fallback tab (first available non-mapped tab).
        mapped_tabs = set(unload_item_id_by_tab) | set(TAB_BY_GID.values())
        available_tabs = sorted(self.game_state.guild_chest.tabs)
        fallback_tabs = [tab for tab in available_tabs if tab not in mapped_tabs]
        if not fallback_tabs:
            fallback_tabs = available_tabs
        fallback_index = 0

        for gid, object in object_by_gid_in_inventory.items():
            if gid in gids_treated:
                continue
            # Rotate through fallback tabs to avoid hitting the 100-slot limit on one tab.
            tab = fallback_tabs[fallback_index % len(fallback_tabs)]
            fallback_index += 1
            gids_treated.add(gid)
            objects_by_tab.setdefault(tab, []).append(object)

        for tab, objects in objects_by_tab.items():
            if objects:
                object_to_unload_on_tab.append((tab, objects))
        return object_to_unload_on_tab

    def on_enter_guild_chest_behavior_finished(self, error_code: str | None) -> None:
        if error_code is not None:
            return self.finish(error_code)

        available_tabs = set(self.game_state.guild_chest.tabs)
        accessible: list[tuple[int, list[ObjectItemInventory]]] = []
        orphaned: list[ObjectItemInventory] = []
        for tab, objects in self.object_to_unload_on_tab:
            if tab in available_tabs:
                accessible.append((tab, objects))
            else:
                orphaned.extend(objects)

        if orphaned:
            # Re-assign items from tabs that turned out to be inaccessible to the
            # available fallback tabs. This prevents non-resource items from being
            # left in inventory just because the pre-entry tab guess was stale.
            from src.core.states.guild_chest_state import TAB_BY_GID

            category_tabs = set(TAB_BY_GID.values())
            fallback_tabs = [tab for tab in sorted(available_tabs) if tab not in category_tabs]
            if not fallback_tabs:
                fallback_tabs = sorted(available_tabs)
            fallback_index = 0
            for obj in orphaned:
                tab = fallback_tabs[fallback_index % len(fallback_tabs)]
                fallback_index += 1
                # Append to existing accessible entry or create a new one.
                for existing_tab, existing_objects in accessible:
                    if existing_tab == tab:
                        existing_objects.append(obj)
                        break
                else:
                    accessible.append((tab, [obj]))
            self.logger.warning(
                f"Reassigned {len(orphaned)} items from inaccessible tabs to fallback tabs"
            )

        self.object_to_unload_on_tab = accessible
        self.logger.info(
            f"Starting unload process for {len(self.object_to_unload_on_tab)} accessible tabs"
        )
        self.run_timer(HumanTimingsService().get_timing_base_action(), self.unload_tab)

    def unload_tab(self) -> None:
        if len(self.object_to_unload_on_tab) == 0:
            return self.run_timer(HumanTimingsService().get_timing_base_action(), self.on_all_unloaded)
        tab, object_to_unloads = self.object_to_unload_on_tab.pop()
        if tab == self.game_state.guild_chest.tab_number:
            return self.unload_object(object_to_unloads)

        self._tab_switch_generation += 1
        generation = self._tab_switch_generation
        self.logger.info(f"Selecting guild chest tab {tab} for {len(object_to_unloads)} items")
        self.run_timer(
            HumanTimingsService().get_timing_short_action(),
            lambda: self.select_guild_chest_tab(tab, object_to_unloads, generation),
        )

    def select_guild_chest_tab(
        self,
        tab: int,
        object_to_unloads: list[ObjectItemInventory],
        generation: int,
    ) -> None:
        if generation != self._tab_switch_generation:
            return
        self.event_manager.on(
            [GuildChestCurrentListenersAddEvent, StorageInventoryContentEvent],
            partial(
                self.on_guild_chest_tab_ready,
                object_to_unloads=object_to_unloads,
                generation=generation,
            ),
            once=True,
            originator=self,
            override_on_self=True,
            timeout=STORAGE_EVENT_TIMEOUT_SECONDS,
            on_timeout=partial(
                self.on_guild_chest_tab_timeout,
                tab=tab,
                item_count=len(object_to_unloads),
                generation=generation,
            ),
        )
        self.event_manager.on(
            TextInformationEvent,
            partial(
                self.on_guild_chest_tab_error,
                tab=tab,
                item_count=len(object_to_unloads),
                generation=generation,
            ),
            once=True,
            originator=self,
            override_on_self=True,
        )
        self.event_manager.send(GuildChestTabSelectRequest(tab_number=tab))

    def on_guild_chest_tab_ready(
        self,
        _msg: GuildChestCurrentListenersAddEvent | StorageInventoryContentEvent,
        *,
        object_to_unloads: list[ObjectItemInventory],
        generation: int,
    ) -> None:
        if generation != self._tab_switch_generation:
            return
        self._tab_switch_generation += 1
        self._clear_guild_chest_tab_listeners()
        self.run_timer(
            HumanTimingsService().get_timing_short_action(), lambda: self.unload_object(object_to_unloads)
        )

    def on_guild_chest_tab_timeout(self, *, tab: int, item_count: int, generation: int) -> None:
        if generation != self._tab_switch_generation:
            return
        self._tab_switch_generation += 1
        self._clear_guild_chest_tab_listeners()
        self.logger.warning(
            f"Guild chest tab {tab} did not confirm selection; skipping {item_count} items "
            "(locked or unavailable tab)"
        )
        self.run_timer(HumanTimingsService().get_timing_base_action(), self.unload_tab)

    def on_guild_chest_tab_error(
        self,
        msg: TextInformationEvent,
        *,
        tab: int,
        item_count: int,
        generation: int,
    ) -> None:
        if msg.message_type is not TextInformationEvent.TextInformationType.TEXT_INFORMATION_ERROR:
            return
        if generation != self._tab_switch_generation:
            return
        self._tab_switch_generation += 1
        self._clear_guild_chest_tab_listeners()
        self.logger.warning(
            f"Guild chest tab {tab} was rejected (message id {msg.message_id}); "
            f"skipping {item_count} items"
        )
        self.run_timer(HumanTimingsService().get_timing_base_action(), self.unload_tab)

    def _clear_guild_chest_tab_listeners(self) -> None:
        self.event_manager.clear_listener_by_origin_and_type(
            GuildChestCurrentListenersAddEvent,
            self,
        )
        self.event_manager.clear_listener_by_origin_and_type(
            StorageInventoryContentEvent,
            self,
        )
        self.event_manager.clear_listener_by_origin_and_type(
            TextInformationEvent,
            self,
        )

    def unload_object(self, object_to_unloads: list[ObjectItemInventory]) -> None:
        if len(object_to_unloads) == 0:
            self.logger.info("Finished unloading current tab")
            return self.run_timer(HumanTimingsService().get_timing_base_action(), self.unload_tab)

        next_object = object_to_unloads.pop()
        item_name = format_item_name(next_object.item.gid)

        storage = self.game_state.guild_chest.storage
        tab_size = storage.get_tab_size(self.game_state.guild_chest.tab_number)
        item_in_chest = storage.get_item_by_gid(
            self.game_state.guild_chest.tab_number,
            next_object.item.gid,
        )

        if item_in_chest is None and tab_size == UNBOUNDED_CHEST_TAB_NUMBER:
            self.logger.warning(
                f"Tab {self.game_state.guild_chest.tab_number} is full (100/100 slots), skipping {item_name}"
            )
            return self.run_timer(
                HumanTimingsService().get_timing_short_action(), lambda: self.unload_object(object_to_unloads)
            )

        self._deposit_generation += 1
        generation = self._deposit_generation
        self.event_manager.on(
            InventoryWeightEvent,
            partial(
                self.on_inventory_weight_updated,
                object_to_unloads=object_to_unloads,
                generation=generation,
            ),
            originator=self,
            once=True,
            override_on_self=True,
            timeout=STORAGE_EVENT_TIMEOUT_SECONDS,
            on_timeout=partial(
                self.on_inventory_weight_timeout,
                object_to_unloads=object_to_unloads,
                generation=generation,
            ),
        )
        self.logger.info(
            f"Unloading {item_name} x{next_object.item.quantity} ({len(object_to_unloads)} remaining)"
        )
        req = ExchangeObjectMoveRequest(object_uid=next_object.item.uid, quantity=next_object.item.quantity)
        self.event_manager.send(req)

    def on_inventory_weight_updated(
        self,
        _msg: InventoryWeightEvent,
        *,
        object_to_unloads: list[ObjectItemInventory],
        generation: int,
    ) -> None:
        if generation != self._deposit_generation:
            return
        self._deposit_generation += 1
        self.run_timer(
            HumanTimingsService().get_timing_short_action(), lambda: self.unload_object(object_to_unloads)
        )

    def on_inventory_weight_timeout(
        self,
        *,
        object_to_unloads: list[ObjectItemInventory],
        generation: int,
    ) -> None:
        if generation != self._deposit_generation:
            return
        self._deposit_generation += 1
        self.logger.warning(
            "No inventory weight update after guild chest deposit; continuing with the remaining items"
        )
        self.run_timer(
            HumanTimingsService().get_timing_short_action(), lambda: self.unload_object(object_to_unloads)
        )

    def on_all_unloaded(self) -> None:
        self.logger.info("Guild chest unload completed")
        self.finish()
