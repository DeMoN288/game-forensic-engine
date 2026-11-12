from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Review(Model):
    id: str
    text: str
    positive: bool
    language: str = "english"
    minutes_at_review: int | None = Field(default=None, ge=0)
    minutes_total: int | None = Field(default=None, ge=0)
    created: int | None = None
    updated: int | None = None
    last_played: int | None = None
    helpful: int = Field(default=0, ge=0)
    free: bool = False
    early_access: bool = False
    refunded: bool = False


class SourceStatus(Model):
    source: str
    url: str
    status: Literal["ok", "partial", "unavailable", "parse_error", "disabled"]
    detail: str = ""
    count: int = 0
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Feature(Model):
    value: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class CoreLoop(Model):
    features: dict[str, Feature] = Field(default_factory=dict)
    origin: Literal["heuristic", "curated"] = "heuristic"


class Game(Model):
    appid: int = Field(gt=0)
    name: str = ""
    description: str = ""
    reviews: list[Review] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)
    candidate_ids: list[int] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    loop: CoreLoop = Field(default_factory=CoreLoop)


class Evidence(Model):
    review_id: str
    category: str
    excerpt: str
    strength: float


class GrindReport(Model):
    gti: float | None = Field(default=None, ge=0, le=100)
    verdict: Literal["RED_FLAG_REJECT", "CAUTION", "NO_RED_FLAG_DETECTED", "INSUFFICIENT_DATA"]
    confidence: float = Field(ge=0, le=1)
    sample_size: int
    complaint_count: int
    categories: dict[str, float]
    early_cluster: dict[str, int | float | None]
    evidence: list[Evidence]
    warnings: list[str]
    formula_version: str = "gti-heuristic-v1"


class Match(Model):
    score: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    vector_similarity: float
    common_dimensions: list[str]


class Assessment(Model):
    game: Game
    grind: GrindReport


class RankedGame(Model):
    appid: int
    name: str
    match: Match
    grind: GrindReport
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RecommendationReport(Model):
    reference: int
    raw_candidates: list[int]
    ranked: list[RankedGame]
    excluded: dict[str, str]
    sources: list[SourceStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
