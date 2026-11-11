import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, RLock, Timer
from time import sleep

import schedule
from ankama_launcher_emulator.controller.bot_storage import (
    BotStorageController,
)
from ankama_launcher_emulator.controller.schedule_profile import (
    ScheduleProfileController,
)
from ankama_launcher_emulator.interfaces.credentials import (
    StoredApiKey,
)
from ankama_launcher_emulator.interfaces.schedule_profile import (
    TimeSlot,
)

from src.controller.bot_config import BotConfig
from src.core.bot.automation import (
    AccountAutomationPlan,
    AutomationPlanController,
    AutomationPlanner,
    AutomationPlanState,
    AutomationStatus,
    PlannedSession,
)
from src.core.bot.execution.behavior_coordinator import BehaviorCoordinator
from src.core.bot.execution.process_manager import ProcessManager
from src.core.events_manager.event_manager import EventManager
from src.core.signals.bot_signals import BotSignals
from src.core.signals.log_signals import LogSignals
from src.core.signals.message_signals import MessageInfoSignals
from src.core.signals.shared_farm_signals import SharedSignals
from src.services.background import run_in_background
from src.services.logging_utils.contextual_logger import ContextualLogger
from src.services.user_activity import UserActivityService
from src.utils.runtime_support import RuntimeSetupError


@dataclass(frozen=True)
class ScheduledSessionWindow:
    start: datetime
    end: datetime


@dataclass
class BotScheduler(ContextualLogger):
    account: StoredApiKey
    bot_signals: BotSignals
    shared_signals: SharedSignals
    log_signals: LogSignals
    is_playing_event: Event
    msg_info_signals: MessageInfoSignals

    get_bot_config: Callable[[], BotConfig | None]

    behavior_coordinator: BehaviorCoordinator
    process_manager: ProcessManager
    event_manager: EventManager
    on_session_started: Callable[[datetime, datetime], None]
    on_session_finished: Callable[[], None]

    clock: Callable[[], datetime] = datetime.now
    is_in_fight: Callable[[], bool] = lambda: False
    plan_controller: AutomationPlanController = field(default_factory=AutomationPlanController)
    planner: AutomationPlanner = field(default_factory=AutomationPlanner)

    _scheduled_jobs: list[schedule.Job] = field(init=False, default_factory=list[schedule.Job])
    _configuration_lock: RLock = field(init=False, default_factory=RLock, repr=False, compare=False)
    _configuration_refresh_pending: bool = field(init=False, default=False)
    _randomized_slots_by_day: dict[int, list[TimeSlot]] = field(
        init=False, default_factory=dict[int, list[TimeSlot]]
    )
    _active_plan: AccountAutomationPlan | None = field(init=False, default=None)
    _active_plan_session: PlannedSession | None = field(init=False, default=None)
    _pending_script_restart: Timer | None = field(init=False, default=None)
    _active_script: str | None = field(init=False, default=None)
    _session_started_at: datetime | None = field(init=False, default=None)
    _script_failures: int = field(init=False, default=0)
    _using_fallback: bool = field(init=False, default=False)
    _last_script_error: str | None = field(init=False, default=None)

    def start(self) -> None:
        with self._configuration_lock:
            if self._is_quarantined():
                return
            self._configure_schedule()

            now = self.clock()
            if self.is_in_randomized_playtime(now) and self._start_active_session(now):
                UserActivityService().record(
                    "info", "Automatic startup within the current scheduled time slot.", login=self.account.apikey.login
                )

    def stop(self) -> None:
        with self._configuration_lock:
            self._configuration_refresh_pending = False
            self._clear_scheduled_jobs()

    def request_configuration_refresh(self) -> None:
        with self._configuration_lock:
            self._configuration_refresh_pending = True
            self.apply_pending_configuration()

    def apply_pending_configuration(self) -> None:
        with self._configuration_lock:
            if self._configuration_refresh_pending and not self.is_playing_event.is_set():
                self.refresh_configuration()
                self._configuration_refresh_pending = False

    def refresh_configuration(self) -> None:
        with self._configuration_lock:
            if self.is_playing_event.is_set():
                return
            self._clear_scheduled_jobs()
            self._configure_schedule()

    def cancel_pending_restart(self) -> None:
        with self._configuration_lock:
            self._cancel_pending_script_restart()
            self._reset_session_runtime()

    def disconnect_now(self) -> None:
        run_in_background(self._manual_disconnect_bot_task)

    def _clear_scheduled_jobs(self) -> None:
        for job in self._scheduled_jobs:
            schedule.cancel_job(job)
        self._scheduled_jobs.clear()
        self._randomized_slots_by_day.clear()
        self._active_plan = None
        self._cancel_pending_script_restart()
        self._reset_session_runtime()

    def _reset_session_runtime(self) -> None:
        self._active_plan_session = None
        self._active_script = None
        self._session_started_at = None
        self._script_failures = 0
        self._using_fallback = False
        self._last_script_error = None

    def _configure_schedule(self) -> None:
        plan = self._load_plan()
        if plan is not None:
            slots = self.plan_controller.resolve_slots(plan)
            if slots:
                self._active_plan = plan
                self._schedule_slots(slots, plan.session_duration_minutes, f"plan {plan.login}")
                return
            self.logger.warning("Automation plan %s has no time slots; using the bot schedule", plan.login)

        config = self.get_bot_config()
        if config is None or config.schedule_profile is None:
            return
        profile = ScheduleProfileController().get_profile(config.schedule_profile)
        if not profile:
            self.logger.error(f"Unknown schedule profile {config.schedule_profile}")
            return
        self._schedule_slots(profile.slots_by_day, None, f"profile {config.schedule_profile}")

    def _load_plan(self) -> AccountAutomationPlan | None:
        try:
            plan = self.plan_controller.get_plan(self.account.apikey.login)
        except RuntimeSetupError as error:
            self.logger.error("Cannot load the automation plan: %s", error)
            return None
        if plan is None or not plan.enabled:
            return None
        return plan

    def _schedule_slots(
        self,
        slots_by_day: Mapping[str, Sequence[TimeSlot]],
        session_duration_minutes: int | None,
        source: str,
    ) -> None:
        self._randomized_slots_by_day = self.planner.randomize_slots(slots_by_day)
        for day, slots in self._randomized_slots_by_day.items():
            for slot in slots:
                self.logger.info("Scheduling day %s: start=%s, end=%s (%s)", day, slot.start, slot.end, source)
                UserActivityService().record(
                    "info",
                    f"Effective time slot: {slot.start}–{slot.end} ({source}).",
                    login=self.account.apikey.login,
                )

                start_job = (
                    _get_day_scheduler(day)
                    .at(slot.start)
                    .do(lambda: run_in_background(self._planned_restart_bot_task))
                )
                self._scheduled_jobs.append(start_job)

                end_time, crosses_midnight = _effective_end_time(
                    slot.start, slot.end, session_duration_minutes
                )
                end_day = (day + 1) % 7 if crosses_midnight else day
                end_job = (
                    _get_day_scheduler(end_day)
                    .at(end_time)
                    .do(lambda: run_in_background(self._planned_stop_bot_task))
                )
                self._scheduled_jobs.append(end_job)

        midnight_job = schedule.every().day.at("00:00").do(self._reschedule_with_new_random_times)
        self._scheduled_jobs.append(midnight_job)

    def _reschedule_with_new_random_times(self) -> None:
        with self._configuration_lock:
            if self.is_playing_event.is_set():
                self._configuration_refresh_pending = True
                return
            self.logger.info("Midnight reschedule: generating new random times")
            self.refresh_configuration()

    def is_in_randomized_playtime(self, now: datetime) -> bool | None:
        if not self._randomized_slots_by_day:
            return None
        if self._active_plan is not None:
            return self._plan_state(now).current is not None

        current_minutes = now.hour * 60 + now.minute
        current_day = now.weekday()
        for slot in self._randomized_slots_by_day.get(current_day, []):
            start, end = _slot_minutes(slot)
            if start <= end and start <= current_minutes <= end:
                return True
            if start > end and current_minutes >= start:
                return True

        previous_day = (current_day - 1) % 7
        for slot in self._randomized_slots_by_day.get(previous_day, []):
            start, end = _slot_minutes(slot)
            if start > end and current_minutes <= end:
                return True
        return False

    def automation_status(self) -> AutomationStatus | None:
        with self._configuration_lock:
            if self._active_plan is None or not self._randomized_slots_by_day:
                return None
            state = self._plan_state(self.clock())
            session_end = (
                self._active_plan_session.end
                if self._active_plan_session is not None
                else (state.current.end if state.current is not None else None)
            )
            return AutomationStatus(
                current=state.current,
                next_start=state.next_start,
                script=self._active_script or self._active_plan.script,
                started_at=self._session_started_at,
                session_end=session_end,
                retry_count=self._script_failures,
                using_fallback=self._using_fallback,
            )

    def _plan_state(self, now: datetime) -> AutomationPlanState:
        assert self._active_plan is not None, "A plan state requires an active plan"
        return self.planner.plan_for_slots(
            self._active_plan, self._randomized_slots_by_day, now
        )

    def _get_current_session_window(self, now: datetime) -> ScheduledSessionWindow | None:
        current_day = now.weekday()
        for day_offset in (0, -1):
            slot_day = (current_day + day_offset) % 7
            slot_date = now.date() if day_offset == 0 else now.date() - timedelta(days=1)
            for slot in self._randomized_slots_by_day.get(slot_day, []):
                start_time = datetime.strptime(slot.start, "%H:%M").time()
                end_time = datetime.strptime(slot.end, "%H:%M").time()
                starts_at = datetime.combine(slot_date, start_time)
                ends_at = datetime.combine(slot_date, end_time)
                if ends_at <= starts_at:
                    ends_at += timedelta(days=1)
                if starts_at <= now <= ends_at:
                    return ScheduledSessionWindow(start=starts_at, end=ends_at)
        return None

    def _start_active_session(self, now: datetime) -> bool:
        if self.is_playing_event.is_set():
            self.logger.info("Bot is playing, dont restart")
            return False
        if self.is_in_fight():
            self.logger.info("Bot is in a fight, skipping the scheduled startup")
            return False

        if self._active_plan is not None:
            return self._start_plan_session(now)

        session_window = self._get_current_session_window(now)
        if session_window is None:
            return False
        self._reset_session_runtime()
        self.on_session_started(session_window.start, session_window.end)
        self.bot_signals.play.emit(False)
        self.shared_signals.launch_account.emit(self.account.apikey.login)
        return True

    def _start_plan_session(self, now: datetime) -> bool:
        assert self._active_plan is not None, "A plan session requires an active plan"
        plan = self._active_plan
        session = self._plan_state(now).current
        if session is None:
            return False

        self._reset_session_runtime()
        self._active_script = plan.script
        script_path = None
        if self._active_script is not None:
            try:
                script_path = self._resolve_active_script_path(plan)
            except (OSError, ValueError) as error:
                self.logger.error("Cannot start automation plan %s: %s", plan.login, error)
                UserActivityService().record(
                    "error", f"Automation script unavailable: {error}", login=plan.login
                )
                self._reset_session_runtime()
                return False

        self._active_plan_session = session
        self._session_started_at = now
        self.on_session_started(session.start, session.end)
        self.bot_signals.play.emit(False)
        if script_path is not None:
            self.logger.info(
                "Starting automation script %s for %s (session %s -> %s)",
                self._active_script,
                plan.login,
                session.start.isoformat(),
                session.end.isoformat(),
            )
            UserActivityService().record(
                "info", f"Automation started script {self._active_script}.", login=plan.login
            )
            self.behavior_coordinator.on_play_script(
                str(script_path), self._on_scheduled_script_finished
            )
        else:
            UserActivityService().record(
                "info", "Automation started without a script; using automatic behavior.", login=plan.login
            )
            self.bot_signals.play_auto_bot.emit(None)
        return True

    def _resolve_active_script_path(self, plan: AccountAutomationPlan) -> Path | None:
        return self.plan_controller.resolve_script_path(
            plan.model_copy(update={"script": self._active_script})
        )

    def stop_scheduled_runtime(
        self, activity_message: str = "Automatic stop at the end of the time slot."
    ) -> None:
        self.logger.info("Stopping bot")
        with self._configuration_lock:
            self._cancel_pending_script_restart()
            self._reset_session_runtime()
        UserActivityService().record("info", activity_message, login=self.account.apikey.login)
        runtime_is_active = (
            self.is_playing_event.is_set()
            or self.event_manager.request_disconnect_callback is not None
            or self.process_manager.is_bot_process_running()
        )
        if runtime_is_active:
            self.on_session_finished()
            self._disconnect_runtime()
        else:
            self.on_session_finished()
            self.logger.info("Bot runtime is already stopped")

    def _on_scheduled_script_finished(self, error_code: str | None) -> None:
        if error_code is not None:
            self._on_scheduled_script_failed(error_code)
            return

        with self._configuration_lock:
            plan = self._active_plan
            session = self._active_plan_session
            if plan is None or session is None:
                self.logger.info("Scheduled script finished without an active plan session")
                return
            self._script_failures = 0
            self._last_script_error = None
            UserActivityService().record(
                "info", "Automation script finished normally.", login=plan.login
            )
            if plan.restart_policy == "stop":
                self.logger.info("Scheduled script finished; restart policy is stop")
                UserActivityService().record(
                    "info", "Automation script finished; stopping the session.", login=plan.login
                )
                should_stop = True
            elif self.clock() >= session.end:
                self.logger.info("Scheduled script finished after the session end")
                should_stop = True
            else:
                self.logger.info("Scheduled script finished normally")
                self._arm_script_restart(plan.restart_delay_seconds)
                return

        if should_stop:
            self.logger.info("Not restarting the scheduled script")
            self.stop_scheduled_runtime()

    def _on_scheduled_script_failed(self, error_code: str) -> None:
        quarantine_reason: str | None = None
        should_stop = False
        with self._configuration_lock:
            plan = self._active_plan
            session = self._active_plan_session
            if plan is None or session is None:
                self.logger.warning(
                    "Scheduled script failed without an active plan session: %s", error_code
                )
                return
            self._last_script_error = error_code
            self.logger.warning(
                "Scheduled script failed for %s (failures: %s/%s): %s",
                plan.login,
                self._script_failures,
                plan.max_retries,
                error_code,
            )
            if self.clock() >= session.end:
                self.logger.info("Scheduled script failed after the session end")
                should_stop = True
            elif self._script_failures < plan.max_retries:
                self._script_failures += 1
                delay = _retry_delay_seconds(plan.restart_delay_seconds, self._script_failures)
                self.logger.warning(
                    "Retrying the scheduled script in %ss (retry %s/%s)",
                    delay,
                    self._script_failures,
                    plan.max_retries,
                )
                UserActivityService().record(
                    "warning",
                    f"Automation script failed; retry {self._script_failures}/{plan.max_retries} "
                    f"in {delay}s: {error_code}",
                    login=plan.login,
                )
                self._arm_script_restart(delay)
                return
            elif plan.fallback_script is not None and not self._using_fallback:
                self._using_fallback = True
                self._active_script = plan.fallback_script
                self._script_failures = 0
                self.logger.warning(
                    "Switching automation for %s to fallback script %s",
                    plan.login,
                    plan.fallback_script,
                )
                UserActivityService().record(
                    "warning",
                    f"Switching to fallback script {plan.fallback_script} after repeated failures.",
                    login=plan.login,
                )
                self._arm_script_restart(plan.restart_delay_seconds)
                return
            else:
                attempts = self._script_failures + 1
                quarantine_reason = (
                    f"Automation script failed repeatedly ({attempts} attempts): {error_code}"
                )
                should_stop = True

        if quarantine_reason is not None:
            self._quarantine_after_failures(quarantine_reason)
            self.stop_scheduled_runtime("Automation stopped after repeated script failures.")
        elif should_stop:
            self.stop_scheduled_runtime()

    def _quarantine_after_failures(self, reason: str) -> None:
        login = self.account.apikey.login
        self.logger.error("Quarantining %s: %s", login, reason)
        try:
            BotStorageController().quarantine(login, reason)
        except (LookupError, OSError) as error:
            self.logger.error("Cannot quarantine %s: %s", login, error)
        UserActivityService().record(
            "error", f"Automation quarantined the account: {reason}", login=login
        )

    def _arm_script_restart(self, delay_seconds: int) -> None:
        self._cancel_pending_script_restart()
        timer = Timer(delay_seconds, self._restart_scheduled_script)
        timer.daemon = True
        self._pending_script_restart = timer
        timer.start()
        self.logger.info("Restarting the scheduled script in %ss", delay_seconds)
        UserActivityService().record(
            "info",
            f"Automation script restart scheduled in {delay_seconds}s.",
            login=self.account.apikey.login,
        )

    def _cancel_pending_script_restart(self) -> None:
        timer = self._pending_script_restart
        self._pending_script_restart = None
        if timer is not None:
            timer.cancel()

    def _restart_scheduled_script(self) -> None:
        if self._is_quarantined():
            self.stop_scheduled_runtime()
            return

        script_path = None
        should_stop = False
        with self._configuration_lock:
            self._pending_script_restart = None
            plan = self._active_plan
            session = self._active_plan_session
            if plan is None or session is None or not self.is_playing_event.is_set():
                return
            if self.clock() >= session.end:
                self.logger.info("Session ended before the script restart")
                should_stop = True
            elif self.is_in_fight():
                self.logger.info("Bot is in a fight, postponing the script restart")
                self._arm_script_restart(plan.restart_delay_seconds)
                return
            else:
                try:
                    script_path = self._resolve_active_script_path(plan)
                except (OSError, ValueError) as error:
                    self.logger.error("Cannot restart automation plan %s: %s", plan.login, error)
                    UserActivityService().record(
                        "error", f"Automation script unavailable: {error}", login=plan.login
                    )
                    if plan.fallback_script is not None and not self._using_fallback:
                        self._using_fallback = True
                        self._active_script = plan.fallback_script
                        self._script_failures = 0
                        self._arm_script_restart(plan.restart_delay_seconds)
                        return
                    should_stop = True
                else:
                    should_stop = script_path is None

        if should_stop:
            self.stop_scheduled_runtime()
        elif script_path is not None:
            self.logger.info(
                "Restarting automation script %s for %s", self._active_script, plan.login
            )
            UserActivityService().record(
                "info", f"Automation restarting script {self._active_script}.", login=plan.login
            )
            self.behavior_coordinator.on_play_script(
                str(script_path), self._on_scheduled_script_finished
            )

    def _disconnect_runtime(self) -> None:
        self.behavior_coordinator.stop_behaviors()
        self.bot_signals.stop.emit()

        request_disconnect = self.event_manager.request_disconnect_callback
        if self.event_manager.is_socket_mode:
            assert request_disconnect is not None, "Socket mode must define a request_disconnect_callback"
        if request_disconnect is not None:
            request_disconnect()

        self.process_manager.kill_process()

    def _manual_disconnect_bot(self) -> None:
        self.logger.info("Disconnecting bot")
        self.cancel_pending_restart()
        self._disconnect_runtime()

    def _planned_restart_bot(self) -> None:
        self.logger.info("Restarting bot")
        now = self.clock()
        if self._is_quarantined():
            return
        if not self.is_in_randomized_playtime(now):
            return self.logger.info("Bot is not anymore in playtime")
        self._start_active_session(now)

    def _planned_stop_bot_task(self, _progress_callback: Callable[[str], None]) -> None:
        self.stop_scheduled_runtime()

    def _manual_disconnect_bot_task(self, _progress_callback: Callable[[str], None]) -> None:
        self._manual_disconnect_bot()

    def _planned_restart_bot_task(self, _progress_callback: Callable[[str], None]) -> None:
        self._planned_restart_bot()

    def _is_quarantined(self) -> bool:
        record = BotStorageController().get_record(self.account.apikey.login)
        if record is None or record.quarantine_reason is None:
            return False
        self.logger.warning("Bot is quarantined: %s", record.quarantine_reason)
        UserActivityService().record(
            "warning", f"Bot quarantined: {record.quarantine_reason}.", login=self.account.apikey.login
        )
        return True


def run_continuously(interval: int = 1) -> threading.Event:
    cease_continuous_run = threading.Event()

    class ScheduleThread(threading.Thread):
        def run(self) -> None:
            while not cease_continuous_run.is_set():
                schedule.run_pending()
                sleep(interval)

    continuous_thread = ScheduleThread()
    continuous_thread.start()
    return cease_continuous_run


_MAX_RETRY_DELAY_SECONDS = 3600


def _retry_delay_seconds(base_delay_seconds: int, failure_count: int) -> int:
    return min(base_delay_seconds * 2 ** (failure_count - 1), _MAX_RETRY_DELAY_SECONDS)


def _effective_end_time(
    start: str, end: str, session_duration_minutes: int | None
) -> tuple[str, bool]:
    start_minutes = _time_minutes(start)
    end_minutes = _time_minutes(end)
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    if session_duration_minutes is not None:
        end_minutes = min(end_minutes, start_minutes + session_duration_minutes)
    return _minutes_to_time(end_minutes), end_minutes >= 24 * 60


def _time_minutes(value: str) -> int:
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def _minutes_to_time(total_minutes: int) -> str:
    total_minutes %= 24 * 60
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _get_day_scheduler(day: int) -> schedule.Job:
    match day:
        case 0:
            return schedule.every().monday
        case 1:
            return schedule.every().tuesday
        case 2:
            return schedule.every().wednesday
        case 3:
            return schedule.every().thursday
        case 4:
            return schedule.every().friday
        case 5:
            return schedule.every().saturday
        case 6:
            return schedule.every().sunday
        case _:
            raise ValueError(f"Unknown day index: {day}")


def _slot_minutes(slot: TimeSlot) -> tuple[int, int]:
    return _time_minutes(slot.start), _time_minutes(slot.end)
