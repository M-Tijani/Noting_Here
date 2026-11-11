from typing import cast

from qfluentwidgets.components.widgets.check_box import CheckBox

from src.controller.settings import SettingsService
from src.core.config import BehaviorSettings, GlobalSettings, StorageTarget
from src.gui.pages.settings.settings_panel import SettingsPanel

BEHAVIOR_LABELS = {
    "do_fighter": "Automatic combat",
    "do_sale_hotel": "Marketplace sales",
    "do_craft": "Automatic crafting",
    "do_dungeon": "Donjons",
    "do_idle": "Breaks during sessions",
    "enable_auto_equipment_market_purchases": "Allow equipment purchases",
    "enable_auto_ogrine_subscriptions": "Allow Ogrine subscriptions",
    "enable_auto_paysafecard_subscriptions": "Allow Paysafecard subscriptions",
}

STORAGE_TARGET_LABELS: dict[StorageTarget, str] = {
    "guild_chest": "Guild chest",
    "bank": "Bank (NPC)",
}
STORAGE_TARGET_BY_LABEL = {label: target for target, label in STORAGE_TARGET_LABELS.items()}


class BehaviorSettingsPanel(SettingsPanel):
    def __init__(self) -> None:
        super().__init__(
            "Behaviors apply the next time the bot starts manually or on schedule. "
            "Account automation applies to the next operation; the current one finishes normally."
        )
        self.checks: dict[str, CheckBox] = {}
        for name, label in BEHAVIOR_LABELS.items():
            check = CheckBox(label, self)
            self.form.addRow(check)
            self.checks[name] = check
        self.storage_target = self.combo("Item storage", list(STORAGE_TARGET_LABELS.values()))
        self.automation = CheckBox("Automatically create and authenticate accounts", self)
        self.form.addRow(self.automation)
        self.hide_fight_animations = CheckBox(
            "Hide fight spell effects in the game client (experimental)", self
        )
        self.form.addRow(self.hide_fight_animations)
        self.fight_allowed_spell_ids = self.line(
            "Combat spell whitelist (IDs separated by commas, empty = all spells)"
        )

    def reload(self) -> None:
        def render(settings: GlobalSettings) -> None:
            for name, check in self.checks.items():
                check.setChecked(getattr(settings.behaviors, name))
            self.storage_target.setCurrentText(STORAGE_TARGET_LABELS[settings.behaviors.storage_target])
            self.automation.setChecked(settings.enable_account_automation)
            self.hide_fight_animations.setChecked(settings.hide_fight_animations)
            self.fight_allowed_spell_ids.setText(
                ", ".join(str(spell_id) for spell_id in settings.behaviors.fight_allowed_spell_ids)
            )

        self.perform(SettingsService().get, render)

    def save(self) -> None:
        try:
            spell_ids = [int(part) for part in self.fight_allowed_spell_ids.text().split(",") if part.strip()]
        except ValueError:
            self.invalid("Spell IDs must be numbers separated by commas.")
            return
        behaviors = BehaviorSettings.model_validate(
            {
                **{name: check.isChecked() for name, check in self.checks.items()},
                "storage_target": cast(
                    StorageTarget, STORAGE_TARGET_BY_LABEL[self.storage_target.currentText()]
                ),
                "fight_allowed_spell_ids": spell_ids,
            }
        )
        automation = self.automation.isChecked()
        hide_fight_animations = self.hide_fight_animations.isChecked()

        self.perform(
            lambda: SettingsService().update_behaviors(behaviors, automation, hide_fight_animations),
            lambda _: self.reload(),
        )
