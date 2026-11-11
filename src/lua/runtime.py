from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from threading import Event
from typing import Any, cast

from lupa import LuaError, LuaRuntime, lua_type

from src.lua.errors import ScriptCancelled, ScriptError, ScriptFinished

ScriptBinding = Callable[..., object]
ScriptBindings = Mapping[str, ScriptBinding]

_BOOTSTRAP = """
function __bind(name)
  return function(...)
    return __await(name, ...)
  end
end

function __await(event, ...)
  -- table.pack keeps nil arguments; {...} truncates the list at the first nil.
  local args = table.pack(...)
  local value, err = coroutine.yield({event = event, args = args, count = args.n})
  if err ~= nil then
    error(err, 0)
  end
  return value
end

function __runner(fn)
  return coroutine.wrap(function()
    local ok, err = pcall(fn)
    coroutine.yield({event = "finished", ok = ok, error = err})
  end)
end

function __move_config()
  return {
    min = MIN_MONSTERS,
    max = MAX_MONSTERS,
    force = FORCE_MONSTERS,
    forbidden = FORBIDDEN_MONSTERS,
  }
end

local __banking = false

function __run_step(step, next_step)
  -- Recovery hook: runs before the map goto so a script can repair a position it
  -- cannot reach with AutoTrip (it is replayed when a restarted script resumes here).
  if type(step.prepare) == "function" then
    step.prepare()
  end
  if step.map ~= nil then
    local target = __resolve_map(step.map)
    if target ~= nil and __map_id() ~= target then
      __action_goto(target)
    end
  end
  if step.deposit then
    __action_deposit()
  end
  if step.sale then
    __action_sale()
  end
  if step.gather then
    if (not __banking) and __full_pods() then
      __run_bank_route()
    end
    __action_gather(false, step.gather)
  end
  if step.forcegather then
    __action_gather(true, step.forcegather)
  end
  if step.fight then
    __action_fight(false, step.fight, __move_config())
  end
  if step.forceFight then
    __action_fight(true, step.forceFight, __move_config())
  end
  if type(step.lockedcustom) == "function" then
    step.lockedcustom()
  end
  if type(step.custom) == "function" then
    step.custom()
  end
  if step.door ~= nil then
    __action_door(step.door)
  end
  if step.path ~= nil then
    -- The next row's map is where this exit leads; it is the fallback target when
    -- the exit is blocked or the transition fails.
    local fallback = nil
    if next_step ~= nil then
      fallback = next_step.map
    end
    __action_path(step.path, fallback)
  end
end

function __run_steps(step_source, track)
  local index = 1
  if track and __pending_resume and __pending_resume > 0 then
    index = __pending_resume
    __pending_resume = 0
  end
  while true do
    local steps = step_source()
    if type(steps) ~= "table" then
      return false
    end
    local step = steps[index]
    if step == nil then
      return true
    end
    if track and __save_progress then
      __save_progress(index)
    end
    __run_step(step, steps[index + 1])
    index = index + 1
  end
end

function __run_bank_route()
  if __banking then
    return __action_deposit()
  end
  if type(bank) ~= "function" then
    return __action_deposit()
  end
  __banking = true
  __run_steps(bank, false)
  __banking = false
  if __full_pods() then
    error("pods are still full after the bank route", 0)
  end
end

function __run_move_engine()
  if type(move) ~= "function" then
    return
  end
  while __run_steps(move, true) do
    if sleep ~= nil then
      sleep(0.5)
    end
  end
end
"""

_FINISHED_EVENT = "finished"


def _ignore_step(*_: object) -> None:
    return None


def _to_python(value: Any) -> Any:
    if lua_type(value) != "table":
        return value
    keys = list(value.keys())
    if not keys:
        return []
    # Only contiguous 1..n keys are an array; sparse numeric keys (e.g. an
    # item-id -> price map) must stay a mapping.
    if all(isinstance(key, int) for key in keys) and sorted(keys) == list(
        range(1, len(keys) + 1)
    ):
        return [_to_python(value[index]) for index in range(1, len(keys) + 1)]
    return {key: _to_python(value[key]) for key in keys}


def _build_globals(lua: LuaRuntime, names: list[str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for name in names:
        node: dict[str, Any] = tree
        parts = name.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = cast("dict[str, Any]", child)
        node[parts[-1]] = lua.globals()["__bind"](name)

    def to_value(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        table = lua.table_from({})
        for key, value in cast("dict[str, Any]", node).items():
            table[key] = to_value(value)
        return table

    return {root: to_value(node) for root, node in tree.items()}


class LuaScriptRuntime:
    """Drives a Lua coroutine that blocks on Python-provided action bindings."""

    def __init__(self) -> None:
        self._lua = LuaRuntime(unpack_returned_tuples=True)
        self._lua.execute(_BOOTSTRAP)

    def _to_lua(self, value: Any) -> Any:
        if isinstance(value, dict):
            return self._lua.table_from(
                {key: self._to_lua(item) for key, item in cast("dict[Any, Any]", value).items()}
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return self._lua.table_from([self._to_lua(item) for item in cast("Iterable[Any]", value)])
        return value

    def run(
        self,
        source: str,
        bindings: ScriptBindings,
        cancel_event: Event,
        *,
        resume_index: int = 0,
        on_step: Callable[[int], None] | None = None,
    ) -> None:
        self._lua.globals()["__pending_resume"] = max(0, int(resume_index))
        self._lua.globals()["__save_progress"] = on_step if on_step is not None else _ignore_step
        try:
            user_function = self._lua.execute(
                f"return function()\n{source}\nreturn __run_move_engine()\nend"
            )
        except LuaError as error:
            raise ScriptError(str(error)) from error

        if lua_type(user_function) != "function":
            raise ScriptError("script did not evaluate to a callable body")

        for name, value in _build_globals(self._lua, list(bindings)).items():
            self._lua.globals()[name] = value

        runner = self._lua.globals()["__runner"](user_function)
        request = runner()
        while True:
            if cancel_event.is_set():
                raise ScriptCancelled()
            if lua_type(request) != "table":
                raise ScriptError("script protocol returned an invalid request")

            event = request["event"]
            if event == _FINISHED_EVENT:
                if not request["ok"]:
                    raise ScriptError(str(request["error"]))
                return

            binding = bindings.get(event)
            if binding is None:
                request = runner(None, f"unknown binding: {event}")
                continue

            raw_args = request["args"]
            argument_count = int(request["count"])
            arguments = [_to_python(raw_args[index]) for index in range(1, argument_count + 1)]
            try:
                value = binding(*arguments)
            except ScriptCancelled:
                raise
            except ScriptFinished:
                raise
            except ScriptError as error:
                request = runner(None, str(error))
            except Exception as error:  # noqa: BLE001 - surfaced to the script as a Lua error
                request = runner(None, f"{type(error).__name__}: {error}")
            else:
                request = runner(self._to_lua(value), None)
