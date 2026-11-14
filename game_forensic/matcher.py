"""Evidence-aware five-axis matching. Scores are heuristics, not probabilities.

No genre cosine or neutral defaults: unknown dimensions reduce coverage/score.
SteamPeek and Steam Store HTML are discovery sources, not safety evidence.
"""
from __future__ import annotations

import asyncio
import re
from statistics import median

from bs4 import BeautifulSoup
from pydantic import Field, model_validator

from .analyzer import SUPPORTED_LANGUAGES, Analyzer
from .models import (
    CoreLoop,
    Feature,
    Game,
    GrindReport,
    Match,
    Model,
    RankedGame,
    RecommendationReport,
    SourceStatus,
)
from .network import SourceError
from .pipeline import Pipeline
from .policy import SafetyPolicy, safety_gate

AXES = ("combat_pace", "build_depth", "tactile_feedback", "punishment_severity", "narrative_weight")
LABELS = {
    "combat_pace": "темп боя", "build_depth": "глубина билдов",
    "tactile_feedback": "отклик управления", "punishment_severity": "цена ошибки",
    "narrative_weight": "сюжетный фокус",
}
# Seed based on the previously confirmed review findings; configurable, not a GTI estimate.
DEFAULT_BLACKLIST = {1599340: "Lost Ark: ранее подтверждены P2W и таймгейты"}
# Low/high endpoints of each independent axis, not genre-to-feature assumptions.
CUES = {
    "combat_pace": (
        (r"turn[- ]based|slow[- ]paced combat|cozy|relaxing|meditative|no rush|at your own pace|"
         r"пошагов\w*|нетороплив\w* бо\w*|медитативн\w*|расслабляющ\w*|без спешки"),
        (r"fast[- ]paced combat|reflex\w*|real[- ]time combat|bullet[- ]hell|intense action|"
         r"быстр\w* бо\w*|реакци\w*|динамичн\w* бо\w*"),
    ),
    "build_depth": (
        r"fixed (?:loadout|build)|no (?:skill tree|build variety|upgrades?)|фиксирован\w* билд\w*|нет прокачк\w*",
        (r"skill trees?|build variety|buildcraft\w*|customi[sz]able builds?|upgrade(?:s|able)?|automation|abilities|"
         r"дерев\w* навыков|вариатив\w* билд\w*|разнообраз\w* билд\w*|прокачк\w*|апгрейд\w*|автоматизац\w*"),
    ),
    "tactile_feedback": (
        r"clunky controls?|unresponsive controls?|floaty combat|неотзывчив\w* управлен\w*",
        (r"responsive controls?|tight controls?|weighty combat|impactful combat|oddly satisfying|very satisfying|"
         r"physics[- ]based|tactile|отзывчив\w* управлен\w*|ощутим\w* удар\w*|залипательн\w*|тактильн\w*"),
    ),
    "punishment_severity": (
        (r"forgiving|no death penalty|frequent checkpoints?|no timers?|no stress|zero stress|"
         r"без штраф\w* за смерть|част\w* чекпоинт\w*|без таймер\w*|без стресс\w*"),
        (r"permadeath|lose (?:all|your) (?:gear|progress|items)|punishing|"
         r"перманентн\w* смерть|потер\w* прогресс\w*|высок\w* цен\w* ошибк\w*"),
    ),
    "narrative_weight": (
        r"no story|minimal (?:story|narrative)|no plot|minimal plot|pure gameplay|без сюжет\w*|минимальн\w* сюжет\w*",
        (r"story[- ]rich|story[- ]driven|narrative[- ]driven|branching (?:story|narrative)|"
         r"сюжетно[- ]ориентирован\w*|глубок\w* сюжет\w*|нелинейн\w* сюжет\w*"),
    ),
}
METADATA_CUES = {
    "combat_pace": (
        r"playable_at_your_own_pace|playable at your own pace|turn-based",
        0.15,
    ),
    "punishment_severity": (
        r"playable without timed input|save anytime",
        0.15,
    ),
}
# Compiled once: `extract_loop` scans every axis against every review text.
COMPILED_CUES = {
    axis: tuple(re.compile(r"\b(?:" + pole + r")\b", re.IGNORECASE) for pole in poles)
    for axis, poles in CUES.items()
}
COMPLETION_CUE = re.compile(
    r"\b(?:beat (?:the|this) game|finished|completed|all achievements?|one sitting)\b|"
    r"(?<!\d)100\s?%|"
    r"прош[её]л|пройдена|на 100%|все достижения|за один присест",
    re.IGNORECASE,
)
# Negation immediately before the cue ("never finished", "не прошёл"). The character class
# is deliberately narrow: a wide window would read "never thought I'd say this, but I finished"
# as an uncompleted game and drop genuine completion evidence.
COMPLETION_NEGATION = re.compile(
    r"\b(?:never|not|no|haven'?t|hasn'?t|hadn'?t|didn'?t|don'?t|doesn'?t|without|не|ни)\b"
    r"[\s\w'\u2019-]{0,15}$",
    re.IGNORECASE,
)
NEGATION = re.compile(r"(?:\b(?:no|not|without)|\b(?:не|нет|без))\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


class MatcherConfig(Model):
    max_candidates: int = Field(default=20, ge=1, le=200)
    top_k: int = Field(default=5, ge=1, le=5)
    concurrency: int = Field(default=3, ge=1, le=12)
    candidate_timeout: float = Field(default=240, gt=0)
    min_coverage: float = Field(default=0.4, gt=0, le=1)
    blacklist: dict[int, str] = Field(default_factory=lambda: dict(DEFAULT_BLACKLIST))
    profiles: dict[int, dict[str, float]] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=lambda: dict.fromkeys(AXES, 1.0))

    @model_validator(mode="after")
    def validate_axes(self) -> MatcherConfig:
        if set(self.weights) != set(AXES) or any(not 0 < w < float("inf") for w in self.weights.values()):
            raise ValueError("Weights must contain all five axes with finite positive values")
        if any(appid <= 0 or not reason.strip() for appid, reason in self.blacklist.items()):
            raise ValueError("Blacklist requires positive AppIDs and nonempty reasons")
        for appid, profile in self.profiles.items():
            if appid <= 0 or not profile or not set(profile) <= set(AXES):
                raise ValueError("Profiles require positive AppIDs and known axes")
            if any(not 0 <= value <= 1 for value in profile.values()):
                raise ValueError("Axis values must be finite and in [0, 1]")
        return self


def extract_loop(game: Game, profile: dict[str, float] | None = None) -> CoreLoop:
    """Curated values override supplied features; otherwise extract explicit EN/RU cues.

    Each description/review is one observation. Conflicting poles reduce confidence;
    description marketing and lexical review cues cannot measure physical input latency.
    """
    features = {axis: feature.model_copy(deep=True) for axis, feature in game.loop.features.items()
                if axis in AXES}
    texts = [("store", game.description)]
    seen = set()
    for review in game.reviews:
        if review.id not in seen and review.language in SUPPORTED_LANGUAGES:
            texts.append((f"review:{review.id}", review.text))
            seen.add(review.id)
    for axis in CUES:
        if axis in features:
            continue
        observations, evidence = [], []
        contradictions = 0
        for source, text in texts:
            hits = []
            for value, pattern in zip((0.15, 0.85), COMPILED_CUES[axis]):
                for hit in pattern.finditer(text):
                    if NEGATION.search(text[max(0, hit.start() - 45):hit.start()]):
                        continue
                    hits.append(value)
                    if len(evidence) < 4:
                        evidence.append(f"{source}: {hit.group(0)}")
                    break
            if hits:
                observations.append(sum(hits) / len(hits))
                contradictions += len(hits) > 1
        if observations:
            value = sum(observations) / len(observations)
            disagreement = sum(abs(x - value) for x in observations) / len(observations)
            confidence = (min(0.85, 0.45 + 0.08 * (len(observations) - 1)) * (1 - disagreement)
                          * (1 - 0.5 * contradictions / len(observations)))
            features[axis] = Feature(value=value, confidence=confidence, evidence=evidence)
    categories_text = " | ".join(
        str(cat.get("description", ""))
        for cat in (game.metadata.get("categories") or [])
        if isinstance(cat, dict) and cat.get("description")
    )
    if categories_text:
        for axis, (pattern, value) in METADATA_CUES.items():
            if axis in features:
                continue
            hit = re.search(pattern, categories_text, re.IGNORECASE)
            if hit:
                features[axis] = Feature(
                    value=value,
                    confidence=0.45,
                    evidence=[f"store_category: {hit.group(0)}"],
                )
    for axis, value in (profile or {}).items():
        features[axis] = Feature(value=value, confidence=1, evidence=["curated profile"])
    return CoreLoop(features=features, origin="curated" if profile else game.loop.origin)


def _confirms_completion(text: str) -> bool:
    """True when a completion cue is stated outright rather than negated.

    Only the 60 characters before a cue are inspected: negations further back belong
    to a different clause and say nothing about whether the player finished.
    """
    for hit in COMPLETION_CUE.finditer(text):
        window = text[max(0, hit.start() - 60):hit.start()]
        if not COMPLETION_NEGATION.search(window):
            return True
    return False


def estimate_completion_hours(game: Game) -> float | None:
    """Estimate median completion playtime (hours) from reviews mentioning finishing the game."""
    hours = [
        r.minutes_at_review / 60
        for r in game.reviews
        if r.minutes_at_review is not None and r.minutes_at_review > 0 and _confirms_completion(r.text)
    ]
    return round(median(hours), 1) if hours else None


def match_loops(reference: CoreLoop, candidate: CoreLoop, weights: dict[str, float] | None = None) -> Match:
    weights = weights or dict.fromkeys(AXES, 1.0)
    # Validate even direct library calls, avoiding NaN/zero denominator and unknown axes.
    MatcherConfig(weights=weights)
    common = [axis for axis in AXES if axis in reference.features and axis in candidate.features
              and min(reference.features[axis].confidence, candidate.features[axis].confidence) > 0]
    observed_weight = sum(weights[axis] for axis in common)
    total_weight = sum(weights.values())
    if not common:
        return Match(score=0, coverage=0, vector_similarity=0, common_dimensions=[])
    distances = {axis: 1 - abs(reference.features[axis].value - candidate.features[axis].value)
                 for axis in common}
    vector = sum(weights[a] * distances[a] for a in common) / observed_weight
    score = sum(weights[a] * distances[a] * min(reference.features[a].confidence,
                                               candidate.features[a].confidence)
                for a in common) / total_weight
    return Match(score=round(score, 6), coverage=observed_weight / total_weight,
                 vector_similarity=vector, common_dimensions=common)


def rejection_reason(report: GrindReport, policy: SafetyPolicy | None = None) -> str | None:
    """Reason string when the shared safety gate blocks a report, else None."""
    decision = safety_gate(report, policy)
    return None if decision.allowed else decision.reason


def store_candidates(html: str, reference: int) -> list[int]:
    """Only Steam's More Like This block; ignore unrelated navigation/DLC links."""
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for element in soup.select('#recommended_block [data-ds-appid], #recommended_block a[href]'):
        raw = element.get("data-ds-appid", "")
        if not raw:
            hit = re.search(r"(?:https?://store\.steampowered\.com)?/app/(\d+)(?:/|$)",
                            element.get("href", ""))
            raw = hit.group(1) if hit else ""
        if raw.isdigit() and int(raw) > 0 and int(raw) != reference:
            ids.append(int(raw))
    return list(dict.fromkeys(ids))


class Matcher:
    def __init__(self, pipeline: Pipeline, analyzer: Analyzer | None = None, config: MatcherConfig | None = None):
        self.pipeline = pipeline
        self.analyzer = analyzer or Analyzer()
        self.config = config or MatcherConfig()

    async def _store_candidates(self, appid: int) -> tuple[list[int], SourceStatus]:
        url = f"https://store.steampowered.com/app/{appid}/"
        try:
            async with asyncio.timeout(self.pipeline.config.source_timeout):
                html = await self.pipeline.transport.get(url, {"cc": self.pipeline.config.country, "l": "english"})
            ids = store_candidates(html, appid)
            return ids, SourceStatus(source="steam_store_similar", url=url, status="ok" if ids else "partial",
                                     count=len(ids), detail="" if ids else "More Like This block missing/empty")
        except (SourceError, TimeoutError, ValueError, TypeError) as exc:
            return [], SourceStatus(source="steam_store_similar", url=url, status="unavailable",
                                    detail=type(exc).__name__)

    _SEARCH_KEYWORDS = (
        "incremental", "cozy", "relaxing", "dismantle", "automation", "roguelike",
        "turn-based", "clicker", "idler", "physics", "sandbox", "puzzle", "survivors",
    )

    async def _store_search_candidates(self, reference: Game) -> tuple[list[int], SourceStatus]:
        url = "https://store.steampowered.com/api/storesearch/"
        corpus = f"{reference.name} {reference.description}".lower()
        terms = [kw for kw in self._SEARCH_KEYWORDS if kw in corpus][:2]
        if not terms and reference.name:
            words = [w for w in re.findall(r"[a-zA-Z0-9]{4,}", reference.name)]
            if words:
                terms = [words[0].lower()]
        if not terms:
            return [], SourceStatus(source="steam_store_search", url=url, status="partial",
                                    count=0, detail="No search keywords extracted")
        ids: list[int] = []
        try:
            async with asyncio.timeout(self.pipeline.config.source_timeout):
                for term in terms:
                    data = await self.pipeline.transport.json(
                        url, {"term": term, "l": "english", "cc": self.pipeline.config.country}
                    )
                    for item in data.get("items", []):
                        cid = item.get("id")
                        if isinstance(cid, int) and cid > 0 and cid != reference.appid:
                            ids.append(cid)
            unique = list(dict.fromkeys(ids))
            return unique, SourceStatus(
                source="steam_store_search", url=url, status="ok" if unique else "partial",
                count=len(unique), detail=f"terms={terms}" if unique else f"No hits for {terms}",
            )
        except (SourceError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            return [], SourceStatus(source="steam_store_search", url=url, status="unavailable",
                                    detail=type(exc).__name__)

    async def recommend(self, appid: int) -> RecommendationReport:
        reference = await self.pipeline.collect(appid, discover=True)
        sources = list(reference.sources)
        if not reference.name.strip():
            raw = list(dict.fromkeys(i for i in reference.candidate_ids if i > 0 and i != appid))
            reason = "Steam Store не подтвердил название эталона; кандидат не проверялся"
            return RecommendationReport(
                reference=appid,
                raw_candidates=raw,
                ranked=[],
                excluded={str(i): reason for i in raw},
                sources=sources,
                warnings=[reason],
            )
        ids = list(reference.candidate_ids)
        if len(set(ids) - {appid}) < self.config.max_candidates:
            extra, status = await self._store_candidates(appid)
            ids.extend(extra)
            sources.append(status)
        if not (set(ids) - {appid}) and hasattr(self.pipeline.transport, "json"):
            search_ids, status = await self._store_search_candidates(reference)
            ids.extend(search_ids)
            sources.append(status)
        raw = list(dict.fromkeys(i for i in ids if i > 0 and i != appid))
        excluded = {str(i): f"BLACKLIST: {self.config.blacklist[i]}" for i in raw if i in self.config.blacklist}
        eligible = [i for i in raw if i not in self.config.blacklist]
        selected = eligible[:self.config.max_candidates]
        excluded.update({str(i): "Не проверен: лимит кандидатов" for i in eligible[self.config.max_candidates:]})
        warnings = ["GTI и сходство — эвристики, не гарантия безопасности или вероятности понравиться.",
                    "Тактильность оценена по текстовым признакам, не по измерениям управления."]
        reference_loop = extract_loop(reference, self.config.profiles.get(appid))
        possible_coverage = sum(self.config.weights[a] for a in AXES if a in reference_loop.features
                                and reference_loop.features[a].confidence > 0) / sum(self.config.weights.values())
        if possible_coverage < self.config.min_coverage:
            warnings.append("Недостаточно признаков эталона: добавьте профиль пяти осей через --profiles.")
            excluded.update({str(i): "Недостаточно данных геймплейного цикла эталона" for i in selected})
            return RecommendationReport(reference=appid, raw_candidates=raw, ranked=[], excluded=excluded,
                                        sources=sources, warnings=warnings)

        async def validate(candidate_id: int) -> tuple[RankedGame | None, str | None]:
            try:
                async with asyncio.timeout(self.config.candidate_timeout):
                    game = await self.pipeline.collect(candidate_id)
                    report = await asyncio.to_thread(self.analyzer.analyze, game)
                    reason = rejection_reason(report)
                    if reason:
                        return None, reason
                    if not game.name:
                        return None, "Steam Store не подтвердил базовую игру и её название"
                    loop = extract_loop(game, self.config.profiles.get(candidate_id))
                    match = match_loops(reference_loop, loop, self.config.weights)
                    if match.coverage < self.config.min_coverage:
                        return None, f"Недостаточно данных геймплейного цикла: покрытие {match.coverage:.0%}"
                    axes = sorted(match.common_dimensions, key=lambda a: (
                        abs(reference_loop.features[a].value - loop.features[a].value), a))
                    reasons = [f"{LABELS[a]}: совпадение {1 - abs(reference_loop.features[a].value - loop.features[a].value):.0%}"
                               for a in axes[:3]]
                    notes = list(report.warnings)
                    notes.append(f"Покрытие осей: {match.coverage:.0%}; {loop.origin}")
                    if report.verdict == "CAUTION":
                        notes.insert(0, "CAUTION: жалобы на гринд; таймгейты/P2W не обнаружены в выборке")
                    return RankedGame(appid=game.appid, name=game.name, match=match, grind=report,
                                      reasons=reasons, warnings=notes), None
            except (SourceError, TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
                return None, f"Ошибка проверки: {type(exc).__name__}"

        ranked = []
        # Bounded batches prevent a task explosion; Pipeline/Transport also limit concurrency.
        for start in range(0, len(selected), self.config.concurrency):
            batch = selected[start:start + self.config.concurrency]
            results = await asyncio.gather(*(validate(i) for i in batch))
            for candidate_id, (item, reason) in zip(batch, results):
                if item:
                    ranked.append(item)
                else:
                    excluded[str(candidate_id)] = reason or "Проверка не завершена"
        ranked.sort(key=lambda item: (-item.match.score, item.grind.gti, item.appid))
        if not ranked:
            warnings.append("Нет кандидатов, прошедших проверку безопасности и покрытия осей.")
        selected_ranked = ranked[:self.config.top_k]
        for item in ranked[self.config.top_k:]:
            excluded[str(item.appid)] = (
                f"Проверен, но не вошёл в топ-{self.config.top_k}: "
                f"сходство {item.match.score:.1%}, GTI {item.grind.gti}")
        return RecommendationReport(reference=appid, raw_candidates=raw, ranked=selected_ranked,
                                    excluded=excluded, sources=sources, warnings=warnings)
