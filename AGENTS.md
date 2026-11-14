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
- `game_forensic/policy.py` — **Single safety gate** (`safety_gate` / `SafetyPolicy`). Every consumer (CLI, `Matcher`, all MCP tools) routes its decision through it; do not reimplement verdict rules elsewhere.
- `game_forensic/storage.py` — Validated `blacklist.json` / `profiles.json` access. Hand-edited entries degrade to warnings instead of raising, and every ignored entry is reported. Mutations are serialized in-process, use atomic replacement, and refuse to overwrite unreadable/non-object JSON.
- `game_forensic/matcher.py` — 5-axis CoreLoop extraction (`description`, `reviews`, and Steam Store `categories` fallback) + weighted similarity matching (`match_loops`) + `Matcher.recommend`.
- `game_forensic/mcp_server.py` — FastMCP / `MCPServer` stdio server exposing 8 tools:
  - `steam_analyze_game`
  - `steam_match_backlog`
  - `steam_batch_filter`
  - `steam_recommend`
  - `steam_search_games`
  - `steam_blacklist_add`
  - `steam_blacklist_remove`
  - `steam_profile_set`
- `game_forensic/cli.py` — CLI entrypoints (`analyze`, `recommend`).

## Persistent User Data

The MCP server reads/writes `blacklist.json` and `profiles.json` in `GAME_FORENSIC_DATA_DIR` (defaults to `~/.game-forensic`).

- Malformed entries (typos, unknown axes, out-of-range values, non-string reasons) are skipped with a warning in the tool output — they never crash a read-only tool. CLI prints these warnings to `stderr`.
- Unreadable or non-object JSON produces a warning on read, but mutations raise an error and preserve the original file. Writes use same-directory atomic replacement; in-process read-modify-write operations share an `RLock`.
- A blacklist value of `null` cancels an entry, including built-in seeds such as Lost Ark. Use `steam_blacklist_remove` or `--blacklist` with `null`.

## Design Invariants

- Do not invent neutral (`0.5`) values for unknown CoreLoop axes; unknown axes reduce `coverage` and `score`.
- Judge report safety **only** through `policy.safety_gate`. Never add a second verdict/threshold check inside a tool.
- Report every dropped candidate: anything in `raw_candidates` must appear in `ranked` or in `excluded` with a reason.
- Never recommend a game whose title was not confirmed by Steam Store. With an active completion-time limit, unknown length is rejected as `LENGTH_UNKNOWN`; without a limit it remains allowed.
- Keep tests in `tests/` offline and deterministic (mock `Transport` / `Pipeline`).
- Run `pytest -q` and `ruff check .` before committing.
