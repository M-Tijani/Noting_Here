import logging

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, FluentIcon, PushButton, TransparentToolButton

from src.core.bot.bot import Bot
from src.lua.loader import ScriptInfo, list_scripts, scripts_folder

logger = logging.getLogger(__name__)


class ScriptsPage(QWidget):
    def __init__(self, bot: Bot, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.bot = bot

        self.main_layout = QVBoxLayout()
        self.main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setLayout(self.main_layout)

        top_widget = QWidget(self)
        top_layout = QHBoxLayout()
        top_widget.setLayout(top_layout)

        self.play_btn = TransparentToolButton(FluentIcon.PLAY, top_widget)
        self.play_btn.clicked.connect(self.on_click_play)
        self.bot.bot_signals.play.connect(self.on_play)
        top_layout.addWidget(self.play_btn)

        self.stop_btn = TransparentToolButton(FluentIcon.PAUSE, top_widget)
        self.stop_btn.clicked.connect(self.on_click_stop)
        self.bot.bot_signals.stop.connect(self.on_stop)
        top_layout.addWidget(self.stop_btn)
        self.stop_btn.hide()

        self.script_combo = ComboBox(top_widget)
        top_layout.addWidget(self.script_combo)

        self.reload_btn = PushButton("Reload", top_widget)
        self.reload_btn.clicked.connect(self.reload_scripts)
        top_layout.addWidget(self.reload_btn)

        self.main_layout.addWidget(top_widget)

        self.status_label = BodyLabel(self)
        self.main_layout.addWidget(self.status_label)

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
        self._update_status()

    def selected_script(self) -> ScriptInfo | None:
        data = self.script_combo.currentData()
        return data if isinstance(data, ScriptInfo) else None

    def _update_status(self) -> None:
        folder = scripts_folder()
        if self.script_combo.count() == 0:
            self.status_label.setText(f"No script found. Add .lua files to {folder}")
        else:
            self.status_label.setText(f"Scripts folder: {folder}")

    @pyqtSlot()
    def on_click_play(self) -> None:
        script = self.selected_script()
        if script is None:
            logger.warning("Play clicked but no script selected (folder: %s)", scripts_folder())
            self.status_label.setText(f"No script selected. Add .lua files to {scripts_folder()}")
            return
        logger.info("Play clicked for script %s (%s)", script.name, script.path)
        self.bot.bot_signals.play.emit(True)
        self.bot.bot_signals.play_script.emit(str(script.path))

    @pyqtSlot()
    def on_click_stop(self) -> None:
        self.bot.bot_signals.stop.emit()

    @pyqtSlot(bool)
    def on_play(self, _: bool) -> None:
        self.stop_btn.show()
        self.play_btn.hide()

    @pyqtSlot()
    def on_stop(self) -> None:
        self.play_btn.show()
        self.stop_btn.hide()
        self.reload_scripts()
