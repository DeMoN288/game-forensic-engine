# Quality Refactor — Tasks

## Implementation Overview

Работа шла test-first в текущем рабочем дереве. Baseline — `14b7f6c`, состояние до рефакторинга: 124 теста, `ruff check .` с дефолтным набором правил. Все изменения разбиты на логичные Conventional Commits.

## Progress

- Baseline: `124 passed`, ruff (дефолтный `E4/E7/E9/F`) — чисто.
- Red R1/R2: `14 failed, 17 passed` в `test_completion.py` + `test_network.py`.
- Green R1/R2: оба набора зелёные, полный прогон `124 passed`.
- R3: кросс-импорты удалены (`grep` → 0 совпадений), `124 passed`.
- Доказательство сохранения силы тестов: с `git checkout 14b7f6c -- game_forensic/matcher.py` падают ровно 8 ожидаемых completion-тестов, остальные 116 проходят.
- R4: расширенный `select` дал 28 находок, все устранены; regex-строки проверены на побайтовую идентичность (`CUES regex strings identical: True`).
- R5: декомпозированы четыре функции; равенство поведения доказано дифференциальным прогоном против модулей из `HEAD` (7 сценариев `Matcher.recommend`, 13 — `Pipeline._reviews`), все — identical.
- R6: workflow добавлен; 124 теста проходят и на 3.11, и на 3.12.
- Текущий этап: завершено.

## Implementation Plan

- [x] 1. Зафиксировать дефект R1 падающими тестами (`tests/test_completion.py`).
  - _Requirements: R1_
  - Evidence: 14 failed, 17 passed до правки.
- [x] 2. Исправить completion cue и отрицание прохождения.
  - _Requirements: R1_
  - Evidence: ветка `100%` вынесена из `\b(?:…)\b`; добавлены `COMPLETION_NEGATION` и `_confirms_completion`; медиана и пороги не тронуты.
- [x] 3. Зафиксировать дефект R2 тестами на non-object кэш и non-string body.
  - _Requirements: R2_
  - Evidence: `TypeError` воспроизведён offline.
- [x] 4. Вынести чтение кэша в `_read_cache` о строгими проверками типов.
  - _Requirements: R2_
- [x] 5. Перенести общие фикстуры в `tests/conftest.py` и удалить кросс-импорты.
  - _Requirements: R3_
  - Evidence: `from test_ …` в `tests/` — 0 совпадений; 124 passed.
- [x] 6. Включить расширенный `select` и устранить все находки без подавлений.
  - _Requirements: R4_
  - Evidence: 28 находок (B905, C416, SIM105, F401, RUF005, RUF043, PLC0415, 18×E501) устранены; `RUF001-003` — осознанный ignore.
- [x] 7. Декомпозировать сложные функции, прогоняя набор после каждой.
  - _Requirements: R5_
  - Evidence: `Transport.get`, `MatcherConfig.validate_axes`, `extract_loop`, `Pipeline._reviews`, `Matcher.recommend`; поведение доказано идентичным.
- [x] 8. Добавить CI-гейт на push/PR.
  - _Requirements: R6_
  - Evidence: `.github/workflows/ci.yml`; YAML валиден; offline-характер набора подтверждён прогоном с заблокированным `getaddrinfo`.
- [x] 9. Финальный прогон, CLI/MCP smoke, ревизия diff и закрытие спеки.
  - _Requirements: Definition of Done_

## Оставленные находки (осознанно)

Два `PLR0911` (`policy.safety_gate`, `mcp_server.synergy_verdict`) не декомпозированы: это плоские лестницы решений с одним `return` на правило, читаемые линейно. Их разбор на helper-функции спрятал бы порядок проверок, который и является контрактом безопасности. Правило `PLR` в `select` не включено, поэтому находки не подавляются, а остаются видимыми при явном запуске.
