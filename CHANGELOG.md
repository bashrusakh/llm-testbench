# Changelog

All notable release changes for LLM Testbench are tracked here.

## v0.2.2 (2026-06-14)

Major refactor — code-review cleanup pass closes an SSRF gap in the SQL
path, stops the backend from overriding server-side sampling settings,
surfaces the tool-call budget and stop reason in the SQL detail card, adds
a no-hardcode version resolver, and removes a pile of dead/misleading code
uncovered by review. **Sampling settings now live on the LLM server, not
in the app** — see the ⚠️ heads-up below.

> ### ⚠️ **MAJOR — Heads up before you re-run anything**
>
> **Sampling parameters (`temperature` / `top_p` / `presence_penalty` /
> `frequency_penalty`) are no longer sent in any LLM request.**
>
> Previously the backend pushed these values into every OpenAI-compatible
> payload. On OpenAI-compatible servers the client-sent value **replaces**
> the model's registered preset, so whatever you configured in
> LM Studio / llama.cpp / Ollama was being silently discarded by us. From
> v0.2.2 the server uses its own preset — that's the source of truth now.
>
> **Why the old behaviour was actively harmful for SQL benchmarks:**
> the SQL tool-calling path was pinned to `temperature: 0.1` (hard-coded
> in `call_llm_single` and `call_llm_tool_calling`). That value runs the
> model in near-deterministic mode. Combined with the tool-calling retry
> loop (`run_question_tool_calling` — up to `max_retries=3` SQL retries
> after a DuckDB error, plus the `MAX_TOOL_CALLS=10` overall ceiling)
> the result was:
>
> 1. Model generates wrong SQL on attempt 1.
> 2. DuckDB returns a `BinderException` / `ParserException`.
> 3. We feed the error back and ask for a fix.
> 4. At `temperature=0.1` the model produces **the same wrong SQL** again
>    — sampling is too tight to escape the local optimum.
> 5. Repeat until `MAX_TOOL_CALLS` is hit. Question fails with
>    `stop_reason=tool_call_limit` even though the model had room to try
>    something different at any other temperature.
>
> Why this snuck through upstream review in
> [nlothian/llm-sql-benchmark](https://github.com/nlothian/llm-sql-benchmark)
> is unclear — best guess is it was an early "make output stable" hack
> that nobody pulled out once the retry loop arrived. We were mirroring
> upstream behaviour, so we carried the same trap.
>
> **What you should do after upgrading:**
> - **Re-run anything you compared between models** — earlier
>   "model X can't recover from BinderException" results were partly our
>   fault, not the model's.
> - Set your sampling preset on the LLM server side (LM Studio model
>   settings, Ollama `Modelfile`, llama.cpp launch flags). The backend
>   no longer pushes back.
> - If you want strict determinism for reproducibility runs, set
>   `temperature=0` in the server preset — but understand the SQL retry
>   loop will degrade exactly the way described above. For benchmarking,
>   `temperature=0.7` or whatever the model recommends is usually saner.
>
> See [`Fixed → Stopped overriding server-side sampling parameters`](#fixed) below
> for the full file-level breakdown.

### Added

- **`GET /api/version` + sidebar version tag** — backend resolves the running
  app's version on first call and caches the result; the UI fetches it at
  startup and renders it next to the "LLM Testbench" title with a tooltip
  showing where the string came from. Source order (first non-empty wins):
  1. `LLM_TESTBENCH_VERSION` env var (CI override),
  2. `git describe --tags --abbrev=0` (local dev clone),
  3. `VERSION` file in the project root (written by the Release workflow into
     the published archive — survives the absence of `.git`),
  4. `"dev"` fallback.
  Lives in `python/_version.py` with 8 regression tests in `tests/test_version.py`.
- **`.github/workflows/release.yml`** — on tag push (`v*`) it writes
  `${GITHUB_REF_NAME}` into `./VERSION`, zips the working tree, and attaches
  the archive to the GitHub release. So downstream zip users get an accurate
  version without having to install git.
- **SQL detail card — Tool-call budget** — the meta strip now shows
  `Tool calls: 5 / 10` (instead of a bare `5`), highlighting the chip in
  amber at ≥80% of `MAX_TOOL_CALLS` and red at 100%. Lets the operator tell
  "model finished comfortably" from "scraped past the ceiling" at a glance.
- **History table — `Questions` and `Thinking` columns** — two new columns between `Benchmark Type` and `Status` so you can scan saved runs and tell at a glance which SQL questions were exercised and which thinking variant ran. `Questions` shows `all (N)` when nothing was filtered, `K selected` (with the full ID list in the tooltip) for explicit subsets. `Thinking` is a coloured chip (`off` muted / `on` accent / `both` warn) — speed runs render `—` so the column stays clean across mixed history. Empty-state colspans bumped 9 → 11.
- **SQL detail card — Stop reason** — the backend `stop_reason` (`results_ok`,
  `text_implicit_ok`, `duplicate_sql_forced_ok`, `limit_forced_ok`,
  `tool_call_limit`, `question_timeout`, `error`) is surfaced as a coloured
  chip with a plain-English label (e.g. `hit limit (used last SQL)`). Hidden
  on the boring happy path (`results_ok` + `success=true`) to avoid noise.
  Hover for the raw backend key.
- **CSS — `--warn` and `--danger` design tokens** added to `:root` so the new
  severity chips reuse a single colour source.

### Changed (CI)

- **CI actions bumped to Node 24 runtimes** — `actions/checkout@v4` → `@v5`
  and `actions/setup-python@v5` → `@v6` in `.github/workflows/ci.yml` and
  `release.yml`. Clears the `Node.js 20 actions are deprecated` warning that
  GitHub started emitting on every "Python checks" run.

### Security

- **SSRF guard now covers the SQL path** — `call_llm_single` and `call_llm_tool_calling` were issuing HTTP requests to user-supplied endpoints *without* the `_validate_endpoint_url` check that speed-mode runs already enforce. A user pointing the SQL benchmark at `http://169.254.169.254/...` (AWS metadata) or an `fe80::/10` link-local address would have bypassed the guard entirely. Both entry points now validate via the lazy `python.server._validate_endpoint_url` import so existing test monkeypatches keep working.

### Fixed

- **⚠️ MAJOR — Stopped overriding server-side sampling parameters** — see the heads-up box at the top of this release for the why-it-mattered story. File-level changes:
  - `benchmark_openai` (speed): `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` removed from the OpenAI-compatible chat-completions payload. `max_tokens` and `stream`/`stream_options` stay.
  - `benchmark_ollama` (speed): `options.temperature` / `options.top_p` removed; only `options.num_predict` stays.
  - `call_llm_single` (SQL, OpenAI-compatible branch): hard-coded `temperature: 0.1` removed.
  - `call_llm_single` (SQL, Ollama branch): hard-coded `temperature: 0.1` removed.
  - `call_llm_tool_calling` (SQL tool-calling main payload): hard-coded `temperature: 0.1` removed.
  - `call_llm_tool_calling` (SQL fallback plain payload when server rejects tools): hard-coded `temperature: 0.1` removed.
  - `BenchmarkRequest` dataclass keeps `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` fields with their defaults (0.7 / 1.0 / 0.0 / 0.0). They aren't used in any payload anymore, but removing them would be a breaking API contract change for anything reading saved JSON records. Treat them as deprecated metadata.
- **`timeoutMs` default was 12000 seconds (~3.3 hours)** — UI defaulted `<input value="12000">` while the JS payload multiplied by 1000 (`numVal('timeoutMs', 12000) * 1000`), producing a 12,000,000 ms HTTP timeout. Fixed to `120` (= 120 s real timeout) in `index.html`, `buildSpeedPayload`, and `buildSqlPayload`.
- **Request-timeout field was hidden in SQL-only mode but its value was still used** — `timeoutMs` was wrapped in `.speed-only-setting`, so picking only "SQL Accuracy" hid the input entirely while `buildSqlPayload` kept reading it from the hidden DOM node. User had no way to change it. Moved the field (and its new hint) out of the speed-only group; the `Mode` select stays speed-only since it has no effect on SQL.
- **Bare "Timeout (s)" label gave the user no clue what to put in it** — relabelled to "Request timeout (s)" with an inline hint explaining the unit, what it covers (one HTTP call in speed mode, one tool-calling round-trip in SQL mode), and rough typical values (60-120 s for 7-13B models, 300-600 s for reasoning models or weak hardware). Mirrors the hint style of `Per-question timeout`.
- **`max(timeout_ms / 1000, 300.0)` floor silently extended SQL timeouts to 5 minutes** — both `call_llm_single` and `call_llm_tool_calling` quietly raised any `read`/`write` timeout below 300 s up to 300 s. A user requesting a 30 s per-question budget actually got 300 s, with no UI hint. Floor removed; the configured timeout is now used verbatim.
- **Speed benchmark lost all in-progress results on crash** — only the SQL path called `flush_job_record` after each result. A speed run with 20 models × 5 prompts that died mid-run wrote nothing to disk until the final `finally`. `run_sequential` and `run_parallel` now fire-and-forget a `flush_job_record` after every appended row, tracked by `job.track_save(...)` so shutdown drains them. Mirrors the SQL path exactly.
- **`fallback_completion_tokens` reported chunk count, not token count** — when a server omitted the SSE `usage` block, `_benchmark_openai` filled `completion_tokens` by counting `delta.content` chunks. One chunk can hold many tokens; `decode_tps` derived from this was under-reported by N×. Behaviour replaced with explicit `None` — the UI now shows `n/a` rather than a believable but wrong number.
- **Mixed `latency_ms` and `latency_s` rows were averaged without conversion** — `build_run_summary` and `/api/benchmark/dashboard` used `first_number(latency_ms, latency_s)`, which returned whichever field existed *as-is*. A history with both ms-records and legacy s-records produced means skewed by 1000×. New `ms_from_ms_or_s` / `_ms_from_ms_or_s` helpers normalise seconds → ms before pushing into the average.
- **SQL execution narrowed to `except duckdb.Error` could still crash the job** — three remaining `_execute_sql` call sites (`run_question`, the text-extraction branch in `run_question_tool_calling`, and the `run_sql_query` tool-call branch) only caught `duckdb.Error`. A `ValueError` from a malformed result or a `TypeError` from `_normalize_mapping` would escape and kill the whole job instead of recording a failed result. Widened to `(duckdb.Error, ValueError, TypeError)` for parity with the already-fixed `_finalize_tool_run`.
- **"Stop failed" misreported as "Start failed"** — copy-paste error in `stopBenchmark`'s catch handler.
- **Speed table `colspan="12"` against an 11-column table** — three empty-state placeholders used `colspan="12"`, leaving a phantom 12th cell. Fixed to `11`. `startNextQueuedJob` ternary simplified accordingly.
- **Dead `len(models) < 1` guard in `BenchmarkRequest.from_dict`** — the SQL branch raised "requires at least one model" after lines 414-418 had already guaranteed `models` was non-empty. Removed.

### Changed

- **`_compute_speed_aggregates` import hoisted out of `_aggregated_speed`** — the lazy import was a holdover from the in-progress module split. The cycle no longer exists (`python.aggregates` has no `models` dependency), so it now imports at module top.
- **`["prepare", "select_tasks", ...]` hardcoded list replaced with `ADAPTER_LIFECYCLE_HOOKS`** — single source of truth.
- **`LOCAL_SCAN_READ_TIMEOUT_S = .5` → `0.5`** — cosmetic consistency with the rest of the file.

### Removed

- **`BenchmarkServer._validate_endpoint_url_is_bound`** — static `return True`, no callers, dead diagnostic.
- **`applyProviderToConfig(provider)`** — empty no-op left after the config card was removed; deleted along with its two call sites.
- **`formatters` / `OUTCOME_META` / `outcomeMeta` in `app.js`** — declared but never referenced anywhere. The standalone `formatNumber` / `formatTps` / `formatMillisecondsAsSeconds` functions are the ones actually used.
- **`fallback_completion_tokens` accumulator** — see "Fixed" above.
- **Unused imports** — `BENCHMARK_PRESETS_BY_ID` in `benchmark_presets`, `flush_job_record` in `run_job` (still imported lazily inside `run_sequential`/`run_parallel`/`run_sql_job` where it is actually used), `_validate_endpoint_url` "re-export" at the top of `job_runner.py`.

### Tests

- **`test_openai_tool_call_reasoning_falls_back_when_unsupported`** and **`test_openai_single_prefers_content_over_reasoning_content`** — added `monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)` so the new SSRF guard doesn't reject the `example.test` fixture hostname.
- **`test_benchmark_openai_uses_chunk_counter_fallback_when_usage_missing`** renamed to **`test_benchmark_openai_returns_none_when_usage_missing`** and rewritten — asserts `completion_tokens is None` / `decode_tps is None` to lock in the new "honest n/a" behaviour. TTFT remains measurable from the first non-empty delta.

### Verification

- Test suite: `150 passed` (142 baseline + 8 new in `tests/test_version.py`).
- `create_app()` boots and registers 50 routes (added `/api/version`).
- `python._version.get_version_info()` against this working tree returns
  `{'version': 'v0.2.1', 'source': 'git'}` — confirms the git fallback works.

## v0.2.1 (2026-06-14)

### Fixed

- **`_finalize_tool_run` crash on invalid LLM-generated SQL** — `duckdb.Error` (e.g. `BinderException` for missing GROUP BY) was not caught, crashing the entire benchmark job instead of recording a failed result. Added `duckdb.Error` to the except clause, consistent with error handling elsewhere in the tool-calling loop. (#32)

## v0.2.0 (2026-06-10)

Major refactor — backend split into focused modules, frontend extracted to separate CSS/JS files, SSRF security hardening, performance optimizations, and numerous UI/UX fixes across speed and SQL views.

### Security

- **SSRF guard for provider endpoints** — `_validate_endpoint_url` resolves hostnames via `socket.getaddrinfo` and rejects targets whose resolved IPs fall into multicast/reserved/unspecified ranges. Loopback and RFC 1918 ranges are permitted for local use. Wired into `_benchmark_openai`, `_benchmark_ollama`, and `_probe_provider`.

### Added

- **`tests/test_speed_unit.py`** — 26 regression tests for aggregates, SSRF guard, OpenAI/Ollama benchmark paths, JSON I/O, phase atomicity, aggregate cache, and save-task draining.

### Changed

- **Server split into focused modules** — `python/server.py` reduced from 3035 to 168 lines. Core logic extracted into:
  - `python/models.py` — dataclasses and registries (EndpointCandidate, BenchmarkRequest, RunState, JobState, etc.)
  - `python/aggregates.py` — per-model speed rollup
  - `python/json_io.py` — JSON load/read with path-tagged errors
  - `python/ssrf.py` — endpoint URL private-IP guard
  - `python/speed_row.py` — uniform row builder for success/failure/stopped
  - `python/persistence.py` — job save/load/export (538 lines)
  - `python/job_runner.py` — benchmark orchestration (825 lines)
  - `python/benchmark_server.py` — HTTP routes and delegate methods (1012 lines)
- **Frontend split into `static/style.css` and `static/app.js`** — `index.html` shrinks from 3971 to 321 lines. CSS and JS are loaded via `<link>` and `<script>` tags.
- **Redundant OPTIONS catch-all route removed** — aiohttp CORS middleware already handles preflight.
- **Persisted `aggregated_speed`** — on-disk job records now include pre-computed aggregates so history opens in aggregated mode immediately.
- **`set_phase` is now atomic** — five phase fields written as a single `RunState` snapshot under `asyncio.Lock`.
- **Per-job save lock + tracked save tasks** — serialises snapshot+write per job; parallel jobs don't race.
- **`_compute_speed_aggregates` memoised per `JobState`** — keyed on `(len(results), last_result_timestamp)`. Cache turns O(n) poll walk into O(1).
- **`_build_speed_row` helper** — unified row-dict builder, eliminated ~80 lines of duplication.
- **Silent `except Exception` blocks narrowed** — all backend modules now catch specific exception types.
- **Per-row DOM diff in speed results** — `Map<rowKey, {row, version}>` cache eliminates DOM mutations on unchanged poll ticks.
- **Frontend helpers** — `setStatusBoth`, `emptyState`, `getActiveJobId`, `formatters` object, `OUTCOME_META` map centralise repeated UI patterns.
- **Accessibility** — `aria-live`, `role="status"`, `aria-busy`, `aria-label` added to status and results elements.
- **Poll timer leak fixed** — `stopPolling()` clears pending `setTimeout`; registered on `pagehide` + `beforeunload`.
- **Inline `onclick` migrated to `data-action` delegation** — works under strict CSP.
- **Removed dead code** — `BenchmarkServer._read_jsonl`, unused frontend helpers, unused DOM ids, unused imports, empty `__init__.py`.

### Fixed

- **Speed `decode_tps` ~2× lower for reasoning models** — TTFT anchor now triggers on first `delta.reasoning_content` or `delta.content`, matching LM Studio's `eval_count / eval_duration`.
- **Ollama `latency_ms` was actually `ttft_ms`** — now `latency_ms == total_time_ms` for both providers.
- **Bare `json.loads(...)` produced errors with no file name** — re-raised as `ValueError("Invalid JSON in {name}: ...")`.
- **Incremental saves could be lost on SIGTERM** — `drain_pending_saves()` + `BenchmarkServer.shutdown()` await every pending save before exit.
- **Speed results stay visible during live run** — failed-only models now appear in aggregated projection.
- **Aggregated/Individual toggle works mid-run** — `renderResults` respects `state.speedViewMode` without silent fallback.
- **Live updates not blocked by open history view** — `startNextQueuedJob` resets `activeHistoryJobId` on new benchmark.
- **Speed results no longer show "—" for missing usage block** — falls back to chunk-counting when backend omits `usage`.
- **HTTP 200 with SSE `error` body is now a hard failure** — raises `RuntimeError` instead of producing phantom passes.
- **Parallel mode no longer drops results on cancellation** — `CancelledError` converted to `stopped_result`.
- **Speed results columns aligned** — `table-layout: fixed`, `tabular-nums`, explicit per-column widths across raw, aggregated, and detail tables.
- **History Open race condition fixed** — debounce flag, generation counter, deduplication guard.
- **Speed view toggle preserves global state** — `syncSpeedToggleButton()` is single source of truth.
- **History Close clears speed table** — both `resultsBody` and `speedResultsContainer` emptied.
- **History Open updates status** — shows opened job ID and status.
- **Raw/aggregated table widths aligned** — toggling no longer causes visible width jump.
- **Aggregated TTFT in seconds** — header and values consistently use seconds.
- **Timeout inputs in seconds** — labels, defaults, and JS payloads use seconds.
- **Number inputs right-aligned** — consistent numeric presentation.
- **Sidebar visual overhaul** — modernised spacing, typography, and interactive states.
- **CSS specificity of aggregated/detail columns raised** — scoped under `#speedResultsContainer > table.speed-aggregated-table` and `#speedResultsContainer .run-detail-table` to beat raw-view rules.
- **Expand-toggle button padding fixed** — `padding: 0` prevents inheriting `6px 12px` from generic `button` rule.

## v0.1.1

- **Speed benchmark: aggregated view** — per-model summary with averaged metrics, sparklines, expandable detail rows.
- **View toggle** — switch between Aggregated and Individual runs; preference persisted in `sessionStorage`.
- **Stats summary cards** — best avg decode, models tested, total runs, avg TTFT.
- **Speed `latency_ms` field corrected** — now stores total time, not TTFT.
- **Model selection locked during benchmark** — controls disabled while job is queued/running/stopping.
- **SQL cell rendering fixed** — empty tool-call blocks hidden; Unsloth Dynamic GGUF quant labels recognised.

## v0.1.0

### Added

- **Initial release** — Speed and SQL Accuracy benchmarks, endpoint discovery, model selection, live results, history with exports.
- **Fixture-ready modules** — Coding Micro, JSON Schema, Prompt Replay (wired to validation endpoints).

### Verification

- Test suite: `142 passed`.
