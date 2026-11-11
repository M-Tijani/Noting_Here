"""Candidate-action extraction shared by dataset building and live inference.

One extractor feeds both paths: live code passes the ``AttackContext`` built by
``GameState``, offline code first rebuilds the same context from a recorded
observation with :func:`attack_context_from_observation`. The frozen feature
names live in :mod:`src.services.play_dataset.features`.

``cand.kill_probability`` is a deterministic proxy: the largest share of an
affected enemy's current life removed by the candidate's expected damage,
capped at ``1.0``. It becomes a real probability only when the damage model is
stochastic.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import msgspec
from google.protobuf.json_format import ParseDict
from google.protobuf.message import Message

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import (
    ActorPositionInformation,
    CharacterCharacteristic,
    FightInvisibilityState,
    SpellModifier,
    SpellModifierType,
)
from DBDofusUnity.datas.protos.non_obf.game.spell_pb2 import SpellItem
from DBDofusUnity.dofus_unity_reader.game_constants.characteristic import EffectElement
from DBDofusUnity.dofus_unity_reader.game_constants.spell_shape_enum import SpellShapeEnum
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint
from DBDofusUnity.dofus_unity_reader.models.datas.monsters_root import (
    MonsterCharacteristic,
    MonsterGrade,
)
from DBDofusUnity.dofus_unity_reader.models.datas.spell_levels_root import (
    Effect,
    SpellLevelsRootItem,
)

from src.core.challenges.models import ChallengeRule, FightConstraints
from src.core.engine.contexts import AttackContext
from src.core.engine.fights.attack.cast_validator import can_cast_spell_on_mp
from src.core.engine.fights.attack.enemy_data import EnemyData
from src.core.engine.fights.attack.positions import (
    AttackPositions,
    get_movable_mps,
    get_positions,
)
from src.core.engine.fights.attack.rejection_stat import RejectionStat
from src.core.engine.fights.attack.spell_filter import get_valid_spells_for_turn
from src.core.engine.fights.attack.weight_calculator import (
    estimate_damage_at,
    spell_damage_effects,
)
from src.core.engine.fights.damage_calculator import DamageCalculator
from src.core.engine.fights.effect import is_push_effect, resolve_effect_element
from src.core.engine.fights.reachable_cells.fight_reachable_cells import (
    FightReachableCells,
)
from src.core.engine.fights.spell import (
    does_spell_need_taken_cell,
    get_possible_mp_spell,
)
from src.core.engine.fights.spell_modifier import SpellModifiers
from src.core.engine.fights.spell_zone import get_zone_mps
from src.core.engine.fights.zones.zone import Zone
from src.services.play_dataset.features import FEATURES

FeatureValue = int | float | bool | str

CAST_ACTIONS = frozenset({"cast", "cast_on_target"})
_ACTION_ORDER = {"ready": 0, "pass_turn": 1, "move": 2, "cast": 3, "cast_on_target": 4}

_MessageT = TypeVar("_MessageT", bound=Message)


@dataclass(frozen=True, slots=True)
class Candidate:
    action_type: str
    spell_id: int = 0
    cell_id: int = 0
    target_actor_id: int = 0
    element: int = -1
    ap_cost: int = 0
    mp_cost: int = 0
    range: int = 0
    target_distance: int = 0
    damage_raw: float = 0.0
    damage_resist_adjusted: float = 0.0
    kill_probability: float = 0.0
    is_kill: bool = False
    cooldown_remaining: int = 0
    casts_this_turn: int = 0
    move_distance: int = 0
    positioning_delta: int = 0
    target_life_fraction: float = 0.0
    challenge_violation: bool = False

    def to_feature_values(self) -> dict[str, FeatureValue]:
        return {
            "cand.action_type": self.action_type,
            "cand.spell_id": self.spell_id,
            "cand.cell_id": self.cell_id,
            "cand.target_actor_id": self.target_actor_id,
            "cand.element": self.element,
            "cand.ap_cost": self.ap_cost,
            "cand.mp_cost": self.mp_cost,
            "cand.range": self.range,
            "cand.target_distance": self.target_distance,
            "cand.damage_raw": self.damage_raw,
            "cand.damage_resist_adjusted": self.damage_resist_adjusted,
            "cand.kill_probability": self.kill_probability,
            "cand.is_kill": self.is_kill,
            "cand.cooldown_remaining": self.cooldown_remaining,
            "cand.casts_this_turn": self.casts_this_turn,
            "cand.move_distance": self.move_distance,
            "cand.positioning_delta": self.positioning_delta,
            "cand.target_life_fraction": self.target_life_fraction,
            "cand.challenge_violation": self.challenge_violation,
        }


def observation_feature_values(context: AttackContext) -> dict[str, FeatureValue]:
    alive_enemies = [enemy for enemy in context.enemies_data if enemy.life_point > 0]
    min_life_fraction = min(
        (
            enemy.life_point / enemy.max_life_point
            for enemy in alive_enemies
            if enemy.max_life_point > 0
        ),
        default=0.0,
    )
    return {
        "obs.fight_turn": context.fight_turn,
        "obs.map_id": context.map_id,
        "obs.player.level": context.player_level,
        "obs.player.breed_id": context.breed_id,
        "obs.player.action_points": context.action_points,
        "obs.player.movement_points": context.movement_points,
        "obs.player.range": context.range,
        "obs.player.life_point": context.life_point,
        "obs.player.max_life_point": context.max_life_point,
        "obs.player.life_fraction": context.life_percentage,
        "obs.player.primary_elem": int(context.primary_elem),
        "obs.player.cell_id": context.player_map_point.cell_id,
        "obs.own.state_count": len(context.own_state_ids),
        "obs.own.summon_count": context.own_active_summon_count,
        "obs.own.max_summon_count": context.max_active_summon_count,
        "obs.spells.count": len(context.spells),
        "obs.enemies.count": len(context.enemy_actors),
        "obs.enemies.alive_count": len(alive_enemies),
        "obs.enemies.total_life": sum(max(enemy.life_point, 0) for enemy in context.enemies_data),
        "obs.enemies.min_life_fraction": min_life_fraction,
        "obs.enemies.max_spell_range": max(
            (enemy.max_spell_range for enemy in context.enemies_data), default=0
        ),
        "obs.enemies.invisible_count": len(context.invisible_enemy_cell_ids),
    }


def feature_row(context: AttackContext, candidate: Candidate) -> dict[str, FeatureValue]:
    values = observation_feature_values(context)
    values.update(candidate.to_feature_values())
    missing = [feature.name for feature in FEATURES if feature.name not in values]
    if missing:
        raise ValueError(f"candidate feature contract is missing values for: {missing}")
    return {feature.name: values[feature.name] for feature in FEATURES}


def feature_vector(context: AttackContext, candidate: Candidate) -> list[FeatureValue]:
    row = feature_row(context, candidate)
    return [row[feature.name] for feature in FEATURES]


def extract_candidates(
    context: AttackContext,
    *,
    fight_reachable_cells: FightReachableCells | None = None,
    damage_calculator: DamageCalculator | None = None,
    include_ready: bool = True,
) -> list[Candidate]:
    positions = get_positions(context)
    movable_mps = get_movable_mps(
        context,
        positions,
        fight_reachable_cells if fight_reachable_cells is not None else FightReachableCells(),
    )
    valid_spells = get_valid_spells_for_turn(context)
    calculator = damage_calculator if damage_calculator is not None else DamageCalculator()

    candidates = _movement_candidates(context, movable_mps)
    candidates.extend(
        _cast_candidates(context, positions, movable_mps, valid_spells, calculator)
    )
    candidates.append(_pass_turn_candidate())
    if include_ready:
        candidates.append(_ready_candidate())
    candidates.sort(key=_candidate_sort_key)
    return candidates


def match_action(
    candidates: Sequence[Candidate], action: Mapping[str, object]
) -> Candidate | None:
    action_type = action.get("type")
    if action_type in CAST_ACTIONS:
        spell_id = _as_int(action.get("spell_id"))
        matches = [
            candidate
            for candidate in candidates
            if candidate.action_type in CAST_ACTIONS and candidate.spell_id == spell_id
        ]
        target_id = action.get("target_id")
        if target_id is not None:
            target_actor_id = _as_int(target_id)
            matches = [
                candidate
                for candidate in matches
                if candidate.target_actor_id == target_actor_id
            ]
        elif action.get("cell_id") is not None:
            cell_id = _as_int(action.get("cell_id"))
            matches = [candidate for candidate in matches if candidate.cell_id == cell_id]
        matches.sort(
            key=lambda candidate: (
                candidate.mp_cost,
                candidate.target_distance,
                candidate.challenge_violation,
            )
        )
        return matches[0] if matches else None

    if action_type == "move":
        destination = action.get("destination_cell_id")
        if destination is None:
            key_cells = action.get("key_cells")
            if isinstance(key_cells, list) and key_cells:
                destination = cast("list[object]", key_cells)[-1]
        if destination is None:
            return None
        destination_cell_id = _as_int(destination)
        return next(
            (
                candidate
                for candidate in candidates
                if candidate.action_type == "move"
                and candidate.cell_id == destination_cell_id
            ),
            None,
        )

    if action_type == "pass_turn":
        return next(
            (candidate for candidate in candidates if candidate.action_type == "pass_turn"),
            None,
        )
    if action_type in {"ready", "turn_ready"}:
        return next(
            (candidate for candidate in candidates if candidate.action_type == "ready"),
            None,
        )
    return None


def attack_context_from_observation(observation: Mapping[str, object]) -> AttackContext:
    player = _as_mapping(observation.get("player"))
    if player is None:
        raise ValueError("observation is missing the player object")

    player_cell_id = _as_int(observation.get("player_cell_id", player.get("cell_id")))
    life_point = _as_int(player.get("life_point"))
    max_life_point = _as_int(player.get("max_life_point"))
    life_percentage = _as_float(
        player.get("life_percentage"),
        life_point / max_life_point if max_life_point else 0.0,
    )
    actors = _deserialize_actors(observation.get("actors"))
    enemy_payloads = _enemy_payloads(observation.get("enemies"))
    enemies = _deserialize_enemies(enemy_payloads, actors)

    return AttackContext(
        map_id=_as_int(observation.get("map_id")),
        player_map_point=MapPoint.from_cell_id(player_cell_id),
        player_character_id=_as_int(player.get("character_id")),
        player_level=_as_int(player.get("level")),
        actor_by_id=actors,
        enemy_actors=[
            ActorPositionInformation(
                actor_id=_as_int(enemy.get("actor_id")),
                disposition={"cell_id": _as_int(enemy.get("cell_id"))},
            )
            for enemy in enemy_payloads
        ],
        enemies_data=enemies,
        spells=[
            _parse_message(payload, SpellItem)
            for payload in _list(observation.get("spells"))
        ],
        primary_elem=EffectElement(_as_int(observation.get("primary_elem"))),
        modifier_by_type_and_spell_id=_deserialize_modifiers(
            observation.get("modifier_by_type_and_spell_id")
        ),
        count_casted_by_spell_id_on_current_turn={
            _as_int(spell_id): _as_int(count)
            for spell_id, count in _dict(
                observation.get("count_casted_by_spell_id_on_current_turn")
            ).items()
        },
        cast_turn_by_spell_id={
            _as_int(spell_id): _as_int(turn)
            for spell_id, turn in _dict(observation.get("cast_turn_by_spell_id")).items()
        },
        fight_turn=_as_int(observation.get("fight_turn")),
        characteristic_by_id=_deserialize_characteristics(
            observation.get("characteristics")
        ),
        action_points=_as_int(player.get("action_points")),
        movement_points=_as_int(player.get("movement_points")),
        range=_as_int(player.get("range")),
        life_point=life_point,
        max_life_point=max_life_point,
        life_percentage=life_percentage,
        invisible_enemy_cell_ids=frozenset(
            _as_int(cell_id)
            for cell_id in _list(observation.get("invisible_enemy_cell_ids"))
        ),
        breed_id=_as_int(player.get("breed_id")),
        own_state_ids=frozenset(
            _as_int(state_id) for state_id in _list(observation.get("own_state_ids"))
        ),
        own_active_stack_count_by_spell_id={
            _as_int(spell_id): _as_int(count)
            for spell_id, count in _dict(
                observation.get("own_active_stack_count_by_spell_id")
            ).items()
        },
        own_active_summon_count=_as_int(observation.get("own_active_summon_count")),
        max_active_summon_count=_as_int(observation.get("max_active_summon_count")),
        constraints=_deserialize_constraints(observation.get("constraints")),
    )


def _movement_candidates(
    context: AttackContext, movable_mps: Mapping[MapPoint, int]
) -> list[Candidate]:
    before = _nearest_enemy_distance(context, context.player_map_point)
    return [
        Candidate(
            action_type="move",
            cell_id=mp.cell_id,
            mp_cost=context.movement_points - remaining_pm,
            range=context.movement_points,
            target_distance=int(
                context.player_map_point.distance_to_map_point(mp)
            ),
            move_distance=context.movement_points - remaining_pm,
            positioning_delta=_positioning_delta(
                before, _nearest_enemy_distance(context, mp)
            ),
            challenge_violation=context.constraints.has(ChallengeRule.STAY_STILL),
        )
        for mp, remaining_pm in movable_mps.items()
        if mp != context.player_map_point
    ]


def _cast_candidates(
    context: AttackContext,
    positions: AttackPositions,
    movable_mps: Mapping[MapPoint, int],
    valid_spells: Sequence[tuple[SpellLevelsRootItem, Effect, SpellModifiers]],
    damage_calculator: DamageCalculator,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    modifiers_map = context.modifier_by_type_and_spell_id

    for spell_lvl, effect, modifiers in valid_spells:
        zone_size = effect.zoneDescr.param1
        needs_taken_cell = does_spell_need_taken_cell(spell_lvl) or zone_size == 0
        shape = SpellShapeEnum(effect.zoneDescr.shape)
        damage_effects = spell_damage_effects(spell_lvl, effect, context.primary_elem)

        for movable_mp, remaining_pm in movable_mps.items():
            zone_spell = get_zone_mps(
                shape=shape,
                alternative_size=effect.zoneDescr.param2,
                size=zone_size,
                caster_mp=movable_mp,
                stop_at_target=bool(effect.zoneDescr.isStopAtTarget),
            )
            targetable_mps = _targetable_mps(
                movable_mp,
                spell_lvl,
                zone_spell,
                needs_taken_cell,
                positions,
                context.range,
                modifiers_map,
            )
            for target_mp in sorted(targetable_mps, key=lambda mp: mp.cell_id):
                rejection_stats: dict[RejectionStat, int] = defaultdict(int)
                if not can_cast_spell_on_mp(
                    context,
                    movable_mp,
                    spell_lvl,
                    target_mp,
                    modifiers,
                    positions.entities_mp,
                    positions.entities_id_by_mp,
                    rejection_stats,
                ):
                    continue
                direction = movable_mp.orientation_to(target_mp)
                impact_mps = zone_spell.get_mps(target_mp, direction)
                candidates.append(
                    _cast_candidate(
                        context,
                        positions,
                        movable_mp,
                        remaining_pm,
                        spell_lvl,
                        effect,
                        target_mp,
                        impact_mps,
                        modifiers,
                        damage_effects,
                        damage_calculator,
                    )
                )
    return candidates


def _targetable_mps(
    movable_mp: MapPoint,
    spell_lvl: SpellLevelsRootItem,
    zone_spell: Zone,
    needs_taken_cell: bool,
    positions: AttackPositions,
    player_range: int,
    modifiers_map: dict[tuple[int, SpellModifierType], SpellModifier],
) -> set[MapPoint]:
    spell_id = spell_lvl.spellId
    targetable_mps = get_possible_mp_spell(
        movable_mp,
        spell_lvl,
        player_range,
        modifier_range_min=modifiers_map.get((spell_id, SpellModifierType.RANGE_MIN)),
        modifier_range_max=modifiers_map.get((spell_id, SpellModifierType.RANGE_MAX)),
        modifier_cast_line=modifiers_map.get((spell_id, SpellModifierType.CAST_LINE)),
    )
    if needs_taken_cell:
        return targetable_mps & positions.entities_mp
    return {
        mp
        for enemy_mp in positions.enemies_mp
        for mp in zone_spell.get_mps(enemy_mp, enemy_mp.orientation_to(movable_mp))
        if mp in targetable_mps
    }


def _cast_candidate(
    context: AttackContext,
    positions: AttackPositions,
    movable_mp: MapPoint,
    remaining_pm: int,
    spell_lvl: SpellLevelsRootItem,
    effect: Effect,
    target_mp: MapPoint,
    impact_mps: set[MapPoint],
    modifiers: SpellModifiers,
    damage_effects: list[Effect],
    damage_calculator: DamageCalculator,
) -> Candidate:
    raw_damage = 0.0
    adjusted_damage = 0.0
    kill_probability = 0.0
    is_kill = False
    primary: EnemyData | None = None
    primary_damage = -1.0
    affected_enemies = 0

    for enemy in context.enemies_data:
        if enemy.map_point not in impact_mps:
            continue
        if enemy.is_invulnerable or enemy.is_hidden or enemy.life_point <= 0:
            continue
        affected_enemies += 1
        adjusted = estimate_damage_at(
            damage_calculator,
            context,
            spell_lvl,
            effect,
            damage_effects,
            target_mp,
            enemy.map_point,
            enemy.monster_grade,
        )
        raw = estimate_damage_at(
            damage_calculator,
            context,
            spell_lvl,
            effect,
            damage_effects,
            target_mp,
            enemy.map_point,
            None,
        )
        raw_damage += raw
        adjusted_damage += adjusted
        kill_probability = max(kill_probability, min(adjusted / enemy.life_point, 1.0))
        if adjusted >= enemy.life_point:
            is_kill = True
        if adjusted > primary_damage:
            primary = enemy
            primary_damage = adjusted

    target_actor_id = positions.entities_id_by_mp.get(target_mp, 0)
    element = resolve_effect_element(effect.effectElement, context.primary_elem)
    target_distance = int(movable_mp.distance_to_map_point(target_mp))
    mp_cost = context.movement_points - remaining_pm
    before = _nearest_enemy_distance(context, context.player_map_point)

    return Candidate(
        action_type="cast_on_target" if target_actor_id else "cast",
        spell_id=spell_lvl.spellId,
        cell_id=target_mp.cell_id,
        target_actor_id=target_actor_id,
        element=element if element is not None else -1,
        ap_cost=modifiers.ap_cost,
        mp_cost=mp_cost,
        range=modifiers.range_max,
        target_distance=target_distance,
        damage_raw=raw_damage,
        damage_resist_adjusted=adjusted_damage,
        kill_probability=kill_probability,
        is_kill=is_kill,
        cooldown_remaining=_cooldown_remaining(context, spell_lvl),
        casts_this_turn=context.count_casted_by_spell_id_on_current_turn.get(
            spell_lvl.spellId, 0
        ),
        move_distance=mp_cost,
        positioning_delta=_positioning_delta(
            before, _nearest_enemy_distance(context, movable_mp)
        ),
        target_life_fraction=(
            primary.life_point / primary.max_life_point
            if primary is not None and primary.max_life_point > 0
            else 0.0
        ),
        challenge_violation=_violates_challenge(
            context,
            spell_lvl,
            effect,
            impact_mps,
            target_actor_id,
            element,
            target_distance,
            mp_cost,
            affected_enemies,
            is_kill,
            primary,
        ),
    )


def _pass_turn_candidate() -> Candidate:
    return Candidate(
        action_type="pass_turn",
        cell_id=0,
        range=0,
        challenge_violation=False,
    )


def _ready_candidate() -> Candidate:
    return Candidate(
        action_type="ready",
        cell_id=0,
        range=0,
        challenge_violation=False,
    )


def _cooldown_remaining(context: AttackContext, spell_lvl: SpellLevelsRootItem) -> int:
    remaining = 0
    if spell_lvl.initialCooldown and context.fight_turn <= spell_lvl.initialCooldown:
        remaining = max(remaining, spell_lvl.initialCooldown - context.fight_turn + 1)

    last_cast_turn = context.cast_turn_by_spell_id.get(spell_lvl.spellId)
    if last_cast_turn is not None:
        elapsed = context.fight_turn - last_cast_turn
        if spell_lvl.globalCooldown:
            remaining = max(remaining, spell_lvl.globalCooldown - elapsed)
        if spell_lvl.minCastInterval:
            remaining = max(remaining, spell_lvl.minCastInterval - elapsed)
    return max(remaining, 0)


def _nearest_enemy_distance(context: AttackContext, from_mp: MapPoint) -> int | None:
    distances = [
        int(from_mp.distance_to_map_point(enemy.map_point))
        for enemy in context.enemies_data
        if enemy.life_point > 0
    ]
    distances.extend(
        int(from_mp.distance_to_map_point(MapPoint.from_cell_id(cell_id)))
        for cell_id in context.invisible_enemy_cell_ids
    )
    return min(distances) if distances else None


def _positioning_delta(before: int | None, after: int | None) -> int:
    if before is None or after is None:
        return 0
    return after - before


def _violates_challenge(
    context: AttackContext,
    spell_lvl: SpellLevelsRootItem,
    effect: Effect,
    impact_mps: set[MapPoint],
    target_actor_id: int,
    element: int | None,
    target_distance: int,
    mp_cost: int,
    affected_enemies: int,
    is_kill: bool,
    primary: EnemyData | None,
) -> bool:
    constraints = context.constraints
    if (
        constraints.has(ChallengeRule.SINGLE_ELEMENT)
        and constraints.damage_element is not None
        and element != int(constraints.damage_element)
    ):
        return True
    if (
        constraints.has(ChallengeRule.TARGET_ONLY)
        and constraints.target_ids
        and target_actor_id not in constraints.target_ids
    ):
        return True
    if (
        constraints.has(ChallengeRule.FOCUS_FIRE)
        and constraints.attacked_enemy_ids
        and target_actor_id not in constraints.attacked_enemy_ids
    ):
        return True
    if constraints.has(ChallengeRule.STAY_STILL) and mp_cost > 0:
        return True
    if constraints.has(ChallengeRule.MELEE_ONLY) and target_distance > 1:
        return True
    if constraints.has(ChallengeRule.RANGED_ONLY) and target_distance <= 1:
        return True
    if constraints.has(ChallengeRule.NO_AOE) and affected_enemies > 1:
        return True
    if constraints.has(ChallengeRule.SINGLE_TARGET) and affected_enemies > 1:
        return True
    if (
        constraints.has(ChallengeRule.NO_PUSH)
        and affected_enemies > 0
        and _spell_has_push(spell_lvl, effect)
    ):
        return True
    if (
        constraints.has(ChallengeRule.NO_ALLY_DEATH)
        and _impact_hits_ally(context, impact_mps)
    ):
        return True
    if (
        constraints.has(ChallengeRule.KILL_TARGET_FIRST)
        and is_kill
        and constraints.target_ids
        and target_actor_id not in constraints.target_ids
    ):
        return True
    if (
        constraints.has(ChallengeRule.KILL_TARGET_LAST)
        and is_kill
        and primary is not None
        and _other_alive_enemy_count(context, primary) > 0
    ):
        return True
    if (
        constraints.has(ChallengeRule.KILL_LOW_TO_HIGH)
        and is_kill
        and primary is not None
        and _has_lower_life_enemy(context, primary)
    ):
        return True
    if (
        constraints.has(ChallengeRule.KILL_HIGH_TO_LOW)
        and is_kill
        and primary is not None
        and _has_higher_life_enemy(context, primary)
    ):
        return True
    return False


def _spell_has_push(spell_lvl: SpellLevelsRootItem, representative: Effect) -> bool:
    return any(
        is_push_effect(candidate_effect)
        for candidate_effect in spell_lvl.effects
        if candidate_effect.zoneDescr == representative.zoneDescr
    )


def _impact_hits_ally(context: AttackContext, impact_mps: set[MapPoint]) -> bool:
    enemy_ids = {actor.actor_id for actor in context.enemy_actors}
    return any(
        actor.actor_id != context.player_character_id
        and actor.actor_id not in enemy_ids
        and actor.disposition.cell_id != -1
        and MapPoint.from_cell_id(actor.disposition.cell_id) in impact_mps
        for actor in context.actor_by_id.values()
    )


def _other_alive_enemy_count(context: AttackContext, primary: EnemyData) -> int:
    return sum(
        1
        for enemy in context.enemies_data
        if enemy.life_point > 0 and enemy.actor.actor_id != primary.actor.actor_id
    )


def _has_lower_life_enemy(context: AttackContext, primary: EnemyData) -> bool:
    primary_fraction = _life_fraction(primary)
    return any(
        enemy.life_point > 0
        and enemy.actor.actor_id != primary.actor.actor_id
        and _life_fraction(enemy) < primary_fraction
        for enemy in context.enemies_data
    )


def _has_higher_life_enemy(context: AttackContext, primary: EnemyData) -> bool:
    primary_fraction = _life_fraction(primary)
    return any(
        enemy.life_point > 0
        and enemy.actor.actor_id != primary.actor.actor_id
        and _life_fraction(enemy) > primary_fraction
        for enemy in context.enemies_data
    )


def _life_fraction(enemy: EnemyData) -> float:
    return enemy.life_point / enemy.max_life_point if enemy.max_life_point > 0 else 0.0


def _candidate_sort_key(candidate: Candidate) -> tuple[int, int, int, int, int, int]:
    return (
        _ACTION_ORDER.get(candidate.action_type, len(_ACTION_ORDER)),
        candidate.spell_id,
        candidate.mp_cost,
        candidate.cell_id,
        candidate.target_actor_id,
        candidate.ap_cost,
    )


def _parse_message(payload: object, message_type: type[_MessageT]) -> _MessageT:
    message = message_type()
    ParseDict(cast("dict[str, Any]", _dict(payload)), message, ignore_unknown_fields=False)
    return message


def _deserialize_actors(payload: object) -> dict[int, ActorPositionInformation]:
    return {
        _as_int(actor_id): _parse_message(actor, ActorPositionInformation)
        for actor_id, actor in _dict(payload).items()
    }


def _deserialize_characteristics(payload: object) -> dict[int, CharacterCharacteristic]:
    return {
        _as_int(characteristic_id): _parse_message(
            characteristic, CharacterCharacteristic
        )
        for characteristic_id, characteristic in _dict(payload).items()
    }


def _deserialize_modifiers(
    payload: object,
) -> dict[tuple[int, SpellModifierType], SpellModifier]:
    modifiers: dict[tuple[int, int], SpellModifier] = {}
    for item in _list(payload):
        modifier = _dict(item)
        spell_id = _as_int(modifier.get("spell_id"))
        modifier_type = _as_int(modifier.get("modifier_type"))
        modifiers[(spell_id, modifier_type)] = _parse_message(
            modifier.get("modifier"), SpellModifier
        )
    return cast("dict[tuple[int, SpellModifierType], SpellModifier]", modifiers)


def _deserialize_enemies(
    payload: object, actors: Mapping[int, ActorPositionInformation]
) -> list[EnemyData]:
    enemies: list[EnemyData] = []
    for item in _enemy_payloads(payload):
        actor_id = _as_int(item.get("actor_id"))
        cell_id = _as_int(item.get("cell_id"))
        actor = actors.get(actor_id) or ActorPositionInformation(
            actor_id=actor_id, disposition={"cell_id": cell_id}
        )
        grade = _deserialize_grade(item)
        enemies.append(
            EnemyData(
                actor=actor,
                map_point=MapPoint.from_cell_id(cell_id),
                life_point=_as_int(item.get("life_point")),
                max_life_point=_as_int(
                    item.get("max_life_point"), grade.lifePoints
                ),
                is_summoned=_as_bool(item.get("is_summoned")),
                monster_grade=grade,
                movement_points=_as_int(
                    item.get("movement_points"), grade.movementPoints
                ),
                max_spell_range=_as_int(item.get("max_spell_range")),
                invisibility=cast(
                    "FightInvisibilityState",
                    _as_int(item.get("invisibility"), FightInvisibilityState.VISIBLE),
                ),
                state_ids=frozenset(
                    _as_int(state) for state in _list(item.get("state_ids"))
                ),
            )
        )
    return enemies


def _deserialize_grade(payload: Mapping[str, object]) -> MonsterGrade:
    grade_payload = payload.get("monster_grade")
    if isinstance(grade_payload, dict):
        return msgspec.convert(grade_payload, MonsterGrade)

    monster = _as_mapping(payload.get("monster"))
    if monster is None:
        raise ValueError("enemy is missing both monster_grade and monster fields")
    resistances = _dict(monster.get("resistances"))
    return MonsterGrade(
        bonusCharacteristics=_zero_bonus_characteristic(),
        grade=_as_int(monster.get("grade")),
        monsterId=_as_int(monster.get("id")),
        level=_as_int(monster.get("level")),
        lifePoints=_as_int(monster.get("life_points")),
        actionPoints=_as_int(monster.get("action_points")),
        movementPoints=_as_int(monster.get("movement_points")),
        vitality=0,
        paDodge=0,
        pmDodge=0,
        wisdom=0,
        earthResistance=_as_int(resistances.get("earth")),
        airResistance=_as_int(resistances.get("air")),
        fireResistance=_as_int(resistances.get("fire")),
        waterResistance=_as_int(resistances.get("water")),
        neutralResistance=_as_int(resistances.get("neutral")),
        gradeXp=0,
        damageReflect=0,
        hiddenLevel=None,
        strength=0,
        intelligence=0,
        chance=0,
        agility=0,
        startingSpellId=_as_int(monster.get("starting_spell_id")),
        bonusRange=_as_int(monster.get("bonus_range")),
    )


def _zero_bonus_characteristic() -> MonsterCharacteristic:
    return MonsterCharacteristic(
        lifePoints=0,
        strength=0,
        wisdom=0,
        chance=0,
        agility=0,
        intelligence=0,
        earthResistance=0,
        fireResistance=0,
        waterResistance=0,
        airResistance=0,
        neutralResistance=0,
        tackleEvade=0,
        tackleBlock=0,
        bonusEarthDamage=0,
        bonusFireDamage=0,
        bonusWaterDamage=0,
        bonusAirDamage=0,
        aPRemoval=0,
    )


def _deserialize_constraints(payload: object) -> FightConstraints:
    constraints = _as_mapping(payload)
    if constraints is None:
        return FightConstraints()
    damage_element = constraints.get("damage_element")
    return FightConstraints(
        rules=frozenset(
            ChallengeRule(str(rule)) for rule in _list(constraints.get("rules"))
        ),
        target_ids=frozenset(
            _as_int(target) for target in _list(constraints.get("target_ids"))
        ),
        damage_element=(
            EffectElement(_as_int(damage_element))
            if damage_element is not None
            else None
        ),
        attacked_enemy_ids=frozenset(
            _as_int(enemy_id)
            for enemy_id in _list(constraints.get("attacked_enemy_ids"))
        ),
    )


def _enemy_payloads(payload: object) -> list[dict[str, object]]:
    return [_dict(item) for item in _list(payload)]


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return False


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, dict):
        return cast("Mapping[str, object]", value)
    return None


def _list(value: object) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"expected a list in observation, got {type(value).__name__}")
    return cast("list[object]", value)


def _dict(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in observation, got {type(value).__name__}")
    return cast("dict[str, object]", value)


__all__ = [
    "Candidate",
    "attack_context_from_observation",
    "extract_candidates",
    "feature_row",
    "feature_vector",
    "match_action",
    "observation_feature_values",
]
