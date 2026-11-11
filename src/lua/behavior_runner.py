from __future__ import annotations

from collections.abc import Callable
from threading import Event
from time import monotonic
from typing import Any, cast

from src.core.behaviors.behavior import Behavior
from src.lua.errors import ScriptCancelled, ScriptError

_BEHAVIOR_POLL_SECONDS = 0.1
_MAX_FIGHT_INTERRUPTIONS = 20


class BehaviorRunHandle:
    """Owns one behavior run so start and wait can be called separately."""

    def __init__(
        self,
        behavior: Behavior,
        *,
        parent: Behavior,
        cancel_event: Event,
        defense_event: Event | None = None,
        rebuild: Callable[[], dict[str, Any]] | None = None,
        **run_kwargs: Any,
    ) -> None:
        self._behavior = behavior
        self._parent = parent
        self._cancel_event = cancel_event
        self._defense_event = defense_event
        self._rebuild = rebuild
        self._kwargs: dict[str, Any] = run_kwargs
        self._finished = Event()
        self._outcome: dict[str, object] = {}
        self._started = False
        self._interruptions = 0

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    @property
    def outcome(self) -> dict[str, object]:
        return dict(self._outcome)

    def start(self) -> None:
        if self._started:
            raise RuntimeError("behavior run handle already started")
        self._started = True
        self._start_attempt()

    def stop(self) -> None:
        if self._finished.is_set():
            return
        self._behavior.stop()
        self._outcome.setdefault("error", "behavior run stopped")
        self._finished.set()

    def wait(self, timeout: float | None = None) -> dict[str, object]:
        if not self._started:
            raise RuntimeError("behavior run handle not started")
        deadline = None if timeout is None else monotonic() + max(0.0, timeout)
        while True:
            interrupted = False
            while not self._finished.is_set():
                if deadline is not None and monotonic() >= deadline:
                    raise TimeoutError("behavior run timed out")
                if self._cancel_event.is_set():
                    self._behavior.stop()
                    raise ScriptCancelled()
                if self._defense_event is not None and self._defense_event.is_set():
                    self._behavior.stop()
                    interrupted = True
                    break
                self._finished.wait(_BEHAVIOR_POLL_SECONDS)

            if not interrupted:
                return self._outcome

            self._interruptions += 1
            if self._interruptions > _MAX_FIGHT_INTERRUPTIONS:
                raise ScriptError("too many fights interrupted the current action")
            if self._rebuild is not None:
                self._kwargs = self._rebuild()
            self._start_attempt()

    def _start_attempt(self) -> None:
        wait_for_defense(self._defense_event, self._cancel_event)
        self._finished.clear()
        self._outcome.clear()

        def on_finished(error_code: str | None, *_: object, **callback_kwargs: object) -> None:
            self._outcome["error"] = error_code
            self._outcome.update(callback_kwargs)
            self._finished.set()

        start = cast("Callable[..., None]", cast(Any, self._behavior).start)
        start(callback=on_finished, parent=self._parent, **self._kwargs)


def run_behavior_blocking(
    behavior: Behavior,
    *,
    parent: Behavior,
    cancel_event: Event,
    defense_event: Event | None = None,
    rebuild: Callable[[], dict[str, Any]] | None = None,
    **run_kwargs: Any,
) -> dict[str, object]:
    """Run a behavior until it finishes.

    If ``defense_event`` is set while the behavior runs (an involuntary fight was
    detected by the script behavior), the running behavior is stopped, the caller
    waits for the fight to end, and the behavior is started again. ``rebuild`` can
    provide fresh run arguments when the previous ones may be stale (e.g. a move
    path computed from a position that changed during the fight).
    """

    handle = BehaviorRunHandle(
        behavior,
        parent=parent,
        cancel_event=cancel_event,
        defense_event=defense_event,
        rebuild=rebuild,
        **run_kwargs,
    )
    handle.start()
    return handle.wait()


def wait_for_defense(defense_event: Event | None, cancel_event: Event) -> None:
    while defense_event is not None and defense_event.is_set():
        if cancel_event.is_set():
            raise ScriptCancelled()
        cancel_event.wait(_BEHAVIOR_POLL_SECONDS)
