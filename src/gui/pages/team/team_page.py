from collections.abc import Callable

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)
from qfluentwidgets.components.widgets.check_box import CheckBox

from src.core.bot.bot import Bot
from src.core.bot.team_controller import MAX_TEAM_SIZE, TeamController, TeamError
from src.core.signals.shared_farm_signals import SharedSignals
from src.lua.loader import ScriptInfo, list_scripts, scripts_folder

BotsProvider = Callable[[], dict[str, Bot]]


class TeamPage(QWidget):
    """Select 1 to 8 accounts, pick a leader, and run one script on all of them."""

    def __init__(
        self,
        shared_signals: SharedSignals,
        bots_provider: BotsProvider,
        team_controller: TeamController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent=parent)
        self.setObjectName("team")
        self._shared_signals = shared_signals
        self._bots_provider = bots_provider
        self._team = team_controller
        self._checkboxes: dict[str, CheckBox] = {}
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("Team", self))
        layout.addWidget(
            BodyLabel(
                f"Pick 1 to {MAX_TEAM_SIZE} accounts, choose the leader, then start a script "
                "on the whole team at once. Each account picks its role from team.isLeader().",
                self,
            )
        )

        self.accounts_card = CardWidget(self)
        self.accounts_layout = QVBoxLayout(self.accounts_card)
        self.accounts_layout.setContentsMargins(16, 16, 16, 16)
        self.accounts_layout.setSpacing(8)
        layout.addWidget(self.accounts_card)

        controls = QWidget(self)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(BodyLabel("Leader", controls))
        self.leader_combo = ComboBox(controls)
        self.leader_combo.setMinimumWidth(220)
        self.leader_combo.currentIndexChanged.connect(self._on_leader_changed)
        controls_layout.addWidget(self.leader_combo)
        controls_layout.addStretch(1)
        controls_layout.addWidget(BodyLabel("Script", controls))
        self.script_combo = ComboBox(controls)
        self.script_combo.setMinimumWidth(220)
        controls_layout.addWidget(self.script_combo)
        self.reload_btn = PushButton("Reload", controls)
        self.reload_btn.clicked.connect(self.reload_scripts)
        controls_layout.addWidget(self.reload_btn)
        layout.addWidget(controls)

        buttons = QWidget(self)
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.start_btn = PrimaryPushButton("Start on team", buttons)
        self.start_btn.clicked.connect(self.on_click_start)
        buttons_layout.addWidget(self.start_btn)
        self.stop_btn = PushButton("Stop team", buttons)
        self.stop_btn.clicked.connect(self.on_click_stop)
        buttons_layout.addWidget(self.stop_btn)
        buttons_layout.addStretch(1)
        layout.addWidget(buttons)

        self.status_label = BodyLabel(self)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self._shared_signals.new_bot_added.connect(self._on_bots_changed)
        self._shared_signals.bot_removed.connect(self._on_bots_changed)

        self.refresh_accounts()
        self.reload_scripts()

    @pyqtSlot()
    def reload_scripts(self) -> None:
        selected = self.selected_script()
        self.script_combo.clear()
        for script in list_scripts():
            self.script_combo.addItem(script.name, userData=script)
        if selected is not None:
            index = self.script_combo.findText(selected.name)
            if index != -1:
                self.script_combo.setCurrentIndex(index)

    def selected_script(self) -> ScriptInfo | None:
        data = self.script_combo.currentData()
        return data if isinstance(data, ScriptInfo) else None

    def _selected_logins(self) -> list[str]:
        return [login for login, box in self._checkboxes.items() if box.isChecked()]

    def _on_bots_changed(self, _bot: object) -> None:
        self.refresh_accounts()

    def _make_member_handler(self, login: str) -> Callable[[int], None]:
        def handler(_state: int) -> None:
            self._on_member_toggled(login)

        return handler

    def refresh_accounts(self) -> None:
        bots = self._bots_provider()
        previously_checked = set(self._selected_logins())
        self._syncing = True
        try:
            while self.accounts_layout.count():
                item = self.accounts_layout.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.deleteLater()
            self._checkboxes = {}
            for login, bot in bots.items():
                name = bot.game_state.player.character_name
                label = f"{name} ({login})" if name else login
                box = CheckBox(label, self.accounts_card)
                box.setChecked(login in previously_checked)
                box.stateChanged.connect(self._make_member_handler(login))
                self.accounts_layout.addWidget(box)
                self._checkboxes[login] = box
            if not bots:
                self.accounts_layout.addWidget(BodyLabel("No account is loaded yet.", self.accounts_card))
        finally:
            self._syncing = False
        self._sync_team()

    def _on_member_toggled(self, login: str) -> None:
        if self._syncing:
            return
        box = self._checkboxes.get(login)
        if box is None:
            return
        try:
            self._team.toggle_member(login, box.isChecked())
        except TeamError as error:
            self._syncing = True
            box.setChecked(not box.isChecked())
            self._syncing = False
            self.status_label.setText(str(error))
        self._sync_team()

    def _sync_team(self) -> None:
        logins = self._selected_logins()
        self._syncing = True
        try:
            self.leader_combo.clear()
            for login in logins:
                bot = self._bots_provider().get(login)
                name = bot.game_state.player.character_name if bot is not None else ""
                self.leader_combo.addItem(f"{name} ({login})" if name else login, userData=login)
            if self._team.leader in logins:
                index = self.leader_combo.findData(self._team.leader)
                if index != -1:
                    self.leader_combo.setCurrentIndex(index)
        finally:
            self._syncing = False
        self.status_label.setText(f"{len(logins)} / {MAX_TEAM_SIZE} selected")

    def _on_leader_changed(self, _index: int) -> None:
        if self._syncing:
            return
        login = self.leader_combo.currentData()
        if isinstance(login, str):
            self._team.set_leader(login)

    def on_click_start(self) -> None:
        script = self.selected_script()
        if script is None:
            self.status_label.setText(f"No script selected. Add .lua files to {scripts_folder()}")
            return
        logins = self._selected_logins()
        if not logins:
            self.status_label.setText("Select at least one account.")
            return
        self._team.set_team(logins)
        leader = self.leader_combo.currentData()
        if isinstance(leader, str) and leader:
            self._team.set_leader(leader)
        started = self._team.start_script(str(script.path))
        self.status_label.setText(f"Started {script.name} on {started} character(s)")

    def on_click_stop(self) -> None:
        stopped = self._team.stop()
        self.status_label.setText(f"Stopped {stopped} character(s)")
