from __future__ import annotations

from collections.abc import Iterable

from src.core.bot.controller_hub import ControllerHub, controller_hub

MAX_TEAM_SIZE = 8


class TeamError(ValueError):
    """Raised when a team selection is invalid."""


class TeamController:
    """Tracks the accounts the user wants to play together (1 to 8) and drives them.

    Bots share a single process, so selecting a set of registered logins is enough to
    start the same script on all of them (the leader first) or stop them together.
    """

    def __init__(self, hub: ControllerHub | None = None) -> None:
        self._hub = hub if hub is not None else controller_hub
        self._members: list[str] = []
        self._leader: str | None = None

    def clear(self) -> None:
        self._members = []
        self._leader = None

    def set_team(self, logins: Iterable[object]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for login in logins:
            text = str(login).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if len(cleaned) > MAX_TEAM_SIZE:
            raise TeamError(f"a team has at most {MAX_TEAM_SIZE} characters")
        self._members = cleaned
        if self._leader not in cleaned:
            self._leader = cleaned[0] if cleaned else None
        return list(self._members)

    def toggle_member(self, login: object, selected: bool) -> list[str]:
        text = str(login).strip()
        if not text:
            raise TeamError("a login is required")
        current = list(self._members)
        if selected:
            if text not in current and len(current) >= MAX_TEAM_SIZE:
                raise TeamError(f"a team has at most {MAX_TEAM_SIZE} characters")
            if text not in current:
                current.append(text)
        elif text in current:
            current.remove(text)
        return self.set_team(current)

    def set_leader(self, login: object) -> None:
        text = str(login).strip()
        if text not in self._members:
            raise TeamError(f"{text!r} is not part of the team")
        self._leader = text

    @property
    def members(self) -> list[str]:
        return list(self._members)

    @property
    def leader(self) -> str | None:
        return self._leader

    def size(self) -> int:
        return len(self._members)

    def is_empty(self) -> bool:
        return not self._members

    def is_member(self, login: object) -> bool:
        return str(login).strip() in self._members

    def is_leader(self, login: object) -> bool:
        return self._leader is not None and str(login).strip() == self._leader

    def ordered_members(self) -> list[str]:
        if self._leader is None:
            return list(self._members)
        return [self._leader, *(login for login in self._members if login != self._leader)]

    def start_script(self, script_path: object) -> int:
        path = str(script_path).strip()
        if not path:
            raise TeamError("a script path is required")
        started = 0
        for login in self.ordered_members():
            bot = self._hub.get_bot(login)
            if bot is None:
                continue
            bot.request_script(path)
            started += 1
        return started

    def stop(self) -> int:
        stopped = 0
        for login in self.ordered_members():
            bot = self._hub.get_bot(login)
            if bot is None:
                continue
            bot.request_stop()
            stopped += 1
        return stopped


team_controller = TeamController()
