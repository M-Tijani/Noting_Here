from collections import defaultdict
from dataclasses import dataclass

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import (
    ActorPositionInformation,
)
from DBDofusUnity.dofus_unity_reader.game_constants.characteristic import CharacteristicEnum
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint
from src.core.behaviors.farms.fight.fight_listener_behavior import FightListenerBehavior
from src.core.behaviors.movements.map_move_behavior import MapMoveBehavior
from src.core.challenges.models import ChallengeRule, FightConstraints
from src.core.config import FAST_FIGHT
from src.core.engine.fights.attack.challenge_rules import (
    END_TURN_RULES,
    is_end_turn_position_valid,
)
from src.core.engine.fights.attack.enemy_data import EnemyData
from src.core.engine.fights.reachable_cells.fight_reachable_cells import (
    FightReachableCells,
)
from src.core.engine.movements.map.path_finding.movement_path import MovementPath
from src.core.engine.movements.map.path_finding.path_finding import Pathfinding

_FAST_MODE_MOVEMENT_RULES = END_TURN_RULES | {ChallengeRule.MELEE_ONLY}


@dataclass
class FightMovementBehavior(FightListenerBehavior):
    map_move_behavior: MapMoveBehavior
    path_finding: Pathfinding
    fight_reachable_cells: FightReachableCells

    def run(self, move_path: MovementPath | None = None) -> None:
        self.register_fight_death_check()

        if move_path is None:
            move_path = self.find_safest_path()
            if move_path is None:
                return self.finish()

        pm: int = self.game_state.fight.get_stat_by_id(CharacteristicEnum.MOVEMENT_POINTS)

        self.logger.info(f"PM : {pm}")

        if pm < 0:
            return self.finish()

        if len(move_path.path) > pm:
            move_path.end = move_path.path[pm].step
            move_path.path = move_path.path[:pm]

        self.map_move_behavior.start(parent=self, move_path=move_path, callback=self.finish)

    def find_near_enemy_with_dist(
        self, start: MapPoint
    ) -> tuple[ActorPositionInformation, MovementPath, float] | None:
        enemies = self.game_state.fight.get_enemies(self.game_state.player.character_id)
        if len(enemies) == 0:
            return None

        enemies_by_mp: dict[MapPoint, list[ActorPositionInformation]] = defaultdict(list)
        for enemy in enemies:
            mp_enemy = MapPoint.from_cell_id(enemy.disposition.cell_id)
            for side_mp_enemy in mp_enemy.side_map_points:
                enemies_by_mp[side_mp_enemy].append(enemy)

        move_path = self.path_finding.find_path(
            self.game_state.get_map_movement_context(),
            start,
            set(enemies_by_mp.keys()),
            allow_trough_entity=False,
            allow_diag=False,
        )

        related_enemies = enemies_by_mp.get(move_path.end)
        if related_enemies is None:
            near_mp, related_enemies = min(
                enemies_by_mp.items(),
                key=lambda mp_with_enemies: mp_with_enemies[0].distance_to_map_point(move_path.end),
            )
            cost_path = (
                len(move_path.path)
                + near_mp.distance_to_map_point(MapPoint.from_cell_id(related_enemies[0].disposition.cell_id))
                + 1
            )
        else:
            cost_path = len(move_path.path)

        return related_enemies[0], move_path, cost_path

    def find_path_to_cell(self, target_mp: MapPoint) -> MovementPath:
        return self.path_finding.find_path(
            self.game_state.get_map_movement_context(),
            self.game_state.map.map_point,
            {target_mp},
            allow_diag=False,
            allow_trough_entity=False,
        )

    def _ally_map_points(self) -> set[MapPoint]:
        character_id = self.game_state.player.character_id
        return {
            MapPoint.from_cell_id(ally.disposition.cell_id)
            for ally in self.game_state.fight.get_allies(character_id)
        }

    def _is_end_turn_position_valid(
        self,
        mp: MapPoint,
        constraints: FightConstraints,
        allies_mp: set[MapPoint],
        enemies_mp: set[MapPoint],
    ) -> bool:
        return is_end_turn_position_valid(constraints, mp, allies_mp, enemies_mp)

    def _end_turn_candidates(
        self,
        constraints: FightConstraints,
        reachable_mps: dict[MapPoint, int],
        allies_mp: set[MapPoint],
        enemies_mp: set[MapPoint],
    ) -> set[MapPoint]:
        candidates = {
            mp
            for mp in reachable_mps
            if self._is_end_turn_position_valid(mp, constraints, allies_mp, enemies_mp)
        }
        current_mp = self.game_state.map.map_point
        if self._is_end_turn_position_valid(current_mp, constraints, allies_mp, enemies_mp):
            candidates.add(current_mp)
        return candidates

    def _choose_safe_target(
        self,
        candidates: set[MapPoint],
        current_mp: MapPoint,
        enemies_mp: set[MapPoint],
        enemies_data: list[EnemyData],
    ) -> MapPoint:
        def is_out_of_threat_range(cell_mp: MapPoint) -> bool:
            return all(
                cell_mp.distance_to_map_point(enemy.map_point) > enemy.movement_points + enemy.max_spell_range
                for enemy in enemies_data
            )

        safe_cells = [cell_mp for cell_mp in candidates if is_out_of_threat_range(cell_mp)]
        if safe_cells:
            target_mp = min(safe_cells, key=lambda cell_mp: cell_mp.distance_to_map_point(current_mp))
            self.logger.info(f"Found nearest out-of-threat-range cell : {target_mp}")
            return target_mp

        target_mp = max(
            candidates,
            key=lambda cell_mp: sum(cell_mp.distance_to_map_point(enemy_mp) for enemy_mp in enemies_mp),
        )
        self.logger.info(f"No cell out of threat range, falling back to farthest cell : {target_mp}")
        return target_mp

    def find_safest_path(self) -> MovementPath | None:
        constraints = self.game_state.challenge.constraints()
        if constraints.has(ChallengeRule.STAY_STILL):
            self.logger.info("Challenge requires staying still, skipping movement")
            return None

        if (
            FAST_FIGHT
            and not constraints.rules & _FAST_MODE_MOVEMENT_RULES
            and not self.game_state.fight.has_boss_enemy(self.game_state.player.character_id)
        ):
            self.logger.info("Fast fight mode: skipping defensive end-of-turn movement")
            return None

        current_mp = self.game_state.map.map_point
        if constraints.has(ChallengeRule.MELEE_ONLY):
            near_enemy = self.find_near_enemy_with_dist(current_mp)
            if near_enemy is None:
                return None
            _, move_path, _ = near_enemy
            if move_path.end.cell_id == current_mp.cell_id:
                return None
            self.logger.info(f"Melee challenge: moving next to enemy at cell {move_path.end.cell_id}")
            return move_path

        self.logger.info("Finding safest path")

        entities_mp: set[MapPoint] = {
            mp for mp, actors in self.game_state.entity.actors_on_mp.items() if len(actors) > 0
        } | {MapPoint.from_cell_id(cell_id) for cell_id in self.game_state.fight.invisible_enemy_cell_ids}
        enemies = self.game_state.fight.get_enemies(self.game_state.player.character_id)
        enemies_mp = {MapPoint.from_cell_id(enemy.disposition.cell_id) for enemy in enemies}

        self.logger.info(f"enemies mp : {enemies_mp}")
        if not enemies_mp:
            self.logger.info("No enemies left, no safe path to compute")
            return None

        reachable_mps = self.fight_reachable_cells.search(
            self.game_state.get_fight_reachable_context(),
            enemies_mp,
            entities_mp,
        )
        if not reachable_mps:
            reachable_mps = self.fight_reachable_cells.search_escape(
                self.game_state.get_fight_reachable_context(),
                entities_mp=entities_mp,
            )
            if not reachable_mps:
                self.logger.info("No reachable cell, even outside enemy tackle zones")
                return None
            self.logger.info("No safe reachable cell; using tackle-zone escape cells")

        enemies_data = self.game_state.fight.get_enemies_data(
            enemies, dict(self.game_state.entity.actor_fight_by_id)
        )

        candidates = set(reachable_mps)
        if constraints.rules & END_TURN_RULES:
            allies_mp = self._ally_map_points()
            end_turn_candidates = self._end_turn_candidates(
                constraints, reachable_mps, allies_mp, enemies_mp
            )
            if end_turn_candidates:
                self.logger.info(
                    f"Challenge end-turn cells: {len(end_turn_candidates)} reachable"
                )
                candidates = end_turn_candidates
            else:
                self.logger.info("No reachable cell satisfies the challenge end-turn position")

        target_mp = self._choose_safe_target(candidates, current_mp, enemies_mp, enemies_data)
        if target_mp == current_mp:
            self.logger.info("Already on a valid challenge end-turn cell")
            return None

        return self.path_finding.find_path(
            self.game_state.get_map_movement_context(),
            self.game_state.map.map_point,
            {target_mp},
            allow_trough_entity=False,
            allow_diag=False,
        )
