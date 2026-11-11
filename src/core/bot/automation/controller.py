import re
from pathlib import Path
from threading import RLock
from typing import Any

from ankama_launcher_emulator.controller.schedule_profile import ScheduleProfileController
from ankama_launcher_emulator.interfaces.schedule_profile import (
    SCHEDULE_RANDOM_MINUTES_MAX,
    TimeSlot,
)
from ankama_launcher_emulator.utils.atomic_file import acquire_file_lock, atomic_write_text
from utils.local_json import read_local_model
from utils.singleton import Singleton

from src.consts import AUTOMATION_PLANS_PATH, LUA_SCRIPTS_FOLDER
from src.core.bot.automation.models import AccountAutomationPlan, AutomationPlans

_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class AutomationPlanController(metaclass=Singleton):
    _LOCK = RLock()

    def get_all_plans(self) -> dict[str, AccountAutomationPlan]:
        with self._LOCK:
            if not AUTOMATION_PLANS_PATH.exists():
                return {}
            return read_local_model(AUTOMATION_PLANS_PATH, AutomationPlans).plans

    def get_plan(self, login: str) -> AccountAutomationPlan | None:
        return self.get_all_plans().get(login)

    def remove_plan(self, login: str) -> None:
        with self._LOCK, acquire_file_lock(AUTOMATION_PLANS_PATH):
            plans = self.get_all_plans()
            if plans.pop(login, None) is not None:
                self._write(plans)

    def save_plan(self, plan: AccountAutomationPlan, *, create: bool = False) -> None:
        self._validate_plan(plan)
        with self._LOCK, acquire_file_lock(AUTOMATION_PLANS_PATH):
            plans = self.get_all_plans()
            if create and plan.login in plans:
                raise ValueError("This account already has an automation plan.")
            if not create and plan.login not in plans:
                raise ValueError("This automation plan no longer exists. Refresh the list.")
            plans[plan.login] = plan
            self._write(plans)

    def resolve_slots(self, plan: AccountAutomationPlan) -> dict[str, list[TimeSlot]]:
        if plan.slots_by_day:
            return plan.slots_by_day
        if plan.schedule_profile is None:
            return {}
        profile = ScheduleProfileController().get_profile(plan.schedule_profile)
        return profile.slots_by_day if profile is not None else {}

    def resolve_script_path(self, plan: AccountAutomationPlan) -> Path | None:
        if plan.script is None:
            return None
        scripts_root = LUA_SCRIPTS_FOLDER.resolve()
        candidate = (scripts_root / plan.script).resolve()
        try:
            candidate.relative_to(scripts_root)
        except ValueError as error:
            raise ValueError("Script must stay inside the scripts folder.") from error
        if not candidate.is_file():
            raise ValueError(f"Script not found: {plan.script}")
        return candidate

    def assign_script(
        self, login: str, schedule_profile: str | None, script: str | None
    ) -> None:
        plan = self.get_plan(login)
        if plan is None:
            if script is None:
                return
            if schedule_profile is None:
                raise ValueError("Choose a schedule before assigning a script.")
            self.save_plan(
                AccountAutomationPlan(
                    login=login, script=script, schedule_profile=schedule_profile
                ),
                create=True,
            )
            return

        updates: dict[str, Any] = {"script": script}
        if not plan.slots_by_day:
            updates["schedule_profile"] = schedule_profile
        self.save_plan(plan.model_copy(update=updates))

    def _validate_plan(self, plan: AccountAutomationPlan) -> None:
        if plan.enabled:
            for script in (plan.script, plan.fallback_script):
                self.resolve_script_path(plan.model_copy(update={"script": script}))
        if plan.schedule_profile is not None:
            if ScheduleProfileController().get_profile(plan.schedule_profile) is None:
                raise ValueError("Unknown schedule profile.")
        if plan.slots_by_day:
            _validate_slots(plan.slots_by_day)

    def _write(self, plans: dict[str, AccountAutomationPlan]) -> None:
        atomic_write_text(
            AUTOMATION_PLANS_PATH,
            AutomationPlans(plans=plans).model_dump_json(indent=2),
        )


def _validate_slots(slots_by_day: dict[str, list[TimeSlot]]) -> None:
    occupied: set[int] = set()
    valid_days = {str(index) for index in range(7)}
    for day, slots in slots_by_day.items():
        if day not in valid_days:
            raise ValueError("Invalid schedule day.")
        for slot in slots:
            if not all(_TIME_PATTERN.fullmatch(value) for value in (slot.start, slot.end)):
                raise ValueError("Times must use the HH:MM format.")
            start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (slot.start, slot.end))
            duration = (end - start) % 1440
            if duration <= 2 * SCHEDULE_RANDOM_MINUTES_MAX:
                raise ValueError("Each time slot must exceed one hour to preserve scheduling margins.")
            minutes = {(int(day) * 1440 + start + offset) % 10080 for offset in range(duration)}
            if occupied & minutes:
                raise ValueError("Schedule time slots overlap.")
            occupied.update(minutes)
