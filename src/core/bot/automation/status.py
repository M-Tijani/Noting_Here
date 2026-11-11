from dataclasses import dataclass
from datetime import datetime

from src.core.bot.automation.planner import PlannedSession


@dataclass(frozen=True)
class AutomationStatus:
    current: PlannedSession | None
    next_start: datetime | None
    script: str | None
    started_at: datetime | None
    session_end: datetime | None
    retry_count: int
    using_fallback: bool
