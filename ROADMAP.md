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

- [x] BFCL v2 local single-turn adapter.
  - Uses local `bfcl_data/questions.jsonl` and `answers.jsonl`.
  - Covers single, parallel, multiple, and relevance/no-call categories.
  - Scores by argument AST comparison without executing live APIs.
  - Can run through `/api/benchmark/start` with repo fixtures or a local data dir.
  - Exposed in the web UI: checkbox alongside Speed and SQL, category and
    task-ID filters, live per-category pass counts, and queue integration.

- [x] Benchmark adapter metadata and API contract.
  - Module registry/detail endpoints.
  - Preset list/detail endpoints.
  - Adapter lifecycle, scoring, task-selection, and UI-renderer metadata.
  - API contract endpoint with schema versions and route map.

- [x] Saved-run exports and summaries.
  - JSONL, CSV, TSV, summary JSON, and manifest JSON.
  - Dashboard endpoint aggregates pass-rate, latency, cost, and token totals.

- [x] Local fixture manifest and validation.
  - `/api/fixtures` reports local fixture paths, task counts, schema version, and categories.
  - `/api/fixtures/validate` checks SQL, BFCL, coding, JSON schema, and prompt replay fixture shape without network access.

- [x] Speed adapter metadata is honest.
  - Live speed generation stays in `BenchmarkServer._run_single_benchmark`,
    `_benchmark_openai`, and `_benchmark_ollama`.
  - `SpeedAdapter.run_task()` raises instead of returning a placeholder pass.

- [x] Small local coding micro-benchmark fixtures.
  - Uses repo-owned `coding_data/tasks.jsonl`.
  - Scores Python syntax and static required/forbidden fragments.
  - No LiveCodeBench/BigCodeBench dependency.

- [x] Small local instruction-following / JSON schema fixtures.
  - Uses repo-owned `json_schema_data/tasks.jsonl`.
  - Scores by deterministic JSON parsing and schema-lite comparison.

- [x] Local prompt replay fixtures.
  - Uses repo-owned `prompt_replay_data/tasks.jsonl`.
  - Scores required, optional, forbidden, and minimum-length checks.

- [x] `/api/benchmark/start` remains simple.
  - Live startable modules stay limited to speed, SQL, and BFCL.
  - No queue managers, distributed workers, remote sandboxes, or complex
    orchestration.

- [x] Dashboard/export code remains generic and small.
  - Aggregates pass-rate, latency, tokens, cost, per-module, and per-model.
  - No complex analytics, leaderboard machinery, or remote publishing.

## Explicitly Out Of Scope

The following were removed from the roadmap because they violate the scope rules:

- Terminal-Bench.
- SWE-bench, SWE-rebench, Multi-SWE-bench, SWE-agent, OpenHands, SWE-ReX.
- WebArena, OSWorld, CodeClash, GAIA, tau-bench.
- LiveCodeBench and BigCodeBench as external benchmark integrations.
- Any benchmark requiring Docker/container orchestration, browser automation,
  desktop automation, external services, or large downloaded datasets.

## Open TODO

- [ ] LM Studio REST API support.
  - LM Studio exposes an extended REST API beyond the OpenAI-compatible subset
    (model load/unload, hardware info, loaded model stats, etc.).
  - When an endpoint is detected as LM Studio (e.g. via the `/api/v0/` routes
    or a distinctive header/model-list response), switch to the native LM Studio
    REST API instead of the generic OpenAI-compatible path.
  - Use the richer metadata (GPU layers, context length, load status) to enrich
    benchmark output and the endpoint configuration panel.
