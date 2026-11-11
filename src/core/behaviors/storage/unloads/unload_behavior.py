from dataclasses import dataclass

from src.core.behaviors.recovery_behavior import RecoverableBehavior
from src.core.behaviors.storage.unloads.unload_in_bank_behavior import (
    UnloadInBankBehavior,
)
from src.core.behaviors.storage.unloads.unload_in_guild_chest_behavior import (
    UnloadInGuildChestBehavior,
)
from src.core.states.guild_chest_state import GIDS_BY_TAB


@dataclass
class UnloadBehavior(RecoverableBehavior):
    unload_in_bank_behavior: UnloadInBankBehavior
    unload_in_guild_chest_behavior: UnloadInGuildChestBehavior

    def run(self) -> None:
        self.init_recovery_listeners()
        self.ensure_free_to_act(lambda: self.start_unloading())

    def start_unloading(self) -> None:
        if self.game_state.settings.storage_target == "guild_chest":
            self.unload_in_guild_chest_behavior.start(
                unload_item_id_by_tab=GIDS_BY_TAB,
                parent=self,
                callback=self.on_unload_in_guild_chest_behavior_finished,
            )
            return
        self.unload_in_bank_behavior.start(callback=self.finish, parent=self)

    def on_unload_in_guild_chest_behavior_finished(self, error_code: str | None) -> None:
        if error_code is not None:
            self.logger.error(f"Guild chest unload failed: {error_code}")
        self.finish(error_code)

