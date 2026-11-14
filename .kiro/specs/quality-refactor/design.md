# Quality Refactor — Design

## Overview

Три независимых дефекта поведения плюс инфраструктурная уборка. Поведенческие правки локализованы в двух файлах (`matcher.py`, `network.py`); остальное — структура тестов, конфигурация и CI.

## Components and Interfaces

### Completion detection (`matcher.py`)

`COMPLETION_CUE` перестраивается: ветка `100%` выносится из группы `\b(?:...)\b` в отдельную альтернативу с защитой от предшествующей цифры.

```python
COMPLETION_CUE = re.compile(
    r"\b(?:beat (?:the|this) game|finished|completed|all achievements?|one sitting)\b|"
    r"(?<!\d)100\s?%|"
    r"прош[её]л|пройдена|на 100%|все достижения|за один присест",
    re.IGNORECASE,
)
```

Причина: `%` не является word-символом, поэтому `\b` после него требовал слово-символ справа и совпадение было возможно только при `100%done`. `(?<!\d)` исключает ложное срабатывание внутри `1100%`.

Добавляется `COMPLETION_NEGATION` — проверка **до** совпадения (окно назад), применённая в новой функции:

```python
def _confirms_completion(text: str) -> bool:
    """True when the review states the player finished, ignoring negated statements."""
```

`estimate_completion_hours` вызывает `_confirms_completion` вместо прямого `COMPLETION_CUE.search`. Медианная формула и фильтр `minutes_at_review > 0` не меняются.

Границы: forward-отрицание («completed nothing but the tutorial») остаётся необработанным осознанно — это риск ложного подавления настоящих прохождений и требует корпуса для калибровки.

### Cache recovery (`network.py`)

Ленивое чтение кэша выносится в `_read_cache(path) -> str | None` со строгой проверкой типа:

```python
cached = json.loads(...)
if isinstance(cached, dict) and time.time() - cached["time"] < ttl:
    body = cached["body"]
    if isinstance(body, str):
        return body
```

Исключения расширяются до `(OSError, ValueError, TypeError, KeyError)`; любая проблема означает «кэш непригоден» → обычный сетевой путь. Это одновременно чинит `TypeError` и снижает сложность `Transport.get`.

### Test fixtures (`tests/conftest.py`)

В `conftest.py` переносятся общие объекты из `test_matcher.py` и helper-функции из `test_policy_and_storage.py`/`test_mcp.py`:

- `loop`, `grind`, `game` — фабрики моделей;
- `FakeTransport`, `FakePipeline` — offline-двойники pipeline;
- `sample_game`, `patch_mcp_network`, `make_reviews`, `report`.

`conftest.py` доступен всем тестам без импорта, поэтому кросс-импорты `from test_matcher import ...` удаляются. Приватные имена становятся публичными (`_report` → `report`), поскольку перестают быть внутримодульными.

### Lint configuration (`pyproject.toml`)

```toml
[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "C4", "SIM", "RET", "RUF"]
ignore = ["RUF001", "RUF002", "RUF003"]
```

`RUF001-003` отключены намеренно: анализируемые строки содержат кириллицу по предметной необходимости (EN/RU-паттерны Steam-отзывов).

### Complexity decomposition

- `MatcherConfig.validate_axes` → `_validate_weights`, `_validate_blacklist`, `_validate_profiles`.
- `Transport.get` → `_read_cache`, `_write_cache` (уже есть), `_fetch`.
- `Pipeline._reviews` → `_fetch_page`, `_collect_page`.
- `Matcher.recommend` → `_collect_candidate_ids`, `_build_ranked`/`_validate_candidate`.

Все извлечения сохраняют порядок операций и текст сообщений: тесты фиксируют и коды решений, и формулировки причин.

## Error Handling

- Отрицание прохождения: отзыв игнорируется как свидетельство, запрос не падает.
- Непригодный кэш: молчаливый переход к сети; при сетевой ошибке — прежний `SourceError`.
- Стилевые правки: поведение неизменно, проверяется существующими тестами.

## Testing Strategy

1. Добавить red-тесты на R1, R2 и подтвердить падение.
2. Исправить поведение до green.
3. Перенести фикстуры в `conftest.py`, убедиться, что набор по-прежнему зелёный.
4. Декомпозировать сложные функции, прогоняя набор после каждой.
5. Включить расширенный `select`, устранить находки, добавить CI.
6. Финальный прогон: `pytest -q`, `ruff check .`, CLI/MCP smoke.
