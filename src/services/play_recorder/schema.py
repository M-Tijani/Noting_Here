"""Versioned record schema for play traces.

Every record carries ``schema_version`` so consumers can evolve independently of
the game protocol. Records are appended as JSON lines, one object per line.
"""

from typing import Any, Literal

from msgspec import Struct

from src.services.debug_recorder.recorder import MessageSource

SCHEMA_VERSION = 2
PROFILES = ("decisions", "full")
Profile = Literal["decisions", "full"]
Context = Literal["fight", "world"]


class Record(Struct, kw_only=True):
    schema_version: int
    datetime: str
    login_hash: str | None
    session: str | None
    seq: int


class MessageRecord(Record, kw_only=True):
    kind: Literal["message"]
    direction: Literal["client_to_server", "server_to_client"]
    source: MessageSource
    type: str
    namespace: str
    decoded: bool
    uid: int | None
    content: dict[str, Any]


class ObservationRecord(Record, kw_only=True):
    kind: Literal["observation"]
    observation_id: str
    context: Context
    fight_id: int | None
    turn: int | None
    data: dict[str, Any]


class ActionRecord(Record, kw_only=True):
    kind: Literal["action"]
    decision_id: str
    observation_id: str | None
    source: MessageSource
    context: Context
    fight_id: int | None
    turn: int | None
    action: dict[str, Any]


class EventRecord(Record, kw_only=True):
    kind: Literal["event"]
    event: str
    observation_id: str | None
    fight_id: int | None
    turn: int | None
    snapshot: dict[str, Any]


class LootItem(Struct, kw_only=True):
    id: int
    quantity: int


class OutcomeRecord(Record, kw_only=True):
    kind: Literal["outcome"]
    result: str
    fight_id: int | None
    duration_seconds: int
    turns: int
    kamas: int
    experience: int
    alive: bool | None
    loot: list[LootItem]
