"""Analyze APPID (legacy syntax supported), or recommend APPID."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap
from pathlib import Path

from .analyzer import Analyzer, AnalyzerConfig
from .matcher import Matcher, MatcherConfig
from .models import Assessment, RecommendationReport
from .network import NetworkConfig, SourceError, Transport
from .pipeline import Pipeline, PipelineConfig


def positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return result


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Steam forensic analysis and gameplay recommendations.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("analyze", "recommend"):
        parser = commands.add_parser(name)
        add_common_arguments(parser)
        if name == "recommend":
            parser.add_argument("--max-candidates", type=positive_int, default=20)
            parser.add_argument("--concurrency", type=positive_int, default=3)
            parser.add_argument("--blacklist", type=Path, help="JSON object: AppID -> exclusion reason")
            parser.add_argument("--profiles", type=Path, help="JSON object: AppID -> axis values [0, 1]")
    return root


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("appid", type=positive_int)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-o", "--output", type=Path, help="Write report to a UTF-8 file")
    parser.add_argument("--review-pages", type=int, choices=range(1, 31), default=4)
    parser.add_argument("--languages", nargs="+", choices=("english", "russian"),
                        default=["english", "russian"])
    parser.add_argument("--min-reviews", type=positive_int, default=20)
    parser.add_argument("--timeout", type=positive_int, default=20, help="HTTP timeout in seconds")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/game-forensic"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--proxy", action="append", default=[], help="Fallback proxy URL; repeatable")
    parser.add_argument("--steamdb", action="store_true", help="Enable optional SteamDB source")
    parser.add_argument("--discover", action="store_true", help="Collect SteamPeek candidate appids")


async def run_analysis(args: argparse.Namespace) -> Assessment:
    network = NetworkConfig(proxies=args.proxy, timeout=args.timeout, cache_dir=args.cache_dir,
                            cache_ttl=0 if args.no_cache else 3600)
    config = PipelineConfig(review_pages=args.review_pages, languages=args.languages,
                            enable_steamdb=args.steamdb)
    async with Transport(network) as transport:
        game = await Pipeline(transport, config).collect(args.appid, discover=args.discover)
    return Analyzer(AnalyzerConfig(min_reviews=args.min_reviews)).assess(game)


async def run_recommendation(args: argparse.Namespace) -> RecommendationReport:
    options = {"max_candidates": args.max_candidates, "concurrency": args.concurrency}
    if args.blacklist:
        overrides = json.loads(args.blacklist.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            raise ValueError("Blacklist must be a JSON object")
        options["blacklist"] = {**MatcherConfig().blacklist, **{int(k): v for k, v in overrides.items()}}
    if args.profiles:
        options["profiles"] = json.loads(args.profiles.read_text(encoding="utf-8"))
    matcher_config = MatcherConfig(**options)
    network = NetworkConfig(proxies=args.proxy, timeout=args.timeout, cache_dir=args.cache_dir,
                            cache_ttl=0 if args.no_cache else 3600)
    config = PipelineConfig(review_pages=args.review_pages, languages=args.languages,
                            enable_steamdb=args.steamdb, game_concurrency=args.concurrency)
    async with Transport(network) as transport:
        return await Matcher(Pipeline(transport, config),
                             Analyzer(AnalyzerConfig(min_reviews=args.min_reviews)),
                             matcher_config).recommend(args.appid)


def render_recommendations(report: RecommendationReport) -> str:
    headers = ["Название", "Сходство %", "GTI балл", "Почему подходит", "Предупреждения"]
    widths = [26, 10, 10, 37, 43]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [f"ТОП-{len(report.ranked)} рекомендаций для AppID {report.reference}", border]

    def row(cells: list[str]) -> None:
        wrapped = [textwrap.wrap(" ".join(cell.split()), width=width) or [""]
                   for cell, width in zip(cells, widths)]
        for n in range(max(map(len, wrapped))):
            lines.append("| " + " | ".join((parts[n] if n < len(parts) else "").ljust(width)
                                            for parts, width in zip(wrapped, widths)) + " |")
        lines.append(border)

    row(headers)
    for item in report.ranked:
        row([f"{item.name} ({item.appid})", f"{item.match.score:.1%}",
             "N/A" if item.grind.gti is None else f"{item.grind.gti:.2f}/100",
             "; ".join(item.reasons), "; ".join(item.warnings)])
    if not report.ranked:
        lines.append("Нет рекомендаций: см. причины исключения и предупреждения ниже.")
    lines.extend(f"Исключён {appid}: {reason}" for appid, reason in report.excluded.items())
    lines.extend(f"Источник {source.source}: {source.status}; {source.detail}" for source in report.sources
                 if source.source in {"steampeek", "steam_store_similar"})
    lines.extend(f"Предупреждение: {warning}" for warning in report.warnings)
    return "\n".join(lines)


def render_text(assessment: Assessment) -> str:
    game, report = assessment.game, assessment.grind
    score = "N/A" if report.gti is None else f"{report.gti:.2f}/100"
    lines = [f"{game.name or 'Unknown game'} (appid={game.appid})",
             f"GTI: {score} | {report.verdict}",
             f"Evidence confidence: {report.confidence:.0%} (heuristic)",
             f"Usable reviews: {report.sample_size}; grind complaints: {report.complaint_count}",
             "Categories (fraction of usable reviews):"]
    lines.extend(f"  {category}: {rate:.1%}" for category, rate in report.categories.items())
    lines.append("Early-review cluster: " + json.dumps(report.early_cluster, ensure_ascii=False))
    for source in game.sources:
        lines.append(f"Source {source.source}: {source.status} ({source.count}) {source.detail}")
    for evidence in report.evidence:
        lines.append(f"[{evidence.review_id}/{evidence.category}] {evidence.excerpt}")
    lines.extend(f"Warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Preserve `game-forensic APPID [options]` without mixing positional grammars.
    if argv and argv[0] not in {"analyze", "recommend", "-h", "--help"}:
        argv.insert(0, "analyze")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "recommend":
            report = asyncio.run(run_recommendation(args))
            text = report.model_dump_json(indent=2) if args.format == "json" else render_recommendations(report)
            exit_code = 0 if report.ranked else 2
        else:
            assessment = asyncio.run(run_analysis(args))
            text = assessment.model_dump_json(indent=2) if args.format == "json" else render_text(assessment)
            exit_code = 2 if assessment.grind.verdict == "INSUFFICIENT_DATA" else 0
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return exit_code
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, SourceError) as exc:
        # Third-party error messages may contain proxy credentials.
        print(f"Analysis failed ({type(exc).__name__}); check configuration and output path.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
