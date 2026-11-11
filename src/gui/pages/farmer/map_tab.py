from functools import partial

from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import SingleDirectionScrollArea, SmoothMode

from src.core.engine.interactives.map_markers import MapMarker, MapMarkerKind
from src.core.signals.grid_signals import GridSignals
from src.core.signals.player_signals import GameInfoSignals
from src.core.states.game_state import GameState
from src.gui.components.graphics.grid_widget import MAP_MARKER_COLORS, GridView
from src.gui.components.qfluent_widget.scrollable_message_box import (
    ScrollableMessageBox,
)
from src.gui.pages.farmer.player_info.property_panel_widget import PropertyPanelWidget

_MARKER_LEGEND_LABELS = {
    MapMarkerKind.GUILD_CHEST: "Guild chest",
    MapMarkerKind.ENTRANCE: "Entrance",
    MapMarkerKind.MAP_EXIT: "Exit",
    MapMarkerKind.ZAAP: "Zaap",
    MapMarkerKind.SALE_HOTEL: "Sale hotel",
    MapMarkerKind.INTERACTIVE: "Other",
}


class MapTab(QWidget):
    def __init__(
        self,
        grid_signals: GridSignals,
        game_info_signals: GameInfoSignals,
        game_state: GameState,
        parent: QWidget | None = None,
    ):
        super().__init__(parent=parent)
        self.grid_signals = grid_signals
        self.game_info_signals = game_info_signals
        self.game_state = game_state

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.marker_summary_label = QLabel(self)
        self.marker_summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.marker_summary_label.setWordWrap(True)
        layout.addWidget(self.marker_summary_label)
        self._update_marker_summary()

        scroll_area_info = SingleDirectionScrollArea(self)

        content_widget = QWidget(scroll_area_info)
        content_widget_layout = QHBoxLayout()
        content_widget.setLayout(content_widget_layout)

        grid_view = GridView(grid_signals)
        content_widget_layout.addWidget(grid_view, 1)

        self.info_panel = PropertyPanelWidget(vertical=True, parent=content_widget)
        self.info_panel.setMinimumWidth(160)
        self.info_panel.setMaximumWidth(220)
        content_widget_layout.addWidget(self.info_panel, 0)
        self._connect_info_signals()

        scroll_area_info.setSmoothMode(SmoothMode.NO_SMOOTH)
        scroll_area_info.setWidgetResizable(True)
        scroll_area_info.setWidget(content_widget)
        scroll_area_info.enableTransparentBackground()

        layout.addWidget(scroll_area_info)

        self.grid_signals.cell_id_clicked.connect(self.on_cell_id_clicked)
        self.grid_signals.set_map_marker_on_cell_id_batch.connect(self.on_map_marker_batch)

        if self.game_state.map.map_id != 0:
            self.on_map_id_changed(self.game_state.map.map_id)

    def _connect_info_signals(self) -> None:
        self.grid_signals.new_map_id.connect(self.on_map_id_changed)
        self.grid_signals.is_in_map_transition.connect(
            partial(self.info_panel.on_received_property, "", "Map transition in progress")
        )
        self.game_info_signals.is_in_haven_bag.connect(
            partial(self.info_panel.on_received_property, "", "Inside the Haven Bag")
        )

        self.combat_group = self.info_panel.pre_create_group("Combat")
        self.combat_group.hide()
        self.game_info_signals.in_fight.connect(self.on_in_fight_changed)
        self.game_info_signals.is_our_turn.connect(
            partial(self.info_panel.on_received_property, "Combat", "Our turn")
        )
        self.game_info_signals.life_point.connect(
            partial(self.info_panel.on_received_property, "Combat", "Health")
        )
        self.game_info_signals.max_life_point.connect(
            partial(self.info_panel.on_received_property, "Combat", "Maximum health")
        )
        self.game_info_signals.action_points.connect(
            partial(self.info_panel.on_received_property, "Combat", "AP")
        )
        self.game_info_signals.movement_points.connect(
            partial(self.info_panel.on_received_property, "Combat", "MP")
        )

    @pyqtSlot(bool)
    def on_in_fight_changed(self, in_fight: bool) -> None:
        self.combat_group.setVisible(in_fight)

    @pyqtSlot(int)
    def on_map_id_changed(self, map_id: int) -> None:
        map_position = DataReader().map_info_by_map_id[map_id]
        self.info_panel.on_received_property("", "Map id", map_id)
        self.info_panel.on_received_property("", "Coordinates", f"({map_position.posX}, {map_position.posY})")
        self._update_marker_summary()

    @pyqtSlot(list)
    def on_map_marker_batch(self, _items: list[tuple[int, tuple[MapMarker, ...]]]) -> None:
        self._update_marker_summary()

    def _update_marker_summary(self) -> None:
        markers_by_cell_id = self.game_state.interactive.marker_by_cell_id
        marker_count = sum(len(markers) for markers in markers_by_cell_id.values())
        legend = " &nbsp; ".join(
            f'<span style="color:{MAP_MARKER_COLORS[kind].name()}; font-weight:600;">&#9632;</span> {label}'
            for kind, label in _MARKER_LEGEND_LABELS.items()
        )
        self.marker_summary_label.setText(f"Markers: {marker_count} &nbsp;&nbsp; {legend}")

    @pyqtSlot(int)
    def on_cell_id_clicked(self, cell_id: int) -> None:
        sections: list[str] = []

        actor = self.game_state.entity.get_first_actor_on_cell_id(cell_id)
        if actor is not None:
            if actor.actor_id in self.game_state.entity.actor_fight_by_id:
                sections.append(str(self.game_state.entity.actor_fight_by_id[actor.actor_id]))
            else:
                sections.append(str(actor))

        stated_elements = self.game_state.interactive.stated_element_by_cell_id.get(cell_id)
        if stated_elements:
            for stated_element, collectable in stated_elements.values():
                content = str(stated_element)
                if collectable is not None:
                    content += f"\n\nCollectable: {collectable}"
                sections.append(content)

        markers = self.game_state.interactive.marker_by_cell_id.get(cell_id, ())
        if markers:
            sections.append("Markers:\n" + "\n".join(self._format_marker(marker) for marker in markers))

        if sections:
            ScrollableMessageBox(f"Cell {cell_id}", "\n\n".join(sections), self).exec()

    @staticmethod
    def _format_marker(marker: MapMarker) -> str:
        details = [marker.label]
        if marker.element_id is not None:
            details.append(f"Element id: {marker.element_id}")
        if marker.element_type_id:
            details.append(f"Element type: {marker.element_type_id}")
        if marker.skill_ids:
            details.append(f"Skills: {', '.join(str(skill_id) for skill_id in marker.skill_ids)}")
        if not marker.enabled:
            details.append("Disabled")
        return " - ".join(details)
