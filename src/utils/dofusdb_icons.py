"""Fetch and cache DofusDB item icons by item id (gid).

Intended use from the GUI or fixtures:

    from src.utils.dofusdb_icons import get_or_download_icon

    icon_path = get_or_download_icon(313)
    # icon_path -> Path(".../resources/icons/items/313.png")
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

from src.utils.project_paths import PROJECT_ROOT

DOFUSDB_ITEMS_URL = "https://api.dofusdb.fr/items"
DEFAULT_ICON_DIR = PROJECT_ROOT / "resources" / "icons" / "items"
REQUEST_TIMEOUT = 30


def _request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"DofusDB request failed: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DofusDB request failed: {exc.reason}") from exc


def _request_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "image/png,image/*"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Icon download failed: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Icon download failed: {exc.reason}") from exc


def fetch_item_icon_url(gid: int) -> str | None:
    """Return the DofusDB icon URL for the given item id, or None if not found."""
    params = [("$skip", "0"), ("$limit", "1"), ("id[]", str(gid))]
    url = f"{DOFUSDB_ITEMS_URL}?{urllib.parse.urlencode(params)}"
    payload = _request_json(url)

    raw_data = payload.get("data", [])
    if not isinstance(raw_data, list):
        return None
    data = cast(list[Any], raw_data)
    for raw_item in data:
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, Any], raw_item)
        if item.get("id") == gid:
            img = item.get("img")
            return img if isinstance(img, str) else None
    return None


def save_icon(gid: int, icon_dir: Path | None = None) -> Path:
    """Download the icon for `gid` and save it as `{icon_dir}/{gid}.png`.

    Raises:
        RuntimeError: if the item is not found on DofusDB or the download fails.
    """
    icon_dir = icon_dir or DEFAULT_ICON_DIR
    icon_dir.mkdir(parents=True, exist_ok=True)

    img_url = fetch_item_icon_url(gid)
    if not img_url:
        raise RuntimeError(f"No DofusDB icon found for item id {gid}")

    icon_bytes = _request_bytes(img_url)
    target_path = icon_dir / f"{gid}.png"
    target_path.write_bytes(icon_bytes)
    return target_path


def get_or_download_icon(gid: int, icon_dir: Path | None = None) -> Path:
    """Return the local path to the icon for `gid`, downloading it if missing.

    The file is named `{gid}.png` so the GUI can map any searched item id
    directly to a file path.
    """
    icon_dir = icon_dir or DEFAULT_ICON_DIR
    target_path = icon_dir / f"{gid}.png"
    if target_path.exists():
        return target_path
    return save_icon(gid, icon_dir=icon_dir)


def download_icons(
    gids: set[int],
    icon_dir: Path | None = None,
) -> dict[int, Path]:
    """Download icons for many item ids and return a gid -> path mapping.

    Items that cannot be fetched are omitted from the result and logged to stderr.
    """
    import sys

    icon_dir = icon_dir or DEFAULT_ICON_DIR
    icon_dir.mkdir(parents=True, exist_ok=True)

    if not gids:
        return {}

    params: list[tuple[str, str]] = [("$skip", "0"), ("$limit", str(len(gids)))]
    for gid in sorted(gids):
        params.append(("id[]", str(gid)))

    url = f"{DOFUSDB_ITEMS_URL}?{urllib.parse.urlencode(params)}"
    payload = _request_json(url)

    items_by_gid: dict[int, dict[str, Any]] = {}
    raw_data = payload.get("data", [])
    if isinstance(raw_data, list):
        data = cast(list[Any], raw_data)
        for raw_item in data:
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            item_id = item.get("id")
            if isinstance(item_id, int):
                items_by_gid[item_id] = item

    missing = gids - set(items_by_gid.keys())
    if missing:
        print(
            f"Warning: DofusDB returned no data for item ids: {sorted(missing)}",
            file=sys.stderr,
        )

    saved: dict[int, Path] = {}
    for gid in sorted(items_by_gid.keys()):
        item = items_by_gid[gid]
        img_url = item.get("img")
        if not isinstance(img_url, str) or not img_url:
            print(f"Warning: item {gid} has no icon URL", file=sys.stderr)
            continue

        try:
            icon_bytes = _request_bytes(img_url)
        except RuntimeError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            continue

        target_path = icon_dir / f"{gid}.png"
        target_path.write_bytes(icon_bytes)
        saved[gid] = target_path

    return saved
