import json
import logging
import tempfile
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

from DBDofusUnity.datas.protos.non_obf.game.common_pb2 import ObjectItemInventory
from DBDofusUnity.dofus_unity_reader.data_center.data_reader import DataReader
from DBDofusUnity.dofus_unity_reader.data_center.i18n import I18N
from DBDofusUnity.dofus_unity_reader.game_constants.sale_hotel import QuantityEnum
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QMouseEvent, QPixmap, QShowEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    SingleDirectionScrollArea,
    SmoothMode,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.check_box import CheckBox
from qfluentwidgets.components.widgets.label import StrongBodyLabel
from qfluentwidgets.components.widgets.spin_box import SpinBox

from src.controller.game_data import GameDataController
from src.core.bot.bot import Bot
from src.core.engine.economy.hdv_sell_bounds import HdvSellTarget
from src.services.background import run_in_background
from src.utils.dofusdb_icons import DEFAULT_ICON_DIR, download_icons

logger = logging.getLogger(__name__)

MAX_PRICE = 2_000_000_000
DEFAULT_MAX_PRICE = 1_000_000_000

LOT_CHOICES: tuple[QuantityEnum, ...] = (
    QuantityEnum.VALUE_1,
    QuantityEnum.VALUE_10,
    QuantityEnum.VALUE_100,
    QuantityEnum.VALUE_1000,
)

MAX_LOT_CHOICES: tuple[int, ...] = (1, 2, 3, 4, 5, 10, 20, 50, 100)
DEFAULT_MAX_LOTS = 5

MAX_SEARCH_RESULTS = 50
MIN_SEARCH_LENGTH = 2
SEARCH_DEBOUNCE_MS = 300

CACHE_SAVE_DEBOUNCE_MS = 500

CATEGORY_ALL = "All"
CATEGORY_EQUIPMENT = "Equipment"
CATEGORY_CONSUMABLES = "Consumables"
CATEGORY_RESOURCES = "Resources"
CATEGORY_COSMETICS = "Cosmetics"
CATEGORY_CHOICES: tuple[str, ...] = (
    CATEGORY_ALL,
    CATEGORY_EQUIPMENT,
    CATEGORY_CONSUMABLES,
    CATEGORY_RESOURCES,
    CATEGORY_COSMETICS,
)

# Dofus item superTypeId buckets used to filter the item database.
CATEGORY_SUPER_TYPES: dict[str, frozenset[int]] = {
    CATEGORY_EQUIPMENT: frozenset({1, 2, 3, 4, 5, 7, 10, 11}),
    CATEGORY_CONSUMABLES: frozenset({6, 14, 15, 16, 17, 18, 19, 20}),
    CATEGORY_RESOURCES: frozenset({9}),
    CATEGORY_COSMETICS: frozenset({12, 22}),
}

# Generated market script template (watch_prices.lua style).
# ITEMS_BY_CATEGORY is injected as a Lua literal before execution.
_MARKET_SCRIPT_TEMPLATE = textwrap.dedent(
    """
    local POLL_SECONDS = 30
    local MAX_BELOW_AVERAGE = 0.5
    local MAX_ABOVE_AVERAGE = 1.0

    local ITEMS_BY_CATEGORY = __ITEMS_BY_CATEGORY__

    local function managed_listing_count(item_id, items)
      local count = 0
      for _, listing in ipairs(hdv.myListings()) do
        if item_id == nil then
          for _, item in ipairs(items) do
            if listing.id == item.id then
              count = count + 1
              break
            end
          end
        elseif listing.id == item_id then
          count = count + 1
        end
      end
      return count
    end

    local function managed_listing_summary(items)
      local parts = {}
      for _, item in ipairs(items) do
        parts[#parts + 1] = string.format(
          "%d=%d/%d",
          item.id,
          managed_listing_count(item.id),
          item.maxLots or 5
        )
      end
      return table.concat(parts, " ")
    end

    local function open_sell(category)
      local ok, result = pcall(hdv.openSell, category)
      if not ok then
        log("HDV open: " .. tostring(result))
        return false
      end
      if not result then
        log("HDV open: failed")
        return false
      end
      return true
    end

    local function list_target(item, max_lots)
      return {
        id = item.id,
        lot = item.lot,
        min = item.min,
        max = item.max,
        maxLots = max_lots,
        maxBelowAverage = MAX_BELOW_AVERAGE,
        maxAboveAverage = MAX_ABOVE_AVERAGE,
      }
    end

    local function build_floors_by_lot(items)
      local floors = {}
      for _, item in ipairs(items) do
        floors[item.lot] = floors[item.lot] or {}
        floors[item.lot][item.id] = item.min
      end
      return floors
    end

    local function fill_sold_slots(items)
      local free = hdv.myFreeSlots()
      log("HDV free slots: " .. tostring(free))
      if free <= 0 then
        return false
      end
      local sellable = hdv.sellableItems()
      local sellable_qty_by_id = {}
      for _, candidate in ipairs(sellable) do
        sellable_qty_by_id[candidate.id] = (sellable_qty_by_id[candidate.id] or 0) + candidate.quantity
      end
      local targets = {}
      for _, item in ipairs(items) do
        local qty = sellable_qty_by_id[item.id] or 0
        if qty >= item.lot then
          local max_lots = item.maxLots or 5
          local missing = max_lots - managed_listing_count(item.id)
          if missing > 0 then
            local to_list = math.min(missing, free)
            targets[#targets + 1] = list_target(item, to_list)
            free = free - to_list
            log("HDV will list " .. to_list .. " lot(s) of " .. item.id .. " (qty " .. qty .. ")")
          end
        else
          log("HDV not enough qty for " .. item.id .. ": have " .. qty .. " need " .. item.lot)
        end
        if free <= 0 then
          break
        end
      end
      if #targets == 0 then
        log("HDV no targets to list")
        return false
      end
      local ok, listed = pcall(hdv.sellInventoryItems, targets)
      if not ok then
        log("HDV list error: " .. tostring(listed))
        return false
      end
      log("HDV list result: " .. tostring(listed))
      return listed == true
    end

    local function reprice_listings(items)
      local floors_by_lot = build_floors_by_lot(items)
      for lot, floors in pairs(floors_by_lot) do
        local ok, summary = pcall(hdv.undercutAll, lot, nil, floors, MAX_BELOW_AVERAGE, MAX_ABOVE_AVERAGE)
        if not ok then
          log("HDV reprice: " .. tostring(summary))
        elseif summary then
          log(string.format(
            "HDV lot %d -> updated=%d skipped=%d errors=%d",
            lot,
            summary.updated,
            summary.skipped,
            summary.errors
          ))
        end
      end
    end

    local function process_category(category, items)
      if not open_sell(category) then
        return
      end
      reprice_listings(items)
      if fill_sold_slots(items) then
        reprice_listings(items)
      end
      log("HDV manager [" .. category .. "]: " .. managed_listing_summary(items))
    end

    while true do
      for category, items in pairs(ITEMS_BY_CATEGORY) do
        process_category(category, items)
      end
      sleep(POLL_SECONDS)
    end
    """
)


def _item_name_by_gid(gid: int) -> str:
    item_data = DataReader().item_by_id.get(gid)
    if item_data and item_data.nameId:
        return I18N().name_by_id.get(item_data.nameId, f"Item {gid}")
    return f"Item {gid}"


def _order_cache_path(bot: Bot) -> Path:
    """Path where the HDV order list is persisted between sessions."""
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(bot))
    return Path("resources") / "script_progress" / safe_name / "hdv_order_cache.json"


def _to_lua_table(value: object, indent: int = 0) -> str:
    """Convert a Python value to a Lua table literal."""
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        if not mapping:
            return "{}"
        items: list[str] = []
        for key, item in mapping.items():
            lua_key = f"[{_to_lua_table(key)}]" if isinstance(key, int) else key
            items.append(f"{lua_key} = {_to_lua_table(item, indent + 1)}")
        return "{\n" + ",\n".join("  " * (indent + 1) + item for item in items) + f"\n{'  ' * indent}}}"
    if isinstance(value, list):
        sequence = cast("list[Any]", value)
        if not sequence:
            return "{}"
        items = [_to_lua_table(item, indent + 1) for item in sequence]
        return "{\n" + ",\n".join("  " * (indent + 1) + item for item in items) + f"\n{'  ' * indent}}}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return repr(value)
    return repr(value)


def _placeholder_pixmap(size: int = 32) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap


def _load_cached_icon(gid: int, size: int = 32) -> QPixmap:
    """Load the cached DofusDB icon for `gid` from local cache (no network)."""
    icon_path = DEFAULT_ICON_DIR / f"{gid}.png"
    if not icon_path.exists():
        return _placeholder_pixmap(size)
    pixmap = QPixmap(str(icon_path))
    if pixmap.isNull():
        return _placeholder_pixmap(size)
    if pixmap.width() != size or pixmap.height() != size:
        pixmap = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


@dataclass
class _AggregatedItem:
    gid: int
    name: str
    bag_quantity: int = 0
    bank_quantity: int = 0
    sale_quantity: int = 0
    sale_price: int = 0

    @property
    def total_quantity(self) -> int:
        return self.bag_quantity + self.bank_quantity + self.sale_quantity


class _HdvInventoryItemRow(QWidget):
    """Selectable item row shown in the left search/add panel."""

    toggled = pyqtSignal()

    def __init__(self, item: _AggregatedItem, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.gid = item.gid
        self.item_name = item.name

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.checkbox = CheckBox(self)
        self.checkbox.stateChanged.connect(self._on_toggled)
        layout.addWidget(self.checkbox)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setPixmap(_load_cached_icon(item.gid))
        layout.addWidget(self.icon_label)

        text = item.name
        if item.total_quantity > 0:
            text += f" (x{item.total_quantity})"
        self.name_label = BodyLabel(text, self)
        layout.addWidget(self.name_label, 1)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.checkbox.setChecked(not self.checkbox.isChecked())

    def _on_toggled(self) -> None:
        self.toggled.emit()

    def is_selected(self) -> bool:
        return self.checkbox.isChecked()

    def set_selected(self, selected: bool) -> None:
        self.checkbox.setChecked(selected)


class _HdvOrderItemRow(QWidget):
    """Editable row in the right-panel order list."""

    removed = pyqtSignal()

    def __init__(self, item: _AggregatedItem, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.gid = item.gid
        self.item_name = item.name

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(12)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setPixmap(_load_cached_icon(item.gid))
        layout.addWidget(self.icon_label)

        self.name_label = BodyLabel(item.name, self)
        self.name_label.setMinimumWidth(140)
        layout.addWidget(self.name_label, 1)

        self.lot_combo = ComboBox(self)
        for choice in LOT_CHOICES:
            self.lot_combo.addItem(str(int(choice)), userData=choice)
        self.lot_combo.setCurrentIndex(2)  # default 100
        self.lot_combo.setMinimumWidth(70)
        layout.addWidget(self.lot_combo)

        self.max_lots_combo = ComboBox(self)
        for choice in MAX_LOT_CHOICES:
            self.max_lots_combo.addItem(str(choice), userData=choice)
        self.max_lots_combo.setCurrentIndex(MAX_LOT_CHOICES.index(DEFAULT_MAX_LOTS))
        self.max_lots_combo.setMinimumWidth(70)
        layout.addWidget(self.max_lots_combo)

        self.min_spin = SpinBox(self)
        self.min_spin.setRange(1, MAX_PRICE)
        self.min_spin.setValue(1)
        self.min_spin.setMinimumWidth(100)
        layout.addWidget(self.min_spin)

        self.max_spin = SpinBox(self)
        self.max_spin.setRange(1, MAX_PRICE)
        self.max_spin.setValue(DEFAULT_MAX_PRICE)
        self.max_spin.setMinimumWidth(100)
        layout.addWidget(self.max_spin)

        self.remove_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.remove_btn.setToolTip("Remove from order list")
        self.remove_btn.clicked.connect(self.removed.emit)
        layout.addWidget(self.remove_btn)

    def set_lot(self, lot: QuantityEnum) -> None:
        for index in range(self.lot_combo.count()):
            data = self.lot_combo.itemData(index)
            if data == lot:
                self.lot_combo.setCurrentIndex(index)
                return

    def set_max_lots(self, max_lots: int) -> None:
        for index in range(self.max_lots_combo.count()):
            data = self.max_lots_combo.itemData(index)
            if data == max_lots:
                self.max_lots_combo.setCurrentIndex(index)
                return

    def set_min(self, value: int) -> None:
        self.min_spin.setValue(value)

    def set_max(self, value: int) -> None:
        self.max_spin.setValue(value)

    def target(self) -> HdvSellTarget:
        data = self.lot_combo.currentData()
        lot = data if isinstance(data, QuantityEnum) else QuantityEnum.VALUE_1
        max_lots_data = self.max_lots_combo.currentData()
        max_lots = int(max_lots_data) if max_lots_data is not None else None
        return HdvSellTarget(
            minimum=self.min_spin.value(),
            maximum=self.max_spin.value(),
            quantity=lot,
            max_lots=max_lots,
        )


class HdvPage(QWidget):
    def __init__(self, bot: Bot, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.bot = bot
        self._items_by_gid: dict[int, _AggregatedItem] = {}
        self._inventory_rows_by_gid: dict[int, _HdvInventoryItemRow] = {}
        self._order_rows_by_gid: dict[int, _HdvOrderItemRow] = {}
        self._inventory_filter = ""
        self._order_filter = ""
        self._current_category = CATEGORY_ALL
        self._item_index: list[tuple[int, str, int]] = self._build_item_index()

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setInterval(50)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self._rebuild_inventory_rows)

        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.timeout.connect(self._run_inventory_search)

        self._cache_save_timer = QTimer(self)
        self._cache_save_timer.setInterval(CACHE_SAVE_DEBOUNCE_MS)
        self._cache_save_timer.setSingleShot(True)
        self._cache_save_timer.timeout.connect(self._save_order_cache)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        top_layout = QHBoxLayout()
        top_layout.setSpacing(16)
        top_layout.addWidget(self._build_left_panel(), 1)
        top_layout.addWidget(self._build_right_panel(), 3)
        layout.addLayout(top_layout, 1)

        self.bot.inventory_signals.bank_refreshed.connect(self._on_bank_changed)
        self.bot.inventory_signals.bank_item_updated.connect(self._on_bank_changed)
        self.bot.inventory_signals.bank_item_removed.connect(self._on_bank_changed)

        self._resync()
        self._load_order_cache()

    def _build_left_panel(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        search_bar = QHBoxLayout()
        search_bar.setSpacing(8)

        self.left_search = LineEdit(card)
        self.left_search.setPlaceholderText("Search Item Bar")
        self.left_search.textChanged.connect(self._on_inventory_search_changed)
        search_bar.addWidget(self.left_search, 1)

        self.category_combo = ComboBox(card)
        for category in CATEGORY_CHOICES:
            self.category_combo.addItem(category, userData=category)
        self.category_combo.setCurrentIndex(0)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        self.category_combo.setMinimumWidth(110)
        search_bar.addWidget(self.category_combo)

        layout.addLayout(search_bar)

        self._left_empty_label = BodyLabel("Type an item name above to search all items.", card)
        self._left_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._left_empty_label.setWordWrap(True)
        layout.addWidget(self._left_empty_label)

        scroll = SingleDirectionScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSmoothMode(SmoothMode.NO_SMOOTH)
        content = QWidget(scroll)
        self._inventory_layout = QVBoxLayout(content)
        self._inventory_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._inventory_layout.setSpacing(4)
        self._inventory_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self.add_btn = PrimaryPushButton("Add", card)
        self.add_btn.clicked.connect(self._on_add_to_order)
        layout.addWidget(self.add_btn)

        return card

    def _build_right_panel(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        self.order_search = LineEdit(card)
        self.order_search.setPlaceholderText("Search for item in order list")
        self.order_search.textChanged.connect(self._on_order_search_changed)
        top_bar.addWidget(self.order_search, 1)

        self.default_lot_combo = ComboBox(card)
        for choice in LOT_CHOICES:
            self.default_lot_combo.addItem(f"Default lot {int(choice)}", userData=choice)
        self.default_lot_combo.setCurrentIndex(2)
        self.default_lot_combo.currentIndexChanged.connect(self._on_default_lot_changed)
        top_bar.addWidget(self.default_lot_combo)

        self.default_max_lots_combo = ComboBox(card)
        for choice in MAX_LOT_CHOICES:
            self.default_max_lots_combo.addItem(f"Default max lots {choice}", userData=choice)
        self.default_max_lots_combo.setCurrentIndex(MAX_LOT_CHOICES.index(DEFAULT_MAX_LOTS))
        self.default_max_lots_combo.currentIndexChanged.connect(self._on_default_max_lots_changed)
        top_bar.addWidget(self.default_max_lots_combo)

        self.play_btn = TransparentToolButton(FluentIcon.PLAY, card)
        self.play_btn.setToolTip("Start selling")
        self.play_btn.clicked.connect(self.on_click_play)
        self.bot.bot_signals.play.connect(self.on_play)
        top_bar.addWidget(self.play_btn)

        self.stop_btn = TransparentToolButton(FluentIcon.PAUSE, card)
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.clicked.connect(self.on_click_stop)
        self.bot.bot_signals.stop.connect(self.on_stop)
        top_bar.addWidget(self.stop_btn)
        self.stop_btn.hide()

        layout.addLayout(top_bar)

        header = QFrame(card)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(12)
        header_labels = ["Name", "Lot", "Max lots", "Min", "Max", "Remove"]
        header_layout.addSpacing(40)  # icon column
        header_layout.addWidget(StrongBodyLabel(header_labels[0], header), 1)
        for label in header_labels[1:-1]:
            header_layout.addWidget(StrongBodyLabel(label, header), alignment=Qt.AlignmentFlag.AlignCenter)
        header_layout.addSpacing(40)  # remove column
        layout.addWidget(header)

        scroll = SingleDirectionScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSmoothMode(SmoothMode.NO_SMOOTH)
        content = QWidget(scroll)
        self._order_layout = QVBoxLayout(content)
        self._order_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._order_layout.setSpacing(4)
        self._order_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self.status_label = CaptionLabel(card)
        layout.addWidget(self.status_label)

        return card

    @pyqtSlot(list)
    @pyqtSlot(ObjectItemInventory)
    @pyqtSlot(int)
    def _on_bank_changed(self, _: object = None) -> None:
        self._schedule_rebuild()

    def _resync(self) -> None:
        self._items_by_gid = self._aggregate_items()
        self._rebuild_inventory_rows()

    def _aggregate_items(self) -> dict[int, _AggregatedItem]:
        aggregated: dict[int, _AggregatedItem] = {}

        def ensure(gid: int) -> _AggregatedItem:
            item = aggregated.get(gid)
            if item is None:
                item = _AggregatedItem(gid=gid, name=_item_name_by_gid(gid))
                aggregated[gid] = item
            return item

        for object_item in self.bot.game_state.inventory.objects_by_uid.values():
            ensure(object_item.item.gid).bag_quantity += object_item.item.quantity

        for object_item in self.bot.game_state.inventory.bank_objects_by_uid.values():
            ensure(object_item.item.gid).bank_quantity += object_item.item.quantity

        for gid, quantity, price in self._sale_entries().values():
            item = ensure(gid)
            item.sale_quantity += quantity
            if price and (item.sale_price == 0 or price < item.sale_price):
                item.sale_price = price

        return aggregated

    def _sale_entries(self) -> dict[int, tuple[int, int, int]]:
        controller = GameDataController()
        server_id = self.bot.game_state.player.server_id
        character_id = self.bot.game_state.player.character_id
        return controller.get_hdv_by_uid_by_player(server_id).get(character_id, {})

    def _schedule_rebuild(self) -> None:
        if self.isVisible() and not self._rebuild_timer.isActive():
            self._rebuild_timer.start()

    def _build_item_index(self) -> list[tuple[int, str, int]]:
        """Pre-build a searchable index of all items: (gid, lower_name, superTypeId)."""
        reader = DataReader()
        i18n = I18N()
        index: list[tuple[int, str, int]] = []
        for gid, item_data in reader.item_by_id.items():
            name = i18n.name_by_id.get(item_data.nameId, "") if item_data.nameId else ""
            if not name:
                continue
            type_info = reader.item_type_by_id.get(item_data.typeId)
            super_type = type_info.superTypeId if type_info else -1
            index.append((gid, name.lower(), super_type))
        return index

    def _filter_item_index(self, text: str, category: str) -> list[int]:
        """Return up to MAX_SEARCH_RESULTS item ids matching text and category."""
        if len(text) < MIN_SEARCH_LENGTH:
            return []
        query = text.lower()
        allowed_super_types = CATEGORY_SUPER_TYPES.get(category)
        results: list[tuple[int, str]] = []
        for gid, lower_name, super_type in self._item_index:
            if allowed_super_types is not None and super_type not in allowed_super_types:
                continue
            if query in lower_name:
                results.append((gid, lower_name))
                if len(results) >= MAX_SEARCH_RESULTS:
                    break
        results.sort(key=lambda entry: entry[1])
        return [gid for gid, _ in results]

    def _run_inventory_search(self) -> None:
        """Execute the search in the background to keep the UI responsive."""
        text = self._inventory_filter
        category = self._current_category

        def search(_progress: Callable[[str], None]) -> list[int]:
            return self._filter_item_index(text, category)

        run_in_background(
            search,
            on_success=self._on_inventory_search_completed,
            on_error=self._on_inventory_search_error,
            parent=self,
        )

    def _on_inventory_search_completed(self, result: object) -> None:
        gids = cast(list[int], result)
        self._render_inventory_search_results(gids)
        self._download_missing_icons_async(gids)

    def _on_inventory_search_error(self, error: object) -> None:
        logger.exception("Inventory search failed: %s", error)
        self._left_empty_label.setText("Search failed. Try again.")
        self._left_empty_label.setVisible(True)

    def _render_inventory_search_results(self, gids: list[int]) -> None:
        wanted_gids = set(gids)

        for gid in list(self._inventory_rows_by_gid):
            if gid in wanted_gids:
                continue
            row = self._inventory_rows_by_gid.pop(gid)
            self._inventory_layout.removeWidget(row)
            row.deleteLater()

        for gid in sorted(wanted_gids, key=lambda g: _item_name_by_gid(g).lower()):
            row = self._inventory_rows_by_gid.get(gid)
            if row is None:
                item = self._items_by_gid.get(gid) or _AggregatedItem(
                    gid=gid, name=_item_name_by_gid(gid)
                )
                row = _HdvInventoryItemRow(item, self)
                row.toggled.connect(self._update_status)
                self._inventory_rows_by_gid[gid] = row
                self._inventory_layout.addWidget(row)
            else:
                row.icon_label.setPixmap(_load_cached_icon(gid))

        has_results = len(self._inventory_rows_by_gid) > 0
        self._left_empty_label.setVisible(not has_results)
        self._update_status()

    def _download_missing_icons_async(self, gids: list[int]) -> None:
        """Download icons that are not cached yet, then refresh visible rows."""
        if not gids:
            return
        missing = {gid for gid in gids if not (DEFAULT_ICON_DIR / f"{gid}.png").exists()}
        if not missing:
            return

        def download(_progress: Callable[[str], None]) -> dict[int, Path]:
            return download_icons(missing)

        run_in_background(
            download,
            on_success=lambda _: self._refresh_inventory_icons(),
            on_error=lambda error: logger.warning("Failed to download icons: %s", error),
            parent=self,
        )

    def _refresh_inventory_icons(self) -> None:
        for gid, row in self._inventory_rows_by_gid.items():
            row.icon_label.setPixmap(_load_cached_icon(gid))

    @pyqtSlot(str)
    def _on_inventory_search_changed(self, text: str) -> None:
        self._inventory_filter = text.strip().lower()
        self._search_debounce_timer.stop()
        if len(self._inventory_filter) < MIN_SEARCH_LENGTH:
            self._render_inventory_search_results([])
            self._left_empty_label.setText(
                f"Type at least {MIN_SEARCH_LENGTH} characters to search."
            )
            return
        self._left_empty_label.setText("Searching...")
        self._left_empty_label.setVisible(True)
        self._search_debounce_timer.start()

    @pyqtSlot(int)
    def _on_category_changed(self, index: int) -> None:
        self._current_category = CATEGORY_CHOICES[index] if 0 <= index < len(CATEGORY_CHOICES) else CATEGORY_ALL
        if len(self._inventory_filter) >= MIN_SEARCH_LENGTH:
            self._search_debounce_timer.stop()
            self._search_debounce_timer.start()
        else:
            self._render_inventory_search_results([])

    def _rebuild_inventory_rows(self) -> None:
        """Resync inventory quantities and refresh the current search results."""
        self._items_by_gid = self._aggregate_items()
        if len(self._inventory_filter) >= MIN_SEARCH_LENGTH:
            self._run_inventory_search()
        else:
            self._render_inventory_search_results([])

    @pyqtSlot(str)
    def _on_order_search_changed(self, text: str) -> None:
        self._order_filter = text.strip().lower()
        self._apply_order_filter()
        self._update_status()

    @pyqtSlot()
    def _on_add_to_order(self) -> None:
        default_lot = self._current_default_lot()
        default_max_lots = self._current_default_max_lots()
        for gid, row in self._inventory_rows_by_gid.items():
            if not row.is_selected():
                continue
            if gid in self._order_rows_by_gid:
                continue
            item = self._items_by_gid.get(gid) or _AggregatedItem(
                gid=gid, name=_item_name_by_gid(gid)
            )
            order_row = _HdvOrderItemRow(item, self)
            order_row.set_lot(default_lot)
            order_row.set_max_lots(default_max_lots)
            order_row.removed.connect(partial(self._remove_order_row, gid))
            self._connect_row_cache_signals(order_row)
            self._order_rows_by_gid[gid] = order_row
            self._order_layout.addWidget(order_row)
            row.set_selected(False)
        self._schedule_cache_save()
        self._apply_order_filter()
        self._update_status()

    def _remove_order_row(self, gid: int) -> None:
        row = self._order_rows_by_gid.pop(gid, None)
        if row is None:
            return
        self._order_layout.removeWidget(row)
        row.deleteLater()
        self._schedule_cache_save()
        self._update_status()

    def _schedule_cache_save(self) -> None:
        self._cache_save_timer.stop()
        self._cache_save_timer.start()

    def _order_cache(self) -> dict[str, object]:
        return {
            "version": 1,
            "items": [
                {
                    "gid": gid,
                    "lot": int(row.target().quantity),
                    "max_lots": row.target().max_lots,
                    "min": row.target().minimum,
                    "max": row.target().maximum,
                }
                for gid, row in self._order_rows_by_gid.items()
            ],
        }

    def _save_order_cache(self) -> None:
        cache_path = _order_cache_path(self.bot)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._order_rows_by_gid:
                cache_path.unlink(missing_ok=True)
                return
            cache_path.write_text(json.dumps(self._order_cache(), indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to save HDV order cache to %s", cache_path)

    def _load_order_cache(self) -> None:
        cache_path = _order_cache_path(self.bot)
        if not cache_path.exists():
            return
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load HDV order cache from %s", cache_path)
            return
        if data.get("version") != 1:
            return
        for entry in data.get("items", []):
            gid = entry.get("gid")
            if not isinstance(gid, int):
                continue
            if gid in self._order_rows_by_gid:
                continue
            item = self._items_by_gid.get(gid) or _AggregatedItem(
                gid=gid, name=_item_name_by_gid(gid)
            )
            order_row = _HdvOrderItemRow(item, self)
            try:
                order_row.set_lot(QuantityEnum(entry.get("lot", 100)))
            except ValueError:
                order_row.set_lot(QuantityEnum.VALUE_100)
            order_row.set_max_lots(entry.get("max_lots", DEFAULT_MAX_LOTS))
            order_row.set_min(entry.get("min", 1))
            order_row.set_max(entry.get("max", DEFAULT_MAX_PRICE))
            order_row.removed.connect(partial(self._remove_order_row, gid))
            self._connect_row_cache_signals(order_row)
            self._order_rows_by_gid[gid] = order_row
            self._order_layout.addWidget(order_row)
        self._apply_order_filter()
        self._update_status()

    def _connect_row_cache_signals(self, row: _HdvOrderItemRow) -> None:
        row.lot_combo.currentIndexChanged.connect(self._schedule_cache_save)
        row.max_lots_combo.currentIndexChanged.connect(self._schedule_cache_save)
        row.min_spin.valueChanged.connect(self._schedule_cache_save)
        row.max_spin.valueChanged.connect(self._schedule_cache_save)

    @pyqtSlot(int)
    def _on_default_lot_changed(self, _: int) -> None:
        lot = self._current_default_lot()
        for row in self._order_rows_by_gid.values():
            row.set_lot(lot)
        self._schedule_cache_save()

    @pyqtSlot(int)
    def _on_default_max_lots_changed(self, _: int) -> None:
        max_lots = self._current_default_max_lots()
        for row in self._order_rows_by_gid.values():
            row.set_max_lots(max_lots)
        self._schedule_cache_save()

    def _current_default_lot(self) -> QuantityEnum:
        data = self.default_lot_combo.currentData()
        return data if isinstance(data, QuantityEnum) else QuantityEnum.VALUE_100

    def _current_default_max_lots(self) -> int:
        data = self.default_max_lots_combo.currentData()
        return int(data) if isinstance(data, int) else DEFAULT_MAX_LOTS

    def _inventory_row_visible(self, row: _HdvInventoryItemRow) -> bool:
        return self._inventory_filter in row.item_name.lower()

    def _apply_order_filter(self) -> None:
        for row in self._order_rows_by_gid.values():
            row.setVisible(self._order_filter in row.item_name.lower())

    def _update_status(self) -> None:
        inventory_shown = sum(
            1 for row in self._inventory_rows_by_gid.values() if self._inventory_row_visible(row)
        )
        inventory_selected = sum(
            1
            for row in self._inventory_rows_by_gid.values()
            if row.is_selected() and self._inventory_row_visible(row)
        )
        order_count = len(self._order_rows_by_gid)
        self.status_label.setText(
            f"Inventory: {inventory_shown} shown, {inventory_selected} selected  ·  Order list: {order_count} item(s)"
        )

    @staticmethod
    def _category_id_of_gid(gid: int) -> int | None:
        item_data = DataReader().item_by_id.get(gid)
        if item_data is None:
            return None
        item_type = DataReader().item_type_by_id.get(item_data.typeId)
        if item_type is None:
            return None
        return int(item_type.categoryId)

    def _build_items_by_category(self) -> dict[int, list[dict[str, object]]] | None:
        items_by_category: dict[int, list[dict[str, object]]] = {}
        for gid, row in self._order_rows_by_gid.items():
            if not row.isVisible():
                continue
            try:
                target = row.target()
            except ValueError as exc:
                self.status_label.setText(f"Invalid price range for {row.item_name}: {exc}")
                return None
            category_id = self._category_id_of_gid(gid)
            if category_id is None:
                self.status_label.setText(f"Cannot determine HDV category for {row.item_name}")
                return None
            items_by_category.setdefault(category_id, []).append(
                {
                    "id": gid,
                    "lot": int(target.quantity),
                    "min": target.minimum,
                    "max": target.maximum,
                    "maxLots": target.max_lots if target.max_lots is not None else DEFAULT_MAX_LOTS,
                }
            )
        return items_by_category

    def _write_market_script(self, items_by_category: dict[int, list[dict[str, object]]]) -> Path:
        lua_table = _to_lua_table(items_by_category)
        source = _MARKET_SCRIPT_TEMPLATE.replace("__ITEMS_BY_CATEGORY__", lua_table)
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(self.bot))
        script_path = Path(tempfile.gettempdir()) / f"hdv_market_{safe_name}.lua"
        script_path.write_text(source, encoding="utf-8")
        return script_path

    @pyqtSlot()
    def on_click_play(self) -> None:
        items_by_category = self._build_items_by_category()
        if items_by_category is None:
            return
        if not items_by_category:
            self.status_label.setText("Add at least one item to the order list and set its min/max price.")
            return

        script_path = self._write_market_script(items_by_category)
        logger.info(
            "Starting generated market script for %d item(s) across %d category(s): %s",
            sum(len(items) for items in items_by_category.values()),
            len(items_by_category),
            script_path,
        )
        self.bot.bot_signals.play.emit(True)
        self.bot.bot_signals.play_script.emit(str(script_path))

    @pyqtSlot()
    def on_click_stop(self) -> None:
        self.bot.bot_signals.stop.emit()

    @pyqtSlot(bool)
    def on_play(self, _: bool) -> None:
        self.play_btn.hide()
        self.stop_btn.show()

    @pyqtSlot()
    def on_stop(self) -> None:
        self.play_btn.show()
        self.stop_btn.hide()

    @pyqtSlot()
    def _on_refresh(self) -> None:
        self._resync()

    def showEvent(self, a0: QShowEvent | None) -> None:
        self._resync()
        super().showEvent(a0)
