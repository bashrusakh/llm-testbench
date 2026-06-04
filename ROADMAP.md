# Roadmap

LLM Testbench is a lightweight local workbench. It should stay fast to run,
small to implement, and easy to understand.

## Scope Rules

Every benchmark kept in this roadmap must satisfy all of these rules:

- Runs locally from repository data or user-provided local model endpoints.
- Requires no external benchmark service, cloud API, Docker image, browser farm,
  desktop VM, or remote repository checkout.
- Is simple enough to implement as a small adapter, not an orchestration system.
- Has small fixture data in the repo for tests and smoke runs.
- Can be covered by deterministic tests without network access.

If a benchmark needs containers, live APIs, web/desktop automation, large external
datasets, or multi-repository agent orchestration, it does not belong in this
project roadmap.

## Implemented

- [x] Speed benchmark for local and OpenAI-compatible inference endpoints.
  - Measures TTFT, total time, prompt tokens, completion tokens, and decode TPS.
  - Sequential and parallel execution paths are implemented in `python/server.py`.

- [x] SQL Accuracy benchmark with local DuckDB execution and result validation.
  - Uses local `sql_benchmark_data/`.
  - Supports grammar and tool-calling modes.
  - Includes thinking mode and provider reasoning-effort controls.

- [x] BFCL local single-turn adapter.
  - Uses local `bfcl_data/questions.jsonl` and `answers.jsonl`.
  - Covers single, parallel, multiple, and relevance/no-call categories.
  - Scores by argument AST comparison without executing live APIs.
  - Can run through `/api/benchmark/start` with repo fixtures or a local data dir.

- [x] Benchmark adapter metadata and API contract.
  - Module registry/detail endpoints.
  - Preset list/detail endpoints.
  - Adapter lifecycle, scoring, task-selection, and UI-renderer metadata.
  - API contract endpoint with schema versions and route map.

- [x] Saved-run exports and summaries.
  - JSONL, CSV, TSV, summary JSON, and manifest JSON.
  - Dashboard endpoint aggregates pass-rate, latency, cost, and token totals.

## Near-Term TODO

- [ ] Fix SpeedAdapter to delegate to the real speed benchmark path or mark it as
  metadata-only.
  - Current live speed generation is in `BenchmarkServer._run_single_benchmark`,
    `_benchmark_openai`, and `_benchmark_ollama`.
  - `SpeedAdapter.run_task()` is a placeholder shim and should not be presented
    as a full executor until it calls the real implementation.

- [ ] Add a small local coding micro-benchmark.
  - Use a tiny repo-owned fixture set, for example 5-20 pure Python tasks.
  - No LiveCodeBench/BigCodeBench dependency.
  - Optional local execution only through normal Python subprocess tests, with
    tight timeout and no network.

- [ ] Add a small local instruction-following / JSON schema benchmark.
  - Tiny fixture set in the repo.
  - Score by deterministic JSON parsing/schema comparison.
  - Useful for non-tool-calling model behavior without external dependencies.

- [ ] Add a local prompt replay benchmark for regression checks.
  - Fixed prompts, expected structural properties, and exportable summaries.
  - No leaderboard claim; just fast local comparison.

## Explicitly Out Of Scope

The following were removed from the roadmap because they violate the scope rules:

- Terminal-Bench.
- SWE-bench, SWE-rebench, Multi-SWE-bench, SWE-agent, OpenHands, SWE-ReX.
- WebArena, OSWorld, CodeClash, GAIA, tau-bench.
- LiveCodeBench and BigCodeBench as external benchmark integrations.
- Any benchmark requiring Docker/container orchestration, browser automation,
  desktop automation, external services, or large downloaded datasets.

## Platform TODO

- [ ] Keep `/api/benchmark/start` simple.
  - One local adapter job at a time is acceptable.
  - Avoid queue managers, distributed workers, remote sandboxes, and complex
    orchestration.

- [ ] Add a local fixture manifest.
  - Document fixture file paths, task counts, and schema versions.
  - Use it to make smoke tests predictable.

- [ ] Add a lightweight benchmark data validation command.
  - Validate JSONL fixtures and required fields.
  - Run locally with pytest or a simple Python module.

- [ ] Keep dashboard/export code generic but small.
  - Aggregates are enough: pass-rate, latency, tokens, cost, per-module, per-model.
  - Avoid complex analytics, leaderboard machinery, or remote publishing.
