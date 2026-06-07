# Changelog

All notable release changes for LLM Testbench are tracked here.

## Unreleased

### Fixed

- **Speed `decode_tps` is now ~2× lower for reasoning models** — `_benchmark_openai` was anchoring `first_token_at` to the first `delta.content` chunk and ignoring `delta.reasoning_content`. For thinking models (DeepSeek-R1, QwQ, gpt-oss, etc.) the entire `reasoning_content` phase was excluded from the decode window while `usage.completion_tokens` (which includes reasoning tokens) was still used as the numerator, producing a value roughly `1 + T_thinking / T_content` (≈2× when thinking time matches answer time). The TTFT/decode anchor now triggers on the first generation delta of either field, matching LM Studio's `eval_count / eval_duration`.

- **Speed results now stay visible during a live run** — `_compute_speed_aggregates` silently dropped models whose every run had `success: False` whenever any *other* model in the same run succeeded. Failed-only models are now added to the aggregated projection as empty rows so they show up in the live aggregated view (previously they only appeared after a page refresh, when the saved record lacked `aggregated_speed` and the dispatcher fell back to the raw view).

- **Aggregated / Individual runs toggle now works mid-run** — `renderResults` now respects `state.speedViewMode` first. Selecting "Individual runs" always shows the raw view; selecting "Aggregated" shows the aggregated view when data is available and a clear "No completed speed runs to aggregate yet" placeholder when it is not, instead of silently falling back to the raw view on every poll tick.

- **Live updates are no longer blocked by an open history view** — `startNextQueuedJob` now resets `state.activeHistoryJobId` when launching a new benchmark. Previously, if a user had opened a finished run from the history panel, the live poll's `viewingOther` guard skipped the entire render block and the live results panel only updated after a page refresh.

- **Speed results no longer show "—" for models whose backend ignored `stream_options.include_usage`** — `_benchmark_openai` now falls back to counting non-empty `delta.content` chunks when the final `usage` chunk is missing, so models that stream tokens but do not emit a usage block still receive a `completion_tokens` value and a `decode_tps`.

- **HTTP 200 with `error` body in SSE is now a hard failure** — `_benchmark_openai` raises `RuntimeError` when an `error` field is present at the top level of an SSE chunk (or in `choices[0]`). Previously such responses were consumed silently, producing rows with `success: True` and all metrics `None`.

- **Parallel mode no longer drops results on cancellation** — `_run_parallel.worker` now converts `asyncio.CancelledError` into a `_stopped_result`, which the result loop already filters out, instead of swallowing the cancellation and losing the row.

- **Speed results columns are now aligned** — Added `table-layout: fixed`, `font-variant-numeric: tabular-nums`, explicit per-column `width` and `text-align` rules (right for numerics, center for status, left for text) to the static raw-view table, the aggregated per-model table, and the per-run detail table. Long model names no longer push the numeric columns off-grid and the decimal points line up across rows.

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
- The test badge and development instructions now report 116 passing tests.

### Verification

- Test suite: `116 passed`.
