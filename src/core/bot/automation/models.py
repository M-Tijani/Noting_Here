import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ankama_launcher_emulator.interfaces.schedule_profile import TimeSlot

_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
RestartPolicy = Literal["restart_on_finish", "stop"]


class AccountAutomationPlan(BaseModel):
    login: str
    enabled: bool = True
    script: str | None = None
    fallback_script: str | None = None
    schedule_profile: str | None = None
    slots_by_day: dict[str, list[TimeSlot]] = Field(default_factory=dict)
    session_duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    restart_policy: RestartPolicy = "restart_on_finish"
    restart_delay_seconds: int = Field(default=30, ge=0, le=3600)
    max_retries: int = Field(default=3, ge=0, le=100)

    @field_validator("login")
    @classmethod
    def _login_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Login is required.")
        return value

    @field_validator("script", "fallback_script")
    @classmethod
    def _script_must_be_a_relative_lua_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Script must be a relative path inside the scripts folder.")
        if path.suffix.lower() != ".lua":
            raise ValueError("Script must be a .lua file.")
        return path.as_posix()

    @field_validator("schedule_profile")
    @classmethod
    def _schedule_profile_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("slots_by_day")
    @classmethod
    def _validate_slot_shape(cls, value: dict[str, list[TimeSlot]]) -> dict[str, list[TimeSlot]]:
        valid_days = {str(index) for index in range(7)}
        for day, slots in value.items():
            if day not in valid_days:
                raise ValueError("Invalid schedule day.")
            for slot in slots:
                if not (_TIME_PATTERN.fullmatch(slot.start) and _TIME_PATTERN.fullmatch(slot.end)):
                    raise ValueError("Times must use the HH:MM format.")
        return value

    @model_validator(mode="after")
    def _enabled_plan_needs_a_time_source(self) -> "AccountAutomationPlan":
        if self.enabled and self.schedule_profile is None and not self.slots_by_day:
            raise ValueError("An enabled plan needs a schedule profile or per-account time slots.")
        if self.fallback_script is not None and self.script is None:
            raise ValueError("Fallback script requires a primary script.")
        if self.fallback_script is not None and self.fallback_script == self.script:
            raise ValueError("Fallback script must differ from the primary script.")
        return self


class AutomationPlans(BaseModel):
    plans: dict[str, AccountAutomationPlan] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_must_match_login(self) -> "AutomationPlans":
        for login, plan in self.plans.items():
            if login != plan.login:
                raise ValueError(f"Plan key {login!r} does not match login {plan.login!r}.")
        return self
