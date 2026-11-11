from collections.abc import Iterable
from dataclasses import dataclass, field

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import (
    InteractiveElement,
    StatedElement,
)
from src.core import config
from src.core.engine.interactives.collectable import (
    Collectable,
    get_stated_element_collectable,
)
from src.core.engine.interactives.map_markers import MapMarker, build_marker_layer
from src.core.signals.grid_signals import GridSignals
from src.core.states.map_state import MapState
from src.core.states.player_state import PlayerState
from src.core.states.state import State


class StatedElementByIdDict(dict[int, tuple[StatedElement, Collectable | None]]):
    def __init__(self, cell_id: int):
        self.cell_id = cell_id
        super().__init__()


class StatedElementByCellIdDict(dict[int, StatedElementByIdDict]):
    def __missing__(self, key: int) -> StatedElementByIdDict:
        value = StatedElementByIdDict(cell_id=key)
        self.__setitem__(key, value)
        return value


@dataclass
class InteractiveState(State):
    grid_signals: GridSignals
    player_state: PlayerState
    map_state: MapState
    interactive_element_by_id: dict[int, InteractiveElement] = field(
        init=False, default_factory=dict[int, InteractiveElement]
    )
    stated_element_by_id: dict[int, tuple[StatedElement, Collectable | None]] = field(
        init=False,
        default_factory=dict[int, tuple[StatedElement, Collectable | None]],
    )
    stated_element_by_cell_id: StatedElementByCellIdDict = field(
        init=False, default_factory=StatedElementByCellIdDict
    )
    marker_by_cell_id: dict[int, tuple[MapMarker, ...]] = field(
        init=False, default_factory=dict[int, tuple[MapMarker, ...]]
    )
    _marker_map_id: int = field(init=False, default=0)
    loaded_map_id: int = field(init=False, default=0)

    def clear_state(self):
        self.interactive_element_by_id.clear()
        self._marker_map_id = 0
        self.loaded_map_id = 0
        self.clear_interactive_markers()
        self.clear_stated_elements()

    def clear_stated_elements(self) -> None:
        if not self.stated_element_by_id:
            return
        batch = [
            (stated_element.cell_id, None, None) for stated_element, _ in self.stated_element_by_id.values()
        ]
        self.stated_element_by_id.clear()
        self.stated_element_by_cell_id.clear()
        if batch and config.DEBUG:
            self.grid_signals.set_stated_element_on_cell_id_batch.emit(batch)
        self._refresh_markers()

    def set_interactive_elements(
        self,
        interactive_elements: Iterable[InteractiveElement],
        map_id: int | None = None,
    ) -> None:
        self.interactive_element_by_id = {element.element_id: element for element in interactive_elements}
        self._marker_map_id = map_id if map_id is not None else self.map_state.map_id
        self._refresh_markers()

    def set_interactive_element(
        self,
        interactive_element: InteractiveElement,
        map_id: int | None = None,
    ) -> None:
        self.interactive_element_by_id[interactive_element.element_id] = interactive_element
        if map_id is not None:
            self._marker_map_id = map_id
        elif not self._marker_map_id:
            self._marker_map_id = self.map_state.map_id
        self._refresh_markers()

    def clear_interactive_markers(self) -> None:
        self._set_markers({})

    def set_stated_elements(self, stated_elements: Iterable[StatedElement]) -> None:
        old_cell_ids = {se.cell_id for se, _ in self.stated_element_by_id.values()}
        self.stated_element_by_id.clear()
        self.stated_element_by_cell_id.clear()
        batch: list[tuple[int, StatedElement | None, Collectable | None]] = []
        new_cell_ids: set[int] = set()
        for stated_element in stated_elements:
            if not stated_element.on_current_map:
                continue
            collectable = self.get_stated_element_collectable(stated_element)
            self.stated_element_by_id[stated_element.element_id] = (
                stated_element,
                collectable,
            )
            self.stated_element_by_cell_id[stated_element.cell_id][stated_element.element_id] = (
                stated_element,
                collectable,
            )
            new_cell_ids.add(stated_element.cell_id)
            batch.append((stated_element.cell_id, stated_element, collectable))
        for old_cell_id in old_cell_ids - new_cell_ids:
            batch.append((old_cell_id, None, None))
        if batch and config.DEBUG:
            self.grid_signals.set_stated_element_on_cell_id_batch.emit(batch)
        self._refresh_markers()

    def set_stated_element(self, stated_element: StatedElement) -> None:
        old_cell_id = None
        if stated_element.element_id in self.stated_element_by_id:
            old_se, _ = self.stated_element_by_id[stated_element.element_id]
            old_cell_id = old_se.cell_id
            del self.stated_element_by_id[stated_element.element_id]
            del self.stated_element_by_cell_id[old_cell_id][stated_element.element_id]

        collectable = self.get_stated_element_collectable(stated_element)
        self.stated_element_by_id[stated_element.element_id] = (
            stated_element,
            collectable,
        )
        self.stated_element_by_cell_id[stated_element.cell_id][stated_element.element_id] = (
            stated_element,
            collectable,
        )
        batch: list[tuple[int, StatedElement | None, Collectable | None]] = [
            (stated_element.cell_id, stated_element, collectable)
        ]
        if old_cell_id is not None and old_cell_id != stated_element.cell_id:
            remaining = self.stated_element_by_cell_id.get(old_cell_id)
            if not remaining or len(remaining) == 0:
                batch.append((old_cell_id, None, None))
        if config.DEBUG:
            self.grid_signals.set_stated_element_on_cell_id_batch.emit(batch)
        self._refresh_markers()

    def _refresh_markers(self) -> None:
        if not self._marker_map_id:
            self._set_markers({})
            return
        markers = build_marker_layer(
            self._marker_map_id,
            self.interactive_element_by_id.values(),
            set(self.stated_element_by_id),
        )
        self._set_markers(markers)

    def _set_markers(self, markers_by_cell_id: dict[int, tuple[MapMarker, ...]]) -> None:
        old_markers = self.marker_by_cell_id
        batch: list[tuple[int, tuple[MapMarker, ...]]] = []
        for cell_id in sorted(old_markers.keys() | markers_by_cell_id.keys()):
            previous = old_markers.get(cell_id, ())
            current = markers_by_cell_id.get(cell_id, ())
            if previous != current:
                batch.append((cell_id, current))
        self.marker_by_cell_id = markers_by_cell_id
        if batch and config.DEBUG:
            self.grid_signals.set_map_marker_on_cell_id_batch.emit(batch)

    def get_farmable_collectables(self, excluded_element_ids: set[int] | None = None) -> list[Collectable]:
        farmable_collectables: list[Collectable] = []
        for stated_element, collectable in self.stated_element_by_id.values():
            if excluded_element_ids is not None and stated_element.element_id in excluded_element_ids:
                continue
            if not collectable:
                continue
            farmable_collectables.append(collectable)

        return farmable_collectables

    def get_stated_element_collectable(self, stated_element: StatedElement) -> Collectable | None:
        return get_stated_element_collectable(
            stated_element,
            self.interactive_element_by_id,
            self.map_state.map_id,
            self.player_state.jobs_lvl_by_id,
        )

    def get_enabled_skill(
        self, element_id: int, skill_id: int | None = None
    ) -> InteractiveElement.InteractiveElementSkill | None:
        element = self.interactive_element_by_id.get(element_id)
        if element is None or len(element.enabled_skills) == 0:
            return None
        if skill_id is None:
            return element.enabled_skills[0]
        return next((skill for skill in element.enabled_skills if skill.skill_id == skill_id), None)

    def get_enabled_skill_ids(self, element_id: int) -> list[int]:
        element = self.interactive_element_by_id.get(element_id)
        if element is None:
            return []
        return [skill.skill_id for skill in element.enabled_skills]

    def get_element_by_skill_id(self, skill_id: int) -> InteractiveElement:
        related_element = next(
            element
            for element in self.interactive_element_by_id.values()
            if any(enabled_skill.skill_id == skill_id for enabled_skill in element.enabled_skills)
        )
        return related_element
