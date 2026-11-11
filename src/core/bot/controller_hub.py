from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.bot.bot import Bot


@dataclass(frozen=True)
class BotCommand:
    sender_login: str
    sender_name: str
    text: str


def command_to_payload(command: BotCommand) -> dict[str, object]:
    return {
        "sender": command.sender_name,
        "senderLogin": command.sender_login,
        "command": command.text,
    }


def as_account_id(target: object) -> int | None:
    if isinstance(target, bool):
        return None
    if isinstance(target, int):
        return target
    if isinstance(target, str) and target.strip().lstrip("-").isdigit():
        return int(target)
    return None


class ControllerHub:
    """Process-wide registry of bots plus their cross-bot command inboxes.

    Bots share a single process, so a Lua script running on one account can look up any
    other account and drop a command in its inbox without any IPC.
    """

    def __init__(self) -> None:
        self._bots_by_login: dict[str, Bot] = {}
        self._inboxes: dict[str, deque[BotCommand]] = {}
        self._lock = Lock()

    def register_bot(self, bot: Bot) -> None:
        with self._lock:
            self._bots_by_login[bot.account.apikey.login] = bot

    def unregister_bot(self, bot: Bot) -> None:
        self.unregister_login(bot.account.apikey.login)

    def unregister_login(self, login: str) -> None:
        with self._lock:
            self._bots_by_login.pop(login, None)
            self._inboxes.pop(login, None)

    def clear(self) -> None:
        with self._lock:
            self._bots_by_login.clear()
            self._inboxes.clear()

    def all_bots(self) -> list[Bot]:
        with self._lock:
            return list(self._bots_by_login.values())

    def get_bot(self, login: str) -> Bot | None:
        with self._lock:
            return self._bots_by_login.get(login)

    def resolve(self, target: object) -> Bot | None:
        account_id = as_account_id(target)
        name = str(target).strip().casefold() if isinstance(target, str) else ""
        bots = self.all_bots()
        if account_id is not None:
            for bot in bots:
                if bot.account.apikey.accountId == account_id:
                    return bot
        if name:
            for bot in bots:
                if bot.account.apikey.login.casefold() == name:
                    return bot
            for bot in bots:
                if bot.game_state.player.character_name.casefold() == name:
                    return bot
        return None

    def send(self, target: object, command: BotCommand) -> bool:
        bot = self.resolve(target)
        if bot is None:
            return False
        self.push(bot.account.apikey.login, command)
        return True

    def push(self, login: str, command: BotCommand) -> bool:
        with self._lock:
            if login not in self._bots_by_login:
                return False
            self._inboxes.setdefault(login, deque()).append(command)
            return True

    def broadcast(self, logins: Iterable[str], command: BotCommand) -> int:
        delivered = 0
        with self._lock:
            for login in logins:
                if login in self._bots_by_login:
                    self._inboxes.setdefault(login, deque()).append(command)
                    delivered += 1
        return delivered

    def poll(self, login: str) -> BotCommand | None:
        with self._lock:
            inbox = self._inboxes.get(login)
            if inbox:
                return inbox.popleft()
        return None


controller_hub = ControllerHub()
