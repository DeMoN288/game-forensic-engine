"""Steam Forensic Engine MCP Server (Model Context Protocol).

Provides forensic game analysis, anti-grind GTI evaluation, and CoreLoop backlog matching.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .analyzer import Analyzer, AnalyzerConfig
from .matcher import Matcher, MatcherConfig, extract_loop, match_loops
from .network import NetworkConfig, Transport
from .pipeline import Pipeline, PipelineConfig

mcp = MCPServer("steam-game-forensic")


async def _resolve_appid(transport: Transport, target: str | int) -> tuple[int, str]:
    """Resolve an AppID from an integer, string id, or game title search."""
    if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
        appid = int(target)
        return appid, f"AppID {appid}"

    title = str(target).strip()
    url = "https://store.steampowered.com/api/storesearch/"
    try:
        data = await transport.json(url, {"term": title, "l": "english", "cc": "us"})
        items = data.get("items", [])
        if items and "id" in items[0]:
            return int(items[0]["id"]), items[0].get("name", title)
    except Exception:
        pass

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
    - Early churn clusters & complaint velocity
    - Category breakdown (repetition, time_gating, rng, pay_to_skip, slow_progression)
    - 5-axis gameplay loop attributes
    - Forensic verdict (RED_FLAG_REJECT, CAUTION, NO_RED_FLAG_DETECTED, INSUFFICIENT_DATA)

    Args:
        game: Steam AppID (e.g. 1086940, 730) or Game Name (e.g. "Baldur's Gate 3", "Elden Ring").
        review_pages: Number of review pages to sample (1-10, default 3).
        languages: List of languages to analyze reviews in (default ['english', 'russian']).
    """
    langs = languages or ["english", "russian"]
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(
        languages=langs,
        review_pages=min(max(review_pages, 1), 10),
        enable_steamdb=False,
        enable_steampeek=False,
    )
    ana_config = AnalyzerConfig()
    matcher_config = MatcherConfig()

    transport = Transport(net_config)
    appid, resolved_name = await _resolve_appid(transport, game)
    pipeline = Pipeline(transport, pipe_config)
    analyzer = Analyzer(ana_config)
    matcher = Matcher(matcher_config)

    game_data = await pipeline.collect(appid, discover=False)
    grind = analyzer.analyze(game_data)
    loop = extract_loop(game_data)

    result = {
        "appid": appid,
        "title": game_data.name or resolved_name,
        "verdict": grind.verdict,
        "gti_score": grind.gti,
        "confidence": grind.confidence,
        "sample_size": grind.sample_size,
        "complaint_count": grind.complaint_count,
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
        "warnings": grind.warnings,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def steam_match_backlog(
    game_a: str | int,
    game_b: str | int,
) -> str:
    """Compare two Steam games using the 5-axis CoreLoop engine.

    Calculates cosine vector similarity and structural alignment between gameplay loops
    (combat pace, build depth, tactile feedback, punishment severity, narrative weight).

    Args:
        game_a: Reference game you loved (AppID or Name), e.g. "Elden Ring".
        game_b: Candidate game from backlog (AppID or Name), e.g. "Lies of P".
    """
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(review_pages=2, languages=["english", "russian"])
    ana_config = AnalyzerConfig()
    matcher_config = MatcherConfig()

    transport = Transport(net_config)
    id_a, name_a = await _resolve_appid(transport, game_a)
    id_b, name_b = await _resolve_appid(transport, game_b)

    pipeline = Pipeline(transport, pipe_config)
    analyzer = Analyzer(ana_config)
    matcher = Matcher(matcher_config)

    game_data_a, game_data_b = await asyncio.gather(
        pipeline.collect(id_a, discover=False),
        pipeline.collect(id_b, discover=False),
    )

    grind_a = analyzer.analyze(game_data_a)
    grind_b = analyzer.analyze(game_data_b)

    loop_a = extract_loop(game_data_a)
    loop_b = extract_loop(game_data_b)

    match = match_loops(loop_a, loop_b)
    similarity_pct = round(match.score * 100, 2)

    comparison = {
        "reference_game": {
            "appid": id_a,
            "name": game_data_a.name or name_a,
            "gti": grind_a.gti,
            "verdict": grind_a.verdict,
        },
        "candidate_game": {
            "appid": id_b,
            "name": game_data_b.name or name_b,
            "gti": grind_b.gti,
            "verdict": grind_b.verdict,
        },
        "match_score_pct": similarity_pct,
        "coverage": round(match.coverage, 2),
        "vector_similarity": round(match.vector_similarity, 3),
        "common_dimensions": match.common_dimensions,
        "loop_a_scores": {k: round(v.value, 2) for k, v in loop_a.features.items()},
        "loop_b_scores": {k: round(v.value, 2) for k, v in loop_b.features.items()},
        "recommendation_verdict": (
            "HIGH_SYNERGY_RECOMMENDED" if similarity_pct >= 70 and (grind_b.gti or 0) < 35 else
            "MODERATE_SYNERGY" if similarity_pct >= 50 and (grind_b.gti or 0) < 40 else
            "DIVERGENT_OR_HIGH_GRIND"
        ),
    }
    return json.dumps(comparison, indent=2, ensure_ascii=False)


@mcp.tool()
async def steam_batch_filter(
    games: list[str | int],
    max_grind: float = 35.0,
) -> str:
    """Filter a list of backlog games by maximum allowable grind threshold (GTI).

    Evaluates games in parallel and discards grind-traps, returning games safe to start.

    Args:
        games: List of AppIDs or game names (up to 8 games per call).
        max_grind: Maximum acceptable Grind-To-Infinity (GTI) score (0-100, default 35.0).
    """
    items = games[:8]
    net_config = NetworkConfig(timeout=30)
    pipe_config = PipelineConfig(review_pages=1, languages=["english", "russian"])
    ana_config = AnalyzerConfig()

    transport = Transport(net_config)
    pipeline = Pipeline(transport, pipe_config)
    analyzer = Analyzer(ana_config)

    resolved = []
    for g in items:
        try:
            aid, name = await _resolve_appid(transport, g)
            resolved.append((aid, name))
        except Exception:
            continue

    async def _eval(aid: int, name: str) -> dict[str, Any]:
        try:
            gdata = await pipeline.collect(aid, discover=False)
            grind = analyzer.analyze(gdata)
            gti_val = grind.gti if grind.gti is not None else 0.0
            passed = gti_val <= max_grind and grind.verdict != "RED_FLAG_REJECT"
            return {
                "appid": aid,
                "name": gdata.name or name,
                "gti": grind.gti,
                "verdict": grind.verdict,
                "sample_size": grind.sample_size,
                "passed": passed,
                "reason": (
                    "Clean progression pacing" if passed else
                    f"GTI {gti_val} exceeds limit {max_grind} or rejected ({grind.verdict})"
                ),
            }
        except Exception as e:
            return {"appid": aid, "name": name, "error": str(e), "passed": False}

    results = await asyncio.gather(*(_eval(aid, name) for aid, name in resolved))

    approved = [r for r in results if r.get("passed")]
    rejected = [r for r in results if not r.get("passed")]

    summary = {
        "max_grind_threshold": max_grind,
        "total_evaluated": len(results),
        "approved_count": len(approved),
        "approved_backlog": sorted(approved, key=lambda x: x.get("gti") or 0),
        "rejected_grind_traps": rejected,
    }
    return json.dumps(summary, indent=2, ensure_ascii=False)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
