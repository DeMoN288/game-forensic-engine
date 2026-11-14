"""Persistent rejection history and explainable, cumulative preference penalties."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .storage import _MUTATION_LOCK, _read_for_mutation, data_dir, read_json_object, write_json_object

REASON_CODES = ("grind", "repetitive", "too_hard", "too_long", "already_played", "other")
_CUES = {
    "grind": ("копать", "гринд", "фарм", "grind", "farm"),
    "repetitive": ("повтор", "монотон", "однообраз", "repetit"),
    "too_hard": ("сложн", "hard", "punish"),
    "too_long": ("долг", "затянут", "мин", "час", "long", "slow"),
    "already_played": ("играл", "played"),
}


def normalize_reason(reason: str) -> str:
    text = reason.strip().casefold()
    if text in REASON_CODES:
        return text
    return next((code for code, cues in _CUES.items() if any(cue in text for cue in cues)),
                "other")


def record_rejection(appid: int, reason_code: str, note: str = "",
                     tags: list[str] | None = None, directory: Path | None = None) -> dict:
    """Append a rejection atomically; unreadable history is never overwritten."""
    if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0:
        raise ValueError("Steam AppID must be positive")
    if not isinstance(reason_code, str) or not isinstance(note, str):
        raise ValueError("Reason and note must be strings")
    if tags is not None and (not isinstance(tags, list)
                             or any(not isinstance(tag, str) for tag in tags)):
        raise ValueError("Tags must be a list of strings")
    entry = {"appid": appid, "reason_code": normalize_reason(reason_code),
             "note": note.strip() or reason_code.strip(),
             "tags": sorted({tag.strip().casefold() for tag in tags or [] if tag.strip()})}
    path = (directory or data_dir()) / "feedback.json"
    with _MUTATION_LOCK:
        raw = _read_for_mutation(path)
        entries = raw.setdefault("rejections", [])
        if not isinstance(entries, list):
            raise ValueError("feedback.json: rejections must be a list")
        entries.append(entry)
        write_json_object(path, raw)
    return entry


def load_rejection_penalties(directory: Path | None = None) -> dict:
    """Derive bounded axis/tag penalties; malformed hand edits produce warnings."""
    warnings: list[str] = []
    raw = read_json_object((directory or data_dir()) / "feedback.json", warnings)
    entries = raw.get("rejections", [])
    if not isinstance(entries, list):
        warnings.append("feedback.json: rejections must be a list; ignored")
        entries = []
    counts = Counter(dict.fromkeys(REASON_CODES, 0))
    tags: Counter[str] = Counter()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get("reason_code") not in REASON_CODES
                or isinstance(entry.get("appid"), bool)
                or not isinstance(entry.get("appid"), int) or entry["appid"] <= 0
                or not isinstance(entry.get("tags", []), list)
                or any(not isinstance(tag, str) for tag in entry.get("tags", []))):
            warnings.append("feedback.json: malformed rejection ignored")
            continue
        counts[entry["reason_code"]] += 1
        tags.update({tag.strip().casefold() for tag in entry.get("tags", []) if tag.strip()})
    grind = counts["grind"] + counts["repetitive"]
    axes = {}
    if grind:
        axes.update(combat_pace=min(0.5, grind * 0.05), gti_weight=min(1.0, grind * 0.1))
    if counts["too_hard"]:
        axes["punishment_severity"] = min(0.8, counts["too_hard"] * 0.15)
    return {"axis_penalties": axes,
            "tag_penalties": {tag: min(0.5, count * 0.05) for tag, count in sorted(tags.items())},
            "gti_penalty_multiplier": 1.0 + min(3.0, grind * 0.25),
            "preferred_max_hours": max(0.5, 2.0 / counts["too_long"])
            if counts["too_long"] else None,
            "counts": dict(counts), "warnings": warnings}
