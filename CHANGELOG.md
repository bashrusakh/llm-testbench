# Changelog

All notable release changes for LLM Testbench are tracked here.

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
