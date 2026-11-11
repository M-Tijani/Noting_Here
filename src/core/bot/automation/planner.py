import logging
import random
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ankama_launcher_emulator.interfaces.schedule_profile import (
    SCHEDULE_RANDOM_MINUTES_MAX,
    SCHEDULE_RANDOM_MINUTES_MIN,
    TimeSlot,
)

from src.core.bot.automation.models import AccountAutomationPlan

logger = logging.getLogger(__name__)

_MINUTES_PER_DAY = 24 * 60
_LOOKAHEAD_DAYS = 9


@dataclass(frozen=True)
class PlannedSession:
    start: datetime
    window_end: datetime
    end: datetime

    @property
    def is_clamped(self) -> bool:
        return self.end < self.window_end


@dataclass(frozen=True)
class AutomationPlanState:
    current: PlannedSession | None
    next_start: datetime | None


@dataclass(frozen=True)
class _Occurrence:
    start: datetime
    window_end: datetime


class AutomationPlanner:
    def __init__(self, random_minutes: Callable[[int, int], int] | None = None) -> None:
        self._random_minutes: Callable[[int, int], int] = random_minutes or random.randint

    def randomize_slots(
        self, slots_by_day: Mapping[str, Sequence[TimeSlot]]
    ) -> dict[int, list[TimeSlot]]:
        randomized: dict[int, list[TimeSlot]] = {}
        for day, slots in slots_by_day.items():
            randomized[int(day)] = [
                TimeSlot(
                    start=_shift_time(
                        slot.start,
                        self._random_minutes(SCHEDULE_RANDOM_MINUTES_MIN, SCHEDULE_RANDOM_MINUTES_MAX),
                    ),
                    end=_shift_time(
                        slot.end,
                        -self._random_minutes(SCHEDULE_RANDOM_MINUTES_MIN, SCHEDULE_RANDOM_MINUTES_MAX),
                    ),
                )
                for slot in slots
            ]
        return randomized

    def plan(
        self,
        plan: AccountAutomationPlan,
        slots_by_day: Mapping[str, Sequence[TimeSlot]],
        now: datetime,
    ) -> AutomationPlanState:
        return self.plan_for_slots(plan, self.randomize_slots(slots_by_day), now)

    def plan_for_slots(
        self,
        plan: AccountAutomationPlan,
        randomized_slots: Mapping[int, Sequence[TimeSlot]],
        now: datetime,
    ) -> AutomationPlanState:
        if not plan.enabled or not randomized_slots:
            state = AutomationPlanState(current=None, next_start=None)
            _log_state(plan.login, state)
            return state

        occurrences = list(
            _occurrences(randomized_slots, now.date() - timedelta(days=1), _LOOKAHEAD_DAYS + 1)
        )
        occurrences.sort(key=lambda occurrence: occurrence.start)
        current = _current_session(occurrences, plan.session_duration_minutes, now)
        next_start = next(
            (
                occurrence.start
                for occurrence in occurrences
                if occurrence.start > now
                and (current is None or occurrence.start >= current.end)
            ),
            None,
        )
        state = AutomationPlanState(current=current, next_start=next_start)
        _log_state(plan.login, state)
        return state


def _occurrences(
    slots_by_day: Mapping[int, Sequence[TimeSlot]],
    first_date: date,
    day_count: int,
) -> Iterator[_Occurrence]:
    for offset in range(day_count):
        slot_date = first_date + timedelta(days=offset)
        for slot in slots_by_day.get(slot_date.weekday(), []):
            start = datetime.combine(slot_date, _parse_time(slot.start))
            window_end = datetime.combine(slot_date, _parse_time(slot.end))
            if window_end <= start:
                window_end += timedelta(days=1)
            yield _Occurrence(start=start, window_end=window_end)


def _current_session(
    occurrences: Sequence[_Occurrence],
    session_duration_minutes: int | None,
    now: datetime,
) -> PlannedSession | None:
    matches = [
        occurrence
        for occurrence in occurrences
        if occurrence.start <= now < _effective_end(occurrence, session_duration_minutes)
    ]
    if not matches:
        return None
    occurrence = max(matches, key=lambda match: match.start)
    return PlannedSession(
        start=occurrence.start,
        window_end=occurrence.window_end,
        end=_effective_end(occurrence, session_duration_minutes),
    )


def _effective_end(occurrence: _Occurrence, session_duration_minutes: int | None) -> datetime:
    if session_duration_minutes is None:
        return occurrence.window_end
    return min(occurrence.window_end, occurrence.start + timedelta(minutes=session_duration_minutes))


def _parse_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour=hour, minute=minute)


def _shift_time(value: str, minutes: int) -> str:
    hour, minute = map(int, value.split(":"))
    total = (hour * 60 + minute + minutes) % _MINUTES_PER_DAY
    return f"{total // 60:02d}:{total % 60:02d}"


def _log_state(login: str, state: AutomationPlanState) -> None:
    if state.current is not None:
        logger.info(
            "Automation plan for %s: session %s -> %s (window ends %s)",
            login,
            state.current.start.isoformat(),
            state.current.end.isoformat(),
            state.current.window_end.isoformat(),
        )
    else:
        logger.info("Automation plan for %s: no active session", login)
    if state.next_start is not None:
        logger.info("Automation plan for %s: next start %s", login, state.next_start.isoformat())
