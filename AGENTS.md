# AGENTS.md — Guide for AI Agents & Contributors

This repository is licensed under the **MIT License** ([LICENSE](./LICENSE)).
AI agents and human contributors are explicitly permitted to use, fork, modify, extend, and redistribute this project freely.

## Overview

**Game Forensic Engine** analyzes Steam reviews (EN/RU) for grind/monetization red flags (GTI), estimates completion time, compares games across a 5-axis CoreLoop profile, and exposes an **MCP (Model Context Protocol)** stdio server.

## Quick Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run unit tests (offline, mocked Steam responses)
pytest -q

# Lint
ruff check .

# Run CLI
python -m game_forensic.cli 1091500
python -m game_forensic.cli recommend 1091500

# Run MCP server (stdio)
python -m game_forensic.mcp_server
# or via entrypoint:
game-forensic-mcp
```

## Architecture

- `game_forensic/models.py` — Strict Pydantic v2 data models (`extra="forbid"`).
- `game_forensic/network.py` — Bounded async `httpx` `Transport` with per-host rate limiting, `Retry-After` handling, proxy rotation, and atomic disk cache (`.cache/game-forensic`).
- `game_forensic/parser.py` — Steam Store `appdetails`, Steam reviews, SteamPeek, and SteamDB HTML/JSON parsers.
- `game_forensic/pipeline.py` — Async data collection (`IUserReviewsService` with legacy `/appreviews/` fallback).
- `game_forensic/analyzer.py` — Explainable EN/RU GTI heuristic (`repetition`, `time_gating`, `rng`, `pay_to_skip`, `slow_progression`) + early-churn cluster stats.
- `game_forensic/matcher.py` — 5-axis CoreLoop extraction (`description`, `reviews`, and Steam Store `categories` fallback) + weighted similarity matching (`match_loops`) + `Matcher.recommend`.
- `game_forensic/mcp_server.py` — FastMCP / `MCPServer` stdio server exposing 7 tools:
  - `steam_analyze_game`
  - `steam_match_backlog`
  - `steam_batch_filter`
  - `steam_recommend`
  - `steam_search_games`
  - `steam_blacklist_add`
  - `steam_profile_set`
- `game_forensic/cli.py` — CLI entrypoints (`analyze`, `recommend`).

## Persistent User Data

The MCP server reads/writes `blacklist.json` and `profiles.json` in `GAME_FORENSIC_DATA_DIR` (defaults to `~/.game-forensic`).

## Design Invariants

- Do not invent neutral (`0.5`) values for unknown CoreLoop axes; unknown axes reduce `coverage` and `score`.
- Keep tests in `tests/` offline and deterministic (mock `Transport` / `Pipeline`).
- Run `pytest -q` and `ruff check .` before committing.
