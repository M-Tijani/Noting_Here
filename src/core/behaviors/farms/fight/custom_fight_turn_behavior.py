from dataclasses import dataclass, field
from threading import Event, Thread

from DBDofusUnity.datas.protos.non_obf.game.fight_pb2 import (
    FightTurnEvent,
    FightTurnFinishRequest,
)
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint

from src.core.behaviors.farms.fight.fight_control import (
    CommandResult,
    FightCommand,
    FightCommandKind,
    FightControl,
    FightPhase,
    FightSnapshot,
)
from src.core.behaviors.farms.fight.fight_listener_behavior import FightListenerBehavior
from src.core.behaviors.farms.fight.fight_movement_behavior import FightMovementBehavior
from src.core.behaviors.farms.fight.fight_spell_behavior import FightSpellBehavior
from src.core.engine.fights.attack.spell_filter import resolve_castable_spell_lvl

_COMMAND_POLL_SECONDS = 0.2
_COMMAND_TIMEOUT_SECONDS = 30.0


@dataclass
class CustomFightTurnBehavior(FightListenerBehavior):
    fight_control: FightControl
    fight_movement_behavior: FightMovementBehavior
    fight_spell_behavior: FightSpellBehavior

    _stop_event: Event = field(init=False, default_factory=Event)
    _worker: Thread | None = field(init=False, default=None)

    def run(self) -> None:
        self._stop_event.clear()
        self.event_manager.on(FightTurnEvent, lambda _: self.finish(), originator=self)
        self.register_fight_death_check()
        self._publish_snapshot()
        self._worker = Thread(
            target=self._command_loop,
            daemon=True,
            name=f"custom-fight-turn-{id(self)}",
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        super().stop()

    def _command_loop(self) -> None:
        while not self._stop_event.is_set():
            command = self.fight_control.take_command(_COMMAND_POLL_SECONDS)
            if command is None:
                if not self.fight_control.manual or not self.fight_control.is_my_turn:
                    self.finish()
                    return
                continue

            try:
                result = self._execute_command(command)
            except Exception as error:
                self.logger.exception("Custom fight command failed")
                result = CommandResult(False, str(error))

            self.fight_control.complete_command(result)
            if command.kind is FightCommandKind.END_TURN and result.ok:
                self.fight_control.begin_enemy_turn()
            elif result.ok:
                self._publish_snapshot()

    def _execute_command(self, command: FightCommand) -> CommandResult:
        if command.kind is FightCommandKind.CAST:
            return self._cast_spell(command)
        if command.kind is FightCommandKind.MOVE:
            return self._move(command)
        return self._end_turn()

    def _cast_spell(self, command: FightCommand) -> CommandResult:
        if command.spell_id is None:
            return CommandResult(False, "spell id is required")
        if command.cell_id is None:
            return CommandResult(False, "target cell id is required")

        context = self.game_state.get_attack_context_if_available()
        if context is None:
            return CommandResult(False, "not in a fight")
        if resolve_castable_spell_lvl(context, command.spell_id) is None:
            return CommandResult(False, f"spell {command.spell_id} is not available")

        target_mp = MapPoint.from_cell_id(command.cell_id)
        finished = Event()
        outcome: dict[str, str | None] = {}

        def on_finished(error_code: str | None, *_: object, **__: object) -> None:
            outcome["error"] = error_code
            finished.set()

        self.fight_spell_behavior.start(
            callback=on_finished,
            parent=self,
            spell_id=command.spell_id,
            target_mp=target_mp,
        )
        return self._wait_for_command(finished, outcome, self.fight_spell_behavior, "spell cast")

    def _move(self, command: FightCommand) -> CommandResult:
        if command.cell_id is None:
            return CommandResult(False, "target cell id is required")

        target_mp = MapPoint.from_cell_id(command.cell_id)
        move_path = self.fight_movement_behavior.find_path_to_cell(target_mp)
        if move_path.end != target_mp:
            return CommandResult(False, f"cell {command.cell_id} is not reachable")

        finished = Event()
        outcome: dict[str, str | None] = {}

        def on_finished(error_code: str | None, *_: object, **__: object) -> None:
            outcome["error"] = error_code
            finished.set()

        self.fight_movement_behavior.start(
            callback=on_finished,
            parent=self,
            move_path=move_path,
        )
        return self._wait_for_command(finished, outcome, self.fight_movement_behavior, "fight move")

    def _end_turn(self) -> CommandResult:
        self.event_manager.send(FightTurnFinishRequest())
        return CommandResult(True)

    def _wait_for_command(
        self,
        finished: Event,
        outcome: dict[str, str | None],
        child_behavior: FightSpellBehavior | FightMovementBehavior,
        action: str,
    ) -> CommandResult:
        if not finished.wait(_COMMAND_TIMEOUT_SECONDS):
            child_behavior.stop()
            return CommandResult(False, f"{action} timed out")
        error = outcome.get("error")
        return CommandResult(error is None, error)

    def _publish_snapshot(self) -> None:
        context = self.game_state.get_attack_context_if_available()
        if context is None:
            return
        self.fight_control.publish_snapshot(
            FightSnapshot(
                phase=FightPhase.MY_TURN,
                turn=context.fight_turn,
                player_cell_id=context.player_map_point.cell_id,
                action_points=context.action_points,
                movement_points=context.movement_points,
                life_points=context.life_point,
                max_life_points=context.max_life_point,
                enemy_cell_ids=tuple(enemy.disposition.cell_id for enemy in context.enemy_actors),
                spell_ids=tuple(spell.spell_id for spell in context.spells),
            )
        )
