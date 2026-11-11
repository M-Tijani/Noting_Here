from datetime import datetime

from PyQt6.QtWidgets import QSpinBox, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox
from qfluentwidgets.components.widgets.check_box import CheckBox

from src.controller.bot_config import BotConfig, BotConfigService
from src.core.bot.automation import (
    AccountAutomationPlan,
    AutomationPlanController,
    AutomationStatus,
)
from src.core.bot.bot import Bot
from src.gui.pages.settings.schedule_day_editor import ScheduleDayEditor
from src.gui.pages.settings.settings_panel import SettingsPanel
from src.lua.loader import ScriptInfo, list_scripts, scripts_folder
from src.services.background import run_in_background

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_INHERIT_SCHEDULE = 0
_CUSTOM_SCHEDULE = 1


class AutomationPage(SettingsPanel):
    def __init__(self, bot: Bot, parent: QWidget | None = None) -> None:
        super().__init__(
            "Choose the Lua script and schedule for this account. Enrollment is saved to "
            "automation_plans.json; the current session finishes normally when the plan changes."
        )
        self.setParent(parent)
        self.bot = bot
        self.login = bot.account.apikey.login
        self.plan: AccountAutomationPlan | None = None
        self.account_schedule_profile: str | None = None

        self.enabled = CheckBox("Enable scheduled automation", self)
        self.form.addRow(self.enabled)

        self.script = self.combo("Script")
        self.fallback_script = self.combo("Fallback script")
        self.schedule_source = self.combo(
            "Schedule", ["Inherit from account schedule", "Custom time slots"]
        )
        self.session_duration = QSpinBox(self)
        self.session_duration.setRange(0, 24 * 60)
        self.session_duration.setSpecialValueText("Full window")
        self.session_duration.setSuffix(" min")
        self.form.addRow(BodyLabel("Session duration", self), self.session_duration)

        self.restart_policy = self.combo("On script completion", ["Restart the script", "Stop"])
        self.restart_delay = QSpinBox(self)
        self.restart_delay.setRange(0, 3600)
        self.restart_delay.setSuffix(" s")
        self.form.addRow(BodyLabel("Restart delay", self), self.restart_delay)

        self.max_retries = QSpinBox(self)
        self.max_retries.setRange(0, 100)
        self.form.addRow(BodyLabel("Max retries", self), self.max_retries)

        self.override_widget = QWidget(self)
        override_layout = QVBoxLayout(self.override_widget)
        override_layout.setContentsMargins(0, 0, 0, 0)
        override_layout.setSpacing(12)
        self.days = [ScheduleDayEditor(day, self.override_widget) for day in _DAYS]
        for day in self.days:
            override_layout.addWidget(day)
        self.form.addRow(BodyLabel("Custom time slots", self), self.override_widget)

        self.schedule_source.currentIndexChanged.connect(self._update_schedule_visibility)
        self._update_schedule_visibility()

    def reload(self) -> None:
        def load() -> tuple[AccountAutomationPlan | None, BotConfig, list[ScriptInfo]]:
            config = BotConfigService().get_bot_config_by_login().get(self.login, BotConfig())
            return (
                AutomationPlanController().get_plan(self.login),
                config,
                list_scripts(),
            )

        self.perform(load, self._render)

    def _render(
        self, result: tuple[AccountAutomationPlan | None, BotConfig, list[ScriptInfo]]
    ) -> None:
        plan, config, scripts = result
        self.plan = plan
        self.account_schedule_profile = config.schedule_profile

        self.enabled.setChecked(plan.enabled if plan is not None else False)
        self._populate_script_combo(
            self.script, scripts, plan.script if plan is not None else None, "None (automatic behavior)"
        )
        self._populate_script_combo(
            self.fallback_script,
            scripts,
            plan.fallback_script if plan is not None else None,
            "No fallback",
        )
        self.schedule_source.setCurrentIndex(
            _CUSTOM_SCHEDULE if plan is not None and plan.slots_by_day else _INHERIT_SCHEDULE
        )
        for index, day in enumerate(self.days):
            day.set_slots(plan.slots_by_day.get(str(index), []) if plan is not None else [])
        self.session_duration.setValue(
            plan.session_duration_minutes if plan is not None and plan.session_duration_minutes else 0
        )
        self.restart_policy.setCurrentIndex(
            0 if plan is None or plan.restart_policy == "restart_on_finish" else 1
        )
        self.restart_delay.setValue(plan.restart_delay_seconds if plan is not None else 30)
        self.max_retries.setValue(plan.max_retries if plan is not None else 3)
        self._update_schedule_visibility()
        self.status.setText(self._status_text(plan))

    def _populate_script_combo(
        self,
        combo: ComboBox,
        scripts: list[ScriptInfo],
        selected: str | None,
        none_label: str,
    ) -> None:
        combo.clear()
        combo.addItem(none_label)
        root = scripts_folder()
        for script in scripts:
            try:
                relative = script.path.relative_to(root).as_posix()
            except ValueError:
                relative = script.path.name
            combo.addItem(script.name, userData=relative)

        if selected is None:
            combo.setCurrentIndex(0)
            return
        index = combo.findData(selected)
        if index < 0:
            combo.addItem(selected, userData=selected)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def _update_schedule_visibility(self) -> None:
        self.override_widget.setVisible(self.schedule_source.currentIndex() == _CUSTOM_SCHEDULE)

    @staticmethod
    def _selected_script(combo: ComboBox) -> str | None:
        data = combo.currentData()
        return data if isinstance(data, str) and data else None

    def _status_text(self, plan: AccountAutomationPlan | None) -> str:
        if plan is None:
            return "No automation plan for this account."
        if not plan.enabled:
            return "Automation is disabled for this account."
        state = self.bot.scheduler.automation_status()
        if state is None:
            return "Automation is enabled; save or refresh to load the schedule."
        return self._format_state(state)

    @staticmethod
    def _format_state(state: AutomationStatus) -> str:
        lines: list[str] = []
        if state.current is not None:
            lines.append(
                f"Current session: {_format_moment(state.current.start)} → "
                f"{_format_moment(state.current.end)}"
            )
        if state.script is not None:
            fallback = " (fallback)" if state.using_fallback else ""
            lines.append(f"Current script: {state.script}{fallback}")
        if state.started_at is not None:
            lines.append(f"Started at: {_format_moment(state.started_at)}")
        if state.session_end is not None:
            lines.append(f"Session end: {_format_moment(state.session_end)}")
        if state.retry_count:
            lines.append(f"Retries this session: {state.retry_count}")
        if state.next_start is not None:
            lines.append(f"Next start: {_format_moment(state.next_start)}")
        if not lines:
            return "No upcoming session in the configured schedule."
        return "\n".join(lines)

    def save(self) -> None:
        custom_schedule = self.schedule_source.currentIndex() == _CUSTOM_SCHEDULE
        slots = (
            {str(index): day.values() for index, day in enumerate(self.days) if day.values()}
            if custom_schedule
            else {}
        )
        if self.enabled.isChecked() and custom_schedule and not slots:
            self.invalid("Add at least one custom time slot or inherit the account schedule.")
            return
        schedule_profile = None if custom_schedule else self.account_schedule_profile
        if self.enabled.isChecked() and not custom_schedule and schedule_profile is None:
            self.invalid("Choose a schedule for this account before enabling automation.")
            return
        script = self._selected_script(self.script)
        fallback_script = self._selected_script(self.fallback_script)
        if fallback_script is not None and script is None:
            self.invalid("Choose a primary script before selecting a fallback script.")
            return
        if fallback_script is not None and fallback_script == script:
            self.invalid("The fallback script must differ from the primary script.")
            return

        plan = AccountAutomationPlan(
            login=self.login,
            enabled=self.enabled.isChecked(),
            script=script,
            fallback_script=fallback_script,
            schedule_profile=schedule_profile,
            slots_by_day=slots,
            session_duration_minutes=self.session_duration.value() or None,
            restart_policy="restart_on_finish" if self.restart_policy.currentIndex() == 0 else "stop",
            restart_delay_seconds=self.restart_delay.value(),
            max_retries=self.max_retries.value(),
        )
        create = self.plan is None

        def done(_: None) -> None:
            run_in_background(
                lambda _progress: self.bot.scheduler.request_configuration_refresh(),
                on_success=lambda _result: self.reload(),
                parent=self,
            )

        self.perform(
            lambda: AutomationPlanController().save_plan(plan, create=create), done
        )


def _format_moment(moment: datetime) -> str:
    return moment.strftime("%a %d %b %H:%M")
