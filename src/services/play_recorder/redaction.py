"""Redaction applied at the recording boundary.

Play traces are kept indefinitely, so credentials must never reach disk. Keys are
normalized (lowercase, alphanumerics only) before matching so ``ticket_key``,
``ticketKey`` and ``Ticket-Key`` are all caught.
"""

import hashlib
from collections.abc import Mapping
from typing import Any, Final, cast

REDACTED: Final = "[redacted]"
LOGIN_HASH_LENGTH: Final = 16

_LOGIN_HASH_DOMAIN: Final = b"lavasmith-play-recorder:"
_SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "ticket",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "authorization",
    "credential",
    "cookie",
    "email",
)


def is_secret_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).lower() if character.isalnum())
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def redact(value: object) -> object:
    if isinstance(value, Mapping):
        items = cast("Mapping[object, object]", value)
        return {
            str(key): REDACTED if is_secret_key(key) else redact(item)
            for key, item in items.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = cast("list[object] | tuple[object, ...] | set[object] | frozenset[object]", value)
        return [redact(item) for item in items]
    return value


def redact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", redact(entry))


def login_digest(login: str | None) -> str | None:
    if not login:
        return None
    normalized = login.strip().lower().encode("utf-8")
    return hashlib.sha256(_LOGIN_HASH_DOMAIN + normalized).hexdigest()[:LOGIN_HASH_LENGTH]
