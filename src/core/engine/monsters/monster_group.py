from logging import Logger

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import (
    ActorPositionInformation,
)
from DBDofusUnity.dofus_unity_reader.game_constants.monster import MonsterGidEnum
from DBDofusUnity.dofus_unity_reader.grid.map_point import MapPoint


def get_level_monster_group(
    monster_group: ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
) -> int:
    total_group_lvl = 0
    total_group_lvl += monster_group.identification.main_creature.level
    for underling in monster_group.identification.underlings:
        total_group_lvl += underling.level
    return total_group_lvl


def get_monster_group_gids(
    monster_group: ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
) -> set[int]:
    gids = {monster_group.identification.main_creature.gid}
    gids.update(underling.gid for underling in monster_group.identification.underlings)
    return gids


def get_monster_group_size(
    monster_group: ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
) -> int:
    return 1 + len(monster_group.identification.underlings)


MonsterGroup = tuple[
    int,
    MapPoint,
    ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
]


def get_monster_groups(
    actor_by_id: dict[int, ActorPositionInformation],
) -> list[MonsterGroup]:
    monster_groups: list[MonsterGroup] = []

    for actor in actor_by_id.values():
        if not (
            actor.actor_information.HasField("role_play_actor")
            and actor.actor_information.role_play_actor.HasField("monster_group_actor")
        ):
            continue

        monster_groups.append(
            (
                actor.actor_id,
                MapPoint.from_cell_id(actor.disposition.cell_id),
                actor.actor_information.role_play_actor.monster_group_actor,
            )
        )

    return monster_groups


FightFighterInformation = ActorPositionInformation.ActorInformation.FightFighterInformation


EntityFighterInformation = FightFighterInformation.EntityFighterInformation
NamedFighterInformation = FightFighterInformation.NamedFighterInformation
AIFighter = FightFighterInformation.AIFighterInformation
MonsterFighter = AIFighter.MonsterFighter


def is_valid_monster_group(
    logger: Logger,
    monster_group: ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
    monster_group_lvl: int,
    lvl_limit: float,
) -> bool:
    if monster_group.identification.main_creature.gid in [
        MonsterGidEnum.POUTCH,
        MonsterGidEnum.PRESPIC,
    ]:
        return False

    logger.info(f"lvl : {lvl_limit} against monster group lvl : {monster_group_lvl}")
    return monster_group_lvl <= lvl_limit


def is_group_targetable(
    logger: Logger,
    monster_group: ActorPositionInformation.ActorInformation.RolePlayActor.MonsterGroupActor,
    monster_group_lvl: int,
    lvl_limit: float,
    monster_ids: set[int] | None,
    *,
    forbidden_monster_ids: set[int] | None = None,
    group_size_min: int | None = None,
    group_size_max: int | None = None,
    exact_monster_ids: bool = False,
) -> bool:
    group_gids = get_monster_group_gids(monster_group)
    if monster_ids is not None:
        if exact_monster_ids and group_gids != monster_ids:
            return False
        if not exact_monster_ids and not (monster_ids & group_gids):
            return False
    if forbidden_monster_ids is not None and (forbidden_monster_ids & group_gids):
        return False
    group_size = get_monster_group_size(monster_group)
    if group_size_min is not None and group_size < group_size_min:
        return False
    if group_size_max is not None and group_size > group_size_max:
        return False
    return is_valid_monster_group(logger, monster_group, monster_group_lvl, lvl_limit)
