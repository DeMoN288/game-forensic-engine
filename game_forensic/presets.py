"""Mechanic-focused target profiles and transparent preference-aware scoring."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .matcher import match_loops
from .models import CoreLoop, Feature


@dataclass(frozen=True)
class Preset:
    name: str
    steam_tags: tuple[str, ...]
    itch_tag: str
    loop: CoreLoop


def _loop(**axes: float) -> CoreLoop:
    return CoreLoop(origin="curated", features={
        axis: Feature(value=value, confidence=1.0, evidence=["preset target"])
        for axis, value in axes.items()})


PRESETS = {
    "perspective": Preset("Иллюзии и перспектива", ("Puzzle", "Surreal"), "puzzle",
                          _loop(build_depth=0.9, tactile_feedback=0.9, punishment_severity=0.1)),
    "meta_narrative": Preset("Мета и рассказчик", ("Comedy", "Narration"), "experimental",
                             _loop(narrative_weight=0.95, punishment_severity=0.1)),
    "tactile_incremental": Preset("Короткая тактильная инкременталка", ("Incremental", "Idler"),
                                  "incremental",
                                  _loop(tactile_feedback=0.95, punishment_severity=0.1)),
}


def get_preset(name: str) -> Preset:
    if name not in PRESETS:
        raise ValueError(f"Unknown preset {name!r}; choose from {', '.join(PRESETS)}")
    return PRESETS[name]


def score_candidate_with_preset(preset_name: str, gti_score: float, loop: CoreLoop,
                                hours: float | None, max_hours: float, penalties: dict,
                                tags: list[str] | None = None) -> tuple[float, list[str]]:
    """Return an unclamped score (higher is better) with every deduction explained."""
    preset = get_preset(preset_name)
    if not math.isfinite(max_hours) or max_hours <= 0:
        raise ValueError("max_hours must be finite and positive")
    if not math.isfinite(gti_score) or not 0 <= gti_score <= 100:
        raise ValueError("gti_score must be finite and between 0 and 100")
    if hours is not None and (not math.isfinite(hours) or hours < 0):
        raise ValueError("hours must be finite and nonnegative")
    match = match_loops(preset.loop, loop)
    score = match.score * 100
    reasons = [f"CoreLoop match: {score:.2f}; coverage: {match.coverage:.0%}"]
    multiplier = max(1.0, penalties.get("gti_penalty_multiplier", 1.0))
    deduction = gti_score * multiplier
    score -= deduction
    reasons.append(f"GTI penalty: -{deduction:.2f} (multiplier {multiplier:.2f})")
    preferred = penalties.get("preferred_max_hours")
    ceiling = min(max_hours, preferred) if preferred is not None else max_hours
    if hours is not None and hours > ceiling:
        deduction = (hours - ceiling) / ceiling * 25
        score -= deduction
        reasons.append(f"Length penalty: -{deduction:.2f} (preferred maximum {ceiling:g}h)")
    for axis, weight in penalties.get("axis_penalties", {}).items():
        feature = loop.features.get(axis)
        value = gti_score / 100 if axis == "gti_weight" else feature.value if feature else None
        if value is not None:
            deduction = value * weight * 100
            score -= deduction
            reasons.append(f"Learned axis {axis}: -{deduction:.2f}")
    tag_weights = {tag.casefold(): weight
                   for tag, weight in penalties.get("tag_penalties", {}).items()}
    for tag in sorted({tag.strip().casefold() for tag in tags or []}):
        if tag in tag_weights:
            deduction = tag_weights[tag] * 100
            score -= deduction
            reasons.append(f"Learned tag {tag}: -{deduction:.2f}")
    return round(score, 6), reasons
