import json
from functools import cache
from pathlib import Path
from typing import cast

from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.data_center.i18n import I18N
from DBDofusUnity.dofus_unity_reader.data_center.map_reader import MapReader
from DBDofusUnity.dofus_unity_reader.game_constants.map_id import (
    MAP_PIXEL_HALF_HEIGHT,
    MAP_PIXEL_HALF_WIDTH,
)
from DBDofusUnity.dofus_unity_reader.models.maps import MapReference

from src.consts import RESOURCE_FOLDER
from src.controller.game_data import GameDataController
from src.core.engine.movements.map.map_tools import MapTools

RESOURCE_MAPS_PATH = Path(RESOURCE_FOLDER) / "dofusdb" / "resource_maps.json"

_ITEM_IDS_BY_MAP_ID: dict[tuple[int, int], frozenset[int]] = {}


def get_farmable_resource_item_ids() -> set[int]:
    item_by_id = DataReader().item_by_id
    return {item_id for item_id in DataReader().gathered_item_ids if item_id in item_by_id}


def get_resource_display_name(item_id: int) -> str:
    item = DataReader().item_by_id.get(item_id)
    if item is None or item.nameId is None:
        return f"Item {item_id}"
    return I18N().name_by_id.get(item.nameId, f"Item {item_id}")


def get_map_ids_with_resource(map_ids: set[int], target_item_ids: set[int]) -> set[int]:
    if not target_item_ids:
        return set(map_ids)

    cached_map_ids_by_item_id = get_cached_map_ids_by_item_id()
    result: set[int] = set()
    items_missing_from_cache: set[int] = set()
    for item_id in target_item_ids:
        item_map_ids = cached_map_ids_by_item_id.get(item_id)
        if item_map_ids is None:
            items_missing_from_cache.add(item_id)
            continue
        result.update(map_ids & item_map_ids)

    if items_missing_from_cache:
        result.update(
            map_id for map_id in map_ids if get_item_ids_on_map(map_id) & items_missing_from_cache
        )

    return result


def get_item_ids_on_map(map_id: int) -> frozenset[int]:
    item_job_by_gfx = GameDataController().get_item_job_by_gfx()
    cache_key = (map_id, len(item_job_by_gfx))
    cached_item_ids = _ITEM_IDS_BY_MAP_ID.get(cache_key)
    if cached_item_ids is not None:
        return cached_item_ids

    item_ids: set[int] = set()
    for reference in MapReader().map_by_id(map_id).references:
        if not _is_reference_inside_map(reference) or reference.gfxId is None:
            continue
        item_and_job = item_job_by_gfx.get(reference.gfxId)
        if item_and_job is None:
            continue
        item_ids.add(item_and_job[0])

    frozen_item_ids = frozenset(item_ids)
    _ITEM_IDS_BY_MAP_ID[cache_key] = frozen_item_ids
    return frozen_item_ids


def get_cached_map_ids_by_item_id() -> dict[int, frozenset[int]]:
    path = RESOURCE_MAPS_PATH
    try:
        modified_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    return _load_resource_maps(str(path), modified_ns)


def _is_reference_inside_map(reference: MapReference) -> bool:
    if reference.position is not None:
        return (
            abs(reference.position.x) <= MAP_PIXEL_HALF_WIDTH
            and abs(reference.position.y) <= MAP_PIXEL_HALF_HEIGHT
        )
    if reference.transform is not None:
        return not MapTools.is_transform_outside_map(reference.transform)
    return False


@cache
def _load_resource_maps(path: str, modified_ns: int) -> dict[int, frozenset[int]]:
    del modified_ns
    payload: object = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Resource map cache must be an object: {path}")

    map_ids_by_item_id: dict[int, frozenset[int]] = {}
    for item_id, map_ids in cast(dict[object, object], payload).items():
        if not isinstance(item_id, str) or not isinstance(map_ids, list):
            raise ValueError(f"Invalid resource map cache entry in {path}")
        map_id_values = cast(list[object], map_ids)
        if not all(isinstance(map_id, int) and not isinstance(map_id, bool) for map_id in map_id_values):
            raise ValueError(f"Invalid map id list for item {item_id} in {path}")
        map_ids_by_item_id[int(item_id)] = frozenset(cast(list[int], map_id_values))

    return map_ids_by_item_id
