"""Append-only play trace recorder used to build imitation-learning datasets.

Each session writes its own ``.jsonl.gz`` file (one JSON record per line) under
``<recording dir>/play/<login_hash>/<date>/<session>.jsonl.gz``. Every flush appends
a complete gzip member, so a crash costs at most the in-flight batch and never
corrupts earlier records.

Records are redacted at the write boundary (see ``redaction.py``): credentials and
account identifiers must never reach disk. The ``decisions`` profile (default)
keeps only observations, actions, lifecycle events and outcomes; ``full`` also
keeps every game message for protocol debugging.
"""

import atexit
import gzip
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import msgspec
from google.protobuf.message import Message

from src.services.debug_recorder.recorder import MessageSource
from src.services.play_recorder.actions import FightOutcome
from src.services.play_recorder.observation import to_jsonable
from src.services.play_recorder.redaction import login_digest, redact
from src.services.play_recorder.schema import (
    PROFILES,
    SCHEMA_VERSION,
    ActionRecord,
    Context,
    EventRecord,
    LootItem,
    MessageRecord,
    ObservationRecord,
    OutcomeRecord,
    Profile,
    Record,
)

OUTPUT_DIRECTORY = "play"
DEFAULT_RETENTION_DAYS = 180
_SESSION_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S_%f%z"
_DATE_FORMAT = "%Y-%m-%d"
_MAX_BUFFER = 100
_FLUSH_INTERVAL_S = 1.0
_MAX_WRITE_FAILURES = 3

logger = logging.getLogger(__name__)
_encoder = msgspec.json.Encoder()
_prune_lock = threading.Lock()
_pruned_roots: set[Path] = set()


@dataclass(slots=True)
class _MessageRaw:
    recorded_at: datetime
    clear_sub_msg: Message | None
    obf_sub_msg: Message
    uid: int | None
    from_server: bool
    source: MessageSource


@dataclass(slots=True)
class _ActionRaw:
    recorded_at: datetime
    source: MessageSource
    context: Context
    action: dict[str, Any]
    observation: dict[str, Any] | None


@dataclass(slots=True)
class _EventRaw:
    recorded_at: datetime
    event: str
    observation: dict[str, Any] | None
    snapshot: dict[str, Any]
    fight_id: int | None


@dataclass(slots=True)
class _OutcomeRaw:
    recorded_at: datetime
    outcome: FightOutcome


_RawEntry = _MessageRaw | _ActionRaw | _EventRaw | _OutcomeRaw


@dataclass
class PlayRecorder:
    file_path: str
    login: str | None = None
    session: str | None = None
    profile: Profile = "decisions"
    _queue: queue.Queue[_RawEntry | None] = field(
        init=False, default_factory=queue.Queue[_RawEntry | None]
    )
    _stop_event: threading.Event = field(init=False, default_factory=threading.Event)
    _login_hash: str | None = field(init=False, default=None)
    _seq: int = field(init=False, default=0)
    _fight_id: int | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}")
        self._login_hash = login_digest(self.login)
        self._thread = threading.Thread(
            target=self._drain,
            daemon=True,
            name=f"PlayRecorder-{Path(self.file_path).stem}",
        )
        self._thread.start()
        atexit.register(self.stop)

    def record_message(
        self,
        clear_sub_msg: Message | None,
        obf_sub_msg: Message,
        uid: int | None,
        from_server: bool,
        source: MessageSource,
    ) -> None:
        if self.profile != "full":
            return
        self._queue.put(
            _MessageRaw(
                datetime.now().astimezone(),
                clear_sub_msg,
                obf_sub_msg,
                uid,
                from_server,
                source,
            )
        )

    def record_action(
        self,
        source: MessageSource,
        action: dict[str, Any],
        observation: dict[str, Any] | None,
        *,
        context: Context,
    ) -> None:
        self._queue.put(
            _ActionRaw(
                datetime.now().astimezone(),
                source,
                context,
                action,
                observation,
            )
        )

    def record_event(
        self,
        event: str,
        observation: dict[str, Any] | None,
        snapshot: dict[str, Any],
        *,
        fight_id: int | None = None,
    ) -> None:
        self._queue.put(
            _EventRaw(
                datetime.now().astimezone(),
                event,
                observation,
                snapshot,
                fight_id,
            )
        )

    def record_outcome(self, outcome: FightOutcome) -> None:
        self._queue.put(_OutcomeRaw(datetime.now().astimezone(), outcome))

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _drain(self) -> None:
        buffer: list[bytes] = []
        failures = 0
        last_flush = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=_FLUSH_INTERVAL_S)
            except queue.Empty:
                item = None
                if self._stop_event.is_set():
                    self._flush(buffer)
                    return

            if item is not None:
                buffer.extend(self._encode(item))

            now = time.monotonic()
            if buffer and (len(buffer) >= _MAX_BUFFER or now - last_flush >= _FLUSH_INTERVAL_S):
                if self._flush(buffer):
                    failures = 0
                else:
                    failures += 1
                    if failures >= _MAX_WRITE_FAILURES:
                        logger.error(
                            "Dropping %d play recording records after repeated write failures",
                            len(buffer),
                        )
                        buffer.clear()
                        failures = 0
                last_flush = now

            if self._stop_event.is_set() and self._queue.empty():
                self._flush(buffer)
                return

    def _encode(self, raw: _RawEntry) -> list[bytes]:
        return [_encoder.encode(record) for record in self._build_records(raw)]

    def _build_records(self, raw: _RawEntry) -> list[Record]:
        if isinstance(raw, _MessageRaw):
            return [self._message_record(raw)]
        if isinstance(raw, _ActionRaw):
            return self._action_records(raw)
        if isinstance(raw, _EventRaw):
            return self._event_records(raw)
        return [self._outcome_record(raw)]

    def _message_record(self, raw: _MessageRaw) -> MessageRecord:
        msg = raw.clear_sub_msg if raw.clear_sub_msg is not None else raw.obf_sub_msg
        return MessageRecord(
            **self._header(raw.recorded_at),
            kind="message",
            direction="server_to_client" if raw.from_server else "client_to_server",
            source=raw.source,
            type=msg.__class__.__name__,
            namespace=msg.DESCRIPTOR.full_name,
            decoded=raw.clear_sub_msg is not None,
            uid=raw.uid,
            content=self._redacted_dict(to_jsonable(msg)),
        )

    def _action_records(self, raw: _ActionRaw) -> list[Record]:
        observation_id = self._next_id("o") if raw.observation is not None else None
        records: list[Record] = []
        turn = _turn(raw.observation)
        if raw.observation is not None and observation_id is not None:
            records.append(
                ObservationRecord(
                    **self._header(raw.recorded_at),
                    kind="observation",
                    observation_id=observation_id,
                    context=raw.context,
                    fight_id=self._fight_id,
                    turn=turn,
                    data=self._redacted_dict(raw.observation),
                )
            )
        records.append(
            ActionRecord(
                **self._header(raw.recorded_at),
                kind="action",
                decision_id=self._next_id("d"),
                observation_id=observation_id,
                source=raw.source,
                context=raw.context,
                fight_id=self._fight_id,
                turn=turn,
                action=self._redacted_dict(raw.action),
            )
        )
        return records

    def _event_records(self, raw: _EventRaw) -> list[Record]:
        if raw.event == "fight_start" and raw.fight_id is not None:
            self._fight_id = raw.fight_id
        observation_id = self._next_id("o") if raw.observation is not None else None
        records: list[Record] = []
        if raw.observation is not None and observation_id is not None:
            records.append(
                ObservationRecord(
                    **self._header(raw.recorded_at),
                    kind="observation",
                    observation_id=observation_id,
                    context="fight",
                    fight_id=self._fight_id,
                    turn=_turn(raw.observation),
                    data=self._redacted_dict(raw.observation),
                )
            )
        records.append(
            EventRecord(
                **self._header(raw.recorded_at),
                kind="event",
                event=raw.event,
                observation_id=observation_id,
                fight_id=self._fight_id,
                turn=_turn(raw.observation),
                snapshot=self._redacted_dict(raw.snapshot),
            )
        )
        if raw.event == "fight_end":
            self._fight_id = None
        return records

    def _outcome_record(self, raw: _OutcomeRaw) -> OutcomeRecord:
        outcome = raw.outcome
        record = OutcomeRecord(
            **self._header(raw.recorded_at),
            kind="outcome",
            result=outcome.result,
            fight_id=self._fight_id,
            duration_seconds=outcome.duration_seconds,
            turns=outcome.turns,
            kamas=outcome.kamas,
            experience=outcome.experience,
            alive=outcome.alive,
            loot=[LootItem(id=item_id, quantity=quantity) for item_id, quantity in outcome.loot],
        )
        self._fight_id = None
        return record

    def _header(self, recorded_at: datetime) -> dict[str, Any]:
        seq = self._seq
        self._seq += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "datetime": recorded_at.isoformat(),
            "login_hash": self._login_hash,
            "session": self.session,
            "seq": seq,
        }

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{self._seq}"

    @staticmethod
    def _redacted_dict(value: Any) -> dict[str, Any]:
        return cast("dict[str, Any]", redact(value))

    def _flush(self, buffer: list[bytes]) -> bool:
        if not buffer:
            return True
        payload = b"\n".join(buffer) + b"\n"
        path = Path(self.file_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Every flush is a complete gzip member, so a crash cannot corrupt earlier flushes.
            with gzip.open(path, "ab") as file:
                file.write(payload)
        except OSError:
            logger.exception("Play recording write failed for %s", path)
            return False
        buffer.clear()
        return True


def _turn(observation: dict[str, Any] | None) -> int | None:
    if not observation:
        return None
    turn = observation.get("fight_turn")
    return turn if isinstance(turn, int) else None


def create_bot_session_play_recorder(
    recording_directory: Path,
    login: str,
    *,
    session_started_at: datetime | None = None,
    profile: str = "decisions",
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> PlayRecorder:
    if profile not in PROFILES:
        raise ValueError(f"PLAY_RECORDING_PROFILE must be one of {PROFILES}")
    started_at = session_started_at or datetime.now().astimezone()
    assert started_at.tzinfo is not None, "Play recording session timestamp must be timezone-aware"

    login_hash = login_digest(login) or "anonymous"
    session = started_at.strftime(_SESSION_TIMESTAMP_FORMAT)
    root = recording_directory / OUTPUT_DIRECTORY
    _prune_expired_sessions(root, retention_days)
    file_path = root / login_hash / started_at.strftime(_DATE_FORMAT) / f"{session}.jsonl.gz"

    return PlayRecorder(
        file_path=str(file_path),
        login=login,
        session=session,
        profile=cast("Profile", profile),
    )


def _prune_expired_sessions(root: Path, retention_days: int) -> None:
    if retention_days <= 0 or not root.is_dir():
        return
    with _prune_lock:
        if root in _pruned_roots:
            return
        _pruned_roots.add(root)
        cutoff = time.time() - retention_days * 86400
        for path in root.rglob("*.jsonl.gz"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                logger.warning("Could not prune expired play recording %s", path)
