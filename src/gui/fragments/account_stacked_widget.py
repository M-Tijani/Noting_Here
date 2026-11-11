from PyQt6.QtWidgets import QScrollArea, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import PivotItem, SegmentedWidget

from src.controller.player_info_storage import PlayerInfoSnapshot
from src.core import config
from src.core.bot.bot import Bot
from src.core.signals.log_signals import LogSignals
from src.gui.fragments.account_quick_info import AccountQuickInfoWidget
from src.gui.pages.automation.automation_page import AutomationPage
from src.gui.pages.craft.craft_page import CraftPage
from src.gui.pages.debugs.sniffer import SnifferWidget
from src.gui.pages.farmer.farmer import FarmerWidget
from src.gui.pages.hdv.hdv_page import HdvPage
from src.gui.pages.scripts.scripts_page import ScriptsPage


class AccountStackedWidget(QWidget):
    @staticmethod
    def _require_pivot_item(item: PivotItem | None, route_key: str) -> PivotItem:
        if item is None:
            raise ValueError(f"Segmented route `{route_key}` is already registered")
        return item

    def __init__(
        self,
        global_log_signals: LogSignals,
        login: str,
        bot: Bot,
        snapshot: PlayerInfoSnapshot | None,
        quarantine_reason: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent=parent)
        self.login = login
        self.bot = bot
        self.global_log_signals = global_log_signals
        self.setObjectName(f"{login}_bot")
        layout = QVBoxLayout()
        self.setLayout(layout)

        self.pivot = SegmentedWidget(self)
        layout.addWidget(self.pivot)

        self.quick_info_widget: AccountQuickInfoWidget | None = None
        if config.DEBUG:
            self.quick_info_widget = AccountQuickInfoWidget(
                self.bot,
                snapshot,
                quarantine_reason,
                parent=self,
            )
            layout.addWidget(self.quick_info_widget)

        self.stacked_widget = QStackedWidget(self)
        layout.addWidget(self.stacked_widget)

        self.harvester_interface = FarmerWidget(login, self.bot, parent=self.stacked_widget)
        self.stacked_widget.addWidget(self.harvester_interface)
        self.harvester_route = f"{login}_harvester"
        self.farmer_pivot_item = self._require_pivot_item(
            self.pivot.addItem(
                routeKey=self.harvester_route,
                text="Farmer",
                onClick=lambda: self.stacked_widget.setCurrentWidget(self.harvester_interface),
            ),
            self.harvester_route,
        )

        if config.DEBUG:
            self._init_debug_interface()

        self.craft_interface = CraftPage(self.bot, parent=self.stacked_widget)
        self.stacked_widget.addWidget(self.craft_interface)
        self.craft_route = f"{login}_craft"
        self._require_pivot_item(
            self.pivot.addItem(
                routeKey=self.craft_route,
                text="Craft",
                onClick=lambda: self.stacked_widget.setCurrentWidget(self.craft_interface),
            ),
            self.craft_route,
        )

        self.scripts_interface = ScriptsPage(self.bot, parent=self.stacked_widget)
        self.stacked_widget.addWidget(self.scripts_interface)
        self.scripts_route = f"{login}_scripts"
        self._require_pivot_item(
            self.pivot.addItem(
                routeKey=self.scripts_route,
                text="Scripts",
                onClick=lambda: self.stacked_widget.setCurrentWidget(self.scripts_interface),
            ),
            self.scripts_route,
        )

        self.automation_interface = AutomationPage(self.bot, parent=self.stacked_widget)
        self.automation_scroll = QScrollArea(self.stacked_widget)
        self.automation_scroll.setWidgetResizable(True)
        self.automation_scroll.setWidget(self.automation_interface)
        self.automation_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )
        self.stacked_widget.addWidget(self.automation_scroll)
        self.automation_route = f"{login}_automation"
        self._require_pivot_item(
            self.pivot.addItem(
                routeKey=self.automation_route,
                text="Automation",
                onClick=lambda: self.stacked_widget.setCurrentWidget(self.automation_scroll),
            ),
            self.automation_route,
        )

        self.hdv_interface = HdvPage(self.bot, parent=self.stacked_widget)
        self.stacked_widget.addWidget(self.hdv_interface)
        self.hdv_route = f"{login}_hdv"
        self._require_pivot_item(
            self.pivot.addItem(
                routeKey=self.hdv_route,
                text="HDV",
                onClick=lambda: self.stacked_widget.setCurrentWidget(self.hdv_interface),
            ),
            self.hdv_route,
        )

        for pivot_item in self.pivot.items.values():
            pivot_item.setFixedHeight(40)

        self.farmer_pivot_item.click()

    def _init_debug_interface(self) -> None:
        sniffer_interface = SnifferWidget(self.bot, self.global_log_signals, parent=self.stacked_widget)
        self.stacked_widget.addWidget(sniffer_interface)
        self.sniffer_route = f"{self.login}_sniffer"
        self._require_pivot_item(
            self.pivot.addItem(
                routeKey=self.sniffer_route,
                text="Debug",
                onClick=lambda: self.stacked_widget.setCurrentWidget(sniffer_interface),
            ),
            self.sniffer_route,
        )

    def set_snapshot_sub_area_name(self, sub_area_name: str | None) -> None:
        if self.quick_info_widget is not None:
            self.quick_info_widget.set_snapshot_sub_area_name(sub_area_name)
