"""Steam Forensic Engine MCP Server (Model Context Protocol).

Provides forensic game analysis, anti-grind GTI evaluation, CoreLoop backlog matching,
Steam discovery search, and persistent blacklist/profile management.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer

from .analyzer import SUPPORTED_LANGUAGES, Analyzer, AnalyzerConfig
from .matcher import (
    AXES,
    DEFAULT_BLACKLIST,
    Matcher,
    MatcherConfig,
    estimate_completion_hours,
    extract_loop,
    match_loops,
)
from .models import GrindReport, Match
from .network import NetworkConfig, SourceError, Transport
from .pipeline import Pipeline, PipelineConfig
from .policy import SafetyPolicy, safety_gate
from .storage import (
    data_dir,
    dump_json,
    load_user_data,
    remove_blacklist,
    set_blacklist,
    set_profile,
)

mcp = MCPServer("steam-game-forensic")

_NON_GAME_SUFFIXES = (" soundtrack", " ost", " demo", " artbook", " dlc")
_MAX_BATCH_GAMES = 8  # `steam_batch_filter` evaluates at most this many games per call.

# Synergy ladder for `steam_match_backlog`. The GTI ceilings and coverage floor are
# derived from the shared config so they cannot drift from the engine defaults again.
_DEFAULT_MATCHER = MatcherConfig()
MIN_SYNERGY_GTI = 35.0  # GTI ceiling for a high-synergy verdict.
MODERATE_SYNERGY_GTI = 40.0  # GTI ceiling for the ladder's loosest step.
MODERATE_SYNERGY_PCT = 50.0
PARTIAL_VECTOR_SIMILARITY = 0.8
HIGH_SYNERGY_PCT = 70.0


def synergy_verdict(
    match: Match,
    report: GrindReport,
    blocked: bool = False,
    min_coverage: float | None = None,
) -> str:
    """Synergy label for a candidate, never above the shared safety gate's allowance."""
    gate = safety_gate(report, SafetyPolicy(max_gti=MODERATE_SYNERGY_GTI))
    if blocked:
        return "BLACKLISTED"
    if not gate.allowed:
        return "INSUFFICIENT_DATA" if gate.code == "INSUFFICIENT_DATA" else "DIVERGENT_OR_HIGH_GRIND"
    floor = _DEFAULT_MATCHER.min_coverage if min_coverage is None else min_coverage
    if match.coverage < floor:
        return "INSUFFICIENT_DATA"
    gti = report.gti if report.gti is not None else 100.0
    if match.score * 100 >= HIGH_SYNERGY_PCT and gti < MIN_SYNERGY_GTI:
        return "HIGH_SYNERGY_RECOMMENDED"
    if match.vector_similarity >= PARTIAL_VECTOR_SIMILARITY and gti < MIN_SYNERGY_GTI:
        return "PARTIAL_MATCH_HIGH_SYNERGY"
    if match.score * 100 >= MODERATE_SYNERGY_PCT and gti < MODERATE_SYNERGY_GTI:
        return "MODERATE_SYNERGY"
    return "DIVERGENT_OR_HIGH_GRIND"


def user_state() -> tuple[dict[int, str], dict[int, dict[str, float]], list[str]]:
    """Validated blacklist, profiles, and load warnings for the current data dir."""
    state = load_user_data(AXES, DEFAULT_BLACKLIST)
    return state.blacklist, state.profiles, state.warnings


async def _resolve_appid(transport: Transport, target: str | int) -> tuple[int, str]:
    """Resolve an AppID from an integer, string id, or game title search."""
    if isinstance(target, int) or (
        isinstance(target, str) and target.strip().lstrip("-").isdigit()
    ):
        appid = int(target)
        if appid <= 0:
            raise ValueError("Steam AppID must be positive")
        return appid, f"AppID {appid}"

    title = str(target).strip()
    url = "https://store.steampowered.com/api/storesearch/"
    try:
        data = await transport.json(url, {"term": title, "l": "english", "cc": "us"})
        items = [it for it in data.get("items", []) if "id" in it]
        if items:
            # Prefer base apps over soundtracks/demos when searching by title.
            preferred = next(
                (
                    it
                    for it in items
                    if not str(it.get("name", "")).lower().endswith(_NON_GAME_SUFFIXES)
                ),
                items[0],
            )
            return int(preferred["id"]), preferred.get("name", title)
    except (SourceError, ValueError, TypeError, KeyError, OSError) as exc:
        raise ValueError(f"Could not resolve Steam AppID for game: '{target}'") from exc

    raise ValueError(f"Could not resolve Steam AppID for game: '{target}'")


@mcp.tool()
async def steam_analyze_game(
    game: str | int,
    review_pages: int = 3,
    languages: list[str] | None = None,
) -> str:
    """Perform a deep forensic analysis on a Steam game.

    Evaluates:
    - Grind-to-Infinity (GTI) index (0-100)
    - Early churn clusters & estimated completion hours
    - Category breakdown (repetition, time_gating, rng, pay_to_skip, slow_progression)
    - 5-axis gameplay loop attributes
    - Forensic verdict (RED_FLAG_REJECT, CAUTION, NO_RED_FLAG_DETECTED, INSUFFICIENT_DATA)

    Args:
        game: Steam AppID (e.g. 1086940, 730) or Game Name (e.g. "Baldur's Gate 3", "Elden Ring").
        review_pages: Number of review pages to sample (1-10, default 3).
        languages: List of languages to analyze reviews in (default ['english', 'russian']).
            The lexical GTI heuristic supports only 'english' and 'russian'; other
            languages are fetched but cannot be classified, and are reported as ignored.
    """
    langs = languages or ["english", "russian"]
    analyzable = [lang for lang in langs if lang in SUPPORTED_LANGUAGES]
    disliked = [lang for lang in langs if lang not in SUPPORTED_LANGUAGES]
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(
        languages=langs,
        review_pages=min(max(review_pages, 1), 10),
        enable_steamdb=False,
        enable_steampeek=False,
    )
    analyzer = Analyzer(AnalyzerConfig())
    blacklist, profiles, load_warnings = user_state()

    async with Transport(net_config) as transport:
        appid, resolved_name = await _resolve_appid(transport, game)
        pipeline = Pipeline(transport, pipe_config)
        game_data = await pipeline.collect(appid, discover=False)

    grind = analyzer.analyze(game_data)
    loop = extract_loop(game_data, profiles.get(appid))
    est_hours = estimate_completion_hours(game_data)
    warnings = [*load_warnings, *grind.warnings]
    if disliked:
        warnings.insert(0, (
            f"Языки {disliked} загружены, но лексический анализ поддерживает только "
            f"{sorted(SUPPORTED_LANGUAGES)}; они не влияют на GTI."))
    if not analyzable:
        warnings.insert(0, "Ни один запрошенный язык не поддерживается анализом; GTI недостоверен.")
    if appid in blacklist:
        warnings.insert(0, f"Игра в блэклисте: {blacklist[appid]}")

    result = {
        "appid": appid,
        "title": game_data.name or resolved_name,
        "verdict": grind.verdict,
        "gti_score": grind.gti,
        "confidence": grind.confidence,
        "sample_size": grind.sample_size,
        "complaint_count": grind.complaint_count,
        "estimated_completion_hours": est_hours,
        "categories": grind.categories,
        "early_cluster": grind.early_cluster,
        "five_axis_profile": {
            axis: {
                "score": round(feat.value, 3),
                "confidence": round(feat.confidence, 3),
                "evidence_samples": feat.evidence[:3],
            }
            for axis, feat in loop.features.items()
        },
        "top_evidence_excerpts": [
            {
                "category": e.category,
                "excerpt": e.excerpt,
                "strength": e.strength,
            }
            for e in grind.evidence[:5]
        ],
        "warnings": warnings,
    }
    return dump_json(result)


@mcp.tool()
async def steam_match_backlog(
    game_a: str | int,
    game_b: str | int,
) -> str:
    """Compare two Steam games using the 5-axis CoreLoop engine.

    Calculates similarity and structural alignment between gameplay loops
    (combat pace, build depth, tactile feedback, punishment severity, narrative weight).

    Args:
        game_a: Reference game you loved (AppID or Name), e.g. "Elden Ring".
        game_b: Candidate game from backlog (AppID or Name), e.g. "Lies of P".
    """
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(
        review_pages=2,
        languages=["english", "russian"],
        enable_steampeek=False,
    )
    analyzer = Analyzer(AnalyzerConfig())
    blacklist, profiles, load_warnings = user_state()

    async with Transport(net_config) as transport:
        id_a, name_a = await _resolve_appid(transport, game_a)
        id_b, name_b = await _resolve_appid(transport, game_b)
        pipeline = Pipeline(transport, pipe_config)
        game_data_a, game_data_b = await asyncio.gather(
            pipeline.collect(id_a, discover=False),
            pipeline.collect(id_b, discover=False),
        )

    grind_a = analyzer.analyze(game_data_a)
    grind_b = analyzer.analyze(game_data_b)
    loop_a = extract_loop(game_data_a, profiles.get(id_a))
    loop_b = extract_loop(game_data_b, profiles.get(id_b))
    match = match_loops(loop_a, loop_b)
    similarity_pct = round(match.score * 100, 2)
    hours_b = estimate_completion_hours(game_data_b)
    verified_a = bool(game_data_a.name.strip())
    verified_b = bool(game_data_b.name.strip())
    warnings = [*load_warnings]
    if not verified_a:
        warnings.append("Steam Store не подтвердил название эталона.")
    if not verified_b:
        warnings.append("Steam Store не подтвердил название кандидата.")
    if id_b in blacklist:
        warnings.insert(0, f"Кандидат в блэклисте: {blacklist[id_b]}")

    comparison = {
        "reference_game": {
            "appid": id_a,
            "name": game_data_a.name or name_a,
            "gti": grind_a.gti,
            "verdict": grind_a.verdict,
            "estimated_completion_hours": estimate_completion_hours(game_data_a),
            "store_verified": verified_a,
        },
        "candidate_game": {
            "appid": id_b,
            "name": game_data_b.name or name_b,
            "gti": grind_b.gti,
            "verdict": grind_b.verdict,
            "estimated_completion_hours": hours_b,
            "store_verified": verified_b,
        },
        "match_score_pct": similarity_pct,
        "coverage": round(match.coverage, 2),
        "vector_similarity": round(match.vector_similarity, 3),
        "common_dimensions": match.common_dimensions,
        "loop_a_scores": {k: round(v.value, 2) for k, v in loop_a.features.items()},
        "loop_b_scores": {k: round(v.value, 2) for k, v in loop_b.features.items()},
        "recommendation_verdict": (
            "UNVERIFIED_GAME" if not (verified_a and verified_b)
            else synergy_verdict(match, grind_b, blocked=id_b in blacklist)
        ),
        "safety_gate": safety_gate(grind_b, SafetyPolicy(max_gti=MODERATE_SYNERGY_GTI)).code,
        "warnings": warnings,
    }
    return dump_json(comparison)


@mcp.tool()
async def steam_batch_filter(
    games: list[str | int],
    max_grind: float = 35.0,
    max_completion_hours: float | None = None,
) -> str:
    """Filter a list of backlog games by grind threshold (GTI), blacklist, and optional length.

    Evaluates games in parallel and discards grind-traps, blacklisted games, and unverified titles.

    Args:
        games: List of AppIDs or game names (up to 8 games per call).
        max_grind: Maximum acceptable Grind-To-Infinity (GTI) score (0-100, default 35.0).
        max_completion_hours: Optional max estimated completion time in hours.
    """
    items = games[:_MAX_BATCH_GAMES]
    dropped = len(games) - len(items)
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(
        review_pages=1,
        languages=["english", "russian"],
        enable_steampeek=False,
    )
    analyzer = Analyzer(AnalyzerConfig())
    blacklist, _, load_warnings = user_state()
    policy = SafetyPolicy(max_gti=max_grind, max_completion_hours=max_completion_hours)

    async with Transport(net_config) as transport:
        pipeline = Pipeline(transport, pipe_config)

        async def _safe_resolve(g: str | int) -> tuple[int, str] | None:
            try:
                return await _resolve_appid(transport, g)
            except (SourceError, ValueError, TypeError, KeyError, OSError):
                return None

        resolved_raw = await asyncio.gather(*(_safe_resolve(g) for g in items))
        resolved = [r for r in resolved_raw if r is not None]
        unresolved = len(items) - len(resolved)

        async def _eval(aid: int, name: str) -> dict[str, Any]:
            if aid in blacklist:
                return {
                    "appid": aid,
                    "name": name,
                    "gti": None,
                    "verdict": "BLACKLISTED",
                    "passed": False,
                    "reason": f"Blacklisted: {blacklist[aid]}",
                }
            try:
                gdata = await pipeline.collect(aid, discover=False)
                if not gdata.name.strip():
                    return {
                        "appid": aid,
                        "name": name,
                        "gti": None,
                        "verdict": "UNVERIFIED_GAME",
                        "store_verified": False,
                        "passed": False,
                        "reason": "Steam Store не подтвердил базовую игру и её название",
                    }
                grind = await asyncio.to_thread(analyzer.analyze, gdata)
                est_hours = estimate_completion_hours(gdata)
                decision = safety_gate(grind, policy, est_hours)

                return {
                    "appid": aid,
                    "name": gdata.name,
                    "gti": grind.gti,
                    "verdict": grind.verdict,
                    "store_verified": True,
                    "sample_size": grind.sample_size,
                    "estimated_completion_hours": est_hours,
                    "passed": decision.allowed,
                    "reason": decision.reason,
                    "policy_code": decision.code,
                }
            except (SourceError, ValueError, TypeError, KeyError, OSError) as e:
                return {"appid": aid, "name": name, "error": str(e), "passed": False}

        results = await asyncio.gather(*(_eval(aid, name) for aid, name in resolved))

    approved = [r for r in results if r.get("passed")]
    rejected = [r for r in results if not r.get("passed")]

    warnings = list(load_warnings)
    if dropped:
        warnings.append(f"Обработаны первые {_MAX_BATCH_GAMES} игры; пропущено по лимиту: {dropped}.")
    if unresolved:
        warnings.append(f"Не удалось определить AppID у {unresolved} записей; они не оценены.")

    summary = {
        "max_grind_threshold": max_grind,
        "max_completion_hours": max_completion_hours,
        "total_requested": len(games),
        "total_evaluated": len(results),
        "skipped_over_limit": dropped,
        "unresolved_count": unresolved,
        "approved_count": len(approved),
        "approved_backlog": sorted(approved, key=lambda x: x.get("gti") or 0),
        "rejected_grind_traps": rejected,
        "warnings": warnings,
    }
    return dump_json(summary)


@mcp.tool()
async def steam_recommend(
    game: str | int,
    max_candidates: int = 10,
) -> str:
    """Recommend similar Steam games based on CoreLoop synergy and anti-grind filtering.

    Args:
        game: Reference Steam AppID or Game Name.
        max_candidates: Maximum number of candidate games to evaluate (1-20, default 10).
    """
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(review_pages=2, languages=["english", "russian"])
    blacklist, profiles, load_warnings = user_state()
    matcher_config = MatcherConfig(
        max_candidates=min(max(max_candidates, 1), 20),
        blacklist=blacklist,
        profiles=profiles,
    )

    async with Transport(net_config) as transport:
        appid, _ = await _resolve_appid(transport, game)
        pipeline = Pipeline(transport, pipe_config)
        matcher = Matcher(pipeline, Analyzer(AnalyzerConfig()), matcher_config)
        report = await matcher.recommend(appid)

    return dump_json(
        {
            "reference": report.reference,
            "raw_candidate_count": len(report.raw_candidates),
            "ranked": [
                {
                    "appid": item.appid,
                    "name": item.name,
                    "match_score_pct": round(item.match.score * 100, 2),
                    "coverage": round(item.match.coverage, 2),
                    "gti": item.grind.gti,
                    "verdict": item.grind.verdict,
                    "reasons": item.reasons,
                    "warnings": item.warnings[:3],
                }
                for item in report.ranked
            ],
            "excluded_count": len(report.excluded),
            "excluded": report.excluded,
            "warnings": [*load_warnings, *report.warnings],
        }
    )


@mcp.tool()
async def steam_search_games(
    query: str,
    limit: int = 10,
) -> str:
    """Search Steam Store for games matching a keyword or phrase.

    Useful as a discovery fallback when SteamPeek or More Like This has no candidates.

    Args:
        query: Search term (e.g. "Tidy Up", "Dismantle", "Incremental").
        limit: Maximum number of results to return (1-25, default 10).
    """
    net_config = NetworkConfig(timeout=20)
    url = "https://store.steampowered.com/api/storesearch/"
    async with Transport(net_config) as transport:
        data = await transport.json(url, {"term": query.strip(), "l": "english", "cc": "us"})

    items = []
    for raw in data.get("items", []):
        name = str(raw.get("name", ""))
        if not raw.get("id") or name.lower().endswith(_NON_GAME_SUFFIXES):
            continue
        price_info = raw.get("price") or {}
        items.append(
            {
                "appid": int(raw["id"]),
                "name": name,
                "price_final": price_info.get("final"),
                "currency": price_info.get("currency"),
            }
        )
        if len(items) >= min(max(limit, 1), 25):
            break

    return dump_json({"query": query, "count": len(items), "items": items})


@mcp.tool()
async def steam_blacklist_add(
    game: str | int,
    reason: str,
) -> str:
    """Add a game to the persistent user blacklist so it is excluded from recommendations.

    Args:
        game: Steam AppID or Game Name.
        reason: Short explanation of why the game was rejected (e.g. "No upgrades / boring").
    """
    if not reason.strip():
        raise ValueError("Reason must not be empty")

    net_config = NetworkConfig(timeout=20)
    async with Transport(net_config) as transport:
        appid, resolved_name = await _resolve_appid(transport, game)

    set_blacklist(data_dir(), appid, reason.strip())

    return dump_json({"status": "added", "appid": appid, "name": resolved_name,
                      "reason": reason.strip()})


@mcp.tool()
async def steam_blacklist_remove(game: str | int) -> str:
    """Remove a game from the user blacklist, including built-in seed entries.

    Args:
        game: Steam AppID or Game Name.
    """
    net_config = NetworkConfig(timeout=20)
    async with Transport(net_config) as transport:
        appid, resolved_name = await _resolve_appid(transport, game)

    still_listed = remove_blacklist(data_dir(), appid, DEFAULT_BLACKLIST)
    return dump_json({
        "status": "still_blacklisted" if still_listed else "removed",
        "appid": appid,
        "name": resolved_name,
    })


@mcp.tool()
async def steam_profile_set(
    game: str | int,
    axes: dict[str, float],
) -> str:
    """Save or update a curated 5-axis CoreLoop profile for a Steam game.

    Args:
        game: Steam AppID or Game Name.
        axes: Mapping of axis name (combat_pace, build_depth, tactile_feedback,
            punishment_severity, narrative_weight) to float score in [0.0, 1.0].
    """
    if not axes or not set(axes) <= set(AXES):
        raise ValueError(f"Axes must be a non-empty subset of {sorted(AXES)}")
    cleaned = {axis: float(value) for axis, value in axes.items()}
    if any(not 0.0 <= value <= 1.0 for value in cleaned.values()):
        raise ValueError("Axis values must be between 0.0 and 1.0")

    net_config = NetworkConfig(timeout=20)
    async with Transport(net_config) as transport:
        appid, resolved_name = await _resolve_appid(transport, game)

    merged = set_profile(data_dir(), appid, cleaned)

    return dump_json({"status": "saved", "appid": appid, "name": resolved_name, "axes": merged})


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
