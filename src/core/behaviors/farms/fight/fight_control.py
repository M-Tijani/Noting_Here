from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from threading import Condition, Event, RLock
from time import monotonic

DEFAULT_WAIT_SECONDS = 30.0


class FightPhase(Enum):
    IDLE = auto()
    PLACEMENT = auto()
    MY_TURN = auto()
    ENEMY_TURN = auto()
    FINISHED = auto()


class FightCommandKind(Enum):
    CAST = auto()
    MOVE = auto()
    END_TURN = auto()


@dataclass(frozen=True, slots=True)
class FightCommand:
    kind: FightCommandKind
    spell_id: int | None = None
    cell_id: int | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FightSnapshot:
    phase: FightPhase = FightPhase.IDLE
    turn: int = 0
    player_cell_id: int | None = None
    action_points: int = 0
    movement_points: int = 0
    life_points: int = 0
    max_life_points: int = 0
    enemy_cell_ids: tuple[int, ...] = ()
    spell_ids: tuple[int, ...] = ()
    placement_cell_ids: tuple[int, ...] = ()

    @property
    def in_fight(self) -> bool:
        return self.phase in {
            FightPhase.PLACEMENT,
            FightPhase.MY_TURN,
            FightPhase.ENEMY_TURN,
        }

    @property
    def is_my_turn(self) -> bool:
        return self.phase is FightPhase.MY_TURN


@dataclass(slots=True)
class _PendingCommand:
    command: FightCommand
    done: Event = field(default_factory=Event)
    result: CommandResult | None = None


class FightControl:
    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._snapshot = FightSnapshot()
        self._manual = False
        self._pending: _PendingCommand | None = None
        self._active: _PendingCommand | None = None
        self._placement_cell_id: int | None = None
        self._error: str | None = None

    @property
    def phase(self) -> FightPhase:
        with self._condition:
            return self._snapshot.phase

    @property
    def manual(self) -> bool:
        with self._condition:
            return self._manual

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def is_in_fight(self) -> bool:
        return self.snapshot().in_fight

    @property
    def is_my_turn(self) -> bool:
        return self.snapshot().is_my_turn

    def snapshot(self) -> FightSnapshot:
        with self._condition:
            return self._snapshot

    def publish_snapshot(self, snapshot: FightSnapshot) -> None:
        with self._condition:
            self._snapshot = snapshot
            if snapshot.phase is not FightPhase.MY_TURN:
                self._fail_commands("fight phase changed")
            self._condition.notify_all()

    def set_manual(self) -> None:
        with self._condition:
            self._manual = True
            self._condition.notify_all()

    def set_auto(self) -> None:
        with self._condition:
            self._manual = False
            self._fail_commands("fight control switched to auto")
            self._condition.notify_all()

    def request_placement_cell(self, cell_id: int) -> None:
        with self._condition:
            self._placement_cell_id = cell_id
            self._condition.notify_all()

    def take_placement_cell(self, timeout: float = 0.0) -> int | None:
        deadline = monotonic() + max(0.0, timeout)
        with self._condition:
            while self._placement_cell_id is None:
                if not self._condition.wait(_remaining(deadline)):
                    return None
            cell_id = self._placement_cell_id
            self._placement_cell_id = None
            return cell_id

    def begin_placement(self, cell_ids: Iterable[int] = ()) -> None:
        placement_cell_ids = tuple(dict.fromkeys(int(cell_id) for cell_id in cell_ids))
        with self._condition:
            self._fail_commands("fight phase changed")
            self._error = None
            self._snapshot = FightSnapshot(
                phase=FightPhase.PLACEMENT,
                placement_cell_ids=placement_cell_ids,
            )
            self._condition.notify_all()

    def end_placement(self) -> None:
        with self._condition:
            if self._snapshot.phase is not FightPhase.PLACEMENT:
                return
            self._placement_cell_id = None
            self._snapshot = replace(
                self._snapshot,
                phase=FightPhase.ENEMY_TURN,
                placement_cell_ids=(),
            )
            self._condition.notify_all()

    def begin_turn(self, turn: int) -> None:
        with self._condition:
            self._fail_commands("fight turn changed")
            self._error = None
            self._snapshot = replace(
                self._snapshot,
                phase=FightPhase.MY_TURN,
                turn=turn,
                placement_cell_ids=(),
            )
            self._condition.notify_all()

    def begin_enemy_turn(self, turn: int | None = None) -> None:
        with self._condition:
            self._fail_commands("fight turn changed")
            self._snapshot = replace(
                self._snapshot,
                phase=FightPhase.ENEMY_TURN,
                turn=self._snapshot.turn if turn is None else turn,
                placement_cell_ids=(),
            )
            self._condition.notify_all()

    def finish(self, error: str | None = None) -> None:
        with self._condition:
            self._fail_commands(error or "fight finished")
            self._placement_cell_id = None
            self._error = error
            self._snapshot = replace(
                self._snapshot,
                phase=FightPhase.FINISHED,
                placement_cell_ids=(),
            )
            self._condition.notify_all()

    def cancel(self, reason: str = "fight control cancelled") -> None:
        self.finish(reason)

    def reset(self) -> None:
        with self._condition:
            self._fail_commands("fight control reset")
            self._snapshot = FightSnapshot()
            self._manual = False
            self._placement_cell_id = None
            self._error = None
            self._condition.notify_all()

    def wait_for_active_fight(self, timeout: float = DEFAULT_WAIT_SECONDS) -> FightSnapshot | None:
        return self._wait_for_phase(
            FightPhase.PLACEMENT,
            timeout,
            stop_phases=frozenset({FightPhase.FINISHED}),
            accepted_phases=frozenset(
                {
                    FightPhase.PLACEMENT,
                    FightPhase.MY_TURN,
                    FightPhase.ENEMY_TURN,
                }
            ),
        )

    def wait_for_placement(self, timeout: float = DEFAULT_WAIT_SECONDS) -> FightSnapshot | None:
        return self._wait_for_phase(
            FightPhase.PLACEMENT,
            timeout,
            stop_phases=frozenset(
                {
                    FightPhase.MY_TURN,
                    FightPhase.ENEMY_TURN,
                    FightPhase.FINISHED,
                }
            ),
        )

    def wait_for_placement_end(
        self, timeout: float = DEFAULT_WAIT_SECONDS
    ) -> FightSnapshot | None:
        deadline = monotonic() + timeout
        with self._condition:
            while self._snapshot.phase is FightPhase.PLACEMENT:
                if not self._condition.wait(_remaining(deadline)):
                    return None
            return self._snapshot

    def wait_for_my_turn(self, timeout: float = DEFAULT_WAIT_SECONDS) -> FightSnapshot | None:
        return self._wait_for_phase(FightPhase.MY_TURN, timeout)

    def wait_for_finish(self, timeout: float = DEFAULT_WAIT_SECONDS) -> FightSnapshot | None:
        return self._wait_for_phase(FightPhase.FINISHED, timeout)

    def take_command(self, timeout: float = DEFAULT_WAIT_SECONDS) -> FightCommand | None:
        deadline = monotonic() + timeout
        with self._condition:
            while True:
                if self._snapshot.phase is not FightPhase.MY_TURN or not self._manual:
                    return None
                if self._pending is not None:
                    pending = self._pending
                    self._pending = None
                    if pending.result is not None:
                        continue
                    self._active = pending
                    return pending.command
                if not self._condition.wait(_remaining(deadline)):
                    return None

    def submit_command(
        self,
        command: FightCommand,
        timeout: float = DEFAULT_WAIT_SECONDS,
    ) -> CommandResult:
        with self._condition:
            if not self._manual:
                return CommandResult(False, "fight control is in auto mode")
            if self._snapshot.phase is not FightPhase.MY_TURN:
                return CommandResult(False, f"not my turn ({self._snapshot.phase.name.lower()})")
            if self._pending is not None or self._active is not None:
                return CommandResult(False, "another fight command is still running")

            pending = _PendingCommand(command)
            self._pending = pending
            self._condition.notify_all()

        if pending.done.wait(timeout):
            return pending.result or CommandResult(False, "fight command finished without a result")

        with self._condition:
            if pending.result is not None:
                return pending.result
            if self._pending is pending:
                self._pending = None
            pending.result = CommandResult(False, "fight command timed out")
            return pending.result

    def complete_command(self, result: CommandResult | None = None) -> None:
        with self._condition:
            pending = self._active
            if pending is None:
                return
            self._active = None
            self._resolve(pending, result or CommandResult(True))
            self._condition.notify_all()

    def _wait_for_phase(
        self,
        phase: FightPhase,
        timeout: float,
        stop_phases: frozenset[FightPhase] = frozenset({FightPhase.FINISHED}),
        accepted_phases: frozenset[FightPhase] | None = None,
    ) -> FightSnapshot | None:
        deadline = monotonic() + timeout
        with self._condition:
            while True:
                if self._snapshot.phase is phase:
                    return self._snapshot
                if accepted_phases is not None and self._snapshot.phase in accepted_phases:
                    return self._snapshot
                if self._snapshot.phase in stop_phases:
                    return None
                if not self._condition.wait(_remaining(deadline)):
                    return None

    def _fail_commands(self, error: str) -> None:
        for pending in (self._pending, self._active):
            if pending is not None:
                self._resolve(pending, CommandResult(False, error))
        self._pending = None
        self._active = None

    @staticmethod
    def _resolve(pending: _PendingCommand, result: CommandResult) -> None:
        if pending.result is None:
            pending.result = result
            pending.done.set()


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - monotonic())
