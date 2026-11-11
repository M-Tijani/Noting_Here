from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint
from DBDofusUnity.dofus_unity_reader.models.datas.spell_levels_root import (
    Effect,
    SpellLevelsRootItem,
)
from src.core.challenges.models import ChallengeRule, FightConstraints
from src.core.engine.contexts import AttackContext
from src.core.engine.fights.attack.enemy_data import EnemyData
from src.core.engine.fights.attack.positions import AttackPositions
from src.core.engine.fights.effect import (
    can_target_enemy,
    is_ap_removal_effect,
    is_heal_effect,
    is_mp_removal_effect,
    is_push_effect,
    is_summon_effect,
    resolve_effect_element,
)

TARGET_PRIORITY_BOOST = 1.5
TARGET_PRIORITY_PENALTY = 0.5

END_TURN_RULES = frozenset(
    {
        ChallengeRule.END_NEXT_TO_ALLY,
        ChallengeRule.END_IN_LINE_WITH_ALLY,
        ChallengeRule.AVOID_ALLY_ADJACENT,
        ChallengeRule.AVOID_ENEMY_ADJACENT,
        ChallengeRule.AVOID_ALLY_LINE,
        ChallengeRule.AVOID_ENEMY_LINE,
    }
)


def get_constraints(context: AttackContext) -> FightConstraints:
    return getattr(context, "constraints", FightConstraints())


def has_end_turn_rules(context: AttackContext) -> bool:
    return bool(get_constraints(context).rules & END_TURN_RULES)


def _allies_mp(context: AttackContext) -> set[MapPoint]:
    enemy_ids = {actor.actor_id for actor in context.enemy_actors}
    return {
        MapPoint.from_cell_id(actor.disposition.cell_id)
        for actor in context.actor_by_id.values()
        if actor.actor_id != context.player_character_id
        and actor.actor_id not in enemy_ids
        and actor.disposition.cell_id != -1
    }


def _enemies_mp(context: AttackContext) -> set[MapPoint]:
    enemies_mp = {
        MapPoint.from_cell_id(actor.disposition.cell_id)
        for actor in context.enemy_actors
        if actor.disposition.cell_id != -1
    }
    invisible_cell_ids: frozenset[int] = getattr(
        context, "invisible_enemy_cell_ids", frozenset[int]()
    )
    enemies_mp.update(MapPoint.from_cell_id(cell_id) for cell_id in invisible_cell_ids)
    return enemies_mp


def is_end_turn_position_valid(
    constraints: FightConstraints,
    mp: MapPoint,
    allies_mp: set[MapPoint],
    enemies_mp: set[MapPoint],
) -> bool:
    if (
        constraints.has(ChallengeRule.END_NEXT_TO_ALLY)
        and allies_mp
        and not (mp.side_map_points & allies_mp)
    ):
        return False
    if (
        constraints.has(ChallengeRule.END_IN_LINE_WITH_ALLY)
        and allies_mp
        and not any(mp.x == ally.x or mp.y == ally.y for ally in allies_mp)
    ):
        return False
    if constraints.has(ChallengeRule.AVOID_ALLY_LINE) and any(
        mp.x == ally.x or mp.y == ally.y for ally in allies_mp
    ):
        return False
    if constraints.has(ChallengeRule.AVOID_ENEMY_LINE) and any(
        mp.x == enemy.x or mp.y == enemy.y for enemy in enemies_mp
    ):
        return False
    if constraints.has(ChallengeRule.AVOID_ALLY_ADJACENT) and mp.side_map_points & allies_mp:
        return False
    if constraints.has(ChallengeRule.AVOID_ENEMY_ADJACENT) and mp.side_map_points & enemies_mp:
        return False
    return True


def is_end_turn_position_forbidden(context: AttackContext, mp: MapPoint) -> bool:
    constraints = get_constraints(context)
    if not constraints.rules & END_TURN_RULES:
        return False
    return not is_end_turn_position_valid(constraints, mp, _allies_mp(context), _enemies_mp(context))


def allowed_target_ids(context: AttackContext) -> frozenset[int] | None:
    constraints = get_constraints(context)
    alive_ids = frozenset(
        enemy.actor.actor_id for enemy in context.enemies_data if enemy.life_point > 0
    )
    restricted: frozenset[int] | None = None

    def apply(ids: frozenset[int]) -> None:
        nonlocal restricted
        restricted = ids if restricted is None else restricted & ids

    if constraints.has(ChallengeRule.TARGET_ONLY) or constraints.has(ChallengeRule.KILL_TARGET_FIRST):
        targets = constraints.target_ids & alive_ids
        if targets:
            apply(targets)

    if constraints.has(ChallengeRule.KILL_HIGH_TO_LOW) or constraints.has(
        ChallengeRule.KILL_LOW_TO_HIGH
    ):
        alive = [enemy for enemy in context.enemies_data if enemy.life_point > 0]
        if alive:
            levels = [enemy.monster_grade.level for enemy in alive]
            extreme = max(levels) if constraints.has(ChallengeRule.KILL_HIGH_TO_LOW) else min(levels)
            apply(
                frozenset(
                    enemy.actor.actor_id
                    for enemy in alive
                    if enemy.monster_grade.level == extreme
                )
            )

    if constraints.has(ChallengeRule.FOCUS_FIRE) or constraints.has(ChallengeRule.KILL_BEFORE_TURN):
        focus_id = constraints.first_attacked_enemy_id
        if focus_id in alive_ids:
            apply(frozenset({focus_id}))

    if constraints.has(ChallengeRule.DUEL):
        forbidden = constraints.ally_attacked_enemy_ids - constraints.player_attacked_enemy_ids
        apply(alive_ids - forbidden)

    if constraints.has(ChallengeRule.KILL_TARGET_LAST):
        non_targets = alive_ids - constraints.target_ids
        if non_targets:
            apply(non_targets)

    return restricted


def allowed_enemies_data(context: AttackContext) -> list[EnemyData]:
    allowed_ids = allowed_target_ids(context)
    if allowed_ids is None:
        return context.enemies_data
    return [
        enemy
        for enemy in context.enemies_data
        if enemy.actor.actor_id in allowed_ids and enemy.life_point > 0
    ]


def allowed_enemy_mps(context: AttackContext, positions: AttackPositions) -> set[MapPoint]:
    allowed_ids = allowed_target_ids(context)
    if allowed_ids is None:
        return positions.enemies_mp
    return {
        enemy.map_point
        for enemy in context.enemies_data
        if enemy.actor.actor_id in allowed_ids and enemy.life_point > 0
    }


def is_impact_forbidden(context: AttackContext, impact_mps: set[MapPoint]) -> bool:
    allowed_ids = allowed_target_ids(context)
    if allowed_ids is not None and any(
        enemy.life_point > 0
        and enemy.map_point in impact_mps
        and enemy.actor.actor_id not in allowed_ids
        for enemy in context.enemies_data
    ):
        return True

    return get_constraints(context).has(ChallengeRule.NO_ALLY_DEATH) and _impact_hits_ally(
        context, impact_mps
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


def is_attack_spell_forbidden(
    context: AttackContext,
    spell_lvl: SpellLevelsRootItem,
    representative_effect: Effect,
) -> bool:
    constraints = get_constraints(context)

    if constraints.has(ChallengeRule.NO_HEAL) and any(
        is_heal_effect(effect) for effect in spell_lvl.effects
    ):
        return True

    if constraints.has(ChallengeRule.NO_AP_REMOVAL) and any(
        is_ap_removal_effect(effect) for effect in spell_lvl.effects
    ):
        return True

    if constraints.has(ChallengeRule.NO_MP_REMOVAL) and any(
        is_mp_removal_effect(effect) for effect in spell_lvl.effects
    ):
        return True

    if constraints.has(ChallengeRule.NO_SUMMON) and any(
        is_summon_effect(effect) for effect in spell_lvl.effects
    ):
        return True

    if constraints.has(ChallengeRule.NO_PUSH) and any(
        effect.zoneDescr == representative_effect.zoneDescr
        and is_push_effect(effect)
        for effect in spell_lvl.effects
    ):
        return True

    if (
        constraints.has(ChallengeRule.NO_AOE) or constraints.has(ChallengeRule.SINGLE_TARGET)
    ) and representative_effect.zoneDescr.param1 > 0:
        return True

    if constraints.has(ChallengeRule.SINGLE_ELEMENT) and constraints.damage_element is not None:
        allowed_element = int(constraints.damage_element)
        for effect in spell_lvl.effects:
            if (
                not can_target_enemy(effect)
                or DataReader().effect_by_id[effect.effectId].characteristicOperator != ""
            ):
                continue
            element = resolve_effect_element(effect.effectElement, context.primary_elem)
            if element is not None and element != allowed_element:
                return True

    return False


def is_support_spell_forbidden(
    context: AttackContext,
    spell_lvl: SpellLevelsRootItem,
) -> bool:
    constraints = get_constraints(context)
    if constraints.has(ChallengeRule.NO_HEAL) and any(
        is_heal_effect(effect) for effect in spell_lvl.effects
    ):
        return True
    return constraints.has(ChallengeRule.NO_SUMMON) and any(
        is_summon_effect(effect) for effect in spell_lvl.effects
    )


def is_support_move_forbidden(context: AttackContext, move_mp: MapPoint) -> bool:
    return (
        get_constraints(context).has(ChallengeRule.STAY_STILL)
        and move_mp.cell_id != context.player_map_point.cell_id
    )


def is_attack_position_forbidden(
    context: AttackContext,
    movable_mp: MapPoint,
    targetable_mp: MapPoint,
    *,
    enforce_end_turn: bool = False,
) -> bool:
    constraints = get_constraints(context)
    if (
        constraints.has(ChallengeRule.STAY_STILL)
        and movable_mp.cell_id != context.player_map_point.cell_id
    ):
        return True

    if constraints.has(ChallengeRule.MELEE_ONLY) or constraints.has(ChallengeRule.RANGED_ONLY):
        distance = movable_mp.distance_to_map_point(targetable_mp)
        if constraints.has(ChallengeRule.MELEE_ONLY) and distance > 1:
            return True
        if constraints.has(ChallengeRule.RANGED_ONLY) and distance <= 1:
            return True

    if enforce_end_turn and is_end_turn_position_forbidden(context, movable_mp):
        return True

    return False


def target_priority_factor(context: AttackContext, enemy: EnemyData) -> float:
    constraints = get_constraints(context)
    if enemy.life_point <= 0:
        return 1.0

    if not (
        constraints.has(ChallengeRule.KILL_LOW_TO_HIGH)
        or constraints.has(ChallengeRule.KILL_HIGH_TO_LOW)
    ):
        return 1.0

    alive = [candidate for candidate in context.enemies_data if candidate.life_point > 0]
    if len(alive) <= 1:
        return 1.0

    levels = {candidate.actor.actor_id: candidate.monster_grade.level for candidate in alive}
    enemy_level = levels[enemy.actor.actor_id]
    extreme = min(levels.values()) if constraints.has(ChallengeRule.KILL_LOW_TO_HIGH) else max(
        levels.values()
    )
    return TARGET_PRIORITY_BOOST if enemy_level == extreme else TARGET_PRIORITY_PENALTY
