from functools import lru_cache

from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.data_center.map_reader import MapReader
from DBDofusUnity.dofus_unity_reader.game_constants.transition_type import TransitionTypeEnum
from DBDofusUnity.dofus_unity_reader.models.world_graph import Edge, Transition

# `mapChangeData` bits are isometric screen directions. The three bits that cross
# one side of a diamond map all scroll to the same cardinal neighbour.
_CARDINAL_EDGE_BITS: dict[int, frozenset[int]] = {
    0: frozenset({0, 1, 7}),
    2: frozenset({1, 2, 3}),
    4: frozenset({3, 4, 5}),
    6: frozenset({5, 6, 7}),
}
_CARDINAL_DELTAS: dict[int, tuple[int, int]] = {
    0: (1, 0),
    2: (0, 1),
    4: (-1, 0),
    6: (0, -1),
}
_SCROLL_TYPES = frozenset({int(TransitionTypeEnum.SCROLL), int(TransitionTypeEnum.SCROLL_ACTION)})


@lru_cache(maxsize=256)
def map_change_cell_ids(map_id: int, direction: int, target_map_id: int) -> tuple[int, ...]:
    """Cells whose map data scrolls to the cardinal neighbour in ``direction``.

    The world graph stores a single representative cell per edge, which can be
    unreachable in game while other cells of the same edge still work.
    """
    edge_bits = _CARDINAL_EDGE_BITS.get(direction)
    if edge_bits is None:
        return ()
    current = DataReader().map_info_by_map_id.get(map_id)
    target = DataReader().map_info_by_map_id.get(target_map_id)
    if current is None or target is None:
        return ()
    dx, dy = _CARDINAL_DELTAS[direction]
    if (current.posX + dx, current.posY + dy) != (target.posX, target.posY):
        return ()
    try:
        cells = MapReader().map_by_id(map_id).mapData.cellsData
    except KeyError:
        return ()
    edge_mask = sum(1 << bit for bit in edge_bits)
    return tuple(
        cell.cellNumber
        for cell in cells
        if cell.mov
        and cell.mapChangeData & (1 << direction)
        and not cell.mapChangeData & ~edge_mask
    )


def with_map_change_alternatives(edge: Edge) -> Edge:
    """Append the other map-change cells of each scroll edge."""
    existing = {(transition.m_direction, transition.m_cellId) for transition in edge.m_transitions}
    alternatives: list[Transition] = []
    for template in edge.m_transitions:
        if int(template.m_type) not in _SCROLL_TYPES:
            continue
        for cell_id in map_change_cell_ids(edge.m_from.m_mapId, template.m_direction, edge.m_to.m_mapId):
            key = (template.m_direction, cell_id)
            if key in existing:
                continue
            existing.add(key)
            alternatives.append(
                Transition(
                    m_type=template.m_type,
                    m_direction=template.m_direction,
                    m_skillId=-1,
                    m_criterion="",
                    m_transitionMapId=edge.m_to.m_mapId,
                    m_cellId=cell_id,
                    m_id=-1,
                )
            )
    if not alternatives:
        return edge
    return Edge(m_from=edge.m_from, m_to=edge.m_to, m_transitions=[*edge.m_transitions, *alternatives])
