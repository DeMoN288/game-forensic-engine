"""Explainable EN/RU heuristic; GTI is not a calibrated churn probability.

GTI = 100 * (0.7 * complaint_rate + 0.3 * weighted_category_rates).
Rates use unique nonempty supported-language reviews as the denominator.
"""
from __future__ import annotations

import re
from statistics import median

from pydantic import Field

from .models import Assessment, Evidence, Game, GrindReport, Model, Review


class AnalyzerConfig(Model):
    min_reviews: int = Field(default=20, ge=1)
    full_confidence_reviews: int = Field(default=200, ge=1)
    caution_threshold: float = Field(default=15, ge=0, le=100)
    reject_threshold: float = Field(default=40, ge=0, le=100)
    min_reject_complaints: int = Field(default=5, ge=1)
    early_hours: float = Field(default=10, gt=0)
    max_evidence: int = Field(default=30, ge=0)


WEIGHTS = {"repetition": 0.25, "time_gating": 0.20, "rng": 0.15,
           "pay_to_skip": 0.25, "slow_progression": 0.15}
PATTERNS = {
    "repetition": r"\bgrind(?:ing|y|s|ed)?\b|\bfarm(?:ing)?\b|\brepetitive\b|"
                  r"\bsame (?:quests?|tasks?|missions?)\b|гринд\w*|фарм\w*|однообраз\w*",
    "time_gating": r"\btime[- ]gat\w*|\bdaily chores?\b|\bweekly chores?\b|"
                   r"\bcooldowns?\b|\bwait(?:ing)? (?:for )?hours\b|таймгейт\w*|"
                   r"ежедневн\w* задания|кулдаун\w*",
    "rng": r"\bdrop rates?\b|\brng\b|\bloot boxes?\b|\brandom drops?\b|"
           r"шанс\w* выпадени\w*|рандом\w*|лутбокс\w*",
    "pay_to_skip": r"\bpay[- ]to[- ](?:win|skip)\b|\bp2w\b|\bpaywalls?\b|"
                   r"\bbuy boosters?\b|донат\w*|плати\w*.{0,20}пропуст\w*",
    "slow_progression": r"\bslow progress\w*|\btime sink\b|"
                        r"\bhours (?:to|of) (?:unlock|level|grind)\w*|"
                        r"медленн\w* прогресс\w*|затянут\w* прокач\w*",
}
COMPILED = {name: re.compile(pattern, re.I) for name, pattern in PATTERNS.items()}
COMPLAINT = re.compile(
    r"\b(?:boring|tedious|excessive|forced|annoying|unfair|awful|hate|"
    r"too much|too slow|endless|unbearable|frustrat\w*|waste\w*)\b|"
    r"скуч\w*|нудн\w*|надоел\w*|слишком|вынужд\w*|бесконеч\w*|"
    r"раздраж\w*|ужас\w*|ненавиж\w*|душн\w*", re.I,
)
NEGATION = re.compile(
    r"(?:\b(?:no|not|without|never)\s+(?:(?:any|much|really|a|an)\s+){0,2}|"
    r"(?:\bне|\bнет|\bбез)\s+(?:\w+\s+){0,2})$", re.I,
)
CLAUSES = re.compile(r"[.!?;\n]+|\bbut\b|\bhowever\b|\bно\b|\bоднако\b", re.I)
SUPPORTED_LANGUAGES = {"english", "russian"}


class Analyzer:
    def __init__(self, config: AnalyzerConfig | None = None):
        self.config = config or AnalyzerConfig()
        if self.config.caution_threshold > self.config.reject_threshold:
            raise ValueError("Caution threshold must not exceed reject threshold")

    def find_patterns(self, review: Review) -> list[Evidence]:
        """One evidence item per category; positive reviews require complaint cues."""
        if review.language not in SUPPORTED_LANGUAGES:
            return []
        found: dict[str, Evidence] = {}
        for raw_clause in CLAUSES.split(review.text):
            clause = " ".join(raw_clause.split())
            if not clause or (review.positive and not COMPLAINT.search(clause)):
                continue
            for category, pattern in COMPILED.items():
                if category in found:
                    continue
                for match in pattern.finditer(clause):
                    if NEGATION.search(clause[max(0, match.start() - 60):match.start()]):
                        continue
                    start = max(0, match.start() - 100)
                    found[category] = Evidence(
                        review_id=review.id, category=category,
                        excerpt=clause[start:min(len(clause), match.end() + 160)],
                        strength=1.0 if COMPLAINT.search(clause) else 0.7,
                    )
                    break
        return list(found.values())

    def analyze(self, game: Game) -> GrindReport:
        unique: dict[str, Review] = {}
        for review in game.reviews:
            if review.text.strip() and review.language in SUPPORTED_LANGUAGES:
                unique.setdefault(review.id, review)
        reviews = list(unique.values())
        hits = {review.id: self.find_patterns(review) for review in reviews}
        complaints = [review for review in reviews if hits[review.id]]
        n, count = len(reviews), len(complaints)
        categories = {
            category: sum(any(e.category == category for e in hits[r.id]) for r in reviews) / n
            if n else 0.0 for category in WEIGHTS
        }
        gti = round(100 * (0.7 * count / n + 0.3 * sum(
            WEIGHTS[category] * rate for category, rate in categories.items())), 2) if n else None
        timed = [r for r in reviews if r.minutes_at_review is not None]
        early = [r for r in timed if r.minutes_at_review <= self.config.early_hours * 60]
        early_complaints = [r for r in early if hits[r.id]]
        complaint_minutes = [r.minutes_at_review for r in complaints if r.minutes_at_review is not None]
        early_cluster = {
            "threshold_hours": self.config.early_hours,
            "known_playtime_count": len(timed),
            "early_review_count": len(early),
            "early_complaint_count": len(early_complaints),
            "early_complaint_rate": len(early_complaints) / len(early) if early else None,
            "median_complaint_hours": median(complaint_minutes) / 60 if complaint_minutes else None,
        }
        warnings = ["GTI is an uncalibrated EN/RU lexical heuristic, not a churn probability.",
                    "Playtime at review does not establish that a player quit."]
        skipped = len(game.reviews) - n
        if skipped:
            warnings.append(f"Excluded duplicate, empty or unsupported-language reviews: {skipped}")
        degraded = False
        for source in game.sources:
            if source.source == "steam_reviews" and source.status != "ok":
                degraded = True
                warnings.append(f"steam_reviews: {source.status}; {source.detail}")
        if game.metadata.get("sampling"):
            warnings.append("Bounded review sample; category rates are not population estimates.")
        confidence = min(1.0, n / self.config.full_confidence_reviews) * (0.75 if degraded else 1)
        if n < self.config.min_reviews:
            verdict = "INSUFFICIENT_DATA"
            warnings.append(f"Need at least {self.config.min_reviews} usable reviews; got {n}.")
        elif gti >= self.config.reject_threshold and count >= self.config.min_reject_complaints:
            verdict = "RED_FLAG_REJECT"
        elif gti >= self.config.caution_threshold:
            verdict = "CAUTION"
        else:
            verdict = "NO_RED_FLAG_DETECTED"
        evidence = sorted((e for items in hits.values() for e in items),
                          key=lambda e: (-e.strength, e.review_id, e.category))
        return GrindReport(gti=gti, verdict=verdict, confidence=round(confidence, 4),
                           sample_size=n, complaint_count=count, categories=categories,
                           early_cluster=early_cluster, evidence=evidence[:self.config.max_evidence],
                           warnings=warnings)

    def assess(self, game: Game) -> Assessment:
        return Assessment(game=game, grind=self.analyze(game))
