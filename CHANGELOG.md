# Changelog

All notable release changes for LLM Testbench are tracked here.

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
