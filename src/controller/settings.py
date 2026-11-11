import json
import os
from threading import RLock
from typing import cast

from ankama_launcher_emulator.utils.atomic_file import acquire_file_lock, atomic_write_text
from pydantic import ValidationError

from src.core.config import BehaviorSettings, GlobalSettings
from src.utils.project_paths import USER_DATA_ROOT
from src.utils.runtime_support import RuntimeSetupError
from utils.local_json import read_local_model
from utils.singleton import Singleton

SETTINGS_PATH = USER_DATA_ROOT / "resources" / "settings.json"


class SettingsService(metaclass=Singleton):
    def __init__(self) -> None:
        self._lock = RLock()
        self._settings: GlobalSettings | None = None

    def get(self) -> GlobalSettings:
        with self._lock:
            if self._settings is None:
                self._settings = (
                    self._load_settings()
                    if SETTINGS_PATH.exists()
                    else GlobalSettings()
                )
            return self._settings

    def _load_settings(self) -> GlobalSettings:
        try:
            payload: object = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return read_local_model(SETTINGS_PATH, GlobalSettings)

        if not isinstance(payload, dict):
            return read_local_model(SETTINGS_PATH, GlobalSettings)
        payload = cast(dict[str, object], payload)
        behaviors = payload.get("behaviors")
        if not isinstance(behaviors, dict):
            return read_local_model(SETTINGS_PATH, GlobalSettings)

        migrated_behaviors = dict(cast(dict[str, object], behaviors))
        migrated = False
        if "do_quest" in migrated_behaviors:
            del migrated_behaviors["do_quest"]
            migrated = True
        if "do_use_guild_chest" in migrated_behaviors:
            use_guild_chest = migrated_behaviors.pop("do_use_guild_chest") is True
            migrated_behaviors.setdefault(
                "storage_target", "guild_chest" if use_guild_chest else "bank"
            )
            migrated = True
        if not migrated:
            return read_local_model(SETTINGS_PATH, GlobalSettings)

        migrated_payload = dict(payload)
        migrated_payload["behaviors"] = migrated_behaviors
        try:
            settings = GlobalSettings.model_validate(migrated_payload)
        except ValidationError as error:
            message = f"Invalid local file: {SETTINGS_PATH.name}. Fix it or restore a backup."
            raise RuntimeSetupError(message) from error
        self._save(settings)
        return settings

    def _save(self, settings: GlobalSettings) -> None:
        with acquire_file_lock(SETTINGS_PATH):
            atomic_write_text(SETTINGS_PATH, settings.model_dump_json(indent=2))
            self._settings = settings

    def sonji_api_key(self) -> str | None:
        key = self.get().sonji_api_key
        return (os.getenv("SONJI_API_KEY") if key is None else key) or None

    def discord_bot_token(self) -> str | None:
        token = self.get().discord_bot_token
        return (os.getenv("DISCORD_BOT_TOKEN") if token is None else token) or None

    def discord_channel_id(self) -> int | None:
        return self.get().discord_channel_id

    def update_behaviors(
        self,
        behaviors: BehaviorSettings,
        enabled: bool,
        hide_fight_animations: bool | None = None,
    ) -> None:
        with self._lock:
            current = self.get()
            updates: dict[str, object] = {
                "behaviors": behaviors,
                "enable_account_automation": enabled,
            }
            if hide_fight_animations is not None:
                updates["hide_fight_animations"] = hide_fight_animations
            self._save(current.model_copy(update=updates))

    def update_sonji_key(self, key: str | None) -> None:
        with self._lock:
            self._save(self.get().model_copy(update={"sonji_api_key": key}))

    def update_discord_settings(self, token: str | None, channel_id: int | None) -> None:
        with self._lock:
            self._save(
                self.get().model_copy(update={"discord_bot_token": token, "discord_channel_id": channel_id})
            )

    def toggle_hide_fight_animations(self) -> bool:
        with self._lock:
            new_value = not self.get().hide_fight_animations
            self._save(self.get().model_copy(update={"hide_fight_animations": new_value}))
            return new_value
