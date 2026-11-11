from PyQt6.QtCore import QObject, pyqtSignal


class BotSignals(QObject):
    play_harvester = pyqtSignal(object, object, object)
    play_fighter = pyqtSignal(object, object)
    play_auto_bot = pyqtSignal(object)
    play_crafter = pyqtSignal(object)
    play_usable_behavior = pyqtSignal(str)
    play_script = pyqtSignal(str)
    play = pyqtSignal(bool)
    stop = pyqtSignal()
    disconnect_runtime = pyqtSignal()
    automation_status_changed = pyqtSignal(str)
