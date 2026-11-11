from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.consts import LUA_SCRIPTS_FOLDER

_SCRIPT_SUFFIX = ".lua"


@dataclass(frozen=True)
class ScriptInfo:
    name: str
    path: Path


def scripts_folder() -> Path:
    LUA_SCRIPTS_FOLDER.mkdir(parents=True, exist_ok=True)
    return LUA_SCRIPTS_FOLDER


def list_scripts() -> list[ScriptInfo]:
    folder = scripts_folder()
    scripts = [
        ScriptInfo(name=path.stem, path=path)
        for path in folder.glob(f"*{_SCRIPT_SUFFIX}")
        if path.is_file()
    ]
    return sorted(scripts, key=lambda script: script.name)
