from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import Callable, cast

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import ObjectUidWithQuantity, PlayerSearch
from DBDofusUnity.datas.protos.non_obf.game.context_pb2 import ContextCreationEvent
from DBDofusUnity.datas.protos.non_obf.game.dialog_pb2 import DialogLeaveEvent, DialogLeaveRequest
from DBDofusUnity.datas.protos.non_obf.game.exchange_pb2 import ExchangeLeaveEvent
from DBDofusUnity.datas.protos.non_obf.game.gamemap_pb2 import MapComplementaryInformationEvent
from DBDofusUnity.datas.protos.non_obf.game.haven_bag_pb2 import HavenBagEnterRequest
from DBDofusUnity.datas.protos.non_obf.game.inventory_pb2 import (
    ObjectAddedEvent,
    ObjectDeleteRequest,
    ObjectDeletedEvent,
    ObjectErrorEvent,
    ObjectMovementEvent,
    ObjectQuantityEvent,
    ObjectSetPositionRequest,
    ObjectsDeletedEvent,
)
from DBDofusUnity.datas.protos.non_obf.game.npc_pb2 import (
    NpcDialogQuestionEvent,
    NpcDialogReplyRequest,
    NpcGenericActionRequest,
)
from DBDofusUnity.datas.protos.non_obf.game.party_pb2 import (
    PartyInvitationAcceptRequest,
    PartyInvitationRefuseRequest,
    PartyInvitationRequest,
    PartyJoinErrorEvent,
    PartyJoinEvent,
    PartyLeaveRequest,
    PartyNewGuestEvent,
    PartyType,
)
from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.data_center.i18n import I18N
from DBDofusUnity.dofus_unity_reader.data_center.map_reader import MapReader
from DBDofusUnity.dofus_unity_reader.data_center.world_graph_reader import WorldGraphReader
from DBDofusUnity.dofus_unity_reader.game_constants.directions import DirectionsEnum
from DBDofusUnity.dofus_unity_reader.game_constants.inventory_position import (
    CharacterInventoryPositionEnum,
)
from DBDofusUnity.dofus_unity_reader.game_constants.item import CategoryItemEnum
from DBDofusUnity.dofus_unity_reader.game_constants.map_id import FORBIDDEN_MAP_IDS
from DBDofusUnity.dofus_unity_reader.game_constants.npc import NpcActionEnum
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint
from DBDofusUnity.dofus_unity_reader.models.world_graph import Edge
from google.protobuf.message import Message

from src.core.behaviors.behavior import DIALOG_LEAVE_KINDS, Behavior, BehaviorState
from src.core.behaviors.farms.fight.attacker_behavior import AttackerBehavior
from src.core.behaviors.farms.fight.fight_behavior import FightBehavior
from src.core.behaviors.farms.fight.fight_control import (
    DEFAULT_WAIT_SECONDS,
    FightCommand,
    FightCommandKind,
    FightControl,
    FightPhase,
    FightSnapshot,
)
from src.core.behaviors.interactives.collect_behavior import CollectBehavior
from src.core.behaviors.interactives.interactive_behavior import InteractiveBehavior
from src.core.behaviors.movements.auto_trip.auto_trip_behavior import AutoTripBehavior
from src.core.behaviors.movements.auto_trip.auto_trip_zaap_behavior import AutoTripZaapBehavior
from src.core.behaviors.movements.edge_behavior import EdgeBehavior
from src.core.behaviors.movements.map_move_behavior import MapMoveBehavior
from src.core.engine.movements.world.live_world_graph import live_world_graph
from src.core.behaviors.sale_hotel.sale_hotel_reprice_behavior import (
    SaleHotelRepriceBehavior,
)
from src.core.behaviors.sale_hotel.sale_hotel_sell_behavior import SaleHotelSellBehavior
from src.core.behaviors.storage.mule.mule_exchange_behavior import MuleExchangeBehavior
from src.core.behaviors.storage.unloads.unload_behavior import UnloadBehavior
from src.core.bot.controller_hub import command_to_payload, controller_hub
from src.core.engine.economy.hdv_sell_bounds import HdvRepriceConfig, HdvSellTarget
from src.core.engine.items.equipment import get_equipment_positions
from src.core.engine.monsters.monster_group import get_monster_group_gids
from src.core.engine.movements.map.path_finding.movement_path import MovementPath
from src.core.engine.movements.map.path_finding.path_finding import Pathfinding
from src.core.engine.movements.world.map_change_cells import with_map_change_alternatives
from src.core.engine.movements.world.map_position import get_map_id_by_coords
from src.lua.behavior_runner import (
    BehaviorRunHandle,
    run_behavior_blocking,
    wait_for_defense,
)
from src.lua.bridge import BehaviorScriptBridge
from src.lua.errors import ScriptCancelled, ScriptError, ScriptFinished
from src.lua.runtime import LuaScriptRuntime
from src.lua.script_progress import clear_progress, load_progress, save_progress

_BEHAVIOR_POLL_SECONDS = 0.1
_DIALOG_WAIT_SECONDS = 10.0
_MAP_READY_TIMEOUT_SECONDS = 30.0
_FIGHT_START_TIMEOUT_SECONDS = 60.0
_FIGHT_COMMAND_TIMEOUT_SECONDS = DEFAULT_WAIT_SECONDS
_GROUP_INVITE_TIMEOUT_SECONDS = 30.0
_GROUP_COMMAND_TIMEOUT_SECONDS = 30.0
_MAX_SCRIPT_RESTARTS = 5
_RESTART_DELAY_SECONDS = 10.0
_PROGRESS_RESET_SECONDS = 60.0

_DIRECTIONS_BY_NAME: dict[str, DirectionsEnum] = {
    "right": DirectionsEnum.RIGHT,
    "left": DirectionsEnum.LEFT,
    "top": DirectionsEnum.UP,
    "bottom": DirectionsEnum.DOWN,
    "up": DirectionsEnum.UP,
    "down": DirectionsEnum.DOWN,
    "topright": DirectionsEnum.UP_RIGHT,
    "topleft": DirectionsEnum.UP_LEFT,
    "bottomright": DirectionsEnum.DOWN_RIGHT,
    "bottomleft": DirectionsEnum.DOWN_LEFT,
}

_WAYPOINT_RE = re.compile(r"(zaap|zaapi|zap)\(\s*(\d+)\s*\)")
_DIRECTION_CELL_RE = re.compile(r"([a-z_]+)\(\s*(\d+)\s*\)")
_CELL_RE = re.compile(r"\d+")

ScriptChild = (
    MapMoveBehavior
    | CollectBehavior
    | AttackerBehavior
    | AutoTripBehavior
    | EdgeBehavior
    | AutoTripZaapBehavior
    | UnloadBehavior
    | MuleExchangeBehavior
    | InteractiveBehavior
    | SaleHotelSellBehavior
    | SaleHotelRepriceBehavior
)

_FIGHT_HANDLING_CHILD_TYPES = (EdgeBehavior, AttackerBehavior)


def _to_id_set(value: object) -> set[int] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return {int(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = cast("list[object] | tuple[object, ...] | set[object] | frozenset[object]", value)
        return {converted for item in items if (converted := _to_int(item)) is not None}
    return None


def _to_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        return int(value)
    return None


def _to_timeout(value: object, default: float, label: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScriptError(f"invalid {label}: {value!r}")
    timeout = float(value)
    if timeout < 0:
        raise ScriptError(f"invalid {label}: {value!r}")
    return timeout


@dataclass
class LuaScriptBehavior(Behavior):
    """Runs a Lua script as a top-level behavior using the bot's primitives."""

    path_finding: Pathfinding
    map_move_behavior: MapMoveBehavior
    collect_behavior: CollectBehavior
    attacker_behavior: AttackerBehavior
    auto_trip_behavior: AutoTripBehavior
    edge_behavior: EdgeBehavior
    auto_trip_zaap_behavior: AutoTripZaapBehavior
    unload_behavior: UnloadBehavior
    mule_exchange_behavior: MuleExchangeBehavior
    fight_behavior: FightBehavior
    sale_hotel_sell_behavior: SaleHotelSellBehavior
    sale_hotel_reprice_behavior: SaleHotelRepriceBehavior
    fight_control: FightControl | None = None

    _cancel_event: Event = field(init=False, default_factory=Event)
    _defense_event: Event = field(init=False, default_factory=Event)
    _worker: Thread | None = field(init=False, default=None)
    _script_path: str | None = field(init=False, default=None)
    _fight_handle: BehaviorRunHandle | None = field(init=False, default=None)

    def run(self, script_path: str) -> None:
        self.logger.info("Starting Lua script: %s", script_path)
        self._script_path = script_path
        self._defense_event.clear()
        self._init_defense_listener()
        self._start_worker()

    def _start_worker(self) -> None:
        assert self._script_path is not None
        self._cancel_event = Event()
        self._worker = Thread(
            target=self._execute,
            args=(self._script_path,),
            daemon=True,
            name=f"lua-script-{self.game_state.map.map_id}",
        )
        self._worker.start()

    def _init_defense_listener(self) -> None:
        self.event_manager.on(ContextCreationEvent, self._on_context_creation_event, originator=self)

    def _on_context_creation_event(self, msg: ContextCreationEvent) -> None:
        if msg.context != ContextCreationEvent.GameContext.FIGHT:
            return
        if self.state != BehaviorState.RUNNING:
            return
        if self._defense_event.is_set():
            return
        if self.fight_behavior.state != BehaviorState.STOPPED:
            return
        if self._has_fight_handling_child():
            return
        self.logger.info("Fight started while running the Lua script, defending before resuming")
        with self.event_manager.lock:
            self._defense_event.set()
            self.clear_behavior()
            self._init_defense_listener()
            self.fight_behavior.start(callback=self._on_defense_finished, parent=self)

    def _on_defense_finished(self, error_code: str | None) -> None:
        if error_code is not None:
            self.logger.warning("Defensive fight finished with error: %s", error_code)
        self._defense_event.clear()

    def _has_fight_handling_child(self) -> bool:
        stack = list(self.children)
        while stack:
            child = stack.pop()
            if isinstance(child, _FIGHT_HANDLING_CHILD_TYPES):
                return True
            stack.extend(child.children)
        return False

    def _wait_for_defense(self) -> None:
        wait_for_defense(self._defense_event, self._cancel_event)

    def _execute(self, script_path: str) -> None:
        try:
            source = Path(script_path).read_text(encoding="utf-8")
        except OSError as error:
            self.logger.error("Cannot read Lua script %s: %s", script_path, error)
            self._finish(f"script_read_error: {error}")
            return
        self.logger.info("Running Lua script %s (%d chars)", script_path, len(source))

        login = str(self.game_state.player.login)
        script_name = Path(script_path).stem
        attempt = 0
        while True:
            resume_step = load_progress(login, script_name)
            started_at = time.monotonic()
            try:
                self._run_once(source, login, script_name, resume_step)
            except ScriptCancelled:
                self.logger.info("Lua script cancelled")
                self._finish("script_cancelled")
                return
            except ScriptFinished:
                self.logger.info("Lua script stopped itself via script.finish()")
                clear_progress(login, script_name)
                self._finish(None)
                return
            except ScriptError as error:
                reason = str(error)
            except Exception as error:  # noqa: BLE001 - guard the worker thread boundary
                reason = f"{type(error).__name__}: {error}"
            else:
                self.logger.info("Lua script finished")
                clear_progress(login, script_name)
                self._finish(None)
                return

            self._reset_after_failure()
            if time.monotonic() - started_at >= _PROGRESS_RESET_SECONDS:
                attempt = 0
            attempt += 1
            if attempt > _MAX_SCRIPT_RESTARTS:
                self.logger.error(
                    "Lua script kept failing, giving up after %d restart(s): %s",
                    _MAX_SCRIPT_RESTARTS,
                    reason,
                )
                self._finish(f"script_error: {reason}")
                return
            delay = min(60.0, _RESTART_DELAY_SECONDS * attempt)
            self.logger.warning(
                "Lua script error, restarting in %.0fs (restart %d/%d): %s",
                delay,
                attempt,
                _MAX_SCRIPT_RESTARTS,
                reason,
            )
            if self._cancel_event.wait(delay):
                self._finish("script_cancelled")
                return

    def _run_once(self, source: str, login: str, script_name: str, resume_step: int) -> None:
        bridge = BehaviorScriptBridge(
            game_state=self.game_state,
            logger=self.logger,
            path_finding=self.path_finding,
            map_move_behavior=self.map_move_behavior,
            collect_behavior=self.collect_behavior,
            attacker_behavior=self.attacker_behavior,
            auto_trip_behavior=self.auto_trip_behavior,
            parent=self,
            cancel_event=self._cancel_event,
            defense_event=self._defense_event,
            actions=self,
        )
        LuaScriptRuntime().run(
            source,
            bridge.bindings(),
            self._cancel_event,
            resume_index=resume_step,
            on_step=lambda step: save_progress(login, script_name, step),
        )

    def _reset_after_failure(self) -> None:
        self._stop_fight_handle()
        if self.fight_control is not None:
            self.fight_control.reset()
        with self.event_manager.lock:
            self.clear_behavior()
            self._init_defense_listener()

    def _finish(self, error_code: str | None) -> None:
        if self.state in {BehaviorState.RUNNING, BehaviorState.STARTING}:
            self.finish(error_code)

    def stop(self) -> None:
        self._cancel_event.set()
        self._stop_fight_handle()
        if self.fight_control is not None:
            self.fight_control.reset()
        self.game_state.set_fight_allowed_spell_ids(None)
        super().stop()

    def map_id(self) -> int:
        return self.game_state.map.map_id

    def map_coords(self) -> str:
        pos = self.game_state.map.map_pos
        return f"{pos.posX},{pos.posY}"

    def job_level(self, job_id: int) -> int:
        return self.game_state.player.jobs_lvl_by_id.get(int(job_id), 0)

    def can_use_bank(self) -> bool:
        return self.game_state.inventory.can_use_bank

    def deposit(self) -> None:
        target = self.game_state.settings.storage_target
        self.logger.info(f"Lua deposit: travelling to {target}")
        self._run_behavior(self.unload_behavior)
        self.logger.info("Lua deposit finished")

    def exchange_with_mule(self, player_id: object) -> None:
        if isinstance(player_id, bool) or not isinstance(player_id, (int, float, str)):
            raise ScriptError(f"invalid mule player id: {player_id!r}")
        try:
            target_id = int(player_id)
        except ValueError as error:
            raise ScriptError(f"invalid mule player id: {player_id!r}") from error
        if target_id <= 0:
            raise ScriptError(f"invalid mule player id: {player_id!r}")
        self.logger.info(f"Lua mule exchange: giving the inventory to player {target_id}")
        self._run_behavior(self.mule_exchange_behavior, target_id=target_id)
        self.logger.info("Lua mule exchange finished")

    def sale_auto(self) -> None:
        self.logger.info("Lua sale step: auto-selling at the sale hotel")
        self._run_behavior(self.sale_hotel_sell_behavior)

    def hdv_auto_sell(self) -> bool:
        self.sale_auto()
        return True

    def hdv_open_sell(self, category: object = None) -> bool:
        self.logger.info("Lua hdv.openSell: opening the sale hotel in sell mode")
        self._run_behavior(
            self.sale_hotel_reprice_behavior,
            category=self._hdv_category(category),
            open_only=True,
        )
        return True

    def hdv_close(self) -> bool:
        self.dialog_leave()
        return True

    def hdv_reprice(self, config: HdvRepriceConfig) -> dict[str, object]:
        outcome = self._run_behavior_outcome(self.sale_hotel_reprice_behavior, config=config)
        summary = outcome.get("summary")
        return cast("dict[str, object]", summary) if isinstance(summary, dict) else {}

    def hdv_watch_prices(self, config: HdvRepriceConfig) -> dict[str, object]:
        outcome = self._run_behavior_outcome(
            self.sale_hotel_reprice_behavior, config=config, watch_only=True
        )
        summary = outcome.get("summary")
        return cast("dict[str, object]", summary) if isinstance(summary, dict) else {}

    def hdv_sell_items(self, targets: dict[int, HdvSellTarget]) -> bool:
        if not targets:
            return False
        # The sell behavior reads manual_config on run() and resets it to None.
        self.sale_hotel_sell_behavior.manual_config = targets
        self._run_behavior(self.sale_hotel_sell_behavior)
        return True

    def hdv_sell_inventory_items(self, targets: dict[int, HdvSellTarget]) -> bool:
        if not targets:
            return False
        # Sell only items already in inventory; do not load from bank or guild chest.
        self.sale_hotel_sell_behavior.manual_config = targets
        self.sale_hotel_sell_behavior.inventory_only = True
        self._run_behavior(self.sale_hotel_sell_behavior)
        return True

    def inventory_items(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for object_item in self.game_state.inventory.objects_by_uid.values():
            gid = object_item.item.gid
            data = DataReader().item_by_id.get(gid)
            name = f"Unknown {gid}"
            if data is not None and data.nameId is not None:
                name = I18N().name_by_id.get(data.nameId, name)
            items.append(
                {
                    "uid": object_item.item.uid,
                    "gid": gid,
                    "name": name,
                    "quantity": object_item.item.quantity,
                    "position": object_item.position,
                    "equipped": object_item.position
                    != CharacterInventoryPositionEnum.InventoryPositionNotEquiped.value,
                }
            )
        return items

    def destroy_item(self, uid: object, quantity: object = None) -> bool:
        object_uid = _to_int(uid)
        if object_uid is None:
            raise ScriptError(f"invalid item uid: {uid!r}")
        object_item = self.game_state.inventory.objects_by_uid.get(object_uid)
        if object_item is None:
            return False
        delete_quantity = object_item.item.quantity if quantity is None else _to_int(quantity)
        if delete_quantity is None or delete_quantity <= 0:
            raise ScriptError(f"invalid item quantity: {quantity!r}")
        request = ObjectDeleteRequest(
            object=ObjectUidWithQuantity(
                object_uid=object_uid,
                quantity=min(delete_quantity, object_item.item.quantity),
            )
        )
        outcome = self._await_response(
            request,
            [ObjectDeletedEvent, ObjectsDeletedEvent, ObjectQuantityEvent, ObjectErrorEvent],
        )
        if outcome is None:
            raise ScriptError(f"timed out destroying item {object_uid}")
        return object_uid not in self.game_state.inventory.objects_by_uid

    def equip_item(self, uid: object, position: object = None) -> bool:
        object_uid = _to_int(uid)
        if object_uid is None:
            raise ScriptError(f"invalid item uid: {uid!r}")
        object_item = self.game_state.inventory.objects_by_uid.get(object_uid)
        if object_item is None:
            return False
        try:
            positions = get_equipment_positions(object_item.item.gid)
        except KeyError:
            return False
        if not positions:
            return False
        target = self._equipment_target_position(object_uid, positions, position)
        if object_item.position == target:
            return True
        request = ObjectSetPositionRequest(
            object_uid=object_uid,
            quantity=1,
            position=target,
        )
        outcome = self._await_response(
            request,
            [ObjectMovementEvent, ObjectAddedEvent, ObjectErrorEvent],
        )
        if outcome is None:
            raise ScriptError(f"timed out equipping item {object_uid}")
        if outcome is ObjectErrorEvent:
            return False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            current = self.game_state.inventory.objects_by_uid.get(object_uid)
            if current is not None and current.position == target:
                return True
            self._sleep(_BEHAVIOR_POLL_SECONDS)
        return False

    def _equipment_target_position(
        self,
        uid: int,
        positions: tuple[CharacterInventoryPositionEnum, ...],
        position: object,
    ) -> CharacterInventoryPositionEnum:
        current = self.game_state.inventory.objects_by_uid[uid].position
        if position is None:
            if current in positions:
                return CharacterInventoryPositionEnum(current)
            occupied = {
                item.position for item in self.game_state.inventory.objects_by_uid.values()
            }
            return next((slot for slot in positions if slot not in occupied), positions[0])
        value = _to_int(position)
        if value is None:
            raise ScriptError(f"invalid equipment position: {position!r}")
        try:
            target = CharacterInventoryPositionEnum(value)
        except ValueError as error:
            raise ScriptError(f"invalid equipment position: {position!r}") from error
        if target not in positions:
            raise ScriptError(f"item {uid} cannot be equipped at position {value}")
        return target

    def _hdv_category(self, category: object) -> CategoryItemEnum:
        value = _to_int(category)
        if value is None:
            return CategoryItemEnum.RESOURCES
        try:
            return CategoryItemEnum(value)
        except ValueError as error:
            raise ScriptError(f"invalid HDV category: {category!r}") from error

    def dialog_leave(self) -> None:
        self._close_dialog()

    def finish_script(self) -> None:
        self.logger.info("Lua script requested to finish")
        raise ScriptFinished()

    def resolve_map(self, reference: object) -> int:
        if isinstance(reference, bool):
            raise ScriptError(f"invalid map reference: {reference!r}")
        if isinstance(reference, (int, float)):
            return int(reference)
        if isinstance(reference, str):
            text = reference.strip()
            if "," in text:
                pos_x, pos_y = (int(part) for part in text.split(",", 1))
                resolved = get_map_id_by_coords(pos_x, pos_y, self.game_state.map.map_id)
                if resolved is None:
                    raise ScriptError(f"unknown map coordinates: {reference!r}")
                return resolved
            return int(text)
        raise ScriptError(f"invalid map reference: {reference!r}")

    def goto_map(self, map_id: int) -> None:
        target = int(map_id)
        self._wait_for_map_ready()
        self._run_behavior(self.auto_trip_zaap_behavior, map_ids={target})

    def gather(self, force: bool = False, spec: object = None) -> None:
        self.wait_for_map_data()
        spec_list = cast("list[object] | tuple[object, ...] | set[object] | frozenset[object] | None", spec)
        target_ids = _to_id_set(spec_list) if isinstance(spec_list, (list, tuple, set, frozenset)) else None
        collectables = self.game_state.interactive.get_farmable_collectables(set())
        present_ids = sorted({collectable.resource_item_id for collectable in collectables})
        matching = (
            collectables
            if target_ids is None
            else [collectable for collectable in collectables if collectable.resource_item_id in target_ids]
        )
        self.logger.info(
            "Lua gather step: %d collectable(s), resource ids=%s, %d matching target=%s, force=%s",
            len(collectables),
            present_ids,
            len(matching),
            sorted(target_ids) if target_ids is not None else "all",
            force,
        )
        while True:
            self._run_behavior(self.collect_behavior, target_resource_item_ids=target_ids)
            if not force:
                return
            self._sleep(1.0)

    def fight(self, force: bool = False, spec: object = None, config: dict[str, object] | None = None) -> None:
        settings = config or {}
        spec_list = cast("list[object] | tuple[object, ...] | set[object] | frozenset[object] | None", spec)
        if isinstance(spec_list, (list, tuple, set, frozenset)):
            monster_ids = _to_id_set(spec_list)
            exact_monster_ids = False
        else:
            monster_ids = _to_id_set(settings.get("force"))
            exact_monster_ids = monster_ids is not None
        forbidden_ids = _to_id_set(settings.get("forbidden"))
        size_min = _to_int(settings.get("min"))
        size_max = _to_int(settings.get("max"))
        groups = self.game_state.entity.get_monster_groups()
        present_gids = sorted({gid for _, _, group in groups for gid in get_monster_group_gids(group)})
        self.logger.info(
            "Lua fight step: %d group(s), species on map=%s, target=%s exact=%s size=%s..%s forbidden=%s wait_for_group=%s",
            len(groups),
            present_gids,
            sorted(monster_ids) if monster_ids is not None else "any",
            exact_monster_ids,
            size_min,
            size_max,
            sorted(forbidden_ids) if forbidden_ids is not None else None,
            bool(force),
        )
        self._run_behavior(
            self.attacker_behavior,
            count_fight_limit=None,
            wait_for_group=bool(force),
            monster_ids=monster_ids,
            forbidden_monster_ids=forbidden_ids,
            group_size_min=size_min,
            group_size_max=size_max,
            exact_monster_ids=exact_monster_ids,
        )
        self.logger.info("Lua fight step finished")

    def start_fight(
        self,
        count: object = None,
        monster_ids: object = None,
        options: object = None,
    ) -> bool:
        control = self._require_fight_control()
        handle = self._fight_handle
        if handle is not None and not handle.finished:
            raise ScriptError("a Lua fight is already running")
        if self.game_state.fight.in_fight:
            raise ScriptError("already in a fight")

        settings = self._fight_options(options)
        fight_count = 1 if count is None else _to_int(count)
        if fight_count is None or fight_count < 1:
            raise ScriptError(f"invalid fight count: {count!r}")
        target_ids = _to_id_set(monster_ids)
        start_timeout = _to_timeout(
            settings.get("timeout"), _FIGHT_START_TIMEOUT_SECONDS, "fight start timeout"
        )

        control.reset()
        control.set_manual()
        self.logger.info(
            "Lua fight.start: manual control, count=%d monster_ids=%s",
            fight_count,
            sorted(target_ids) if target_ids is not None else "any",
        )
        run_handle = BehaviorRunHandle(
            self.attacker_behavior,
            parent=self,
            cancel_event=self._cancel_event,
            count_fight_limit=fight_count,
            wait_for_group=settings.get("wait_for_group", False),
            monster_ids=target_ids,
            forbidden_monster_ids=settings.get("forbidden_monster_ids"),
            group_size_min=settings.get("group_size_min"),
            group_size_max=settings.get("group_size_max"),
            exact_monster_ids=settings.get("exact_monster_ids", False),
            only_group_actor_id=settings.get("only_group_actor_id"),
        )
        self._fight_handle = run_handle
        try:
            run_handle.start()
            self._wait_for_fight_start(control, run_handle, start_timeout)
        except Exception:
            self._stop_fight_handle()
            control.reset()
            raise
        return True

    def wait_fight(self, timeout: object = None) -> bool:
        handle = self._fight_handle
        if handle is None:
            raise ScriptError("no fight has been started")
        wait_timeout = None if timeout is None else _to_timeout(timeout, 0.0, "fight wait timeout")
        try:
            outcome = handle.wait(wait_timeout)
        except TimeoutError as error:
            raise ScriptError(str(error)) from error
        error_code = outcome.get("error")
        if error_code is not None:
            raise ScriptError(str(error_code))
        return True

    def fight_is_in_fight(self) -> bool:
        return self.game_state.fight.in_fight

    def fight_is_my_turn(self) -> bool:
        return self._require_fight_control().is_my_turn

    def fight_state(self) -> dict[str, object]:
        return self._fight_snapshot_to_dict(self._require_fight_control().snapshot())

    def fight_placement_cells(self) -> list[int]:
        return list(self._require_fight_control().snapshot().placement_cell_ids)

    def fight_set_placement_cell(self, cell_id: object, timeout: object = None) -> bool:
        control = self._require_fight_control()
        target_cell_id = _to_int(cell_id)
        if target_cell_id is None:
            raise ScriptError(f"invalid placement cell: {cell_id!r}")
        snapshot = control.snapshot()
        if snapshot.phase is not FightPhase.PLACEMENT:
            raise ScriptError(f"not in placement ({snapshot.phase.name.lower()})")
        if snapshot.placement_cell_ids and target_cell_id not in snapshot.placement_cell_ids:
            raise ScriptError(f"cell {target_cell_id} is not a valid placement cell")

        wait_timeout = _to_timeout(timeout, DEFAULT_WAIT_SECONDS, "placement timeout")
        control.request_placement_cell(target_cell_id)
        placed = control.wait_for_placement_end(wait_timeout)
        if placed is None:
            raise ScriptError("timed out waiting for manual placement")
        if placed.phase is FightPhase.FINISHED:
            raise ScriptError("fight ended during placement")
        return True

    def fight_wait_for_placement(self, timeout: object = None) -> dict[str, object] | None:
        wait_timeout = _to_timeout(timeout, DEFAULT_WAIT_SECONDS, "placement wait timeout")
        snapshot = self._require_fight_control().wait_for_placement(wait_timeout)
        return None if snapshot is None else self._fight_snapshot_to_dict(snapshot)

    def fight_wait_for_my_turn(self, timeout: object = None) -> dict[str, object] | None:
        wait_timeout = _to_timeout(timeout, DEFAULT_WAIT_SECONDS, "turn wait timeout")
        snapshot = self._require_fight_control().wait_for_my_turn(wait_timeout)
        return None if snapshot is None else self._fight_snapshot_to_dict(snapshot)

    def fight_cast_spell(self, spell_id: object, cell_id: object) -> bool:
        target_spell_id = _to_int(spell_id)
        target_cell_id = _to_int(cell_id)
        if target_spell_id is None:
            raise ScriptError(f"invalid spell id: {spell_id!r}")
        if target_cell_id is None:
            raise ScriptError(f"invalid target cell: {cell_id!r}")
        return self._submit_fight_command(
            FightCommand(
                FightCommandKind.CAST,
                spell_id=target_spell_id,
                cell_id=target_cell_id,
            )
        )

    def fight_move_to_cell(self, cell_id: object) -> bool:
        target_cell_id = _to_int(cell_id)
        if target_cell_id is None:
            raise ScriptError(f"invalid fight cell: {cell_id!r}")
        return self._submit_fight_command(
            FightCommand(FightCommandKind.MOVE, cell_id=target_cell_id)
        )

    def fight_end_turn(self) -> bool:
        return self._submit_fight_command(FightCommand(FightCommandKind.END_TURN))

    def fight_set_auto(self) -> bool:
        control = self._require_fight_control()
        control.set_auto()
        self.logger.info("Lua fight control switched to automatic")
        return True

    def _wait_for_fight_start(
        self,
        control: FightControl,
        handle: BehaviorRunHandle,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self._cancel_event.is_set():
                raise ScriptCancelled()
            if control.is_in_fight:
                return
            if handle.finished:
                error_code = handle.outcome.get("error")
                if error_code is not None:
                    raise ScriptError(str(error_code))
                raise ScriptError("fight did not start: no valid monster group found")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScriptError("timed out waiting for the fight to start")
            if control.wait_for_active_fight(min(_BEHAVIOR_POLL_SECONDS, remaining)) is not None:
                return

    def _submit_fight_command(self, command: FightCommand) -> bool:
        control = self._require_fight_control()
        if not control.manual:
            raise ScriptError("fight control is in auto mode")
        if not control.is_my_turn:
            raise ScriptError("not my turn")
        result = control.submit_command(command, _FIGHT_COMMAND_TIMEOUT_SECONDS)
        if not result.ok:
            raise ScriptError(result.error or "fight command failed")
        return True

    def _require_fight_control(self) -> FightControl:
        if self.fight_control is None:
            raise ScriptError("fight control is not available")
        return self.fight_control

    def _fight_options(self, options: object) -> dict[str, object]:
        if options is None:
            return {}
        if isinstance(options, (list, tuple, set, frozenset)) and not options:
            return {}
        if not isinstance(options, dict):
            raise ScriptError(f"invalid fight options: {options!r}")
        values = cast("dict[str, object]", options)
        settings: dict[str, object] = {
            "wait_for_group": bool(values.get("force", False)),
            "exact_monster_ids": bool(values.get("exact", False)),
            "forbidden_monster_ids": _to_id_set(values.get("forbidden")),
        }
        for key, source in (
            ("group_size_min", "min"),
            ("group_size_max", "max"),
            ("only_group_actor_id", "group"),
        ):
            raw = values.get(source)
            if raw is None:
                continue
            converted = _to_int(raw)
            if converted is None:
                raise ScriptError(f"invalid fight {source}: {raw!r}")
            settings[key] = converted
        if values.get("timeout") is not None:
            settings["timeout"] = values["timeout"]
        return settings

    def _fight_snapshot_to_dict(self, snapshot: FightSnapshot) -> dict[str, object]:
        return {
            "phase": snapshot.phase.name.lower(),
            "turn": snapshot.turn,
            "playerCellId": snapshot.player_cell_id,
            "actionPoints": snapshot.action_points,
            "movementPoints": snapshot.movement_points,
            "lifePoints": snapshot.life_points,
            "maxLifePoints": snapshot.max_life_points,
            "enemies": [{"cellId": cell_id} for cell_id in snapshot.enemy_cell_ids],
            "enemyCellIds": list(snapshot.enemy_cell_ids),
            "spells": [{"spellId": spell_id} for spell_id in snapshot.spell_ids],
            "spellIds": list(snapshot.spell_ids),
            "placementCellIds": list(snapshot.placement_cell_ids),
        }

    def _stop_fight_handle(self) -> None:
        handle = self._fight_handle
        self._fight_handle = None
        if handle is not None:
            handle.stop()

    def use_door(self, cell: int) -> None:
        self._wait_for_map_ready()
        edge = self._transition_edge(direction=None, cell=int(cell))
        if edge is None:
            raise ScriptError(f"no door on cell {cell}")
        self._run_edge(edge)

    def use_path(self, path: str, fallback_map: object = None) -> None:
        spec = path.strip()
        if spec == "":
            raise ScriptError("empty path")
        if spec == "havenbag":
            self._toggle_haven_bag()
            return
        waypoint = _WAYPOINT_RE.fullmatch(spec)
        if waypoint is not None:
            self._wait_for_map_ready()
            self._run_behavior(self.auto_trip_zaap_behavior, map_ids={int(waypoint.group(2))})
            return
        if _CELL_RE.fullmatch(spec):
            self._move_to_cell(int(spec))
            return
        self._wait_for_map_ready()
        if "|" in spec:
            options = [option.strip() for option in spec.split("|") if option.strip()]
            random.shuffle(options)
            edge = next(
                (resolved for option in options if (resolved := self._edge_for_option(option)) is not None),
                None,
            )
        else:
            edge = self._edge_for_option(spec)
        if self._take_edge(edge, fallback_map, spec):
            return
        raise ScriptError(f"no valid exit for path {spec!r}")

    def _take_edge(self, edge: Edge | None, fallback_map: object, spec: str) -> bool:
        """Walk ``edge``; if it is missing or fails, route to ``fallback_map`` instead."""
        if edge is not None:
            try:
                self._run_edge(edge)
            except ScriptError as error:
                if fallback_map is None:
                    raise
                self.logger.warning(
                    "Lua exit %r failed (%s), pathfinding to the next map instead", spec, error
                )
            else:
                return True
        if fallback_map is None:
            return False
        target = self.resolve_map(fallback_map)
        self.logger.info("Lua exit %r is blocked, pathfinding to map %s instead", spec, target)
        self.goto_map(target)
        return True

    def use_zaap(self, map_id: object) -> None:
        try:
            target = _to_int(map_id)
        except ValueError as error:
            raise ScriptError(f"invalid map id: {map_id!r}") from error
        if target is None:
            raise ScriptError(f"invalid map id: {map_id!r}")
        self._wait_for_map_ready()
        self._run_behavior(self.auto_trip_zaap_behavior, map_ids={target})

    def _run_edge(self, edge: Edge) -> None:
        self._run_behavior(self.edge_behavior, edge=edge)
        self._wait_for_map_ready()

    def npc_talk(self, npc_id: int) -> None:
        self._close_dialog()
        self.game_state.dialog.npc_id = int(npc_id)
        request = NpcGenericActionRequest(
            npc_action_id=int(NpcActionEnum.TALK),
            npc_id=int(npc_id),
            npc_map_id=self.game_state.map.map_id,
        )
        self._wait_for_events(request, [NpcDialogQuestionEvent, DialogLeaveEvent])

    def npc_reply(self, reply_id: int) -> None:
        request = NpcDialogReplyRequest(reply_id=int(reply_id))
        self._wait_for_events(request, [NpcDialogQuestionEvent, DialogLeaveEvent])

    def npc_reply_index(self, index: int) -> None:
        question = self.game_state.dialog.npc_question
        replies = list(question.visible_replies) if question is not None else []
        if index < 1 or index > len(replies):
            raise ScriptError(f"invalid NPC reply index {index} (have {len(replies)} replies)")
        self.npc_reply(replies[index - 1].reply_id)

    def invite_to_group(self, name: object) -> bool:
        target_name = str(name).strip()
        if target_name == "":
            raise ScriptError(f"invalid player name for group invite: {name!r}")
        request = PartyInvitationRequest(
            target=PlayerSearch(
                search_by_character_name=PlayerSearch.SearchByCharacterName(name=target_name)
            ),
            party_type=PartyType.CLASSICAL,
        )
        outcome = self._await_response(request, [PartyNewGuestEvent, PartyJoinErrorEvent])
        if outcome is None:
            raise ScriptError(f"timed out inviting {target_name} to the group")
        return outcome is not PartyJoinErrorEvent

    def leave_group(self) -> bool:
        party_id = self.game_state.party.party_id
        if party_id == 0:
            return False
        self.event_manager.send(PartyLeaveRequest(party_id=party_id))
        return True

    def accept_group_invite(self) -> bool:
        pending = self.game_state.party.pending_invite
        if pending is None:
            raise ScriptError("no pending group invite to accept")
        self._wait_for_events(PartyInvitationAcceptRequest(party_id=pending.party_id), [PartyJoinEvent])
        return True

    def decline_group_invite(self) -> bool:
        pending = self.game_state.party.pending_invite
        if pending is None:
            raise ScriptError("no pending group invite to decline")
        self.event_manager.send(PartyInvitationRefuseRequest(party_id=pending.party_id))
        self.game_state.party.pending_invite = None
        return True

    def wait_for_group_invite(self, timeout: object = None) -> dict[str, object] | None:
        wait_seconds = _GROUP_INVITE_TIMEOUT_SECONDS
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ScriptError(f"invalid invite timeout: {timeout!r}")
            wait_seconds = float(timeout)
        deadline = time.monotonic() + wait_seconds
        while True:
            self._wait_for_defense()
            pending = self.game_state.party.pending_invite
            if pending is not None:
                return {"id": pending.party_id, "name": pending.from_player_name}
            if time.monotonic() >= deadline:
                return None
            if self._cancel_event.wait(_BEHAVIOR_POLL_SECONDS):
                raise ScriptCancelled()

    def wait_for_group_command(self, timeout: object = None) -> dict[str, object] | None:
        wait_seconds = _GROUP_COMMAND_TIMEOUT_SECONDS
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ScriptError(f"invalid command timeout: {timeout!r}")
            wait_seconds = float(timeout)
        deadline = time.monotonic() + wait_seconds
        login = self.game_state.player.login
        while True:
            self._wait_for_defense()
            command = controller_hub.poll(login)
            if command is not None:
                return command_to_payload(command)
            if time.monotonic() >= deadline:
                return None
            if self._cancel_event.wait(_BEHAVIOR_POLL_SECONDS):
                raise ScriptCancelled()

    def change_map(self, direction: str) -> bool:
        self.use_path(str(direction))
        return True

    def move_toward_map(self, map_id: int) -> bool:
        self._wait_for_map_ready()
        target_map_id = int(map_id)
        if self.game_state.map.map_id == target_map_id:
            return False
        path = self.auto_trip_behavior.world_path_finder.find_path(
            self.game_state.get_world_path_context(),
            self.game_state.map.curr_vertex,
            {target_map_id},
        )
        if not path:
            return False
        self._run_edge(path[0])
        return True

    def move_toward(self, pos_x: int, pos_y: int) -> bool:
        map_id = get_map_id_by_coords(int(pos_x), int(pos_y), self.game_state.map.map_id)
        if map_id is None:
            return False
        return self.move_toward_map(map_id)

    def wait_for_map_change(self, timeout: float | None = None) -> bool:
        start_map_id = self.game_state.map.map_id
        deadline = time.monotonic() + (timeout if timeout is not None else _MAP_READY_TIMEOUT_SECONDS)
        while self.game_state.map.map_id == start_map_id:
            if self._cancel_event.is_set():
                raise ScriptCancelled()
            if time.monotonic() >= deadline:
                return False
            self._sleep(_BEHAVIOR_POLL_SECONDS)
        return True

    def use_interactive(self, cell_id: int, skill_index: int | None = None) -> None:
        self._wait_for_map_ready()
        element_id, skill_id = self._resolve_interactive(int(cell_id), skill_index)
        self._run_behavior(
            self.collect_behavior.interactive_behavior,
            element_mp=MapPoint.from_cell_id(int(cell_id)),
            element_id=element_id,
            skill_id=skill_id,
        )

    def _resolve_interactive(self, cell_id: int, skill_index: int | None) -> tuple[int, int]:
        ref_by_element_id = MapReader().get_ref_data_by_element_id_by_map_id(self.game_state.map.map_id)
        element_ids = [
            element_id
            for element_id, ref in ref_by_element_id.items()
            if ref.cellId == cell_id
            and element_id in self.game_state.interactive.interactive_element_by_id
        ]
        if not element_ids:
            raise ScriptError(f"no interactive object on cell {cell_id}")
        element = self.game_state.interactive.interactive_element_by_id[element_ids[0]]
        if len(element.enabled_skills) == 0:
            raise ScriptError(f"interactive object on cell {cell_id} has no enabled skill")
        if skill_index is None:
            return element.element_id, element.enabled_skills[0].skill_id
        if skill_index < 1 or skill_index > len(element.enabled_skills):
            raise ScriptError(f"invalid skill index {skill_index} for cell {cell_id}")
        return element.element_id, element.enabled_skills[skill_index - 1].skill_id

    def _edge_for_option(self, option: str) -> Edge | None:
        direction_cell = _DIRECTION_CELL_RE.fullmatch(option)
        if direction_cell is not None:
            direction = _DIRECTIONS_BY_NAME.get(direction_cell.group(1))
            if direction is None:
                return None
            return self._transition_edge(direction=int(direction), cell=int(direction_cell.group(2)))
        direction = _DIRECTIONS_BY_NAME.get(option)
        if direction is None:
            return None
        return self._transition_edge(direction=int(direction), cell=None)

    def _transition_edge(self, direction: int | None, cell: int | None) -> Edge | None:
        src_vertex = self.game_state.map.curr_vertex
        live_edges = live_world_graph.get_edges(src_vertex, direction)
        cached_edges = WorldGraphReader().get_outgoing_edges_from_vertex(src_vertex)
        # Live observations take precedence over the static cache so the bot can
        # learn new or changed map connections without waiting for a graph regen.
        # Observed transitions also override stale forbidden-map flags.
        for edge in live_edges:
            result = self._match_transition(edge, direction, cell)
            if result is not None:
                return result
        for edge in cached_edges:
            if cell is None and edge.m_to.m_mapId in FORBIDDEN_MAP_IDS:
                continue
            result = self._match_transition(edge, direction, cell)
            if result is not None:
                return result
        return None

    def _match_transition(
        self, edge: Edge, direction: int | None, cell: int | None
    ) -> Edge | None:
        edge = with_map_change_alternatives(edge)
        matched = [
            transition
            for transition in edge.m_transitions
            if (direction is None or transition.m_direction == direction)
            and (cell is None or transition.m_cellId == cell)
        ]
        if not matched:
            return None
        if cell is None:
            return Edge(m_from=edge.m_from, m_to=edge.m_to, m_transitions=matched)
        first = matched[0]
        return Edge(
            m_from=edge.m_from,
            m_to=edge.m_to,
            m_transitions=[first, *(transition for transition in matched if transition is not first)],
        )

    def _move_to_cell(self, cell: int) -> None:
        self._wait_for_map_ready()
        move_path = self._compute_move_path(cell)
        self._run_behavior(
            self.map_move_behavior,
            rebuild=lambda: {"move_path": self._compute_move_path(cell)},
            move_path=move_path,
        )

    def _compute_move_path(self, cell: int) -> MovementPath:
        target = MapPoint.from_cell_id(cell)
        move_path = self.path_finding.find_path(
            self.game_state.get_map_movement_context(),
            self.game_state.map.map_point,
            {target},
        )
        if move_path.end.cell_id != cell:
            raise ScriptError(f"cell {cell} is unreachable from the current position")
        return move_path

    def _toggle_haven_bag(self) -> None:
        self._wait_for_events(HavenBagEnterRequest(), [MapComplementaryInformationEvent])

    def _close_dialog(self) -> None:
        if self.game_state.dialog.is_any_open:
            # Exchange dialogs (HDV, shops) leave via ExchangeLeaveEvent, not DialogLeaveEvent.
            leave_event = (
                DialogLeaveEvent
                if self.game_state.dialog.kind in DIALOG_LEAVE_KINDS
                else ExchangeLeaveEvent
            )
            self._wait_for_events(DialogLeaveRequest(), [leave_event])

    def _wait_for_events(self, request: Message, wait_types: list[type[Message]]) -> None:
        self._await_response(request, wait_types)

    def _await_response(
        self, request: Message, wait_types: list[type[Message]]
    ) -> type[Message] | None:
        while True:
            self._wait_for_defense()
            fired: list[type[Message]] = []
            finished = Event()

            def on_event(msg: Message) -> None:
                fired.append(type(msg))
                finished.set()

            for msg_type in wait_types:
                self.event_manager.on(msg_type, on_event, originator=self, once=True)
            self.event_manager.send(request)
            deadline = time.monotonic() + _DIALOG_WAIT_SECONDS
            try:
                while not finished.wait(_BEHAVIOR_POLL_SECONDS):
                    if self._cancel_event.is_set():
                        raise ScriptCancelled()
                    if self._defense_event.is_set():
                        break
                    if time.monotonic() >= deadline:
                        raise ScriptError("timed out waiting for server response")
            finally:
                for msg_type in wait_types:
                    self.unregister_listener(msg_type, reason="script dialog wait done")

            if not self._defense_event.is_set():
                return fired[0] if fired else None

    def _run_behavior(
        self,
        behavior: ScriptChild,
        *,
        rebuild: Callable[[], dict[str, object]] | None = None,
        **run_kwargs: object,
    ) -> None:
        self._run_behavior_outcome(behavior, rebuild=rebuild, **run_kwargs)

    def _run_behavior_outcome(
        self,
        behavior: ScriptChild,
        *,
        rebuild: Callable[[], dict[str, object]] | None = None,
        **run_kwargs: object,
    ) -> dict[str, object]:
        outcome = run_behavior_blocking(
            behavior,
            parent=self,
            cancel_event=self._cancel_event,
            defense_event=self._defense_event,
            rebuild=rebuild,
            **run_kwargs,
        )
        error_code = outcome.get("error")
        if error_code is not None:
            raise ScriptError(str(error_code))
        return outcome

    def _sleep(self, seconds: float) -> None:
        self._wait_for_defense()
        if self._cancel_event.wait(max(0.0, float(seconds))):
            raise ScriptCancelled()
        self._wait_for_defense()

    def _is_map_ready(self) -> bool:
        return (
            not self.game_state.map.is_in_map_transition
            and self.game_state.player.character_id in self.game_state.entity.actor_by_id
        )

    def _wait_for_map_ready(self) -> None:
        if self._is_map_ready():
            return
        deadline = time.monotonic() + _MAP_READY_TIMEOUT_SECONDS
        while not self._is_map_ready():
            if self._cancel_event.is_set():
                raise ScriptCancelled()
            if time.monotonic() >= deadline:
                raise ScriptError("timed out waiting for the new map to load")
            self._sleep(_BEHAVIOR_POLL_SECONDS)

    def wait_for_map_data(self) -> None:
        if self._is_map_data_ready():
            return
        deadline = time.monotonic() + _MAP_READY_TIMEOUT_SECONDS
        while not self._is_map_data_ready():
            if self._cancel_event.is_set():
                raise ScriptCancelled()
            if time.monotonic() >= deadline:
                raise ScriptError("timed out waiting for the new map data to load")
            self._sleep(_BEHAVIOR_POLL_SECONDS)

    def _is_map_data_ready(self) -> bool:
        return (
            self._is_map_ready()
            and self.game_state.interactive.loaded_map_id == self.game_state.map.map_id
        )
