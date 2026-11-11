from dataclasses import dataclass

from DBDofusUnity.datas.protos.non_obf.game.fight_pb2 import (
    FightTurnEvent,
    FightTurnStartPlayingEvent,
)
from DBDofusUnity.datas.protos.non_obf.game.gamemap_pb2 import (
    FightMapInformationEvent,
    MapComplementaryInformationEvent,
)

from src.core.behaviors.behavior import Behavior, BehaviorState
from src.core.behaviors.farms.fight.custom_fight_turn_behavior import (
    CustomFightTurnBehavior,
)
from src.core.behaviors.farms.fight.fight_control import FightControl
from src.core.behaviors.farms.fight.fight_preparation_behavior import (
    FightPreparationBehavior,
)
from src.core.behaviors.farms.fight.fight_turn_behavior import FightTurnBehavior
from src.core.behaviors.items.auto_equipment_from_inventory_behavior import (
    AutoEquipmentFromInventoryBehavior,
)
from src.core.engine.movements.map.path_finding.path_finding import Pathfinding
from src.core.signals.shared_farm_signals import SharedSignals
from src.services.human_timings import HumanTimingsService

FIGHT_TIMEOUT_SECONDS = 30 * 60
CHARACTERISTIC_UPGRADE_WAIT_SECONDS = 0.05


@dataclass
class FightBehavior(Behavior):
    path_finding: Pathfinding
    fight_turn_behavior: FightTurnBehavior
    custom_fight_turn_behavior: CustomFightTurnBehavior
    fight_preparation_behavior: FightPreparationBehavior
    fight_control: FightControl
    shared_signals: SharedSignals
    login: str
    auto_equipment_from_inventory_behavior: AutoEquipmentFromInventoryBehavior

    def run(self) -> None:
        self.run_timer(FIGHT_TIMEOUT_SECONDS, self.on_fight_timeout)
        self.event_manager.on(
            MapComplementaryInformationEvent,
            callback=self.on_map_complementary_information_event,
            originator=self,
            once=True,
        )
        if not self.game_state.map.is_in_map_transition:
            self.on_fight_map_initialized()
        else:
            self.event_manager.on(
                FightMapInformationEvent,
                lambda _: self.on_fight_map_initialized(),
                originator=self,
                once=True,
            )

    def on_fight_map_initialized(self):
        self.event_manager.on(FightTurnStartPlayingEvent, self.on_player_turn_event, originator=self)
        self.event_manager.on(FightTurnEvent, self.on_fight_turn_event, originator=self)
        if self.game_state.fight.is_our_turn:
            self.logger.info("It's already our turn, let's play")
            self.on_player_turn()
        elif len(self.game_state.fight.fight_placement_possible_positions) != 0:
            self.logger.info("It's fight preparation time")
            self.fight_control.begin_placement(
                self.game_state.fight.fight_placement_possible_positions
            )
            self.fight_preparation_behavior.start(
                callback=self.on_fight_preparation_behavior_finish, parent=self
            )
        else:
            self.fight_control.begin_enemy_turn()

    def on_map_complementary_information_event(self, msg: MapComplementaryInformationEvent):
        if self.fight_control.is_in_fight:
            self.fight_control.finish()
        self._finish_after_characteristic_upgrade()

    def _finish_after_characteristic_upgrade(self) -> None:
        if not self.game_state.player.is_characteristic_upgrade_complete_event.is_set():
            return self.run_timer(
                CHARACTERISTIC_UPGRADE_WAIT_SECONDS,
                self._finish_after_characteristic_upgrade,
            )
        self.run_timer(
            HumanTimingsService().get_timing_equipment_inventory_opening(),
            self.start_auto_equipment_from_inventory,
        )

    def start_auto_equipment_from_inventory(self) -> None:
        self.auto_equipment_from_inventory_behavior.start(
            callback=self.on_auto_equipment_from_inventory_finished,
            parent=self,
        )

    def on_auto_equipment_from_inventory_finished(self, error_code: str | None) -> None:
        self.raise_if_error(error_code)
        self.finish()

    def on_fight_preparation_behavior_finish(self, error_code: str | None):
        self.fight_control.end_placement()
        self.raise_if_error(error_code)

    def on_fight_turn_event(self, msg: FightTurnEvent) -> None:
        if self.game_state.fight.in_fight:
            self.fight_control.begin_enemy_turn()

    def on_player_turn_event(self, msg: FightTurnStartPlayingEvent) -> None:
        self.on_player_turn()

    def on_player_turn(self):
        if not self._can_start_fight_turn():
            return
        self.fight_control.begin_turn(self.game_state.fight.fight_turn)
        if self.game_state.fight.fight_turn > 100:
            self.logger.error("Bot Might be stuck")
            self.shared_signals.launch_account.emit(self.login)
        self._start_fight_turn_if_still_valid()

    def _start_fight_turn_if_still_valid(self) -> None:
        if not self._can_start_fight_turn():
            return
        behavior = self._active_turn_behavior()
        callback = (
            self.on_custom_fight_turn_behavior_finish
            if behavior is self.custom_fight_turn_behavior
            else None
        )
        behavior.start(callback=callback, parent=self)

    def on_custom_fight_turn_behavior_finish(self, error_code: str | None) -> None:
        self.raise_if_error(error_code)
        if (
            not self.fight_control.manual
            and self.game_state.fight.in_fight
            and self.game_state.fight.is_our_turn
            and self.fight_control.is_my_turn
        ):
            self._start_fight_turn_if_still_valid()

    def _active_turn_behavior(self) -> FightTurnBehavior | CustomFightTurnBehavior:
        if self.fight_control.manual:
            return self.custom_fight_turn_behavior
        return self.fight_turn_behavior

    def _can_start_fight_turn(self) -> bool:
        return (
            self.game_state.fight.in_fight
            and self._active_turn_behavior().state == BehaviorState.STOPPED
        )

    def on_fight_timeout(self):
        self.logger.error("Fight timeout reached (30 min), relaunching game")
        self.fight_control.finish("fight timeout")
        self.shared_signals.launch_account.emit(self.login)
