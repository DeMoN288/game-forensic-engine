# Reliability Hardening — Quick Spec

**Spec Type:** Quick Spec
**Created:** 2026-11-14
**Status:** Complete

## Overview

Устранить найденные при анализе расхождения в предупреждениях, проверке игр, фильтре длительности и хранении пользовательских JSON, не меняя архитектуру и алгоритмы GTI/CoreLoop.

**Success Metric:** новые регрессионные тесты воспроизводят дефекты на исходном коде, после исправлений весь offline-набор тестов и Ruff проходят, CLI и MCP запускаются.

## Requirements

### R1 — Видимые ошибки пользовательской конфигурации

- WHEN CLI получает `--blacklist` с пропущенными некорректными записями THEN каждая проблема SHALL быть выведена в `stderr`, а JSON stdout SHALL остаться валидным.
- WHEN загрузчик пропускает запись blacklist или profile THEN предупреждение SHALL сохраняться в существующем результате/потоке предупреждений.

### R2 — Подтверждённая базовая игра

- WHEN Steam Store не подтвердил название базовой игры THEN Matcher SHALL NOT выдавать рекомендации на основе только отзывов.
- WHEN кандидат в `steam_batch_filter` не подтверждён Store THEN он SHALL быть отклонён с явной причиной даже при достаточном числе отзывов.
- WHEN эталон или кандидат в `steam_match_backlog` не подтверждён Store THEN вердикт SHALL быть `UNVERIFIED_GAME`, а не рекомендация.
- WHEN numeric AppID неположителен THEN MCP SHALL отклонить ввод до сетевой или файловой операции.

### R3 — Строгий фильтр длительности

- WHEN задан `max_completion_hours` AND оценка длительности отсутствует THEN safety gate SHALL отклонить игру с кодом `LENGTH_UNKNOWN`.
- WHEN лимит длительности не задан THEN неизвестная длительность SHALL NOT влиять на решение.
- Существующие правила GTI, verdict и gated categories SHALL остаться без изменений.

### R4 — Безопасное хранение

- WHEN blacklist/profiles записываются THEN замена файла SHALL быть атомарной через временный файл в том же каталоге.
- WHEN существующий JSON не читается или не является объектом THEN mutating operation SHALL завершиться ошибкой и SHALL NOT изменить исходный файл.
- WHEN in-process mutations выполняются параллельно THEN read-modify-write SHALL быть сериализован, чтобы не терять независимые обновления.
- Существующие tolerant read semantics и предупреждения для read-only инструментов SHALL остаться без изменений.

### R5 — Проверяемость и регрессии

- Добавить offline-тесты для R1–R4 и сетевых граничных случаев: pagination, retry/Retry-After, повреждённый кэш и превышение размера ответа.
- Все тесты SHALL быть детерминированными и не обращаться к реальному Steam.
- README и AGENTS.md SHALL описывать итоговое поведение.

## Constraints

- Python >=3.11, существующие зависимости, минимальные точечные изменения.
- Не менять формулу GTI, CoreLoop axes, пороговые значения и публичные команды без необходимости.
- Сохранить все текущие незакоммиченные изменения пользователя.

## Definition of Done

- Новые тесты сначала падают на исходной реализации по ожидаемым причинам.
- `pytest -q` и `ruff check .` проходят в `.venv`.
- CLI help и offline MCP stdio handshake/tool listing выполняются успешно.
- Diff проверен, спека обновлена, создан один Conventional Commit.

## Verification Evidence

- Baseline: 76 passed; Ruff passed.
- Red: 14 ожидаемых failures по R1–R4; network edge tests прошли без изменения implementation.
- Дополнительный red: invalid UTF-8 не сообщал имя повреждённого файла; после исправления reader тест проходит.
- Final: 98 passed in 1.56s; `ruff check .` — All checks passed.
- CLI smoke: `python -m game_forensic.cli --help` успешно перечисляет `analyze` и `recommend`.
- MCP smoke: stdio initialize/list_tools успешно вернул `steam-game-forensic`, protocol `2025-11-25`, все 8 инструментов.
