"""Build a reproducible train/val/test dataset from the Phase 3 Parquet ingest.

Filters match the plan: human-only decisions, won fights, a turn budget and complete
observations. Splits are assigned per session (never per row) so one session cannot
leak across splits. Output layout::

    <output>/manifest.json
    <output>/features.json
    <output>/splits/train.parquet
    <output>/splits/val.parquet
    <output>/splits/test.parquet
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from src.services.play_dataset.features import (
    FEATURES,
    FEATURE_SPEC_VERSION,
    feature_spec,
    feature_spec_hash,
)
from src.services.play_dataset.ingest import (
    DECISION_SCHEMA,
    GO_NO_GO_HUMAN_DECISIONS,
    HUMAN_SOURCE,
)

DATASET_SCHEMA_VERSION = 1
SPLIT_NAMES = ("train", "val", "test")
COMPLETE_OBSERVATION_KEYS = ("fight_turn", "player", "spells", "actors", "enemies", "characteristics")
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class DatasetFilters:
    human_only: bool = True
    won_fights: bool = True
    max_turn: int | None = 50
    require_complete_observations: bool = True
    val_fraction: float = 0.1
    test_fraction: float = 0.1

    def __post_init__(self) -> None:
        if not 0 <= self.val_fraction < 1:
            raise ValueError("val_fraction must be in [0, 1)")
        if not 0 <= self.test_fraction < 1:
            raise ValueError("test_fraction must be in [0, 1)")
        if self.val_fraction + self.test_fraction >= 1:
            raise ValueError("val_fraction + test_fraction must be below 1")
        if self.max_turn is not None and self.max_turn < 0:
            raise ValueError("max_turn must be non-negative or None")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BuildStats:
    input_decisions: int = 0
    usable_decisions: int = 0
    non_human: int = 0
    non_fight: int = 0
    missing_outcome: int = 0
    not_won: int = 0
    turn_budget: int = 0
    incomplete_observation: int = 0


@dataclass(slots=True)
class BuildResult:
    output: Path
    manifest: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        return self.manifest


def build_dataset(
    input_dir: Path,
    output: Path,
    *,
    filters: DatasetFilters | None = None,
    compression: str = "zstd",
) -> BuildResult:
    filters = filters or DatasetFilters()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"ingest output not found: {input_dir}")

    rows = _read_parquet_rows(input_dir / "decisions")
    rows.sort(key=_row_sort_key)
    usable, stats = _filter_rows(rows, filters)
    split_sessions = _assign_splits(usable, filters)
    session_to_split = {
        session: split for split, sessions in split_sessions.items() for session in sessions
    }
    split_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in SPLIT_NAMES}
    for row in usable:
        split_rows[session_to_split[_session_key(row)]].append(row)

    _reset_output(output)
    splits_dir = output / "splits"
    splits_dir.mkdir(parents=True, exist_ok=True)
    for name in SPLIT_NAMES:
        table = pa.Table.from_pylist(split_rows[name], schema=DECISION_SCHEMA)
        pq.write_table(table, splits_dir / f"{name}.parquet", compression=compression)

    spec = feature_spec()
    (output / "features.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")

    report = _read_report(input_dir / "report.json")
    manifest = _manifest(
        input_dir=input_dir,
        filters=filters,
        stats=stats,
        split_sessions=split_sessions,
        split_rows=split_rows,
        report=report,
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return BuildResult(output=output, manifest=manifest)


def _read_parquet_rows(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.rglob("*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    return rows


def _filter_rows(
    rows: list[dict[str, Any]], filters: DatasetFilters
) -> tuple[list[dict[str, Any]], BuildStats]:
    stats = BuildStats()
    usable: list[dict[str, Any]] = []
    for row in rows:
        stats.input_decisions += 1
        if filters.human_only and row.get("source") != HUMAN_SOURCE:
            stats.non_human += 1
            continue
        if row.get("context") != "fight":
            stats.non_fight += 1
            continue
        outcome = row.get("outcome_result")
        if not isinstance(outcome, str) or not outcome:
            stats.missing_outcome += 1
            continue
        if filters.won_fights and outcome != "victory":
            stats.not_won += 1
            continue
        observation = _parse_observation(row)
        if filters.require_complete_observations and not _is_complete(observation):
            stats.incomplete_observation += 1
            continue
        turn = _turn(row, observation)
        if filters.max_turn is not None and (turn is None or turn > filters.max_turn):
            stats.turn_budget += 1
            continue
        stats.usable_decisions += 1
        usable.append(row)
    return usable, stats


def _parse_observation(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("observation_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _is_complete(observation: dict[str, Any] | None) -> bool:
    if observation is None:
        return False
    if not _is_int(observation.get("fight_turn")):
        return False
    player = observation.get("player")
    if not isinstance(player, dict) or not player:
        return False
    return all(
        isinstance(observation.get(key), expected)
        for key, expected in (
            ("spells", list),
            ("actors", dict),
            ("enemies", list),
            ("characteristics", dict),
        )
    )


def _turn(row: dict[str, Any], observation: dict[str, Any] | None) -> int | None:
    turn = row.get("turn")
    if _is_int(turn):
        return cast("int", turn)
    if observation is not None and _is_int(observation.get("fight_turn")):
        return cast("int", observation["fight_turn"])
    return None


def _assign_splits(
    rows: list[dict[str, Any]], filters: DatasetFilters
) -> dict[str, list[tuple[str, str]]]:
    dates: dict[tuple[str, str], date] = {}
    for row in rows:
        key = _session_key(row)
        day = _as_date(row.get("date"))
        if day is not None:
            previous = dates.get(key)
            dates[key] = day if previous is None else min(previous, day)
        else:
            dates.setdefault(key, date.min)
    ordered = sorted(dates, key=lambda key: (dates[key], key))
    sessions: dict[str, list[tuple[str, str]]] = {name: [] for name in SPLIT_NAMES}
    if not ordered:
        return sessions

    n = len(ordered)
    if n == 1:
        sessions["train"] = ordered
        return sessions

    n_val = _split_count(n, filters.val_fraction)
    n_test = _split_count(n, filters.test_fraction)
    while n_val + n_test > n - 1:
        if n_test >= n_val and n_test > 0:
            n_test -= 1
        elif n_val > 0:
            n_val -= 1
        else:
            break

    train_end = n - n_val - n_test
    sessions["train"] = ordered[:train_end]
    sessions["val"] = ordered[train_end : train_end + n_val]
    sessions["test"] = ordered[train_end + n_val :]
    return sessions


def _split_count(n: int, fraction: float) -> int:
    if fraction <= 0:
        return 0
    return max(1, int(n * fraction + 0.5))


def _manifest(
    *,
    input_dir: Path,
    filters: DatasetFilters,
    stats: BuildStats,
    split_sessions: dict[str, list[tuple[str, str]]],
    split_rows: dict[str, list[dict[str, Any]]],
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    all_usable = [row for rows in split_rows.values() for row in rows]
    fight_keys = {row["fight_key"] for row in all_usable if isinstance(row.get("fight_key"), str)}
    days = [_as_date(row.get("date")) for row in all_usable]
    known_days = [day for day in days if day is not None]
    by_split = {
        name: {
            "decisions": len(split_rows[name]),
            "fights": len(
                {row["fight_key"] for row in split_rows[name] if isinstance(row.get("fight_key"), str)}
            ),
            "sessions": len(split_sessions[name]),
        }
        for name in SPLIT_NAMES
    }
    return {
        "dataset": "play_decisions",
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "source": {
            "ingest_dir": str(input_dir),
            "ingest_generated_at": report.get("generated_at") if report else None,
            "ingest_formats": report.get("formats") if report else None,
            "ingest_dates": report.get("dates") if report else None,
        },
        "filters": filters.to_dict(),
        "feature_spec": {
            "version": FEATURE_SPEC_VERSION,
            "hash": feature_spec_hash(),
            "count": len(FEATURES),
        },
        "counts": {
            "input_decisions": stats.input_decisions,
            "excluded": {
                "non_human": stats.non_human,
                "non_fight": stats.non_fight,
                "missing_outcome": stats.missing_outcome,
                "not_won": stats.not_won,
                "turn_budget": stats.turn_budget,
                "incomplete_observation": stats.incomplete_observation,
            },
            "usable_decisions": stats.usable_decisions,
            "usable_fights": len(fight_keys),
            "usable_sessions": len(split_sessions["train"])
            + len(split_sessions["val"])
            + len(split_sessions["test"]),
            "by_split": by_split,
        },
        "date_range": {
            "min": min(known_days).isoformat() if known_days else None,
            "max": max(known_days).isoformat() if known_days else None,
        },
        "splits": {
            name: [{"login_hash": login, "session": session} for login, session in split_sessions[name]]
            for name in SPLIT_NAMES
        },
        "go_no_go": {
            "threshold": GO_NO_GO_HUMAN_DECISIONS,
            "usable_human_decisions": stats.usable_decisions,
            "defer_learned_policy": stats.usable_decisions < GO_NO_GO_HUMAN_DECISIONS,
        },
    }


def _reset_output(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(output / "splits", ignore_errors=True)
    for name in ("manifest.json", "features.json"):
        path = output / name
        if path.exists():
            path.unlink()


def _read_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _git_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and sha else None


def _session_key(row: dict[str, Any]) -> tuple[str, str]:
    login_hash = row.get("login_hash")
    session = row.get("session")
    return (
        login_hash if isinstance(login_hash, str) else "",
        session if isinstance(session, str) else "",
    )


def _row_sort_key(row: dict[str, Any]) -> tuple[str, str, str, int, str]:
    login, session = _session_key(row)
    seq = row.get("seq")
    sort_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
    return (
        _iso(row.get("date")) or "",
        login,
        session,
        sort_seq,
        str(row.get("decision_id") or ""),
    )


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _iso(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value if isinstance(value, str) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "BuildResult",
    "BuildStats",
    "COMPLETE_OBSERVATION_KEYS",
    "DATASET_SCHEMA_VERSION",
    "DatasetFilters",
    "SPLIT_NAMES",
    "build_dataset",
]
