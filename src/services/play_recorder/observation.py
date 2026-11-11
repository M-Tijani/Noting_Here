from dataclasses import fields, is_dataclass
from typing import Any, cast

import msgspec
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from msgspec import Struct

from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint
from src.core.engine.contexts import AttackContext
from src.core.engine.fights.attack.enemy_data import EnemyData


def to_jsonable(value: object) -> Any:
    if isinstance(value, Message):
        return MessageToDict(
            value,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=True,
        )
    if isinstance(value, MapPoint):
        return {"cell_id": value.cell_id, "x": value.x, "y": value.y}
    if isinstance(value, Struct):
        return to_jsonable(msgspec.to_builtins(value))
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        items = cast("dict[object, object]", value)
        return {str(key): to_jsonable(item) for key, item in items.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = cast("list[object] | tuple[object, ...] | set[object] | frozenset[object]", value)
        return [to_jsonable(item) for item in items]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def serialize_attack_context(context: AttackContext | None) -> dict[str, Any] | None:
    if context is None:
        return None
    return {
        "map_id": context.map_id,
        "fight_turn": context.fight_turn,
        "player_cell_id": context.player_map_point.cell_id,
        "player": {
            "character_id": context.player_character_id,
            "level": context.player_level,
            "breed_id": context.breed_id,
            "action_points": context.action_points,
            "movement_points": context.movement_points,
            "range": context.range,
            "life_point": context.life_point,
            "max_life_point": context.max_life_point,
            "life_percentage": context.life_percentage,
        },
        "primary_elem": int(context.primary_elem),
        "spells": to_jsonable(context.spells),
        "modifier_by_type_and_spell_id": [
            {"spell_id": spell_id, "modifier_type": int(modifier_type), "modifier": to_jsonable(modifier)}
            for (spell_id, modifier_type), modifier in context.modifier_by_type_and_spell_id.items()
        ],
        "count_casted_by_spell_id_on_current_turn": dict(context.count_casted_by_spell_id_on_current_turn),
        "cast_turn_by_spell_id": dict(context.cast_turn_by_spell_id),
        "characteristics": to_jsonable(context.characteristic_by_id),
        "own_state_ids": sorted(context.own_state_ids),
        "own_active_stack_count_by_spell_id": dict(context.own_active_stack_count_by_spell_id),
        "own_active_summon_count": context.own_active_summon_count,
        "max_active_summon_count": context.max_active_summon_count,
        "invisible_enemy_cell_ids": sorted(context.invisible_enemy_cell_ids),
        "constraints": to_jsonable(context.constraints),
        "actors": to_jsonable(context.actor_by_id),
        "enemies": [_serialize_enemy(enemy) for enemy in context.enemies_data],
    }


def _serialize_enemy(enemy: EnemyData) -> dict[str, Any]:
    grade = enemy.monster_grade
    return {
        "actor_id": enemy.actor.actor_id,
        "cell_id": enemy.map_point.cell_id,
        "life_point": enemy.life_point,
        "max_life_point": enemy.max_life_point,
        "is_summoned": enemy.is_summoned,
        "is_invulnerable": enemy.is_invulnerable,
        "is_hidden": enemy.is_hidden,
        "invisibility": int(enemy.invisibility),
        "state_ids": sorted(enemy.state_ids),
        "movement_points": enemy.movement_points,
        "max_spell_range": enemy.max_spell_range,
        "monster_grade": to_jsonable(grade),
        "monster": {
            "id": grade.monsterId,
            "grade": grade.grade,
            "level": grade.level,
            "life_points": grade.lifePoints,
            "action_points": grade.actionPoints,
            "movement_points": grade.movementPoints,
            "resistances": {
                "neutral": grade.neutralResistance,
                "earth": grade.earthResistance,
                "fire": grade.fireResistance,
                "water": grade.waterResistance,
                "air": grade.airResistance,
            },
            "starting_spell_id": grade.startingSpellId,
            "bonus_range": grade.bonusRange,
        },
    }
