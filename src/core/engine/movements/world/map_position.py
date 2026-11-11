from functools import cache

from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.game_constants.area import AreaEnum
from DBDofusUnity.dofus_unity_reader.models.datas.map_positions_root import MapInformationRootItem

from src.consts import FAKE_INFINITY_VALUE


@cache
def _map_candidates_by_coordinates() -> dict[tuple[int, int], tuple[MapInformationRootItem, ...]]:
    # A coordinate can hold several maps (overworld, Incarnam, indoor copies).
    candidates: dict[tuple[int, int], list[MapInformationRootItem]] = {}
    for info in DataReader().map_info_by_map_id.values():
        candidates.setdefault((info.posX, info.posY), []).append(info)
    return {coords: tuple(options) for coords, options in candidates.items()}


def _select_map_candidate(
    candidates: tuple[MapInformationRootItem, ...], reference_map_id: int | None
) -> MapInformationRootItem:
    if reference_map_id is not None:
        reference = DataReader().map_info_by_map_id.get(reference_map_id)
        if reference is not None:
            for candidate in candidates:
                if candidate.id == reference.id:
                    return candidate
            reference_area_id = DataReader().sub_area_by_id[reference.subAreaId].areaId
            for matches in (
                [candidate for candidate in candidates if candidate.subAreaId == reference.subAreaId],
                [
                    candidate
                    for candidate in candidates
                    if DataReader().sub_area_by_id[candidate.subAreaId].areaId == reference_area_id
                ],
            ):
                if matches:
                    return min(matches, key=lambda candidate: (candidate.worldMap != 1, candidate.id))
    # Prefer the overworld copy (worldMap == 1), then the lowest id.
    return min(candidates, key=lambda candidate: (candidate.worldMap != 1, candidate.id))


def get_map_id_by_coords(
    pos_x: int, pos_y: int, reference_map_id: int | None = None
) -> int | None:
    candidates = _map_candidates_by_coordinates().get((pos_x, pos_y))
    if not candidates:
        return None
    return _select_map_candidate(candidates, reference_map_id).id


def get_coords_by_map_id(map_id: int) -> tuple[int, int] | None:
    info = DataReader().map_info_by_map_id.get(map_id)
    if info is None:
        return None
    return (info.posX, info.posY)


def get_dist_to_maps(map_pos: MapInformationRootItem, ends_pos: list[MapInformationRootItem]) -> float:
    if DataReader().sub_area_by_id[map_pos.subAreaId].areaId == AreaEnum.INCARNAM and not any(
        DataReader().sub_area_by_id[end_pos.subAreaId].areaId != AreaEnum.INCARNAM for end_pos in ends_pos
    ):
        return FAKE_INFINITY_VALUE
    dist_to_end_pos: list[float] = []
    for end_pos in ends_pos:
        manhattan_dist = abs(map_pos.posX - end_pos.posX) + abs(map_pos.posY - end_pos.posY)
        if manhattan_dist == 0 and end_pos.subAreaId != map_pos.subAreaId:
            dist = 10
        else:
            dist = manhattan_dist
        dist_to_end_pos.append(dist)
    return min(dist_to_end_pos)
