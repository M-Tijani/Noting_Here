from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ankama_launcher_emulator.utils.atomic_file import atomic_write_text
from src.consts import LUA_SCRIPT_PROGRESS_FOLDER

logger = logging.getLogger(__name__)

_UNSAFE_RE = re.compile(r"[^0-9A-Za-z_.-]")


class ScriptProgress(BaseModel):
    """Resume point of a Lua script's move-engine cycle."""

    step: int = 0


def progress_path(login: str, script_name: str) -> Path:
    return LUA_SCRIPT_PROGRESS_FOLDER / _safe(login) / f"{_safe(script_name)}.json"


def load_progress(login: str, script_name: str) -> int:
    path = progress_path(login, script_name)
    if not path.exists():
        return 0
    try:
        progress = ScriptProgress.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        logger.warning("Cannot read Lua script progress %s: %s", path, error)
        return 0
    return max(0, progress.step)


def save_progress(login: str, script_name: str, step: int) -> None:
    path = progress_path(login, script_name)
    try:
        atomic_write_text(path, ScriptProgress(step=max(0, int(step))).model_dump_json())
    except OSError as error:
        logger.warning("Cannot save Lua script progress %s: %s", path, error)


def clear_progress(login: str, script_name: str) -> None:
    try:
        progress_path(login, script_name).unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Cannot clear Lua script progress for %s: %s", script_name, error)


def _safe(part: str) -> str:
    return _UNSAFE_RE.sub("_", part.strip()) or "_"
