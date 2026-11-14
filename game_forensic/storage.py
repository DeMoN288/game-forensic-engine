"""Validated persistent user data: `blacklist.json` and `profiles.json`.

Both files are hand-editable (see README), so every entry is validated on load.
Malformed input degrades to a warning instead of crashing an MCP tool, and
ignored entries are always reported rather than silently dropped.

A blacklist value of `null` (or an empty string) explicitly cancels an entry,
which is the only way to remove a built-in seed such as Lost Ark.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from pydantic import Field

from .models import Model


def data_dir() -> Path:
    custom = os.environ.get("GAME_FORENSIC_DATA_DIR")
    return Path(custom) if custom else Path.home() / ".game-forensic"


def dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def read_json_object(path: Path, warnings: list[str]) -> dict[str, Any]:
    """Read a JSON object, degrading unreadable or non-object content to a warning."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(f"{path.name}: не читается ({type(exc).__name__}); файл игнорируется")
        return {}
    if not isinstance(raw, dict):
        warnings.append(f"{path.name}: ожидался JSON-объект; файл игнорируется")
        return {}
    return raw


_MUTATION_LOCK = threading.RLock()


def write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(dump_json(payload), encoding="utf-8")
        temp.replace(path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _read_for_mutation(path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    raw = read_json_object(path, warnings)
    if warnings:
        raise ValueError("; ".join(warnings))
    return raw


class UserData(Model):
    blacklist: dict[int, str] = Field(default_factory=dict)
    unblacklisted: set[int] = Field(default_factory=set)
    profiles: dict[int, dict[str, float]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _appid(key: Any) -> int | None:
    text = str(key)
    return int(text) if text.isdigit() and int(text) > 0 else None


def parse_blacklist(raw: dict[str, Any], defaults: dict[int, str] | None,
                    warnings: list[str]) -> tuple[dict[int, str], set[int]]:
    """Merge user entries over the built-in seeds; `null` cancels an entry."""
    blacklist = dict(defaults or {})
    unblacklisted: set[int] = set()
    for key, reason in raw.items():
        appid = _appid(key)
        if appid is None:
            warnings.append(f"blacklist.json: пропущен ключ {key!r} (нужен положительный AppID)")
            continue
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            unblacklisted.add(appid)
            blacklist.pop(appid, None)
            continue
        if not isinstance(reason, str):
            warnings.append(
                f"blacklist.json: пропущен AppID {appid} "
                f"(причина должна быть строкой или null, получено {type(reason).__name__})")
            continue
        blacklist[appid] = reason.strip()
    return blacklist, unblacklisted


def parse_profiles(raw: dict[str, Any], axes: tuple[str, ...],
                   warnings: list[str]) -> dict[int, dict[str, float]]:
    """Keep only known axes with numeric values inside [0, 1]."""
    profiles: dict[int, dict[str, float]] = {}
    known = set(axes)
    for key, axes_map in raw.items():
        appid = _appid(key)
        if appid is None:
            warnings.append(f"profiles.json: пропущен ключ {key!r} (нужен положительный AppID)")
            continue
        if not isinstance(axes_map, dict) or not axes_map:
            warnings.append(f"profiles.json: пропущен AppID {appid} (нужен непустой объект осей)")
            continue
        cleaned: dict[str, float] = {}
        for axis, value in axes_map.items():
            if axis not in known:
                warnings.append(f"profiles.json: AppID {appid}, неизвестная ось {axis!r} пропущена")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                warnings.append(f"profiles.json: AppID {appid}, ось {axis} = {value!r} не число")
                continue
            if not 0.0 <= number <= 1.0:
                warnings.append(f"profiles.json: AppID {appid}, ось {axis} = {number} вне [0, 1]")
                continue
            cleaned[axis] = number
        if cleaned:
            profiles[appid] = cleaned
        else:
            warnings.append(f"profiles.json: у AppID {appid} не осталось валидных осей")
    return profiles


def load_user_data(axes: tuple[str, ...], defaults: dict[int, str] | None = None,
                   directory: Path | None = None) -> UserData:
    warnings: list[str] = []
    root = directory or data_dir()
    raw_blacklist = read_json_object(root / "blacklist.json", warnings)
    blacklist, unblacklisted = parse_blacklist(raw_blacklist, defaults, warnings)
    profiles = parse_profiles(read_json_object(root / "profiles.json", warnings), axes, warnings)
    return UserData(blacklist=blacklist, unblacklisted=unblacklisted, profiles=profiles,
                    warnings=warnings)


def set_blacklist(directory: Path, appid: int, reason: str) -> None:
    path = directory / "blacklist.json"
    with _MUTATION_LOCK:
        raw = _read_for_mutation(path)
        raw[str(appid)] = reason
        write_json_object(path, raw)


def remove_blacklist(directory: Path, appid: int, defaults: dict[int, str] | None = None) -> bool:
    """Drop a user entry, or write `null` when it shadows a built-in seed.

    Returns True when the AppID is still blacklisted afterwards.
    """
    path = directory / "blacklist.json"
    with _MUTATION_LOCK:
        raw = _read_for_mutation(path)
        if appid in (defaults or {}):
            raw[str(appid)] = None
        else:
            raw.pop(str(appid), None)
        write_json_object(path, raw)
        merged, _ = parse_blacklist(raw, defaults, [])
        return appid in merged


def set_profile(directory: Path, appid: int, axes: dict[str, float]) -> dict[str, float]:
    path = directory / "profiles.json"
    with _MUTATION_LOCK:
        raw = _read_for_mutation(path)
        existing = raw.get(str(appid))
        merged = {**(existing if isinstance(existing, dict) else {}), **axes}
        raw[str(appid)] = merged
        write_json_object(path, raw)
        return merged
