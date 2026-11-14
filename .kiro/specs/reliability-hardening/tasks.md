# Reliability Hardening — Tasks

## Implementation Overview

Работа выполняется test-first в текущем рабочем дереве. Существующие изменения рассматриваются как baseline и не откатываются.

## Progress

- Baseline: 76 tests passed, Ruff passed в `.venv`.
- Red: 14 ожидаемых failures в `test_hardening.py`; 5 network tests passed, сетевых дефектов не подтверждено.
- Дополнительный red: invalid UTF-8 не превращался в ошибку с именем файла; исправлено в tolerant reader.
- Green: 98 tests passed, Ruff passed, CLI/MCP smoke passed.
- Текущий этап: завершено.

## Implementation Plan

- [x] 1. Добавить regression tests для CLI warnings, game verification, numeric AppID и строгого length limit.
  - _Requirements: R1, R2, R3_
- [x] 2. Добавить tests для atomic/safe storage, параллельных mutations и HTTP edge cases.
  - _Requirements: R4, R5_
- [x] 3. Запустить новые тесты на baseline и зафиксировать ожидаемые failures.
  - _Requirements: R1–R5_
  - Evidence: 14 failed, 5 passed; падения соответствуют R1–R4.
- [x] 4. Реализовать минимальные исправления CLI, policy, Matcher и MCP.
  - _Requirements: R1, R2, R3_
- [x] 5. Реализовать безопасную атомарную запись и защищённые mutations.
  - _Requirements: R4_
- [x] 6. Исправить только подтверждённые тестами network defects, если red это обнаружит.
  - _Requirements: R5_
  - Evidence: все 5 network tests прошли на baseline; implementation не изменялся.
- [x] 7. Обновить README/AGENTS.md и прогнать полный offline QA.
  - _Requirements: R5_
  - Evidence: 98 passed in 1.56s; `ruff check .` — All checks passed; `git diff --check` — чисто.
- [x] 8. Выполнить CLI/MCP smoke, проверить diff, закрыть Acceptance Criteria и создать один коммит.
  - _Requirements: Definition of Done_
  - Evidence: CLI `--help` отображает `analyze`/`recommend`; MCP stdio initialize/list_tools успешно вернул 8 инструментов по протоколу 2025-11-25.
