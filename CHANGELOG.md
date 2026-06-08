# Changelog

All notable release changes for LLM Testbench are tracked here.

## Unreleased

### Changed

- **Server split into focused modules** — `python/server.py` is now a 168-line entry point that wires the aiohttp app, defines the module-level path/config constants (`INDEX_HTML`, `SQL_BENCHMARK_DATA_DIR`, `RESULTS_STORE_DIR`, `OPENAI_*_PATH`, `OLLAMA_*_PATH`, `DEFAULT_*_CANDIDATES`, `LOCAL_SCAN_*_TIMEOUT_S`), and re-exports the names older tests and `server_module.httpx` / `server_module.SqlBenchmarkRunner` monkeypatching expect. The 3035-line monolith is split as:
  - `python/models.py` (669 lines) — dataclasses and registries: `EndpointCandidate`, `BenchmarkTarget`, `BenchmarkRequest` (with `from_dict`), `BenchmarkModule`, `BenchmarkPreset`, `RunState`, `JobState`, plus the `BENCHMARK_MODULES` / `BENCHMARK_PRESETS` tuples and their `BY_ID` lookups.
  - `python/aggregates.py` (130 lines) — `_compute_speed_aggregates`, the per-model rollup.
  - `python/json_io.py` (39 lines) — `ts_utc`, `_load_json`, `_read_json` with path-tagged errors.
  - `python/ssrf.py` (49 lines) — `_validate_endpoint_url` private-IP guard.
  - `python/speed_row.py` (76 lines) — `_build_speed_row` for success / failure / stopped paths.
  - `python/persistence.py` (538 lines) — free functions: `inverted_prefix`, `record_filename`, `find_record_path`, `load_results_store`, `migrate_legacy_filenames`, `reconcile_stale_records`, `save_job_record`, `flush_job_record`, `append_job_to_results_store`, `load_job_record`, `build_results_jsonl` / `_table` / `_csv` / `_tsv`, `build_run_manifest`, `build_run_summary`, `clear_results`.
  - `python/job_runner.py` (825 lines) — free functions: `post_openai_chat_with_reasoning_fallback`, `run_job`, `run_sql_job`, `call_llm_single`, `call_llm_tool_calling`, `sql_result_row`, `run_sequential`, `run_parallel`, `run_single_benchmark`, `stopped_result`, `benchmark_openai`, `benchmark_ollama`, `build_report`, `build_speed_report`, `build_sql_report`. Owns the `REASONING_FALLBACK_STATE` ContextVar.
  - `python/benchmark_server.py` (1012 lines) — `BenchmarkServer` class with `__init__`, route handlers (HTTP), and thin delegate methods (`_run_job`, `_run_sql_job`, `_run_sequential`, `_run_parallel`, `_run_single_benchmark`, `_stopped_result`, `_benchmark_openai`, `_benchmark_ollama`, `_call_llm_single`, `_call_llm_tool_calling`, `_sql_result_row`, `_post_openai_chat_with_reasoning_fallback`, `_validate_endpoint_url`, `_build_report`, `_build_speed_report`, `_build_sql_report`, `_save_job_record`, `_flush_job_record`, `_append_job_to_results_store`, `_load_results_store`, `_find_record_path`, `_record_filename`, `_build_*` static builders, `migrate_legacy_filenames`, `reconcile_stale_records`) that bridge to the free functions. Delegates preserve the `monkeypatch.setattr(BenchmarkServer, "_x", fake)` test surface; the free functions in `job_runner.py` call the delegates (`self._benchmark_openai`, `self._validate_endpoint_url`) so the patches take effect.
- **Frontend split into `static/style.css` and `static/app.js`** — `index.html` shrinks from 3971 to 321 lines. The 1011 lines of CSS (lines 10-1020) live in `static/style.css`; the 2641 lines of JS (lines 1329-3969) live in `static/app.js`. The HTML now loads both via `<link rel="stylesheet" href="/static/style.css">` and `<script src="/static/app.js"></script>`. The aiohttp app registers `app.router.add_static("/static/", PROJECT_ROOT / "static", show_index=False)`. Wrapper `<script>` and `<style>` tags are stripped from the extracted files during the build to avoid a double-parse in the browser.
- **Redundant OPTIONS catch-all route removed** — `aiohttp`'s CORS middleware already handles preflight OPTIONS on every registered route; the explicit `app.router.add_route('OPTIONS', '/{path:.*}', ...)` handler was removed to avoid double-setting headers.
- **Persisted `aggregated_speed`** — `append_job_to_results_store` now writes the pre-computed aggregated speed rows into the on-disk record so that a saved run opened from history shows the aggregated view immediately, without recomputing on load.
- **Removed unused server.py imports/constants** — dropped `contextvars`, `Any, Dict, List, Optional, Tuple`, `DEFAULT_TIMEOUT_MS`, `OPENAI_CHAT_PATH`, and `OLLAMA_CHAT_PATH` from the slim entry point; these were left over from the file split and are not re-exported or consumed.
- **`tests/test_sql_frontend_integration.py` updated to read JS from the new file** — new `_read_frontend()` helper concatenates `index.html` + `static/app.js` so existing substring assertions on JS-only strings (`conv-thinking`, `visibleToolCalls`, `formatMillisecondsAsSeconds(...)`, etc.) keep working without per-test rewrites.

### Security

- **SSRF guard for provider endpoints** — `_validate_endpoint_url` now resolves the hostname via `socket.getaddrinfo` and rejects targets whose resolved IPs fall into `is_multicast`/`is_reserved`/`is_unspecified` (covering `169.254.0.0/16` link-local incl. cloud metadata, `100.64.0.0/10` CGNAT, `0.0.0.0`, IPv6 ULA / link-local, etc.). Loopback (`127.0.0.0/8`, `::1`) and RFC 1918 private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) are now allowed, since the scan loopback probe targets local servers by construction. Wired into `_benchmark_openai`, `_benchmark_ollama`, and `_probe_provider` (which catches the error and reports the target as `unreachable` instead of crashing). DNS resolution failures now raise a clear `ValueError` instead of producing a confusing `httpx.ConnectError` deeper in the stack.

### Added

- **`tests/test_speed_unit.py`** — Regression tests for `_compute_speed_aggregates` (single-model, multi-model, failed-only), the new SSRF guard (private IPv4/IPv6, loopback, link-local, public IPv4, public HTTPS, DNS failure), `_benchmark_openai` (reasoning-content first-token anchor, chunk-counter fallback, SSE error body), `_benchmark_ollama` (full `eval_count` + `prompt_eval_count`, missing-fields fallback), `_load_json` / `_read_json` (path-tagged errors, non-object rejection), `set_phase` atomicity (4 readers + 1 writer × 200 iterations), the aggregate cache (`is`-identity on repeat), and `drain_pending_saves` / `server.shutdown` awaiting fire-and-forget save tasks.

### Fixed

- **Ollama `latency_ms` was actually `ttft_ms`** — `_benchmark_ollama` set `latency_ms` to the prefill time (TTFT), not the end-to-end time. The OpenAI path correctly uses total wall-clock. Now `latency_ms == total_time_ms` for both providers, so cross-provider comparison and row-shape parity hold. New test `test_ollama_latency_ms_equals_total_time_ms` pins the behaviour.
- **Bare `json.loads(...)` on disk files produced errors with no file name** — `_load_json` / `_read_json` re-raise `JSONDecodeError` as `ValueError("Invalid JSON in {name}: ...")`. Used in the result-record loader, the SQL fixture loader, and `_validate_fixtures`. Failures in CI / on a user's machine now name the file.
- **Incremental saves could be lost on SIGTERM** — `_flush_job_record` was `asyncio.ensure_future(...)` with no tracking. PR C adds `JobState.track_save()` / `drain_pending_saves()` and a new `BenchmarkServer.shutdown()` registered as the aiohttp `on_cleanup` hook. The job's `finally` and the server's shutdown both `await` every pending save before exiting, so a row that was in flight when the process was killed is no longer silently dropped.

### Changed

- **`set_phase` is now atomic** — the five phase fields (`phase`, `message`, `run_index`, `benchmark_type`, `last_event_at`) are written as a single `RunState` snapshot under an `asyncio.Lock`. `JobState.get_phase_snapshot()` returns the snapshot reference (a single ref-assign in CPython is atomic, so readers never see a torn phase/message). The 15 `set_phase` call sites are now `await set_phase(...)`.
- **Per-job save lock + tracked save tasks** — `_flush_job_record` and the job's `finally` `await self._append_job_to_results_store` take `job._save_lock`, serialising the "snapshot `job.results` + write record" window per job. Saves for different jobs run in parallel; two parallel workers on the same job no longer race the file.
- **`_compute_speed_aggregates` is memoised per `JobState`** — keyed on `(len(results), last_result_timestamp)`. The cached list is reused on every poll tick; adding a new result invalidates the cache. The aggregator is called from `to_dict()` on every poll; on a 20-model × 5-run job the cache turns an O(100) walk per tick into O(1).
- **`_build_speed_row` helper** — single source of truth for the row dict used by `_run_single_benchmark` (success + error paths) and `_stopped_result`. `prompt_hash` is computed once in the caller and passed in. Eliminated ~80 lines of row-dict duplication.
- **Silent `except Exception` blocks narrowed** — the result-record loader and the export-record loader now catch `(OSError, ValueError)` only. In `sql_benchmark.py`: connection failure at line 149 narrowed to `(duckdb.Error, ValueError, TypeError, KeyError)`; HTTP 4xx/5xx at line 222 narrowed to `duckdb.Error`; assertion failure at line 373 narrowed to `(ValueError, TypeError)` (also fixed the misleading error message that said "LLM returned empty tool call result" when the assertion actually fires on mismatched row counts); missing-result at line 432 narrowed to `(duckdb.Error, ValueError, TypeError, KeyError)`; file write at line 606 narrowed to `duckdb.Error`; report build at line 757 narrowed to `duckdb.Error`. In `local_benchmarks.py`: JSONL reader at line 63 narrowed to `(OSError, ValueError)`. The two broad `except Exception` blocks at lines 173 and 542 in `sql_benchmark.py` are kept intentionally — they catch arbitrary errors from user-supplied LLM `Awaitable` objects.
- **Per-row DOM diff in the speed results table** — `renderSpeedResults` now uses a `Map<rowKey, {row, version}>` cache keyed on `(provider, model, run_index)`. A poll tick that returns the same results produces zero DOM mutations; new runs are appended; removed runs are reaped. Combined with a cheap results-fingerprint skip at the top of `pollJob`, steady-state polling is effectively free.
- **Frontend helpers** — `setStatusBoth(msg, type)` replaces 11 pairs of `setStatus($('jobStatus'), …) + setStatus($('mainJobStatus'), …)`. `emptyState(text, { colspan, padding })` replaces 11 inline `class="empty-state"` HTML strings (panels + table rows). `getActiveJobId()` is the single source of truth for "which job is the user looking at" (3 sites). `formatters = { ms, msAsSeconds, tps, tpsFixed, number, percent }` centralises the inline `(value).toFixed(...)` ternaries. `OUTCOME_META` map keys `'pass' | 'fail' | 'error'` to `{className, icon, label}` for the SQL cell renderers.
- **Accessibility** — `aria-live="polite"` + `role="status"` on `#jobStatus` and `#mainJobStatus`; `aria-busy` on the results `<tbody>`; `aria-label` on the four icon-only status badges in the speed results table.
- **Poll timer leak fixed** — `stopPolling()` clears the pending `setTimeout` instead of just nulling the pointer. Registered as the `pagehide` + `beforeunload` handler so a hot-reload or tab close does not leave a zombie poll running.
- **Inline `onclick="..."` migrated to delegated `data-action`** — 3 speed-preset buttons now use `<button data-action="applySpeedPreset:smoke">` and a single document-level click handler dispatches by name. Works under strict CSP and is the first step before the planned PR D file split.

### Changed

- Removed dead code:
  - `BenchmarkServer._read_jsonl` (`python/server.py`) — duplicate of `local_benchmarks.read_jsonl`, no callers.
  - Frontend helpers `generateProviderId`, `selectedModels`, `selectedSqlModel`, `renderSqlDetailChecks` (`index.html`) — replaced earlier by `stableProviderId`, `aggregateSelectedModels`, `getSelectedModelsForProvider`, and `renderSqlDetailChecksInline`.
  - Unused ids `resultsHeaderRow` and `promptGroup` (`index.html`) — no readers.
  - Unused imports: `os` (`python/server.py`), `ToolLlmCallback` (`python/server.py`), `math` (`python/sql_benchmark.py`).
  - Root `__init__.py` — empty, no consumers (the actual package is `python/`).


### Fixed

- **Speed `decode_tps` is now ~2× lower for reasoning models** — `_benchmark_openai` was anchoring `first_token_at` to the first `delta.content` chunk and ignoring `delta.reasoning_content`. For thinking models (DeepSeek-R1, QwQ, gpt-oss, etc.) the entire `reasoning_content` phase was excluded from the decode window while `usage.completion_tokens` (which includes reasoning tokens) was still used as the numerator, producing a value roughly `1 + T_thinking / T_content` (≈2× when thinking time matches answer time). The TTFT/decode anchor now triggers on the first generation delta of either field, matching LM Studio's `eval_count / eval_duration`.

- **Speed results now stay visible during a live run** — `_compute_speed_aggregates` silently dropped models whose every run had `success: False` whenever any *other* model in the same run succeeded. Failed-only models are now added to the aggregated projection as empty rows so they show up in the live aggregated view (previously they only appeared after a page refresh, when the saved record lacked `aggregated_speed` and the dispatcher fell back to the raw view).

- **Aggregated / Individual runs toggle now works mid-run** — `renderResults` now respects `state.speedViewMode` first. Selecting "Individual runs" always shows the raw view; selecting "Aggregated" shows the aggregated view when data is available and a clear "No completed speed runs to aggregate yet" placeholder when it is not, instead of silently falling back to the raw view on every poll tick.

- **Live updates are no longer blocked by an open history view** — `startNextQueuedJob` now resets `state.activeHistoryJobId` when launching a new benchmark. Previously, if a user had opened a finished run from the history panel, the live poll's `viewingOther` guard skipped the entire render block and the live results panel only updated after a page refresh.

- **Speed results no longer show "—" for models whose backend ignored `stream_options.include_usage`** — `_benchmark_openai` now falls back to counting non-empty `delta.content` chunks when the final `usage` chunk is missing, so models that stream tokens but do not emit a usage block still receive a `completion_tokens` value and a `decode_tps`.

- **HTTP 200 with `error` body in SSE is now a hard failure** — `_benchmark_openai` raises `RuntimeError` when an `error` field is present at the top level of an SSE chunk (or in `choices[0]`). Previously such responses were consumed silently, producing rows with `success: True` and all metrics `None`.

- **Parallel mode no longer drops results on cancellation** — `_run_parallel.worker` now converts `asyncio.CancelledError` into a `_stopped_result`, which the result loop already filters out, instead of swallowing the cancellation and losing the row.

- **Speed results columns are now aligned** — Added `table-layout: fixed`, `font-variant-numeric: tabular-nums`, explicit per-column `width` and `text-align` rules (right for numerics, center for status, left for text) to the static raw-view table, the aggregated per-model table, and the per-run detail table. Long model names no longer push the numeric columns off-grid and the decimal points line up across rows.

- **History Open button race condition fixed** — `openHistoryJob` now guards against rapid double-clicks (`_openHistoryJobInFlight` debounce flag) and repeated opens of the same job (`activeHistoryJobId === jobId` early return). A generation counter (`_openJobGeneration`) ensures a stale fetch for an older job cannot overwrite the current view. The speed view mode is reset to aggregated on each job open so every job starts with a clean toggle state.

- **Speed view toggle no longer silently overwrites global state** — `renderResults` no longer mutates `state.speedViewMode` or syncs the toggle button as a side effect of the raw-fallback path. The `syncSpeedToggleButton()` helper is now the single source of truth for toggle sync, called explicitly from `openHistoryJob`, `closeHistoryView`, and the toggle handler's stale-fetch guard.

- **Speed preset hint is now a compact 3-column table** — The single-line status text listing Smoke/Balanced/Leaderboard presets is replaced with a 3-column `<table>` at 11px font size, improving readability in the settings panel.

- **History Close button now clears speed table** — `closeHistoryView` was only resetting `resultsBodyEl` (raw table tbody) but not clearing `speedResultsContainer.innerHTML`. When the aggregated view replaced the container contents, closing left the aggregated table visible. Now both containers are cleared.

- **History Open updates status to show opened job** — `openHistoryJob` now sets `#historyStatus` to display the opened job ID and status, replacing the stale "Loaded N saved benchmark run(s)" message from the initial history load.

- **Raw / Aggregated table widths aligned** — Raw view column widths (Provider 14%, Model 26%, numeric columns 7-8%) now match the proportions of the aggregated view (Provider 14%, Model 26%, Avg Decode 140px, etc.) so toggling between views no longer causes a visible width jump.

- **Aggregated TTFT now in seconds** — Header changed from "Avg TTFT (ms)" to "Avg TTFT (s)" and values now use `formatMillisecondsAsSeconds()` for consistent seconds formatting across raw and aggregated views.

- **Timeout inputs now in seconds** — Both "Timeout" and "Per-question timeout" labels changed from `(ms)` to `(s)`. Default timeout changed from 12000000ms to 12000s. JS payloads multiply by 1000 before sending to API.

- **Number inputs right-aligned** — Numeric input fields in the speed settings grid now use `text-align: right` for consistent number presentation.

## v0.1.0

### Added

- **Speed benchmark: aggregated view** — Results now default to a per-model summary table (one row per model) showing averaged metrics across runs: avg/min/max decode throughput, TTFT, total time, and prefill throughput. Warmup runs (`run_index=0`) are excluded.
- **Sparklines** — Mini bar charts in the aggregated table visualize per-run decode throughput variance, color-coded by fixed thresholds: green ≥50 tok/s, amber 20–50, red <20.
- **Failed runs indicator** — Aggregated rows show "X/Y failed" count when applicable.
- **Expandable detail rows** — Click the ▼ toggle to see individual run data (run index, decode/TTFT/total, tokens).
- **View toggle** — Switch between "Aggregated" (default) and "Individual runs" views; preference persisted in `sessionStorage`.
- **Updated summary cards** — Best avg decode, models tested, total runs, avg TTFT.

### Fixed

- **Speed `latency_ms` field** — Corrected OpenAI streaming benchmark to store total elapsed time (was incorrectly storing TTFT). `total_time_ms`, `ttft_ms`, `decode_tps`, `prefill_tps` were already correct.
- Model selection controls are now locked while a benchmark job is queued, running, or stopping.
- SQL result rendering no longer shows empty tool-call analysis blocks for empty `results_ok` confirmation calls.
- SQL model quantization badges now recognize Unsloth Dynamic GGUF labels such as `UD_IQ1_S` instead of falling back to an unknown format.

### Changed

- Startable benchmark modules are now limited to Speed and SQL Accuracy.
- Fixture validation now covers SQL, Coding Micro, JSON Schema, and Prompt Replay only.
- The README and roadmap now describe the project as a fast, lightweight local benchmark workbench.
- The test badge and development instructions now report 142 passing tests.

### Verification

- Test suite: `142 passed`.
