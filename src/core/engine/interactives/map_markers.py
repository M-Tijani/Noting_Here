from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum, auto

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import InteractiveElement
from DBDofusUnity.dofus_unity_reader.data_center.map_reader import MapReader
from DBDofusUnity.dofus_unity_reader.data_center.world_graph_reader import WorldGraphReader
from DBDofusUnity.dofus_unity_reader.game_constants.element_type import ElementTypeEnum
from DBDofusUnity.dofus_unity_reader.game_constants.skill import SkillEnum


class MapMarkerKind(StrEnum):
    GUILD_CHEST = auto()
    ENTRANCE = auto()
    MAP_EXIT = auto()
    ZAAP = auto()
    SALE_HOTEL = auto()
    INTERACTIVE = auto()


@dataclass(frozen=True, slots=True)
class MapMarker:
    cell_id: int
    element_id: int | None
    kind: MapMarkerKind
    label: str
    enabled: bool = True
    skill_ids: tuple[int, ...] = ()
    element_type_id: int = 0


_ENTRANCE_SKILL_IDS = frozenset({int(SkillEnum.EXIT), int(SkillEnum.POINT_OUT_EXIT)})
_SALE_HOTEL_TYPE_IDS = frozenset(
    {
        int(ElementTypeEnum.RESOURCE_SALE_HOTEL),
        int(ElementTypeEnum.EQUIPMENT_SALE_HOTEL),
        int(ElementTypeEnum.CONSUMABLE_SALE_HOTEL),
    }
)

_KIND_LABELS = {
    MapMarkerKind.GUILD_CHEST: "Guild chest",
    MapMarkerKind.ENTRANCE: "Entrance",
    MapMarkerKind.MAP_EXIT: "Map exit",
    MapMarkerKind.ZAAP: "Zaap",
    MapMarkerKind.SALE_HOTEL: "Sale hotel",
    MapMarkerKind.INTERACTIVE: "Interactive",
}

_KIND_PRIORITY = {
    MapMarkerKind.GUILD_CHEST: 0,
    MapMarkerKind.ENTRANCE: 1,
    MapMarkerKind.MAP_EXIT: 2,
    MapMarkerKind.ZAAP: 3,
    MapMarkerKind.SALE_HOTEL: 4,
    MapMarkerKind.INTERACTIVE: 5,
}


def build_interactive_markers(
    map_id: int,
    elements: Iterable[InteractiveElement],
) -> dict[int, tuple[MapMarker, ...]]:
    try:
        reference_by_element_id = MapReader().get_ref_data_by_element_id_by_map_id(map_id)
    except KeyError:
        return {}

    markers_by_cell_id: dict[int, list[MapMarker]] = {}
    seen_element_ids: set[int] = set()
    for element in elements:
        if not element.on_current_map or element.element_id in seen_element_ids:
            continue
        seen_element_ids.add(element.element_id)

        reference = reference_by_element_id.get(element.element_id)
        if reference is None or reference.cellId is None:
            continue

        enabled_skill_ids = tuple(skill.skill_id for skill in element.enabled_skills)
        disabled_skill_ids = tuple(skill.skill_id for skill in element.disabled_skills)
        skill_ids = tuple(dict.fromkeys(enabled_skill_ids + disabled_skill_ids))
        kind = _marker_kind(element, skill_ids)
        markers_by_cell_id.setdefault(reference.cellId, []).append(
            MapMarker(
                cell_id=reference.cellId,
                element_id=element.element_id,
                kind=kind,
                label=_KIND_LABELS[kind],
                enabled=bool(enabled_skill_ids),
                skill_ids=skill_ids,
                element_type_id=element.element_type_id,
            )
        )

    return _normalize(markers_by_cell_id)


def build_marker_layer(
    map_id: int,
    elements: Iterable[InteractiveElement],
    stated_element_ids: set[int] | frozenset[int] = frozenset(),
) -> dict[int, tuple[MapMarker, ...]]:
    markers_by_cell_id = {
        cell_id: [
            marker
            for marker in markers
            if marker.element_id is None or marker.element_id not in stated_element_ids
        ]
        for cell_id, markers in build_interactive_markers(map_id, elements).items()
    }

    for cell_id in WorldGraphReader().get_exit_cell_ids(map_id):
        cell_markers = markers_by_cell_id.setdefault(cell_id, [])
        if any(marker.kind is MapMarkerKind.ENTRANCE for marker in cell_markers):
            continue
        cell_markers.append(
            MapMarker(
                cell_id=cell_id,
                element_id=None,
                kind=MapMarkerKind.MAP_EXIT,
                label=_KIND_LABELS[MapMarkerKind.MAP_EXIT],
            )
        )

    return _normalize(markers_by_cell_id)


def _marker_kind(element: InteractiveElement, skill_ids: tuple[int, ...]) -> MapMarkerKind:
    if element.element_type_id == ElementTypeEnum.GUILD_CHEST:
        return MapMarkerKind.GUILD_CHEST
    if element.element_type_id == ElementTypeEnum.ZAAP:
        return MapMarkerKind.ZAAP
    if element.element_type_id in _SALE_HOTEL_TYPE_IDS:
        return MapMarkerKind.SALE_HOTEL
    if any(skill_id in _ENTRANCE_SKILL_IDS for skill_id in skill_ids):
        return MapMarkerKind.ENTRANCE
    return MapMarkerKind.INTERACTIVE


def _normalize(
    markers_by_cell_id: dict[int, list[MapMarker]],
) -> dict[int, tuple[MapMarker, ...]]:
    return {
        cell_id: tuple(
            sorted(markers_by_cell_id[cell_id], key=_marker_sort_key),
        )
        for cell_id in sorted(markers_by_cell_id)
        if markers_by_cell_id[cell_id]
    }


def _marker_sort_key(marker: MapMarker) -> tuple[int, int, str]:
    return (
        _KIND_PRIORITY[marker.kind],
        -1 if marker.element_id is None else marker.element_id,
        marker.label,
    )
