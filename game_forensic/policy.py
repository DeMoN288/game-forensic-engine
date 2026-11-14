"""Single shared safety gate for GrindReport decisions.

Every consumer (CLI, Matcher, all MCP tools) judges a report through
`safety_gate`. Callers may stack extra filters on top (`max_gti`,
`max_completion_hours`), but the verdict and category rules live here only —
they previously existed in three divergent copies.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import GrindReport, Model

# Categories that block a game even when GTI stays below the RED_FLAG threshold.
GATE_CATEGORIES: tuple[str, ...] = ("time_gating", "pay_to_skip")

DecisionCode = Literal[
    "OK",
    "RED_FLAG_REJECT",
    "INSUFFICIENT_DATA",
    "GATED_CATEGORY",
    "OVER_GTI",
    "OVER_LENGTH",
    "LENGTH_UNKNOWN",
]


class SafetyPolicy(Model):
    """Thresholds for the shared gate.

    `max_gti` and `max_completion_hours` are optional caller-specific filters,
    not the analyzer verdict threshold (`AnalyzerConfig.reject_threshold`).
    """

    gate_categories: tuple[str, ...] = GATE_CATEGORIES
    max_gti: float | None = Field(default=None, ge=0, le=100)
    max_completion_hours: float | None = Field(default=None, gt=0)


class SafetyDecision(Model):
    allowed: bool
    code: DecisionCode
    reason: str = ""


def safety_gate(
    report: GrindReport,
    policy: SafetyPolicy | None = None,
    completion_hours: float | None = None,
) -> SafetyDecision:
    """Decide whether a report may be recommended, using one rule set for all callers."""
    policy = policy or SafetyPolicy()
    if report.verdict == "RED_FLAG_REJECT":
        categories = ", ".join(
            f"{key}={rate:.1%}" for key, rate in report.categories.items() if rate > 0
        )
        return SafetyDecision(allowed=False, code="RED_FLAG_REJECT",
                              reason=f"RED_FLAG_REJECT: GTI={report.gti}; {categories}")
    if report.verdict == "INSUFFICIENT_DATA" or report.gti is None:
        return SafetyDecision(
            allowed=False, code="INSUFFICIENT_DATA",
            reason=f"INSUFFICIENT_DATA: {report.sample_size} пригодных отзывов; "
                   f"безопасность не подтверждена")
    risks = [key for key in policy.gate_categories
             if report.verdict == "CAUTION" and report.categories.get(key, 0) > 0]
    if risks:
        return SafetyDecision(
            allowed=False, code="GATED_CATEGORY",
            reason="CAUTION: " + ", ".join(f"{key}={report.categories[key]:.1%}" for key in risks))
    if policy.max_gti is not None and report.gti > policy.max_gti:
        return SafetyDecision(allowed=False, code="OVER_GTI",
                              reason=f"GTI {report.gti} exceeds limit {policy.max_gti}")
    if policy.max_completion_hours is not None and completion_hours is None:
        return SafetyDecision(
            allowed=False, code="LENGTH_UNKNOWN",
            reason=f"Estimated length is unknown; limit is {policy.max_completion_hours}h")
    if (policy.max_completion_hours is not None
            and completion_hours > policy.max_completion_hours):
        return SafetyDecision(
            allowed=False, code="OVER_LENGTH",
            reason=f"Estimated length {completion_hours}h exceeds limit "
                   f"{policy.max_completion_hours}h")
    return SafetyDecision(allowed=True, code="OK", reason="Clean progression pacing")
