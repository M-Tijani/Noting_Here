from collections.abc import Collection

from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.game_constants.npc import BANK_NPCS, NpcInfo

from src.core.engine.movements.map.map_tools import MapTools
from src.core.engine.movements.world.map_position import get_dist_to_maps


def get_bank_npc_info(is_sub: bool, preferred_map_id: int | None = None) -> list[NpcInfo]:
    allowed = [
        npc_info for npc_info in BANK_NPCS if is_sub or MapTools.is_map_allowed_for_unsub(npc_info.npc_map_id)
    ]
    if preferred_map_id is None:
        return allowed

    preferred = [npc_info for npc_info in allowed if npc_info.npc_map_id == preferred_map_id]
    # A forced bank the account cannot reach must not block the unload.
    return preferred or allowed


def get_near_bank_npc_info(
    bank_npc_infos: list[NpcInfo], available_waypoint_map_ids: Collection[int]
) -> list[NpcInfo]:
    """Keep the single bank closest to an owned zaap, to shorten the walk after teleporting."""
    if len(bank_npc_infos) <= 1 or not available_waypoint_map_ids:
        return bank_npc_infos

    waypoint_positions = [
        DataReader().map_info_by_map_id[waypoint_map_id] for waypoint_map_id in available_waypoint_map_ids
    ]
    return [
        min(
            bank_npc_infos,
            key=lambda npc_info: get_dist_to_maps(
                DataReader().map_info_by_map_id[npc_info.npc_map_id], waypoint_positions
            ),
        )
    ]
