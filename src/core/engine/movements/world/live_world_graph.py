from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from DBDofusUnity.dofus_unity_reader.data_center.world_graph_reader import WorldGraphReader
from DBDofusUnity.dofus_unity_reader.game_constants.transition_type import TransitionTypeEnum
from DBDofusUnity.dofus_unity_reader.models.datas.map_positions_root import MapInformationRootItem
from DBDofusUnity.dofus_unity_reader.models.world_graph import Edge, Transition, Vertice
from src.core.engine.movements.world.map_change_cells import map_change_cell_ids
from src.utils.project_paths import USER_DATA_ROOT

_LIVE_GRAPH_PATH = USER_DATA_ROOT / "resources" / "live_world_graph.json"

# Dofus map coordinates move one unit per adjacent map. The bit directions used
# by movement/pathfinding map to these coordinate deltas.
_CARDINAL_DELTAS: dict[int, tuple[int, int]] = {
    0: (1, 0),  # right
    2: (0, 1),  # bottom
    4: (-1, 0),  # left
    6: (0, -1),  # top
}


def infer_cardinal_direction(
    src_pos: MapInformationRootItem, dst_pos: MapInformationRootItem
) -> int | None:
    delta = (dst_pos.posX - src_pos.posX, dst_pos.posY - src_pos.posY)
    for direction, expected in _CARDINAL_DELTAS.items():
        if delta == expected:
            return direction
    return None


@dataclass(frozen=True, slots=True)
class LiveTransition:
    src_map_id: int
    dst_map_id: int
    direction: int


class LiveWorldGraph:
    """Transitions observed during play, persisted across sessions.

    The cached ``world_graph.json`` is authoritative for maps the bot has never
    visited, but once a transition is observed live it takes precedence. This
    lets the bot learn new/changed map connections without regenerating the
    whole static graph.
    """

    def __init__(self, path: Path = _LIVE_GRAPH_PATH) -> None:
        self._path = path
        self._transitions: dict[int, dict[LiveTransition, LiveTransition]] = {}
        self._lock = threading.Lock()
        self._load()

    def record_transition(
        self, src_map_id: int, dst_map_id: int, direction: int | None
    ) -> None:
        if direction is None or src_map_id == dst_map_id:
            return
        transition = LiveTransition(
            src_map_id=src_map_id, dst_map_id=dst_map_id, direction=direction
        )
        with self._lock:
            transitions = self._transitions.setdefault(src_map_id, {})
            if transition not in transitions:
                transitions[transition] = transition
                self._save_locked()

    def get_edges(
        self,
        src_vertex: Vertice,
        direction: int | None = None,
    ) -> list[Edge]:
        with self._lock:
            transitions = self._transitions.get(src_vertex.m_mapId, {})
        return [
            self._to_edge(src_vertex, transition)
            for transition in transitions.values()
            if direction is None or transition.direction == direction
        ]

    def get_edge(
        self,
        src_vertex: Vertice,
        dst_map_id: int,
        direction: int | None = None,
    ) -> Edge | None:
        with self._lock:
            transitions = self._transitions.get(src_vertex.m_mapId, {})
        for transition in transitions.values():
            if transition.dst_map_id == dst_map_id and (
                direction is None or transition.direction == direction
            ):
                return self._to_edge(src_vertex, transition)
        return None

    def known_destinations(self, src_map_id: int) -> set[int]:
        with self._lock:
            return {
                transition.dst_map_id
                for transition in self._transitions.get(src_map_id, {}).values()
            }

    def _to_edge(self, src_vertex: Vertice, transition: LiveTransition) -> Edge:
        dst_vertex = self._resolve_dst_vertex(transition.dst_map_id, src_vertex)
        cell_ids = map_change_cell_ids(
            src_vertex.m_mapId, transition.direction, transition.dst_map_id
        )
        if not cell_ids:
            cell_ids = (0,)
        transitions = [
            Transition(
                m_type=int(TransitionTypeEnum.SCROLL_ACTION),
                m_direction=transition.direction,
                m_skillId=-1,
                m_criterion="",
                m_transitionMapId=transition.dst_map_id,
                m_cellId=cell_id,
                m_id=-1,
            )
            for cell_id in cell_ids
        ]
        return Edge(m_from=src_vertex, m_to=dst_vertex, m_transitions=list(transitions))

    def _resolve_dst_vertex(self, dst_map_id: int, src_vertex: Vertice) -> Vertice:
        cached_vertices = WorldGraphReader().get_vertexes(dst_map_id)
        if cached_vertices:
            for vertex in cached_vertices:
                if vertex.m_zoneId == src_vertex.m_zoneId:
                    return vertex
            return next(iter(cached_vertices))
        # No cached vertex for this map: synthesise one. UID is irrelevant for
        # in-memory equality/hash, but we keep it deterministic per map/zone.
        return Vertice(m_mapId=dst_map_id, m_zoneId=0, m_uid=dst_map_id * 1000)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as file:
                raw = json.load(file)
        except (json.JSONDecodeError, OSError):
            return
        for item in raw:
            try:
                transition = LiveTransition(
                    src_map_id=int(item["src_map_id"]),
                    dst_map_id=int(item["dst_map_id"]),
                    direction=int(item["direction"]),
                )
            except (KeyError, ValueError, TypeError):
                continue
            self._transitions.setdefault(transition.src_map_id, {})[transition] = transition

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "src_map_id": transition.src_map_id,
                "dst_map_id": transition.dst_map_id,
                "direction": transition.direction,
            }
            for transitions in self._transitions.values()
            for transition in transitions.values()
        ]
        with self._path.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)


# Global instance used by frames and behaviors. Tests can replace this.
live_world_graph = LiveWorldGraph()
