"""Streaming readers for play traces.

Two on-disk shapes are supported:

- the legacy ``humanMovement*.json`` file: a JSON array written with one object per
  line, possibly gzip-compressed;
- the v2 ``*.jsonl.gz`` sessions: one JSON object per line, one gzip member per flush.

Readers never load the whole input in memory and report malformed or truncated input
as data instead of raising, so one bad file cannot abort an ingest run.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_CHUNK_SIZE = 1 << 20
_MAX_RECORD_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class RawRecord:
    data: dict[str, Any] | None = None
    error: str | None = None


def discover_inputs(paths: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(path.rglob("*.jsonl.gz"))
            files.update(path.glob("humanMovement*.json"))
            files.update(path.glob("humanMovement*.json.gz"))
        elif path.is_file():
            files.add(path)
        else:
            raise FileNotFoundError(path)
    return sorted(files)


def detect_format(path: Path) -> str:
    with _open_text(path) as file:
        head = file.read(4096)
    return "legacy_array" if head.lstrip().startswith("[") else "jsonl"


def iter_records(path: Path) -> Iterator[RawRecord]:
    try:
        if detect_format(path) == "legacy_array":
            yield from _iter_json_array(path)
        else:
            yield from _iter_jsonl(path)
    except (EOFError, OSError) as exc:
        yield RawRecord(error=f"truncated input: {exc}")


def _open_text(path: Path) -> Any:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _iter_jsonl(path: Path) -> Iterator[RawRecord]:
    with _open_text(path) as file:
        for number, line in enumerate(file, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                yield RawRecord(error=f"line {number}: {exc.msg}")
                continue
            if isinstance(value, dict):
                yield RawRecord(data=cast("dict[str, Any]", value))
            else:
                yield RawRecord(error=f"line {number}: expected object, got {type(value).__name__}")


def _iter_json_array(path: Path) -> Iterator[RawRecord]:
    decoder = json.JSONDecoder()
    with _open_text(path) as file:
        buffer = file.read(_CHUNK_SIZE)
        position = 0
        started = False
        eof = False
        while True:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    more = file.read(_CHUNK_SIZE)
                    if not more:
                        yield RawRecord(error="empty input")
                        return
                    buffer = buffer[position:] + more
                    position = 0
                    continue
                if buffer[position] != "[":
                    yield RawRecord(error="not a JSON array")
                    return
                started = True
                position += 1

            while True:
                while position < len(buffer) and (buffer[position].isspace() or buffer[position] == ","):
                    position += 1
                if position < len(buffer):
                    break
                more = file.read(_CHUNK_SIZE)
                if not more:
                    eof = True
                    break
                buffer = buffer[position:] + more
                position = 0

            if position < len(buffer) and buffer[position] == "]":
                return
            if eof and position >= len(buffer):
                yield RawRecord(error="truncated JSON array")
                return

            try:
                value, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError as exc:
                if eof:
                    yield RawRecord(error=f"truncated JSON array at offset {position}: {exc.msg}")
                    return
                if len(buffer) > _MAX_RECORD_BYTES:
                    yield RawRecord(error=f"record exceeds {_MAX_RECORD_BYTES} bytes")
                    return
                more = file.read(_CHUNK_SIZE)
                if not more:
                    eof = True
                    continue
                buffer = buffer[position:] + more
                position = 0
                continue

            if isinstance(value, dict):
                yield RawRecord(data=cast("dict[str, Any]", value))
            else:
                yield RawRecord(error=f"expected object, got {type(value).__name__}")
            position = end
            if position > _CHUNK_SIZE:
                buffer = buffer[position:]
                position = 0
