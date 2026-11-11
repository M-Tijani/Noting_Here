from collections import defaultdict
from dataclasses import dataclass
from math import floor

from DBDofusUnity.dofus_unity_reader.models.datas.map_positions_root import MapInformationRootItem
from PyQt6.QtCore import QRectF, Qt, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from src.utils.project_paths import BUNDLE_ROOT

from src.core.signals.world_signals import WorldSignals
from src.gui.utils.profiling import profiled_slot

type Coord = tuple[int, int]
type RGBColor = tuple[int, int, int]

TEXT_SIZE = 9
CELL_SIZE: int = 50
LIMIT_GRID = 8
MAX_PATH_EDGES = 500

WIDTH_AROUND_CURRENT_MAP = CELL_SIZE * LIMIT_GRID * 2
HEIGHT_AROUND_CURRENT_MAP = CELL_SIZE * LIMIT_GRID * 2

# DofusDB World 1 geometry (PC-like / matches DofusDB)
WORLD1_ID = 1
WORLD1_ORIGINE_X = 6480.0
WORLD1_ORIGINE_Y = 4944.0
WORLD1_MAP_WIDTH = 69.5
WORLD1_MAP_HEIGHT = 49.70000076293945
WORLD1_TOTAL_WIDTH = 10000.0
WORLD1_TOTAL_HEIGHT = 8000.0
WORLD1_SCALE = 0.6  # assembled scale

WORLD_MAP_DIR = BUNDLE_ROOT / "resources" / "worldmap"
WORLD_MAP_IMG = WORLD_MAP_DIR / "world_1.jpg"

VIEW_MAPS_X = 20
VIEW_MAPS_Y = 20


class SquareMap(QGraphicsRectItem):
    DEFAULT = Qt.GlobalColor.white

    def __init__(self, width: float = CELL_SIZE, height: float = CELL_SIZE):
        self.colors_by_map_id: dict[int, RGBColor] = {}
        rect = QRectF(0, 0, width, height)
        super().__init__(rect)
        self.set_border()

    def add_color(self, map_id: int, color: RGBColor):
        self.colors_by_map_id[map_id] = color
        self.update()

    def reset_color(self):
        self.colors_by_map_id.clear()
        self.setBrush(SquareMap.DEFAULT)

    def set_border(self):
        pen = QPen(QColor("#A9A9A9"))
        pen.setWidth(0)
        self.setPen(pen)

    def paint(
        self,
        painter: QPainter | None,
        option: QStyleOptionGraphicsItem | None,
        widget: QWidget | None = None,
    ) -> None:
        if painter is None or option is None:
            return
        if len(self.colors_by_map_id) == 0:
            return super().paint(painter, option, widget)

        all_colors = set(self.colors_by_map_id.values())
        divided_height = floor(self.boundingRect().height() / len(all_colors))
        for index, color in enumerate(all_colors):
            painter.setBrush(QColor(*color))
            rect = QRectF(
                self.boundingRect().x(),
                self.boundingRect().y() + divided_height * index,
                self.boundingRect().width(),
                divided_height,
            )
            painter.drawRect(rect)


class MapCircle(QGraphicsEllipseItem):
    def __init__(self) -> None:
        super().__init__(
            CELL_SIZE // 4,
            CELL_SIZE // 4 + TEXT_SIZE,
            CELL_SIZE // 2,
            CELL_SIZE // 2,
        )
        self.setZValue(1)
        self.setBrush(Qt.GlobalColor.red)


@dataclass
class CurrMapInfo:
    state: MapCircle
    map_pos: MapInformationRootItem


class MapWorldView(QGraphicsView):
    def __init__(self, world_signals: WorldSignals | None = None, debug: bool = False) -> None:
        super().__init__()
        self.debug = debug
        self.world_signals = world_signals
        self.curr_map_info: CurrMapInfo | None = None
        self.line_items: list[QGraphicsLineItem] = []
        self._scene: QGraphicsScene = QGraphicsScene()
        self.square_by_coord: dict[Coord, SquareMap] = {}
        self.map_pos_by_coord: dict[Coord, set[MapInformationRootItem]] = defaultdict(set)
        self._colors_by_map_id: dict[int, tuple[MapInformationRootItem, RGBColor]] = {}
        self._path: list[tuple[MapInformationRootItem, MapInformationRootItem]] = []
        self._current_map: MapInformationRootItem | None = None
        self._render_dirty = False

        self.setStyleSheet("border: 0px")
        self.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._world_bg_item: QGraphicsPixmapItem | None = None
        self._world_mode = False
        self._view_rect: QRectF | None = None
        self._load_world_background()

        self.setScene(self._scene)
        self._apply_view()

        if self.world_signals:
            self.world_signals.color_pos.connect(profiled_slot(self.on_color_pos))
            self.world_signals.color_pos_batch.connect(profiled_slot(self.on_color_pos_batch))
            self.world_signals.arrow_pos.connect(profiled_slot(self.on_arrow_pos))
            self.world_signals.arrow_pos_batch.connect(profiled_slot(self.on_arrow_pos_batch))
            self.world_signals.reset_weight.connect(profiled_slot(self.on_reset_weight))
            self.world_signals.reset_path.connect(profiled_slot(self.on_reset_path))
            self.world_signals.curr_map_pos.connect(profiled_slot(self.on_curr_map))

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        self._apply_view()
        return super().resizeEvent(event)

    def _apply_view(self) -> None:
        rect = self._view_rect if self._view_rect is not None else self._scene.sceneRect()
        if not rect.isEmpty():
            self.fitInView(rect, mode=Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event: QShowEvent | None) -> None:
        if self._render_dirty:
            self._rebuild_scene()
        super().showEvent(event)

    def _load_world_background(self) -> None:
        if not WORLD_MAP_IMG.exists():
            return
        pixmap = QPixmap(str(WORLD_MAP_IMG))
        if pixmap.isNull():
            return
        self._world_bg_item = QGraphicsPixmapItem(pixmap)
        self._world_bg_item.setZValue(-10)
        self._world_bg_item.setPos(0, 0)
        self._scene.addItem(self._world_bg_item)
        self._scene.setSceneRect(QRectF(0, 0, pixmap.width(), pixmap.height()))
        self._world_mode = True

    def _world_map_to_scene(self, map_pos: MapInformationRootItem) -> tuple[float, float]:
        if map_pos.worldMap != WORLD1_ID:
            return map_pos.posX * CELL_SIZE, map_pos.posY * CELL_SIZE
        px = WORLD1_ORIGINE_X + map_pos.posX * WORLD1_MAP_WIDTH
        py = WORLD1_ORIGINE_Y + map_pos.posY * WORLD1_MAP_HEIGHT
        return px * WORLD1_SCALE, py * WORLD1_SCALE

    @pyqtSlot(MapInformationRootItem)
    def on_curr_map(self, map_pos: MapInformationRootItem):
        self._current_map = map_pos
        self._world_mode = self._world_mode or (map_pos.worldMap == WORLD1_ID)
        if not self.isVisible():
            self._render_dirty = True
            return
        self._render_current_map(map_pos)

    def _render_current_map(self, map_pos: MapInformationRootItem) -> None:
        coord = map_pos.posX, map_pos.posY
        if self.curr_map_info is None:
            state = MapCircle()
            self._scene.addItem(state)
            self.curr_map_info = CurrMapInfo(state=state, map_pos=map_pos)
        else:
            self.curr_map_info.map_pos = map_pos

        x, y = self._world_map_to_scene(map_pos)
        self.curr_map_info.state.setPos(x, y)
        if map_pos.worldMap == WORLD1_ID:
            img_w = WORLD1_TOTAL_WIDTH * WORLD1_SCALE
            img_h = WORLD1_TOTAL_HEIGHT * WORLD1_SCALE
            view_w = VIEW_MAPS_X * WORLD1_MAP_WIDTH * WORLD1_SCALE
            view_h = VIEW_MAPS_Y * WORLD1_MAP_HEIGHT * WORLD1_SCALE
            view_w = min(view_w, img_w)
            view_h = min(view_h, img_h)
            left = min(max(x - view_w / 2, 0), img_w - view_w)
            top = min(max(y - view_h / 2, 0), img_h - view_h)
            self._view_rect = QRectF(left, top, view_w, view_h)
            self._apply_view()
            self._prune_outside_current_window()
            return
        xg, yg = self.get_pos_by_coord(coord)
        self.curr_map_info.state.setPos(xg, yg)
        left = xg - CELL_SIZE * LIMIT_GRID
        top = yg - CELL_SIZE * LIMIT_GRID
        self._scene.setSceneRect(QRectF(left, top, WIDTH_AROUND_CURRENT_MAP, HEIGHT_AROUND_CURRENT_MAP))
        self.centerOn(xg, yg)
        self._prune_outside_current_window()

    def get_or_create_map(self, map_pos: MapInformationRootItem) -> SquareMap:
        coord = map_pos.posX, map_pos.posY
        if coord not in self.square_by_coord:
            self.map_pos_by_coord[coord].add(map_pos)
            if map_pos.worldMap == WORLD1_ID:
                square = SquareMap(WORLD1_MAP_WIDTH * WORLD1_SCALE, WORLD1_MAP_HEIGHT * WORLD1_SCALE)
                x, y = self._world_map_to_scene(map_pos)
            else:
                square = SquareMap()
                x, y = self.get_pos_by_coord(coord)
            square.setPos(x, y)
            self.square_by_coord[coord] = square
            self._scene.addItem(square)

            if self.debug:
                text = QGraphicsTextItem(f"{coord[0]},{coord[1]}")
                text.setPos(x, y)
                text.setZValue(2)
                self._scene.addItem(text)

        return self.square_by_coord[coord]

    def get_pos_by_coord(self, coord: Coord) -> tuple[int, int]:
        return coord[0] * CELL_SIZE, coord[1] * CELL_SIZE

    @pyqtSlot(MapInformationRootItem, tuple)
    def on_color_pos(self, map_pos: MapInformationRootItem, color: RGBColor):
        self._colors_by_map_id[map_pos.id] = (map_pos, color)
        if not self.isVisible():
            self._render_dirty = True
            return
        square_cell = self.get_or_create_map(map_pos)
        square_cell.add_color(map_pos.id, color)

    @pyqtSlot(list)
    def on_color_pos_batch(self, items: list[tuple[MapInformationRootItem, RGBColor]]):
        for map_pos, color in items:
            self._colors_by_map_id[map_pos.id] = (map_pos, color)
        if not self.isVisible():
            self._render_dirty = True
            return
        self.setUpdatesEnabled(False)
        for map_pos, color in items:
            square_cell = self.get_or_create_map(map_pos)
            square_cell.add_color(map_pos.id, color)
        self.setUpdatesEnabled(True)

    @pyqtSlot(MapInformationRootItem, MapInformationRootItem)
    def on_arrow_pos(self, map_pos_start: MapInformationRootItem, map_pos_end: MapInformationRootItem):
        self._path.append((map_pos_start, map_pos_end))
        self._trim_path()
        if not self.isVisible():
            self._render_dirty = True
            return
        self._render_arrow(map_pos_start, map_pos_end)

    def _render_arrow(
        self, map_pos_start: MapInformationRootItem, map_pos_end: MapInformationRootItem
    ) -> None:
        start_square = self.get_or_create_map(map_pos_start)
        end_square = self.get_or_create_map(map_pos_end)
        if map_pos_start.worldMap == WORLD1_ID or map_pos_end.worldMap == WORLD1_ID:
            start_x, start_y = self._world_map_to_scene(map_pos_start)
            end_x, end_y = self._world_map_to_scene(map_pos_end)
            line_item = QGraphicsLineItem(start_x, start_y, end_x, end_y)
        else:
            line_item = QGraphicsLineItem(
                start_square.x() + CELL_SIZE / 2,
                start_square.y() + CELL_SIZE / 2,
                end_square.x() + CELL_SIZE / 2,
                end_square.y() + CELL_SIZE / 2,
            )
        pen = QPen(Qt.GlobalColor.green)
        pen.setWidth(2)
        line_item.setPen(pen)
        self.line_items.append(line_item)
        self._scene.addItem(line_item)

    @pyqtSlot(list)
    def on_arrow_pos_batch(self, items: list[tuple[MapInformationRootItem, MapInformationRootItem]]):
        self._path.extend(items)
        self._trim_path()
        if not self.isVisible():
            self._render_dirty = True
            return
        self.setUpdatesEnabled(False)
        for map_pos_start, map_pos_end in items:
            self._render_arrow(map_pos_start, map_pos_end)
        self.setUpdatesEnabled(True)

    @pyqtSlot()
    def on_reset_path(self):
        self._path.clear()
        if not self.isVisible():
            self._render_dirty = True
            return
        self.setUpdatesEnabled(False)
        while self.line_items:
            line_item = self.line_items.pop()
            self._scene.removeItem(line_item)
        self.setUpdatesEnabled(True)

    @pyqtSlot()
    def on_reset_weight(self):
        self._colors_by_map_id.clear()
        if not self.isVisible():
            self._render_dirty = True
            return
        self.setUpdatesEnabled(False)
        for square in self.square_by_coord.values():
            square.reset_color()
        self.setUpdatesEnabled(True)

    def _rebuild_scene(self) -> None:
        self._scene.clear()
        self._world_bg_item = None
        self._load_world_background()
        self.curr_map_info = None
        self.line_items.clear()
        self.square_by_coord.clear()
        self.map_pos_by_coord.clear()
        for map_pos, color in self._colors_by_map_id.values():
            self.get_or_create_map(map_pos).add_color(map_pos.id, color)
        for map_pos_start, map_pos_end in self._path:
            self._render_arrow(map_pos_start, map_pos_end)
        if self._current_map is not None:
            self._render_current_map(self._current_map)
        self._render_dirty = False

    def _trim_path(self) -> None:
        overflow = len(self._path) - MAX_PATH_EDGES
        if overflow <= 0:
            return
        del self._path[:overflow]
        while len(self.line_items) > MAX_PATH_EDGES:
            self._scene.removeItem(self.line_items.pop(0))

    def _prune_outside_current_window(self) -> None:
        if self._current_map is None:
            return
        if self._current_map.worldMap == WORLD1_ID:
            return
        current_x, current_y = self._current_map.posX, self._current_map.posY
        kept_coords = {
            coord
            for coord in self.square_by_coord
            if abs(coord[0] - current_x) <= LIMIT_GRID and abs(coord[1] - current_y) <= LIMIT_GRID
        }
        for coord in set(self.square_by_coord) - kept_coords:
            self._scene.removeItem(self.square_by_coord.pop(coord))
            self.map_pos_by_coord.pop(coord, None)
        self._colors_by_map_id = {
            map_id: value
            for map_id, value in self._colors_by_map_id.items()
            if (value[0].posX, value[0].posY) in kept_coords
        }
