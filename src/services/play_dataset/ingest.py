"""Turn raw play traces into a partitioned Parquet decision dataset.

The ingest is streaming: messages are read for fight metadata and outcomes but are
not written, actions are buffered per fight until its outcome is known, and each
partition is flushed independently. Output layout::

    <output>/decisions/<human|bot|other>/<YYYY-MM-DD>/part-00000.parquet
    <output>/fights/<YYYY-MM-DD>/part-00000.parquet
    <output>/report.json
    <output>/malformed.jsonl
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from src.services.play_dataset.readers import RawRecord, detect_format, iter_records
from src.services.play_recorder.redaction import redact

HUMAN_SOURCE = "client_forwarded"
BOT_SOURCE = "framework_injected"
GO_NO_GO_HUMAN_DECISIONS = 5000
_MAX_ERROR_SAMPLES = 100
_OBSERVATION_WINDOW = 64

DECISION_SCHEMA = pa.schema(
    [
        pa.field("login_hash", pa.string()),
        pa.field("session", pa.string()),
        pa.field("date", pa.date32()),
        pa.field("datetime", pa.timestamp("us", tz="UTC")),
        pa.field("schema_version", pa.int16()),
        pa.field("seq", pa.int64()),
        pa.field("decision_id", pa.string()),
        pa.field("observation_id", pa.string()),
        pa.field("fight_key", pa.string()),
        pa.field("fight_seq", pa.int32()),
        pa.field("fight_id", pa.int64()),
        pa.field("turn", pa.int32()),
        pa.field("source", pa.string()),
        pa.field("context", pa.string()),
        pa.field("action_type", pa.string()),
        pa.field("action_json", pa.string()),
        pa.field("observation_json", pa.string()),
        pa.field("observation_map_id", pa.int64()),
        pa.field("outcome_result", pa.string()),
        pa.field("outcome_kamas", pa.int64()),
        pa.field("outcome_experience", pa.int64()),
        pa.field("outcome_alive", pa.bool_()),
        pa.field("fight_monsters", pa.list_(pa.int32())),
        pa.field("fight_contain_boss", pa.bool_()),
        pa.field("fight_duration_seconds", pa.int32()),
        pa.field("fight_turns", pa.int32()),
    ]
)

FIGHT_SCHEMA = pa.schema(
    [
        pa.field("login_hash", pa.string()),
        pa.field("session", pa.string()),
        pa.field("date", pa.date32()),
        pa.field("fight_key", pa.string()),
        pa.field("fight_seq", pa.int32()),
        pa.field("fight_id", pa.int64()),
        pa.field("start_datetime", pa.timestamp("us", tz="UTC")),
        pa.field("end_datetime", pa.timestamp("us", tz="UTC")),
        pa.field("monsters", pa.list_(pa.int32())),
        pa.field("contain_boss", pa.bool_()),
        pa.field("result", pa.string()),
        pa.field("duration_seconds", pa.int32()),
        pa.field("turns", pa.int32()),
        pa.field("kamas", pa.int64()),
        pa.field("experience", pa.int64()),
        pa.field("alive", pa.bool_()),
        pa.field("loot_json", pa.string()),
        pa.field("human_decisions", pa.int32()),
        pa.field("bot_decisions", pa.int32()),
        pa.field("has_outcome", pa.bool_()),
    ]
)


@dataclass(slots=True)
class IngestStats:
    raw_records: int = 0
    malformed: int = 0
    duplicates: int = 0
    messages: int = 0
    actions: int = 0
    observations: int = 0
    events: int = 0
    outcome_records: int = 0
    outcome_messages: int = 0
    outcomes_attached: int = 0
    outcomes_without_fight: int = 0
    decisions_written: int = 0
    fights_written: int = 0
    human_decisions: int = 0
    bot_decisions: int = 0
    human_decisions_with_outcome: int = 0
    truncated_inputs: int = 0
    unknown_kind: int = 0


@dataclass(slots=True)
class IngestResult:
    output: Path
    stats: IngestStats
    formats: dict[str, int]
    dates: list[str]
    partitions: list[str]
    parquet_bytes: int
    errors: list[dict[str, str]]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        human_with_outcome = self.stats.human_decisions_with_outcome
        return {
            "generated_at": self.generated_at,
            "output": str(self.output),
            "counts": asdict(self.stats),
            "formats": self.formats,
            "dates": self.dates,
            "partitions": self.partitions,
            "parquet_bytes": self.parquet_bytes,
            "go_no_go": {
                "threshold": GO_NO_GO_HUMAN_DECISIONS,
                "human_decisions_with_outcome": human_with_outcome,
                "defer_learned_policy": human_with_outcome < GO_NO_GO_HUMAN_DECISIONS,
            },
            "errors": self.errors,
        }


@dataclass(slots=True)
class _Outcome:
    result: str
    duration_seconds: int
    turns: int | None
    kamas: int
    experience: int
    alive: bool | None
    loot: list[dict[str, int]]


@dataclass(slots=True)
class _Fight:
    login_hash: str
    session: str
    date: date
    fight_seq: int
    key: str
    fight_id: int | None
    start_datetime: datetime
    last_datetime: datetime
    monsters: list[int]
    contain_boss: bool | None
    decisions: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    outcome: _Outcome | None = None
    max_turn: int | None = None
    human: int = 0
    bot: int = 0


@dataclass(slots=True)
class _Session:
    login_hash: str
    session: str
    seq: int = 0
    character_id: int | None = None
    fight: _Fight | None = None
    fight_count: int = 0
    pending_start: dict[str, Any] | None = None
    observations: OrderedDict[str, dict[str, Any]] = field(
        default_factory=OrderedDict[str, dict[str, Any]]
    )


class _Deduper:
    def __init__(self, window: int) -> None:
        self._window = window
        self._seen: set[tuple[str, ...]] = set()
        self._order: deque[tuple[str, ...]] = deque()

    def seen(self, key: tuple[str, ...]) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._window:
            self._seen.discard(self._order.popleft())
        return False


class _ParquetPartitions:
    def __init__(self, root: Path, schema: pa.Schema, rows_per_file: int, compression: str) -> None:
        self.root = root
        self.schema = schema
        self.rows_per_file = rows_per_file
        self.compression = compression
        self.buffers: dict[str, list[dict[str, Any]]] = {}
        self.parts: dict[str, int] = {}
        self.partitions: set[str] = set()

    def add(self, partition: str, row: dict[str, Any]) -> None:
        self.partitions.add(partition)
        buffer = self.buffers.setdefault(partition, [])
        buffer.append(row)
        if len(buffer) >= self.rows_per_file:
            self._flush(partition)

    def close(self) -> None:
        for partition in list(self.buffers):
            self._flush(partition)

    def _flush(self, partition: str) -> None:
        rows = self.buffers[partition]
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=self.schema)
        directory = self.root / partition
        directory.mkdir(parents=True, exist_ok=True)
        part = self.parts.get(partition, 0)
        pq.write_table(table, directory / f"part-{part:05d}.parquet", compression=self.compression)
        self.parts[partition] = part + 1
        rows.clear()


def ingest(
    inputs: list[Path],
    output: Path,
    *,
    rows_per_file: int = 100_000,
    compression: str = "zstd",
    dedupe_window: int = 200_000,
) -> IngestResult:
    run = _IngestRun(output, rows_per_file, compression, dedupe_window)
    return run.run(inputs)


class _IngestRun:
    def __init__(self, output: Path, rows_per_file: int, compression: str, dedupe_window: int) -> None:
        self.output = output
        self.stats = IngestStats()
        self.formats: dict[str, int] = {}
        self.errors: list[dict[str, str]] = []
        self.dates: set[date] = set()
        self.sessions: dict[tuple[str, str], _Session] = {}
        self.deduper = _Deduper(dedupe_window)
        _reset_output(output)
        self.decisions = _ParquetPartitions(output / "decisions", DECISION_SCHEMA, rows_per_file, compression)
        self.fights = _ParquetPartitions(output / "fights", FIGHT_SCHEMA, rows_per_file, compression)

    def run(self, inputs: list[Path]) -> IngestResult:
        for path in inputs:
            self._run_input(path)
        for session in list(self.sessions.values()):
            self._close_fight(session)
        self.decisions.close()
        self.fights.close()
        self._write_malformed()
        result = IngestResult(
            output=self.output,
            stats=self.stats,
            formats=self.formats,
            dates=sorted(day.isoformat() for day in self.dates),
            partitions=sorted(self.decisions.partitions | self.fights.partitions),
            parquet_bytes=_parquet_bytes(self.output),
            errors=self.errors,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        (self.output / "report.json").write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8"
        )
        return result

    def _run_input(self, path: Path) -> None:
        try:
            fmt = detect_format(path)
        except (OSError, EOFError) as exc:
            self._record_error(path, f"unreadable input: {exc}")
            self.stats.malformed += 1
            return
        self.formats[fmt] = self.formats.get(fmt, 0) + 1
        truncated = False
        for raw in iter_records(path):
            if raw.error is not None:
                self.stats.malformed += 1
                self._record_error(path, raw.error)
                truncated = truncated or raw.error.startswith("truncated")
                continue
            if raw.data is not None:
                self._process(raw.data)
        if truncated:
            self.stats.truncated_inputs += 1

    def _process(self, record: dict[str, Any]) -> None:
        redacted = redact(record)
        if not isinstance(redacted, dict):
            self.stats.malformed += 1
            return
        record = cast("dict[str, Any]", redacted)
        self.stats.raw_records += 1
        kind = record.get("kind")
        schema_version = _int(record.get("schema_version"), 1)
        session = self._session_for(record)
        raw_seq = record.get("seq")
        seq = raw_seq if isinstance(raw_seq, int) else session.seq
        if self.deduper.seen(_dedupe_key(record, schema_version, seq)):
            self.stats.duplicates += 1
            return
        if isinstance(raw_seq, int):
            session.seq = max(session.seq, raw_seq + 1)
        else:
            session.seq += 1

        if kind == "message":
            self._handle_message(record, session)
        elif kind == "action":
            self._handle_action(record, session, seq)
        elif kind == "observation":
            self._handle_observation(record, session)
        elif kind == "event":
            self._handle_event(record, session)
        elif kind == "outcome":
            self._handle_outcome(record, session)
        else:
            self.stats.unknown_kind += 1
            self._record_error(Path("<stream>"), f"unknown kind {kind!r}")

    def _session_for(self, record: dict[str, Any]) -> _Session:
        login_hash = _text(record.get("login_hash"))
        session = _text(record.get("session"))
        key = (login_hash, session)
        existing = self.sessions.get(key)
        if existing is None:
            existing = _Session(login_hash=login_hash, session=session)
            self.sessions[key] = existing
        return existing

    def _handle_message(self, record: dict[str, Any], session: _Session) -> None:
        self.stats.messages += 1
        content = record.get("content")
        if not isinstance(content, dict):
            return
        content = cast("dict[str, Any]", content)
        msg_type = record.get("type")
        if msg_type == "FightStartingEvent":
            attacker = _int_or_none(content.get("attacker_id"))
            if attacker is not None and attacker > 0:
                session.character_id = attacker
            contain_boss = content.get("contain_boss")
            session.pending_start = {
                "fight_id": _int_or_none(content.get("fight_id")),
                "monsters": _int_list(content.get("monsters")),
                "contain_boss": contain_boss if isinstance(contain_boss, bool) else None,
                "datetime": _parse_datetime(record.get("datetime")),
            }
        elif msg_type == "FightEndEvent":
            self.stats.outcome_messages += 1
            outcome = _outcome_from_message(content, session.character_id)
            if outcome is not None:
                self._attach_outcome(session, outcome)

    def _handle_action(self, record: dict[str, Any], session: _Session, seq: int) -> None:
        self.stats.actions += 1
        source = _text(record.get("source"))
        context = _text(record.get("context")) or "world"
        observation, pending_turn, pending_fight_id = self._resolve_observation(record, session)
        turn = _int_or_none(record.get("turn"))
        if turn is None:
            turn = pending_turn
        fight_id = _int_or_none(record.get("fight_id"))
        if fight_id is None:
            fight_id = pending_fight_id
        if isinstance(observation, dict):
            _capture_character_id(session, observation)
        if context == "fight":
            if session.fight is None:
                self._open_fight(session, fight_id=fight_id, meta=None, record=record)
            elif (
                fight_id is not None
                and session.fight.fight_id is not None
                and fight_id != session.fight.fight_id
            ):
                self._close_fight(session)
                self._open_fight(session, fight_id=fight_id, meta=None, record=record)
        elif session.fight is not None:
            self._close_fight(session)

        fight = session.fight
        if fight is not None:
            if fight.fight_id is None and fight_id is not None:
                fight.fight_id = fight_id
            recorded_at = _parse_datetime(record.get("datetime"))
            if recorded_at is not None:
                fight.last_datetime = recorded_at
            if turn is not None:
                fight.max_turn = turn if fight.max_turn is None else max(fight.max_turn, turn)

        row = _decision_row(
            record=record,
            session=session,
            seq=seq,
            source=source,
            context=context,
            observation=observation,
            turn=turn,
            fight=fight,
        )
        partition = _decision_partition(source, row["date"])
        if fight is None:
            self._write_decision(partition, row)
            return
        fight.decisions.append((partition, row))
        if source == HUMAN_SOURCE:
            fight.human += 1
        elif source == BOT_SOURCE:
            fight.bot += 1

    def _handle_observation(self, record: dict[str, Any], session: _Session) -> None:
        self.stats.observations += 1
        observation_id = record.get("observation_id")
        if not isinstance(observation_id, str):
            return
        data = record.get("data")
        if not isinstance(data, dict):
            data = {}
        data = cast("dict[str, Any]", data)
        _capture_character_id(session, data)
        session.observations[observation_id] = {
            "data": data,
            "turn": _int_or_none(record.get("turn")),
            "fight_id": _int_or_none(record.get("fight_id")),
        }
        while len(session.observations) > _OBSERVATION_WINDOW:
            session.observations.popitem(last=False)

    def _handle_event(self, record: dict[str, Any], session: _Session) -> None:
        self.stats.events += 1
        event = record.get("event")
        if event == "fight_start":
            self._close_fight(session)
            observation = record.get("observation")
            if isinstance(observation, dict):
                _capture_character_id(session, cast("dict[str, Any]", observation))
            fight_id = _int_or_none(record.get("fight_id"))
            meta = session.pending_start
            if meta is not None and (fight_id is None or meta.get("fight_id") == fight_id):
                if fight_id is None:
                    fight_id = meta.get("fight_id")
            else:
                meta = None
            session.pending_start = None
            self._open_fight(session, fight_id=fight_id, meta=meta, record=record)
        elif event == "fight_end":
            self._close_fight(session)

    def _handle_outcome(self, record: dict[str, Any], session: _Session) -> None:
        self.stats.outcome_records += 1
        outcome = _outcome_from_record(record)
        if outcome is not None:
            self._attach_outcome(session, outcome)

    def _attach_outcome(self, session: _Session, outcome: _Outcome) -> None:
        fight = session.fight
        if fight is None:
            self.stats.outcomes_without_fight += 1
            return
        fight.outcome = outcome
        if outcome.turns is not None:
            fight.max_turn = outcome.turns if fight.max_turn is None else max(fight.max_turn, outcome.turns)
        self.stats.outcomes_attached += 1

    def _resolve_observation(
        self, record: dict[str, Any], session: _Session
    ) -> tuple[dict[str, Any] | None, int | None, int | None]:
        observation_id = record.get("observation_id")
        if isinstance(observation_id, str):
            pending = session.observations.pop(observation_id, None)
            if pending is not None:
                return pending["data"], pending["turn"], pending["fight_id"]
        observation = record.get("observation")
        if isinstance(observation, dict):
            obs = cast("dict[str, Any]", observation)
            return obs, _int_or_none(obs.get("fight_turn")), None
        return None, None, None

    def _open_fight(
        self,
        session: _Session,
        *,
        fight_id: int | None,
        meta: dict[str, Any] | None,
        record: dict[str, Any],
    ) -> None:
        recorded_at = _parse_datetime(record.get("datetime")) or datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        local_date = _local_date(record.get("datetime"), recorded_at)
        if meta is not None:
            if meta.get("datetime") is not None:
                recorded_at = meta["datetime"]
            if fight_id is None:
                fight_id = meta.get("fight_id")
        session.fight_count += 1
        session.fight = _Fight(
            login_hash=session.login_hash,
            session=session.session,
            date=local_date,
            fight_seq=session.fight_count,
            key=f"{session.login_hash}:{session.session}:{session.fight_count}",
            fight_id=fight_id,
            start_datetime=recorded_at,
            last_datetime=recorded_at,
            monsters=list(meta["monsters"]) if meta is not None else [],
            contain_boss=meta.get("contain_boss") if meta is not None else None,
        )

    def _close_fight(self, session: _Session) -> None:
        fight = session.fight
        if fight is None:
            return
        session.fight = None
        outcome = fight.outcome
        turns = outcome.turns if outcome is not None and outcome.turns is not None else fight.max_turn
        if not fight.decisions and outcome is None:
            return
        for partition, row in fight.decisions:
            row["outcome_result"] = outcome.result if outcome is not None else None
            row["outcome_kamas"] = outcome.kamas if outcome is not None else None
            row["outcome_experience"] = outcome.experience if outcome is not None else None
            row["outcome_alive"] = outcome.alive if outcome is not None else None
            row["fight_duration_seconds"] = outcome.duration_seconds if outcome is not None else None
            row["fight_turns"] = turns
            self._write_decision(partition, row)
        self.fights.add(_fight_partition(fight.date), _fight_row(fight, turns, outcome))
        self.stats.fights_written += 1
        self.dates.add(fight.date)

    def _write_decision(self, partition: str, row: dict[str, Any]) -> None:
        self.decisions.add(partition, row)
        self.stats.decisions_written += 1
        source = row["source"]
        if source == HUMAN_SOURCE:
            self.stats.human_decisions += 1
            if row["outcome_result"] is not None:
                self.stats.human_decisions_with_outcome += 1
        elif source == BOT_SOURCE:
            self.stats.bot_decisions += 1
        self.dates.add(row["date"])

    def _record_error(self, path: Path, message: str) -> None:
        if len(self.errors) < _MAX_ERROR_SAMPLES:
            self.errors.append({"input": str(path), "error": message})

    def _write_malformed(self) -> None:
        with (self.output / "malformed.jsonl").open("w", encoding="utf-8") as file:
            for sample in self.errors:
                file.write(json.dumps(sample, ensure_ascii=False) + "\n")


def _reset_output(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(output / "decisions", ignore_errors=True)
    shutil.rmtree(output / "fights", ignore_errors=True)
    for name in ("report.json", "malformed.jsonl"):
        path = output / name
        if path.exists():
            path.unlink()


def _decision_row(
    *,
    record: dict[str, Any],
    session: _Session,
    seq: int,
    source: str,
    context: str,
    observation: dict[str, Any] | None,
    turn: int | None,
    fight: _Fight | None,
) -> dict[str, Any]:
    action = record.get("action")
    if not isinstance(action, dict):
        action = {}
    action = cast("dict[str, Any]", action)
    recorded_at = _parse_datetime(record.get("datetime")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    local_date = _local_date(record.get("datetime"), recorded_at)
    observation_id = record.get("observation_id")
    return {
        "login_hash": session.login_hash,
        "session": session.session,
        "date": local_date,
        "datetime": recorded_at,
        "schema_version": _int(record.get("schema_version"), 1),
        "seq": seq,
        "decision_id": _text(record.get("decision_id")) or f"{session.session}:{seq}",
        "observation_id": observation_id if isinstance(observation_id, str) else None,
        "fight_key": fight.key if fight is not None else None,
        "fight_seq": fight.fight_seq if fight is not None else None,
        "fight_id": fight.fight_id if fight is not None else None,
        "turn": turn,
        "source": source,
        "context": context,
        "action_type": _text(action.get("type")),
        "action_json": json.dumps(action, separators=(",", ":"), ensure_ascii=False),
        "observation_json": json.dumps(observation, separators=(",", ":"), ensure_ascii=False)
        if observation is not None
        else None,
        "observation_map_id": _int_or_none(observation.get("map_id")) if observation else None,
        "outcome_result": None,
        "outcome_kamas": None,
        "outcome_experience": None,
        "outcome_alive": None,
        "fight_monsters": fight.monsters if fight is not None else [],
        "fight_contain_boss": fight.contain_boss if fight is not None else None,
        "fight_duration_seconds": None,
        "fight_turns": None,
    }


def _fight_row(fight: _Fight, turns: int | None, outcome: _Outcome | None) -> dict[str, Any]:
    return {
        "login_hash": fight.login_hash,
        "session": fight.session,
        "date": fight.date,
        "fight_key": fight.key,
        "fight_seq": fight.fight_seq,
        "fight_id": fight.fight_id,
        "start_datetime": fight.start_datetime,
        "end_datetime": fight.last_datetime,
        "monsters": fight.monsters,
        "contain_boss": fight.contain_boss,
        "result": outcome.result if outcome is not None else None,
        "duration_seconds": outcome.duration_seconds if outcome is not None else None,
        "turns": turns,
        "kamas": outcome.kamas if outcome is not None else None,
        "experience": outcome.experience if outcome is not None else None,
        "alive": outcome.alive if outcome is not None else None,
        "loot_json": json.dumps(outcome.loot, separators=(",", ":")) if outcome is not None else None,
        "human_decisions": fight.human,
        "bot_decisions": fight.bot,
        "has_outcome": outcome is not None,
    }


def _dedupe_key(record: dict[str, Any], schema_version: int, seq: int) -> tuple[str, ...]:
    if schema_version >= 2 and isinstance(record.get("seq"), int):
        return (
            "v2",
            _text(record.get("login_hash")),
            _text(record.get("session")),
            _text(record.get("kind")),
            str(seq),
        )
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ("legacy", hashlib.sha256(payload.encode("utf-8")).hexdigest())


def _outcome_from_record(record: dict[str, Any]) -> _Outcome | None:
    result = _normalize_result(record.get("result"))
    if result is None:
        return None
    alive = record.get("alive")
    return _Outcome(
        result=result,
        duration_seconds=_int(record.get("duration_seconds")),
        turns=_int_or_none(record.get("turns")),
        kamas=_int(record.get("kamas")),
        experience=_int(record.get("experience")),
        alive=alive if isinstance(alive, bool) else None,
        loot=_loot(record.get("loot")),
    )


def _outcome_from_message(content: dict[str, Any], character_id: int | None) -> _Outcome | None:
    results = content.get("results")
    if not isinstance(results, list):
        return None
    own = _own_result(cast("list[Any]", results), character_id)
    if own is None:
        return None
    fighter = own.get("fighter_list_entry")
    if not isinstance(fighter, dict):
        fighter = {}
    fighter = cast("dict[str, Any]", fighter)
    rewards = own.get("rewards")
    if not isinstance(rewards, dict):
        rewards = {}
    rewards = cast("dict[str, Any]", rewards)
    alive = fighter.get("alive")
    return _Outcome(
        result=_normalize_result(own.get("outcome")) or "unknown",
        duration_seconds=_int(content.get("duration")),
        turns=None,
        kamas=_int(rewards.get("kamas")),
        experience=_experience_from_fighter(fighter),
        alive=alive if isinstance(alive, bool) else None,
        loot=_loot(rewards.get("objects")),
    )


def _own_result(results: list[Any], character_id: int | None) -> dict[str, Any] | None:
    parsed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        result = cast("dict[str, Any]", entry)
        fighter = result.get("fighter_list_entry")
        if not isinstance(fighter, dict):
            continue
        parsed.append((result, cast("dict[str, Any]", fighter)))
    if character_id is not None:
        for result, fighter in parsed:
            if _int_or_none(fighter.get("fighter_id")) == character_id:
                return result
    candidates = [result for result, fighter in parsed if "player_list_entry" in fighter]
    return candidates[0] if len(candidates) == 1 else None


def _experience_from_fighter(fighter: dict[str, Any]) -> int:
    player_entry = fighter.get("player_list_entry")
    if not isinstance(player_entry, dict):
        return 0
    player = cast("dict[str, Any]", player_entry)
    additional = player.get("additional")
    if not isinstance(additional, list):
        return 0
    for entry in cast("list[Any]", additional):
        if isinstance(entry, dict):
            item = cast("dict[str, Any]", entry)
            experience_data = item.get("experience_data")
            if isinstance(experience_data, dict):
                return _int(cast("dict[str, Any]", experience_data).get("experience_fight_delta"))
    return 0


def _loot(value: Any) -> list[dict[str, int]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, int]] = []
    for entry in cast("list[Any]", value):
        if isinstance(entry, dict):
            item = cast("dict[str, Any]", entry)
            items.append({"id": _int(item.get("id")), "quantity": _int(item.get("quantity"))})
    return items


def _normalize_result(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    name = value.lower()
    if name.startswith("result_"):
        name = name[len("result_") :]
    return name if name in {"lost", "draw", "victory", "tax", "defender_group"} else "unknown"


def _capture_character_id(session: _Session, observation: dict[str, Any]) -> None:
    player = observation.get("player")
    if isinstance(player, dict):
        character_id = _int_or_none(cast("dict[str, Any]", player).get("character_id"))
        if character_id is not None and character_id > 0:
            session.character_id = character_id


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_date(value: Any, fallback: datetime) -> date:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            pass
    return fallback.date()


def _decision_partition(source: str, day: date) -> str:
    group = "human" if source == HUMAN_SOURCE else "bot" if source == BOT_SOURCE else "other"
    return f"{group}/{day.isoformat()}"


def _fight_partition(day: date) -> str:
    return day.isoformat()


def _parquet_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*.parquet"))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in (_int_or_none(entry) for entry in cast("list[Any]", value)) if item is not None]


__all__ = [
    "GO_NO_GO_HUMAN_DECISIONS",
    "IngestResult",
    "IngestStats",
    "RawRecord",
    "ingest",
]
