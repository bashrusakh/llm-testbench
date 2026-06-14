# LLM Testbench — Анализ кода и план исправлений

Анализ кодовой базы (Python backend + JS/HTML frontend). Цель: найти ошибки, мёртвый код, нарушения логики, возможности оптимизации и предложить план аккуратных правок без поломок текущего поведения.

Структура отчёта:
- [Раздел 1: Серьёзные баги](#1-серьёзные-баги-влияющие-на-поведение)
- [Раздел 2: Безопасность / SSRF](#2-безопасность--ssrf)
- [Раздел 3: Нарушения логики](#3-нарушения-логики)
- [Раздел 4: Мёртвый код / неиспользуемые импорты](#4-мёртвый-код--неиспользуемые-импорты)
- [Раздел 5: Производительность / оптимизация](#5-производительность--оптимизация)
- [Раздел 6: Поверхностные / косметические](#6-поверхностные--косметические)
- [Раздел 7: План исправлений](#7-план-исправлений-порядок-и-меры-предосторожности) — включает **Фазу 8: SQL Benchmark Comparison View** (новая фича: точное сравнение thinking vs no-think, разных моделей, разных прогонов).

Приоритет: 🔴 высокий, 🟠 средний, 🟡 низкий, 🟢 чисто косметика.

**Статус выполнения (по состоянию на v0.2.2 — 2026-06-14):**
- ✅ **DONE** — правка вошла в v0.2.2, см. [CHANGELOG.md](CHANGELOG.md).
- 🔁 **DONE (другим способом)** — проблема устранена, но реализация отличается от исходного предложения. В разделе указано, что сделано вместо.
- ⏳ **DEFERRED** — не делалось в этой итерации; остаётся в плане на следующие версии.
- 🚫 **DECLINED** — после повторного анализа решено не делать (с обоснованием).
- 📋 **PLANNED** — новая работа, ещё не начата (например, Фаза 8).

---

## 1. Серьёзные баги (влияющие на поведение)

### 1.1 🔴 `temperature=0` и `top_p=0` молча подменяются на дефолты — 🔁 **DONE (другим способом, MAJOR)**
**Файл:** [python/models.py:491-492](python/models.py)

```python
temperature=float(data.get("temperature", 0.7) or 0.7),
top_p=float(data.get("top_p", 1.0) or 1.0),
```

`0.0 or 0.7 == 0.7` — пользователь, выставивший `temperature=0` (детерминированная генерация), получит `0.7`. Аналогично `top_p=0` → `1.0`.

`presence_penalty` / `frequency_penalty` тоже используют `or 0.0`, но там подмена `0 → 0` безвредна.

**Как починить:** заменить на явное `data.get("temperature", 0.7)` с приведением к `float` и проверкой `None`. Не использовать `or` для числовых значений, где `0` валиден.

**Что реально сделано в v0.2.2 (по обсуждению с автором):** вместо локального фикса `or`-bug'а **полностью убраны** `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` из всех LLM-payload'ов:
- `benchmark_openai` (speed, OpenAI-compatible)
- `benchmark_ollama` (speed, Ollama)
- `call_llm_single` (SQL, обе ветки provider'ов)
- `call_llm_tool_calling` (SQL, основной payload + fallback plain-payload)

Это закрывает 1.1 как сторонний эффект (через `or` уже ничего не пройдёт в request), плюс закрывает гораздо большую проблему:
**Хардкод `temperature: 0.1` в SQL tool-calling делал retry-loop почти бесполезным** — в детерминированном режиме модель повторяла ту же неправильную SQL после `BinderException`, проедала весь `MAX_TOOL_CALLS=10` бюджет и записывалась как `stop_reason=tool_call_limit`. Это диагностировано автором при реальном прогоне.

Поведение совпадает с upstream [nlothian/llm-sql-benchmark](https://github.com/nlothian/llm-sql-benchmark) после удаления sampling. Поля в `BenchmarkRequest` оставлены как deprecated metadata, чтобы не ломать чтение старых JSON-записей.

См. [CHANGELOG.md → v0.2.2 → ⚠️ MAJOR Heads up](CHANGELOG.md) для полного объяснения user-facing impact и инструкций «что делать после обновления».

---

### 1.2 🔴 Дефолт `timeoutMs = 12000` в UI приводит к ~3.3 часам таймаута — ✅ **DONE + улучшен hint**
**Файлы:** [index.html:148](index.html), [static/app.js:883](static/app.js), [static/app.js:908](static/app.js)

```html
<input id="timeoutMs" type="number" value="12000" min="1" step="1">
```

Метка лейбла — `Timeout (s)`. В `buildSpeedPayload` / `buildSqlPayload`:
```js
timeout_ms: numVal('timeoutMs', 12000) * 1000
```

`12000 * 1000 = 12_000_000 мс = 200 минут`. Скорее всего ожидалось `value="120"` (120 c = 2 мин). На бэкенде это превращается в гигантский `httpx` таймаут.

**Как починить:** заменить дефолт в HTML и в `numVal` fallback'ах (`buildSpeedPayload`, `buildSqlPayload`) на `120`. Проверить, что тесты `tests/test_speed_unit.py` не подсовывают это значение.

**Сделано в v0.2.2:** дефолт `12000 → 120` в трёх местах + лейбл `Timeout (s) → Request timeout (s)` + inline-hint под полем с объяснением единицы и типичных значений (60-120 с для 7-13B, 300-600 с для reasoning-моделей). Поле также вынесено из `.speed-only-setting` (раньше оно скрывалось в чистом SQL-режиме, но `buildSqlPayload` всё равно его читало).

---

### 1.3 🔴 `call_llm_single` / `call_llm_tool_calling` принудительно минимум 300с read-timeout, игнорируя `timeout_ms` — ✅ **DONE**
**Файл:** [python/job_runner.py:313-318, 394-399](python/job_runner.py)

```python
timeout = httpx.Timeout(
    connect=30.0,
    read=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
    write=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
    pool=30.0,
)
```

`max(..., 300.0)` означает: если пользователь поставит `timeout_ms=30_000` (30c), реально read-timeout будет 300с. Никак не отражено в API/UI.

**Как починить:** либо удалить `max(..., 300.0)` и доверять пользователю, либо явно показать минимум в UI/документации. Согласовать со значением `question_timeout_ms`, который уже даёт «per-question» бюджет.

**Сделано в v0.2.2:** floor удалён в трёх местах (`call_llm_single` ×1, `call_llm_tool_calling` ×1, плюс зеркальная правка для Ollama ветки `call_llm_single`). Теперь `read`/`write` = ровно `timeout_ms / 1000.0`. UI-хинт под полем `Request timeout` объясняет типичные значения.

---

### 1.4 🔴 Speed-бенчмарк не делает инкрементальных сохранений — ✅ **DONE**
**Файл:** [python/job_runner.py:485-556](python/job_runner.py) (`run_sequential`, `run_parallel`)

`run_sql_job` после каждого вопроса вызывает `flush_job_record(server, job)` через `asyncio.ensure_future` и `job.track_save(...)`. `run_sequential` и `run_parallel` этого **не делают**: единственная запись на диск — финальный `append_job_to_results_store` в `run_job.finally`.

Последствия:
- При краше сервера / kill -9 во время 5-моделей × 5-прогонов теряется весь прогресс.
- `/api/benchmark/{id}` показывает live-данные из памяти, но `/api/benchmark/results` (история) ничего не видит до завершения.

**Как починить:** в `run_sequential` после `job.results.append(result)` запустить `asyncio.ensure_future(flush_job_record(...))` + `job.track_save(task)`. Аналогично в `run_parallel` после `job.results.append(result)` в цикле `as_completed`. Это уже есть в `run_sql_job`, паттерн скопировать.

**Сделано в v0.2.2:** скопирован паттерн `flush_job_record + job.track_save` в `run_sequential` (после каждого результата) и `run_parallel` (внутри `as_completed`). `drain_pending_saves` + `BenchmarkServer.shutdown` уже жнут их при остановке.

---

### 1.5 🟠 `closeHistoryView` и пустое состояние используют `colspan=12`, но в speed-таблице 11 колонок — ✅ **DONE**
**Файл:** [static/app.js:1077, 2272](static/app.js), [static/app.js:977](static/app.js)

```js
resultsBodyEl.innerHTML = '<tr><td colspan="12" class="empty-state">No benchmark results yet</td></tr>';
```

`startNextQueuedJob` уже выбирает `colspan = benchmarkType === 'sql' ? 10 : benchmarkType === 'speed' ? 11 : 12`. Остальные `colspan="12"` рассогласованы с таблицей (11 столбцов в [index.html:240-258](index.html)).

**Как починить:** заменить все `colspan="12"` в speed-контексте на `11`. Косметика, но в Firefox даёт пустой 12-й «фантомный» столбец.

---

### 1.6 🟠 `stopBenchmark` сообщает «Start failed» — ✅ **DONE**
**Файл:** [static/app.js:998](static/app.js)

```js
} catch (error) {
    setStatusBoth(`Start failed: ${error.message}`, 'error');
}
```

Copy-paste из `startBenchmark`. Должно быть `Stop failed`.

---

### 1.7 🟠 `benchmark_openai`: `fallback_completion_tokens` считает чанки, а не токены — ✅ **DONE (заменено на `None`)**
**Файл:** [python/job_runner.py:678-685](python/job_runner.py)

```python
if isinstance(content, str) and content:
    fallback_completion_tokens += 1
...
if completion_tokens is None and fallback_completion_tokens > 0:
    completion_tokens = fallback_completion_tokens
```

Это число *чанков с непустым `content`*, а не токенов. Один чанк может содержать несколько токенов. `decode_tps` вычисляется как `completion_tokens / decode_seconds` — если usage в стриме нет (старые llama.cpp / некоторые ollama-сборки), TPS будет занижен в N раз.

**Как починить:** либо честно делать `len(content)` как character-rate (с пометкой `tps_chars`), либо хотя бы переименовать переменную и `decode_tps` с пометкой `from_chunks` чтобы было видно в UI.

---

### 1.8 🟠 `build_run_summary` / dashboard смешивают `latency_ms` и `latency_s` без перевода — ✅ **DONE**
**Файл:** [python/persistence.py:461-465](python/persistence.py), [python/benchmark_server.py:553](python/benchmark_server.py)

```python
latency_values = [
    value
    for value in (first_number(item.get("latency_ms"), item.get("latency_s")) for item in rows)
    if value is not None
]
```

`first_number(ms, s)` берёт первое не-None. Если у одних строк есть `latency_ms`, у других — `latency_s`, они попадают в один список и усредняются как однородные. Записи `speed_row` всегда пишут `latency_ms`, но если на диске остался legacy-формат с `latency_s`, среднее искажается.

**Как починить:** при сборе значений конвертировать `latency_s` в `latency_ms` (умножить на 1000) перед добавлением в список. Или удалить `latency_s` из источников, если он гарантированно неактуален (проверить grep по фикстурам/история на диске).

---

### 1.9 🟠 `_finalize_tool_run` корректно ловит `duckdb.Error`, но дублирующий `_execute_sql` в основном цикле — ✅ **DONE**
**Файл:** [python/sql_benchmark.py:373](python/sql_benchmark.py)

После фикса в commit `b3e627f` `_finalize_tool_run` ловит `duckdb.Error`. Это уже исправлено. Однако в `run_question_tool_calling` ветка `if not tool_calls / extracted text` (строка 605) тоже вызывает `self._execute_sql(last_sql)`, ловит только `duckdb.Error` — на любое другое исключение (PermissionError из-за внешних импортов через DuckDB, MemoryError) задача упадёт целиком вместо `outcome=error`. Так же в основной ветке `func_name == "run_sql_query"` строка 747.

**Как починить:** аккуратно расширить `except` до `(duckdb.Error, ValueError, TypeError, MemoryError)` или общего `Exception` с записью в `error` — паритет с `_finalize_tool_run`.

---

## 2. Безопасность / SSRF

### 2.1 🔴 SQL-бенчмарк обходит SSRF-валидацию — ✅ **DONE**
**Файлы:** [python/job_runner.py:288-358 (call_llm_single)](python/job_runner.py), [python/job_runner.py:361-465 (call_llm_tool_calling)](python/job_runner.py)

`benchmark_openai` / `benchmark_ollama` (speed-путь) вызывают `_validate_endpoint_url(target.base_url)` лениво (строки 647-649, 733-735). А `call_llm_single` / `call_llm_tool_calling`, через которые работает **весь SQL-бенчмарк**, не делают этого. Пользователь может ввести ручной endpoint, ведущий на `169.254.169.254` (cloud metadata) или `fe80::/10`, и SQL-режим к нему обратится.

Защита, описанная в [python/ssrf.py](python/ssrf.py), для SQL-режима фактически выключена.

**Как починить:** в начале `call_llm_single` и `call_llm_tool_calling` вызвать
```python
from python.server import _validate_endpoint_url as _server_validate_endpoint_url
_server_validate_endpoint_url(target.base_url)
```
(как уже сделано в `benchmark_openai`). Это не сломает локальные адреса — `_validate_endpoint_url` пропускает loopback и RFC1918. Затронет только злоумышленные/случайные ввиды cloud-metadata.

Дополнительно: вынести `_validate_endpoint_url` в одно место (например, обёрткой над `httpx.AsyncClient.post`) — сейчас 4 разных места.

---

### 2.2 🟡 DuckDB позволяет `INSTALL`/`LOAD`/`COPY TO`/`read_csv('http://...')` — ⏳ **DEFERRED**
**Файл:** [python/sql_benchmark.py:1091-1096](python/sql_benchmark.py)

Соединение DuckDB in-memory, но LLM-сгенерированный SQL выполняется без ограничений по функциональности. Запросы типа `INSTALL httpfs; LOAD httpfs; SELECT * FROM read_csv('http://attacker/x.csv')` теоретически возможны, как и `COPY ... TO '/etc/passwd'` (DuckDB пишет туда, куда разрешает ОС).

Для локального тестбенча это приемлемо (пользователь сам себя бенчмаркает). Но если приложение хоть в каком-то виде станет «multi-user», это критично.

**Как починить (опционально):** перейти на `duckdb.connect(database=":memory:", config={"enable_external_access": False})`. Проверить, что фикстура AdventureWorks всё ещё загружается (она делает `read_csv_auto` локально, что считается external access, поэтому скорее всего понадобится загружать в read-only-конфиге после инициализации, либо разрешить только `read_csv_auto`). На текущей фазе достаточно отметить в README scope «local-only, single user, доверенный LLM endpoint».

---

## 3. Нарушения логики

### 3.1 🟠 `BenchmarkRequest.from_dict`: мёртвая проверка `len(models) < 1` для SQL — ✅ **DONE**
**Файл:** [python/models.py:427-428](python/models.py)

```python
if benchmark_type == "sql" and len(models) < 1:
    raise ValueError(f"{benchmark_type} benchmark requires at least one model")
```

К этому моменту `models` уже гарантированно непустой (строки 414-418 кидают `ValueError`, если их нет). Условие никогда не срабатывает.

**Как починить:** удалить блок (это явная dead-branch). Или, если хочется паранойи — превратить в `assert`.

---

### 3.2 🟠 Speed `run_parallel` не сохраняет промежуточные результаты + теряет порядок warmup — 🔁 **DONE частично / ⏳ остаток DEFERRED**

**Сделано в v0.2.2:** инкрементальное сохранение в `run_parallel` добавлено (см. 1.4). Тройной `for ... for _ in range(warmup_runs) for run_index in [0]` и параллельный warmup-через-семафор оставлены как есть — это **не баг**, а вопрос интерпретации «что должен делать warmup в parallel-режиме». Решение перенесено в отдельную задачу: либо явно сериализовать warmup per-model, либо задокументировать текущее поведение.
**Файл:** [python/job_runner.py:533-542](python/job_runner.py)

```python
tasks = [
    asyncio.create_task(worker(model, run_index))
    for model in target.models
    for _ in range(job.request.warmup_runs)
    for run_index in [0]
] + ...
```

Условие `for run_index in [0]` — это всегда один элемент. Эквивалент `[(model, 0) for model in target.models for _ in range(warmup_runs)]`. Тройной for читается тяжелее, чем надо.

Также: для каждой пары `(model, 0)` запускается **отдельная** задача warmup. Это значит, что под семафором `concurrency=N` warmup-ы одной модели стартуют параллельно, что не имеет смысла (warmup нужен для подгрузки в кэш модели/KV).

**Как починить:**
- Упростить выражение (`for _ in range(warmup_runs)`).
- Подумать: warmup, наверное, должен быть **последовательным per-model**, а только measured-прогоны — параллельными. Поведение зафиксировать в комментарии или поменять.

---

### 3.3 🟠 `JobState._aggregates_cache` — кэш по `(n, last_ts)` некорректен для `parallel`-режима — 🚫 **DECLINED**

**Обоснование:** при повторном анализе подтверждено, что инвалидация работает корректно — `len(results)` всегда увеличивается при добавлении, поэтому кэш-ключ всегда меняется. Раздел был писан с подозрением, оно не подтвердилось. Комментарий в коде ([python/models.py:600-615](python/models.py)) оставлен, поясняет почему это OK.
**Файл:** [python/models.py:600-615](python/models.py)

```python
cache_key = (n, last_ts)
```

В `run_parallel` `job.results.append(...)` вызывается в порядке завершения, не в порядке `run_index`. Если параллельно завершатся два прогона, между двумя сохранениями `n` увеличится с N до N+1, `last_ts` поменяется — кэш будет инвалидирован верно.

Но если **между двумя поллами** на самом деле завершились *два* прогона, то `n` вырос на 2, `last_ts` — это timestamp последнего; кэш инвалидируется тоже. OK.

Проблема: если timestamp двух одновременно завершившихся прогонов идентичный (с микросекундной точностью на сильно нагруженной CPU), `last_ts` совпадёт, но `n` всё равно отличается — ок.

В итоге кэш корректен. Можно оставить, но **закомментировать**, что инвалидация работает потому, что `len(results)` всегда меняется при добавлении.

---

### 3.4 🟡 `run_question_tool_calling`: budget timer не учитывает warmup внутри одного prompts'а — ⏳ **DEFERRED**
**Файл:** [python/sql_benchmark.py:471-475, 502-515](python/sql_benchmark.py)

После *первого* успешного ответа модель считается warm. Но первый вопрос мог быть «сложным» — ответ пришёл за 60 секунд, а простой второй вопрос завершится за 1 секунду. Бюджет включает всё warm-время. Документация в docstring это объясняет, но `question_timeout_ms` декларируется как «per-question budget», что вводит в заблуждение.

**Как починить:** либо обновить документ строку, либо ввести двойной бюджет («budget per question», «budget total»), но это уже фича, не баг.

---

### 3.5 🟡 `app.js` рассинхрон порядка SQL-таблицы при сохранённых job'ах — ⏳ **DEFERRED**

**Связь:** частично перекрывается с **Фазой 8** (Comparison view) — сравнение think-vs-no-think решает основной use-case без необходимости менять текущий layout matrix.
**Файл:** [static/app.js:1689-1693](static/app.js)

`Object.keys(byModel).sort(...)` сортирует по `passedCount(b) - passedCount(a)`. Для одинаковых scores — `a.localeCompare(b, ...)`. Хорошо. Но `byModel` ключ — `model + ' [' + thinking_mode + ']'`. При thinking_mode='both' будет два *разных* ряда для одной модели — пользователь увидит «llama [on]» и «llama [off]» как два разных бенчмарка. Это намеренно? Если да — ОК. Если нет — потеря наглядности.

**Как починить (опционально):** сгруппировать по `model`, показать колонки thinking_mode внутри строки модели. Большая UI-работа, оставить как «possible enhancement».

---

### 3.6 🟡 `models.py:413` — `models = sorted({model for target in targets for model in target.models})` теряет порядок предпочтений — ⏳ **DEFERRED**
**Файл:** [python/models.py:412-413](python/models.py)

```python
if targets:
    models = sorted({model for target in targets for model in target.models})
```

Сортировка `sorted` стирает порядок, заявленный фронтендом. Для UI агрегатной таблицы это не критично (фронтенд сам сортирует), но в JSONL/CSV экспорте порядок «строк по моделям» меняется на алфавитный. Если у пользователя есть скрипт, ожидающий «как в UI» — расхождение.

**Как починить (опционально):** заменить на dedup с сохранением порядка:
```python
seen = set(); models = []
for target in targets:
    for m in target.models:
        if m not in seen: seen.add(m); models.append(m)
```

---

### 3.7 🟡 `local_benchmarks.py` — `validate_local_fixtures` останавливается на первой ошибке per-фикстуре — ⏳ **DEFERRED**
**Файл:** [python/local_benchmarks.py:71-82](python/local_benchmarks.py)

`break` после первой ошибки внутри файла. Пользователь увидит только первую проблему. Для CI это «fail fast», для разработчика лучше «найди все».

**Как починить:** заменить `break` на `continue` и собирать все ошибки. Минорный UX.

---

## 4. Мёртвый код / неиспользуемые импорты

### 4.1 🟡 `static/app.js:57-85` — `formatters` и `OUTCOME_META` объявлены и нигде не используются — ✅ **DONE**
**Файл:** [static/app.js:57-85](static/app.js)

```js
const formatters = { ms, msAsSeconds, tps, tpsFixed, number, percent };
const OUTCOME_META = { pass, fail, error };
function outcomeMeta(outcome) { ... }
```

Grep подтверждает: `formatters.` / `OUTCOME_META\b` / `outcomeMeta(` нигде больше не встречаются. Код реально использует разнесённые функции `formatNumber`, `formatTps`, `formatMillisecondsAsSeconds` (строки 232-243), которые дублируют функционал `formatters.*`.

**Как починить:** удалить `formatters`, `OUTCOME_META`, `outcomeMeta`. Альтернатива (правильнее) — *использовать* их и убрать дубликаты в строках 232-243.

---

### 4.2 🟡 `python/benchmark_server.py:252-254` — `_validate_endpoint_url_is_bound` — ✅ **DONE**
```python
@staticmethod
def _validate_endpoint_url_is_bound() -> bool:
    """Diagnostic: True if the delegate is wired (always True)."""
    return True
```

Всегда возвращает `True`, нигде не вызывается. Артефакт прежней диагностики.

**Как починить:** удалить.

---

### 4.3 🟡 `python/benchmark_server.py:429` — `BENCHMARK_PRESETS_BY_ID` импортирован, не используется — ✅ **DONE**
**Файл:** [python/benchmark_server.py:428-434](python/benchmark_server.py)

```python
async def benchmark_presets(self, _request: web.Request) -> web.Response:
    from python.models import BENCHMARK_PRESETS_BY_ID
    return web.json_response({
        "status": "ok",
        "presets": [preset.to_dict() for preset in BENCHMARK_PRESETS],
        ...
    })
```

Импорт есть, объект не используется.

**Как починить:** убрать локальный импорт.

---

### 4.4 🟡 `python/job_runner.py:121` — `flush_job_record` импортирован в `run_job`, но используется только внутри `run_sql_job` — ✅ **DONE**
**Файл:** [python/job_runner.py:121, 180](python/job_runner.py)

```python
async def run_job(...):
    from python.persistence import append_job_to_results_store, flush_job_record  # flush_job_record не используется здесь
```

`flush_job_record` нужен только в `run_sql_job` (см. строку 180). Это не «опасный» dead code, но добавляет шум.

**Как починить:** убрать `flush_job_record` из строки 121. Связка с 1.4: если speed-путь начнёт делать incremental save, импорт станет нужен — тогда не удалять.

---

### 4.5 🟡 `python/job_runner.py:27` — `_validate_endpoint_url` импортирован «для re-export», но реально вызовы идут через `python.server` — ✅ **DONE**
**Файл:** [python/job_runner.py:27](python/job_runner.py)

```python
from python.ssrf import _validate_endpoint_url  # noqa: F401  (kept for re-export; runtime calls go via server._validate_endpoint_url)
```

Комментарий объясняет почему. Но «re-export» от модуля, который не имеет `__all__` и не явно ре-экспортирует, — это просто рудимент.

**Как починить:** удалить. Если какой-то тест зависит — добавить `__all__ = [...]` и явно ре-экспортировать.

---

### 4.6 🟡 `python/aggregates.py` — функция используется только лениво из `models.py` — ✅ **DONE**
Не «мёртвый код», но архитектурный комментарий: в [python/models.py:19-22](python/models.py) сказано «pending refactor; будет hoisted to the top». Сейчас всё уже разнесено в свои модули, циклическая зависимость отсутствует (проверьте импортами). Ленивый импорт внутри `_aggregated_speed` можно поднять наверх.

**Как починить:** убрать lazy import в `_aggregated_speed`, поднять `from python.aggregates import _compute_speed_aggregates` в шапку `models.py`. Запустить тесты — circular import не появится (`aggregates.py` ни на что не зависит из `models.py`).

---

### 4.7 🟡 `python/job_runner.py:180-184` — `from python.persistence import flush_job_record` и `from python.server import SqlBenchmarkRunner` оба ленивые — 🚫 **DECLINED (оставлено осознанно)**

**Обоснование:** `flush_job_record` теперь нужен в `run_sequential` и `run_parallel` (после фикса 1.4), поэтому он импортируется лениво в каждой функции, где используется. Это намеренно: позволяет тестам делать `monkeypatch.setattr(server_module, "flush_job_record", ...)`. `SqlBenchmarkRunner` остался ленивым по той же причине (для monkeypatch'а).
**Файл:** [python/job_runner.py:180-184](python/job_runner.py)

`flush_job_record` — можно сделать module-level (нет циклов).
`SqlBenchmarkRunner` — действительно нужен ленивый, см. комментарий «для monkeypatch». Оставить.

---

### 4.8 🟡 `python/server.py:59` — стилистический мусор — ✅ **DONE**
```python
LOCAL_SCAN_READ_TIMEOUT_S = .5
```
В коде используется `0.5` везде, кроме одной строки `.5`. Сменить на `0.5` для единообразия.

---

### 4.9 🟢 `app.js:399-401` — пустая функция `applyProviderToConfig` — ✅ **DONE**
```js
function applyProviderToConfig(_provider) {
    // Config card removed — provider data lives in state only.
}
```
Каркас старой UI-карты. Вызывается из 3 мест — это no-op.

**Как починить:** удалить функцию и три её вызова.

---

## 5. Производительность / оптимизация — ⏳ **DEFERRED полностью**

Все правки этого раздела отложены до появления реальных жалоб на нагрузку. На текущем масштабе (десятки сохранённых ранов, single-user локально) overhead'ы не критичны.

### 5.1 🟠 `benchmark_summaries_list`: двойная JSON-сериализация
**Файл:** [python/benchmark_server.py:786-789](python/benchmark_server.py)

```python
items = await self._load_results_store()
summaries = [json.loads(self._build_run_summary(item)) for item in items]
```

`_build_run_summary` возвращает `json.dumps(...)`, мы тут же его парсим обратно. На больших историях это десятки/сотни JSON-touch'ей.

**Как починить:** рефакторить `build_run_summary` так, чтобы внутренний `_build_run_summary_obj(record)` возвращал `dict`, а внешний `build_run_summary(record)` обёртка делала `json.dumps`. Используем `_build_run_summary_obj` в `benchmark_summaries_list`. Эндпоинт `/api/benchmark/{id}/summary.json` продолжит выдавать pretty-JSON.

---

### 5.2 🟠 `/api/benchmark/dashboard`, `/results`, `/summaries` каждый раз читают весь каталог
**Файл:** [python/persistence.py:75-93 (load_results_store)](python/persistence.py)

Грузим все файлы в память на каждый запрос. На 1000 сохранённых ранов это:
- N x `path.read_text` через `asyncio.to_thread` — каждый запуск thread'а имеет накладные расходы.
- Полный JSON.parse каждого.

UI поллит `/api/benchmark/results` при `loadHistory` (раз на сессию + при кнопке refresh) — реально нечасто. Но `/dashboard` может вызываться часто (зависит от UI).

**Как починить (оппозиционно):**
- Кэшировать каталог по mtime + checksum, инвалидировать на каждой записи.
- Или вести отдельный `index.json` с метаданными (job_id, status, created_at) — отдавать его без чтения тел.

Не критично, пока ранов десятки. Если ожидается рост — стоит сделать.

---

### 5.3 🟡 `aggregates.py`: трёхкратный проход по `decode_values` для avg/min/max
**Файл:** [python/aggregates.py:95-100](python/aggregates.py)

```python
"avg_decode_tps": round(sum(decode_values) / len(decode_values), 2) if decode_values else None,
"min_decode_tps": round(min(decode_values), 2) if decode_values else None,
"max_decode_tps": round(max(decode_values), 2) if decode_values else None,
```

Каждый список проходится трижды. Для типичных `repeat_count <= 5` неважно. Если списки большие — можно одним проходом.

**Как починить:** объединить в один проход (`accumulator`) или хранить как кортеж.

---

### 5.4 🟡 `pollJob` всегда 1с интервал
**Файл:** [static/app.js:2484](static/app.js)

```js
state.pollTimer = setTimeout(pollJob, 1000);
```

Для медленного SQL-бенчмарка с долгими вопросами это избыточно. Можно начинать с 500мс и расти до 2-3с.

**Как починить:** ввести `backoff`. Сбрасывать на 500мс при изменении `resultsFingerprint`, удваивать до 3с при отсутствии изменений.

---

### 5.5 🟡 `JobState.to_dict()` пересчитывает aggregate каждый poll
**Файл:** [python/models.py:596-598](python/models.py)

```python
if self.request.benchmark_type == "speed":
    base["aggregated_speed"] = self._aggregated_speed()
```

Кэш по `(len(results), last_ts)` уже есть — это OK. Но при росте `results` — `_compute_speed_aggregates` строит весь словарь группировки заново. Можно вести инкрементальный аккумулятор (по модели). Минорная оптимизация.

---

### 5.6 🟡 `_validate_endpoint_url` делает `getaddrinfo` на каждом запросе
**Файл:** [python/ssrf.py:42](python/ssrf.py)

На SQL-бенчмарке из 100 вопросов это 100 синхронных DNS-резолвов. Системный кэш обычно их съест, но для строгих сред можно мемоизировать на 5 минут.

**Как починить (опционально):** lru_cache с TTL по hostname.

---

## 6. Поверхностные / косметические

### 6.1 🟢 `models.py:17-22` — устаревший комментарий «refactor in progress» — ✅ **DONE** (удалён вместе с lazy-import при правке 4.6)
Уже сделано, файл `aggregates.py` существует и работает. Удалить «while the refactor is in progress».

### 6.2 🟢 `benchmark_server.py:461` — хардкод `["prepare", "select_tasks", ...]` хотя есть `ADAPTER_LIFECYCLE_HOOKS` — ✅ **DONE**

```python
"hooks": ["prepare", "select_tasks", "run_task", "score", "render", "cleanup"],
```

Заменить на `ADAPTER_LIFECYCLE_HOOKS` (импорт из `python.models`).

### 6.3 🟢 `persistence.py:194-198` — одностроковые `try: x; except: y` не PEP8 — ⏳ **DEFERRED**
```python
try: other.unlink()
except OSError: pass
```
Стиль непривычен. Развернуть в три строки.

### 6.4 🟢 `sql_benchmark.py:161-162`, `:185-186`, `:201-202`, `:217-218`, `:234-235` — `thinking_mode=thinking_mode,)` с лишним переводом строки — ⏳ **DEFERRED**
Несколько мест с `\n    thinking_mode=thinking_mode,)` — артефакт автоматической вставки `thinking_mode`. Сжать в одну строку.

### 6.5 🟢 `app.js` отсутствует IIFE wrapper — ⏳ **DEFERRED** (отдельный refactor)
Все объявления верхнего уровня (`const state = {...}` на строке 1) — реально в script-scope (потому что нет `type="module"`). Имена `function` утекают в `window` (`window.applySpeedPreset` и пр. это и используется в `data-action`-обработчике). Работает, но менее изолировано.

**Как починить (опционально):**
- Либо вернуть IIFE и явно повесить нужные функции на `window.applySpeedPreset = applySpeedPreset`.
- Либо перейти на `<script type="module">` (и тогда `data-action`-делегат должен резолвить функции через имя-таблицу, а не `window[fnName]`).

### 6.6 🟢 `app.js:1077` — `colspan="12"` ещё раз (см. 1.5) — ✅ **DONE** (вместе с 1.5)

### 6.7 🟢 `app.js:1689-1693` — Group + sort: повторное прохождение по `allQuestionIds` — ⏳ **DEFERRED**
`passedCount(modelKey)` фильтрует `allQuestionIds` для каждой модели. Меньшая `O(M*Q)` оптимизация — посчитать pass-count при первом проходе.

### 6.8 🟢 `app.js:1599` — мёртвый комментарий — ⏳ **DEFERRED** (безвредный)
```js
// Detail view is now a modal rendered outside the table, so matrix re-renders no longer append panels below the heatmap.
```
Нет соответствующего кода, который бы «append panels». Просто историческая заметка — оставить или удалить.

### 6.9 🟢 `apdater.py` (опечатка)
Проверьте: файл называется правильно `adapter.py` — нормально. (Я имел в виду строку в style.css/HTML, проверять не нужно.)

---

## 7. План исправлений (порядок и меры предосторожности)

Принцип: каждое изменение — отдельный коммит, прогон полного `pytest` после каждого. Группирую по фазам.

### Фаза 0 — Подготовка — ✅ **DONE**
- [ ] Прогон `python -m pytest tests -q` на чистом репо. Зафиксировать baseline: 142 теста зелёных (по README).
- [ ] Создать ветку `chore/code-review-cleanup`.

### Фаза 1 — Безопасность (минимальный риск, высокая ценность) — ✅ **DONE**
**Задача:** Закрыть SSRF-gap в SQL-режиме (раздел 2.1).
- [ ] Добавить вызов `_validate_endpoint_url(target.base_url)` в начало `call_llm_single` и `call_llm_tool_calling` (lazy import через `python.server`, как в `benchmark_openai`).
- [ ] Добавить юнит-тест: SQL-бенчмарк с `base_url="http://169.254.169.254"` → `ValueError`.
- [ ] Прогон `pytest`.

### Фаза 2 — Серьёзные баги поведения — ✅ **DONE (с расширением 2.1)**

**Замечание по 2.1:** вместо локального фикса `or`-bug'а в `from_dict` сделана более широкая правка — sampling params (`temperature`, `top_p`, `presence_penalty`, `frequency_penalty`) полностью убраны из всех LLM-payload'ов в speed и SQL путях. См. раздел 1.1 выше для подробностей и [CHANGELOG.md → MAJOR Heads up](CHANGELOG.md).
**Задача 2.1 — temperature/top_p (раздел 1.1):**
- [ ] В `BenchmarkRequest.from_dict` заменить `float(data.get("temperature", 0.7) or 0.7)` на explicit-`None`-coalesce:
  ```python
  raw_temp = data.get("temperature", 0.7)
  temperature = 0.7 if raw_temp is None else float(raw_temp)
  ```
  Аналогично для `top_p`. Не трогать `presence_penalty` / `frequency_penalty` (там дефолт 0.0, баг отсутствует).
- [ ] Юнит-тест: `from_dict({"temperature": 0})` → `request.temperature == 0.0`.

**Задача 2.2 — дефолт `timeoutMs` (раздел 1.2):**
- [ ] В [index.html:148](index.html) поменять `value="12000"` на `value="120"`.
- [ ] В [static/app.js:883, 908](static/app.js) поменять fallback `12000` на `120`.
- [ ] Ручной smoke-test: запустить speed-run, проверить что таймаут поведёт себя как 120с.

**Задача 2.3 — `max(..., 300.0)` floor (раздел 1.3):**
- [ ] Заменить на просто `timeout_ms / 1000.0`. Документировать в README/UI hint, что `timeoutMs` — реальный таймаут, а не подсказка.
- [ ] Прогон SQL-тестов: `tests/test_sql_backend_integration.py` чтобы убедиться, что моки не зависели от 300с.

**Задача 2.4 — incremental save в speed (раздел 1.4):**
- [ ] В `run_sequential` после `job.results.append(result); job.progress_completed += 1` добавить:
  ```python
  save_task = asyncio.ensure_future(flush_job_record(server, job))
  job.track_save(save_task)
  ```
- [ ] То же в `run_parallel` внутри цикла `as_completed` после `job.results.append(result)`.
- [ ] Импортировать `flush_job_record` (как уже сделано в `run_sql_job`).
- [ ] Юнит-тест: запустить speed-job с одной моделью, после первого результата проверить что файл в `benchmarks/` уже есть.

### Фаза 3 — Логика / косметика — ✅ **DONE**
- [ ] **3.1** — удалить мёртвую проверку `len(models) < 1` для SQL (раздел 3.1).
- [ ] **3.2** — исправить `colspan="12"` → `colspan="11"` в speed-контексте (раздел 1.5).
- [ ] **3.3** — переписать сообщение об ошибке в `stopBenchmark` (раздел 1.6).
- [ ] **3.4** — починить `fallback_completion_tokens` (раздел 1.7): либо умножить на эвристический коэффициент с пометкой, либо просто оставлять `None` (точнее, чем заведомо неверная цифра). Рекомендация: оставлять `None` и в UI показывать «n/a» для decode_tps вместо неверного.
- [ ] **3.5** — конвертировать `latency_s → ms` перед усреднением (раздел 1.8).
- [ ] **3.6** — расширить `except duckdb.Error` до общего (раздел 1.9).

После каждой группы — `pytest` + ручной smoke.

### Фаза 4 — Чистка мёртвого кода (низкий риск) — ✅ **DONE**
- [ ] Удалить `formatters`, `OUTCOME_META`, `outcomeMeta` из [static/app.js](static/app.js).
- [ ] Удалить `_validate_endpoint_url_is_bound` из [benchmark_server.py:252](python/benchmark_server.py).
- [ ] Убрать неиспользуемый импорт `BENCHMARK_PRESETS_BY_ID` в `benchmark_presets`.
- [ ] Убрать `flush_job_record` из импорта в `run_job` (только если задача 2.4 уже сделана и не использует его).
- [ ] Поднять `from python.aggregates import _compute_speed_aggregates` в шапку `models.py`, убрать lazy import.
- [ ] Заменить `.5` на `0.5` в [server.py:59](python/server.py).
- [ ] Удалить пустую `applyProviderToConfig` и её вызовы.
- [ ] Прогон pytest.

### Фаза 5 — Производительность (опционально, по требованию) — ⏳ **DEFERRED**
- [ ] Двойная JSON-сериализация в `benchmark_summaries_list` (раздел 5.1): рефактор `build_run_summary` → `build_run_summary_obj` + `build_run_summary_json`.
- [ ] Кэширование `_load_results_store` (раздел 5.2), если есть жалобы на лаги при большой истории.
- [ ] Adaptive poll interval (раздел 5.4).
- [ ] LRU-cache для `_validate_endpoint_url` (раздел 5.6).

### Фаза 6 — Стилистика (по желанию) — частично ✅ **DONE** / частично ⏳ **DEFERRED**

См. отметки в разделах 6.1-6.8 выше.
- [ ] Удалить устаревшие комментарии (раздел 6.1, 6.8).
- [ ] Заменить хардкод `["prepare", ...]` на `ADAPTER_LIFECYCLE_HOOKS` (раздел 6.2).
- [ ] Развернуть одностроковые `try/except` (раздел 6.3).
- [ ] Сжать многострочные `thinking_mode=thinking_mode,)` (раздел 6.4).

### Фаза 7 — Архитектурные предложения (не «исправления», обсуждение) — ⏳ **DEFERRED (обсуждение)**
- IIFE / module wrapper для app.js — сильнее изолирует, требует переписать `data-action` маппинг.
- DuckDB sandbox (`enable_external_access=False`) — потребует переустройства загрузки CSV.
- thinking_mode='both' для SQL — переделать UI в группировку по модели.

### Фаза 8 — SQL Benchmark Comparison View (новая фича) — 📋 **PLANNED**

**Цель.** Дать пользователю аккуратное, точное и лаконичное сравнение результатов SQL-бенчмарка между двумя выборками. Use-case'ы, которые должно покрыть:
- **thinking=on vs thinking=off** одной модели (самый частый).
- **Разные модели** на одном датасете вопросов.
- **Разные прогоны** одной модели с разными настройками (`reasoning_effort`, `question_timeout_ms`, разная версия модели, разный chat template — кейс ровно как у LM Studio betas с поломанным шаблоном).
- **До/после** изменения конфигурации сервера (например, после фикса шаблона sushi-coder'а).

**Текущее состояние.**
- В SQL matrix модели с `thinking_mode=on` и `=off` уже отрисовываются как **отдельные строки** ([app.js:1652-1655](static/app.js), ключ `byModel[model + ' [thinking]']`). Это даёт визуальное сопоставление, но **нет явного diff'а**: какие конкретно вопросы переехали из fail в pass, какие наоборот регрессировали.
- Cross-run сравнения нет вообще.
- Нет агрегированных дельт (`+5 пассов с thinking`, `-2 потеряли`).
- В history checkbox-выбор уже есть (используется для Clear Selected) — инфраструктура multi-select готова.

**Дизайн — рекомендуемая комбинация:**

#### Вариант B — Compare overlay (основной механизм)
- В history: при выборе ровно **2 записей** появляется кнопка `Compare selected`.
- Открывается полноэкранный overlay (как `openSqlDetailModal`):
  - **Header:** две колонки side-by-side с метаданными прогонов — `job_id`, дата, модель, `thinking_mode`, `reasoning_effort`, `question_timeout_ms`. Подсвечивать различающиеся поля (фоном `--warn`).
  - **Summary strip:** sticky-полоска со сводными метриками A → B → Δ:
    - Pass rate (`67% → 78% (+11%)`)
    - Avg tool calls (`6.2 → 4.1 (−2.1)`)
    - Total tokens in/out
    - Avg time per question (если будет добавлено в результат — см. ниже)
    - Distribution of `stop_reason` (мини-stacked-bar): сколько `results_ok` / `tool_call_limit` / `duplicate_sql_forced_ok` / `question_timeout` в каждом.
  - **Diff matrix (главный экран):** по строке на вопрос, отсортированы по difficulty → id (как в текущем matrix). Колонки:
    - `Q#` (+ difficulty chip)
    - Статус A (✓/✗/!) + кол-во tool-calls
    - Δ-arrow (`✓→✓` серый, `✗→✗` тёмно-серый, `✗→✓` зелёный **«gained»**, `✓→✗` красный **«lost»**, `✓→!` красный **«regressed to error»**)
    - Статус B (✓/✗/!) + кол-во tool-calls
    - Δ tool calls (числом)
    - Δ tokens (числом)
  - **Filters (toolbar над матрицей):**
    - `Only differences` (default ON) — скрывает строки с `same-pass` и `same-fail`.
    - `Only regressions` — оставляет только lost + regressed.
    - `Only gains` — только gained.
    - `Group by difficulty` (default ON).
  - **Row click → deep-dive:** клик по строке открывает существующий `openSqlDetailModal` в двух табах (`A` / `B`), без переписывания этого модала.

#### Вариант C — Compare modes shortcut в matrix (для самого частого случая)
- Если у модели в текущем SQL matrix отрисованы две строки (`model [on]` и `model [off]`) — добавить рядом с moдель-ячейкой маленькую кнопку `Compare modes`.
- Клик → открывает тот же Compare overlay из варианта B, заранее подставив эти два варианта (без захода в history).

**Backend.**
- Никаких изменений не требуется: `/api/benchmark/{id}` уже возвращает full results, на фронте всё считается.
- *Опционально* — эндпоинт `GET /api/benchmark/compare?a=<id>&b=<id>` возвращающий уже посчитанный diff (gained / lost / regressed lists, summary deltas). Полезно если кто-то захочет дёргать сравнение из скрипта или CI. Не блокирует MVP.

**Edge cases (важно — пользователь сказал «точное»):**
- **Разные `question_ids` у прогонов A и B.** Считать diff только по пересечению. Под матрицей — секция `Only in A: Q3, Q12 (12 questions)` / `Only in B: Q7 (1 question)` с явным указанием, что они не попали в pass-rate-сравнение. Никакого магического усреднения.
- **Разные модели.** Не объединять; в header указать оба имени. Метрика «модель» в выводе comparison — `A vs B`, не «модель X».
- **A или B содержит `thinking=both`.** В таком прогоне есть `model [on]` и `model [off]` варианты. UI обязан попросить выбрать конкретный вариант для сравнения (radio в header). Без явного выбора — выбран `off` (как default).
- **Если в A или B нет ни одного результата** — показать пустую сводку и пометку `Run A/B has no results`, не падать.
- **Округление в Δ.** Pass rate `66.666 → 78.333` показывать как `66.7% → 78.3% (+11.7%)`, без обмана точностью.

**UI guidelines (для «красивое\лаконичное»):**
- Использовать существующие SQL-цвета (`pass`/`fail`/`error`) без нового палитра. Только новые `gained`/`lost`/`regressed` бейджи через уже добавленные `--warn` / `--danger`.
- Δ-стрелки — один Unicode-глиф (`→`), цветной фон бэджа делает работу.
- Sticky header колонок и sticky summary strip при скролле длинной матрицы.
- Mobile: при ширине < 720px переключаться на stacked-карточки (один вопрос — одна карточка с двумя статусами).
- Никаких лишних accordion'ов и табов на главном экране — два прогона, один scroll, одна матрица. Простота — приоритет.

**Что класть в результат (мелкое расширение схемы, опционально, не блокирует фичу):**
- `result.duration_ms` — время от старта до конца обработки вопроса (включая все tool-call round-trips). Сейчас этого поля нет; есть только cumulative `latency_ms` в speed-результате. Добавление позволит показать `Δ avg time per question`.

**Тесты:**
- Frontend: `Compare selected` появляется только при выборе 2 элементов в history.
- Frontend: matrix корректно отображает 5 типов Δ (same-pass, same-fail, gained, lost, regressed-to-error).
- Frontend: фильтр `Only differences` скрывает same-pass/same-fail.
- Edge case: разные question_ids — секция «Only in A / Only in B» появляется и корректно перечисляет вопросы.
- Edge case: разные модели — header показывает оба имени, метрики не объединяются.

**Тесты backend (если будет добавлен эндпоинт `/compare`):**
- Возврат diff для двух валидных id.
- 404 для несуществующего id.
- Корректный handling пересечения question_ids.

**Объём работы — оценка:**
- 1.0 ч — финальный UI-набросок и финальное согласование с пользователем (цвета, иконки, расположение filter-bar).
- 2.5 ч — реализация Compare overlay (HTML/CSS/JS): рендер header, summary strip, diff matrix, фильтры.
- 1.0 ч — edge cases (разные question_ids, разные модели, thinking=both selector).
- 0.5 ч — интеграция кнопки `Compare modes` в SQL matrix (вариант C).
- 1.0 ч — frontend-тесты + ручной smoke.
- *Опционально* +1.5 ч — backend-эндпоинт `/api/benchmark/compare` и тесты к нему.

**Что НЕ делать на этом этапе:**
- Сравнение 3+ прогонов одновременно (UI становится перегруженным; делать только если попросят).
- Регрессионные алерты («автоматически пометить, если pass rate упал > 10%») — это уже dashboard-уровень.
- Сравнение speed-бенчмарков — отдельная задача, метрики там другие, шаблон UI не переиспользуется один-в-один.

**Связь с другими фазами:**
- Хорошо ложится поверх сделанной Фазы 4 (карточка с `tool_calls X/10` и `stop_reason`) — те же данные используются в diff matrix без новых полей.
- Если будет сделана Фаза 5.2 (кэширование `/results`) — Compare загружает быстрее без дополнительных оптимизаций.

---

## Резюме

### Статистика по результатам v0.2.2

| Категория | Всего | ✅ Done | 🔁 Done иначе | 🚫 Declined | ⏳ Deferred | 📋 Planned |
|---|---|---|---|---|---|---|
| Серьёзные баги | 9 | 8 | 1 (1.1 — целиком убрали sampling) | 0 | 0 | — |
| Безопасность | 2 | 1 (SSRF) | 0 | 0 | 1 (DuckDB sandbox) | — |
| Логика | 7 | 1 (3.1) | 1 (3.2 частично) | 1 (3.3) | 4 | — |
| Мёртвый код | 9 | 8 | 0 | 1 (4.7 — оставлено как fix-tooling) | 0 | — |
| Производительность | 6 | 0 | 0 | 0 | 6 | — |
| Косметика | 9 | 3 | 0 | 0 | 6 | — |
| **Новые фичи** | | | | | | |
| SQL Comparison view | 1 | 0 | 0 | 0 | 0 | 1 (Фаза 8) |

**Что вошло в v0.2.2 (одной строкой):** все 🔴 (4 шт.) + почти все 🟠, плюс ⚠️ MAJOR — полное удаление sampling override из всех LLM-payload'ов (включая хардкод `temperature=0.1` в SQL retry-loop). Полный список — в [CHANGELOG.md → v0.2.2](CHANGELOG.md).

**Что осталось на потом:**
- Фаза 8 — SQL Comparison view (новая фича, см. ниже план).
- Перформанс-разделы (5.1-5.6) — по факту нагрузки.
- DuckDB sandbox (2.2) — если приложение перестанет быть строго single-user local.
- Несколько косметических правок (6.3, 6.4, 6.7, 6.8) — по желанию.

**Verified after v0.2.2:** 150 тестов passing, sanity-check `create_app` регистрирует 50 routes (включая `/api/version`), `python._version.get_version_info()` корректно подхватывает версию из git.
