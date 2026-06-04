# LLM Testbench

Local web workbench for evaluating LLMs across practical benchmark modules.

LLM Testbench is meant to grow beyond a single benchmark. The current build can
run latency/throughput tests and SQL accuracy tests against local or
OpenAI-compatible inference servers. The roadmap keeps future modules local,
small, and dependency-light: tool-calling, small coding fixtures, schema checks,
and prompt replay.

## Current Modules

- **Speed** — measures latency, total generation time, prompt throughput, and
  decode throughput.
- **SQL Accuracy** — asks models to solve AdventureWorks-style analytical SQL
  tasks, executes generated SQL against DuckDB, and checks row count, columns,
  and first row values.
- **BFCL** — Berkeley Function Calling Leaderboard adapter (v1/v2 single-turn
  subset). Loads tasks from a local `bfcl_data/` directory and evaluates four
  call categories: single, parallel, multiple, and relevance (no-call) via AST
  comparison. No external dependencies; runs entirely from local data files.
  Multi-turn (v3) and agentic (v4) categories are not yet supported.
- **Coding Micro** - tiny Python coding fixtures with deterministic syntax and
  static-fragment checks.
- **JSON Schema** - local instruction-following fixtures scored by JSON parsing
  and schema-lite validation.
- **Prompt Replay** - fixed local prompts for fast regression checks.

The backend exposes module metadata for live and fixture-ready adapters:

```text
GET /api/benchmark/contract
GET /api/benchmark/modules
GET /api/benchmark/modules/{module_id}
GET /api/benchmark/modules/{module_id}/adapter
GET /api/benchmark/presets
GET /api/benchmark/presets/{preset_id}
GET /api/benchmark/dashboard
GET /api/fixtures
GET /api/fixtures/validate
```

Implemented live modules are marked `startable: true`; fixture-ready modules are
visible in the registry and fixture endpoints but are not wired into live
generation yet. Presets describe small local smoke runs, balanced comparisons,
and fuller local runs.
Module metadata includes setup requirements and task-selection hints so future
adapters and UI panels can share one contract. It also describes scoring and UI
renderer hints for tables, traces, detail panels, and summary cards. The contract
endpoint exposes schema versions, lifecycle hooks, endpoint paths, and export
formats for integrations.

The `/api/benchmark/dashboard` endpoint aggregates pass-rate, latency, cost, and
token counts across all saved runs, broken down by module and model. Supports
`?module=`, `?model=`, and `?since=` query filters.

The fixture endpoints describe and validate local benchmark data in the repo.
They are intentionally local-only and do not download datasets.

## Scope

The roadmap is deliberately narrow. Heavy orchestrator-style suites such as
SWE-bench, Terminal-Bench, WebArena, OSWorld, CodeClash, GAIA, tau-bench,
LiveCodeBench, and BigCodeBench are intentionally out of scope. The project
keeps small local fixtures instead of external benchmark dependencies.

## Supported Backends

- OpenAI-compatible endpoints.
- LM Studio and llama.cpp servers that expose OpenAI-compatible APIs.
- Ollama for speed tests and compatible chat flows.

Provider-specific behavior matters. Some servers accept provider reasoning
payloads such as `reasoning: { "effort": "medium" }`; others reject unknown
fields. LLM Testbench retries without that field when it sees an unsupported
reasoning response.

## Quick Start

Windows:

```bat
run.bat
```

Linux/macOS:

```bash
./run.sh
```

The launcher creates a local virtual environment when needed, installs
`python/requirements.txt`, starts the backend, and opens:

```text
http://127.0.0.1:8765/
```

Useful server options:

```bash
./run.sh --host 127.0.0.1 --port 8765 --log-level INFO
```

```bat
run.bat --host 127.0.0.1 --port 8765 --log-level INFO
```

## Basic Workflow

1. Start an inference server such as LM Studio, llama.cpp, Ollama, or another
   OpenAI-compatible endpoint.
2. Open LLM Testbench.
3. Scan endpoints or enter a base URL manually.
4. Discover models.
5. Select one or more benchmark modules.
6. Start the run and watch live results.
7. Use history to inspect previous runs, SQL diffs, saved reports, and JSONL
   exports.

## SQL Accuracy Notes

SQL Accuracy currently supports:

- tool-calling and grammar-style SQL generation modes;
- prompt thinking mode: off, on, or both;
- provider reasoning effort: provider default, none, minimal, low, medium, high,
  xhigh;
- per-question timeout;
- stop/reload recovery;
- mismatch details for row count, columns, first row, and generated SQL.

`Provider default (omit)` does not send a reasoning field. `none` sends an
explicit provider request to disable reasoning. This difference matters because
unsupported servers may reject the field entirely.

For llama.cpp/Qwen-style models, real thinking/no-thinking can also be controlled
when the server is launched, for example with chat-template arguments. The UI
reasoning selector only controls request payloads.

## Repository Contents

Keep these files and directories in the repository:

- `README.md` - project overview and quick start.
- `ROADMAP.md` - future benchmark modules and implementation plan.
- `index.html` - single-page browser UI.
- `run.bat` / `run.sh` - launchers.
- `python/server.py` - backend API and benchmark orchestration.
- `python/sql_benchmark.py` - SQL benchmark runner.
- `python/adapter.py` - `BenchmarkAdapter` ABC and concrete speed/SQL/BFCL adapters.
- `python/bfcl.py` - BFCL adapter: loader, scorer, argument comparator.
- `python/local_benchmarks.py` - local fixture loaders, validators, and scorer helpers.
- `python/requirements.txt` - Python dependencies.
- `python/__init__.py` - backend package marker.
- `sql_benchmark_data/` - SQL benchmark questions and AdventureWorks tables.
- `bfcl_data/` - BFCL task files (`questions.jsonl`, `answers.jsonl`). The
  included stub dataset covers all four categories and is used by the test suite.
  Replace or extend with the full BFCL dataset for leaderboard-style runs.
- `coding_data/` - tiny Python coding tasks for local static scoring.
- `json_schema_data/` - JSON instruction-following fixtures.
- `prompt_replay_data/` - fixed prompt replay fixtures.
- `tests/` - backend, SQL, BFCL, adapter, dashboard, and frontend regression tests.

## Exports

Saved benchmark runs can be exported from the History table as JSONL, CSV, TSV,
summary JSON, or a run manifest. JSONL keeps the complete payload with one
benchmark result per line. CSV and TSV provide common columns for spreadsheet
workflows and keep the original result payload in the `result_json` column. The
summary JSON provides pass-rate, latency, token, cost, and per-model aggregates.
The manifest records the run configuration and summary without embedding every
result.

```text
GET /api/benchmark/{job_id}/results.jsonl
GET /api/benchmark/{job_id}/results.csv
GET /api/benchmark/{job_id}/results.tsv
GET /api/benchmark/{job_id}/summary.json
GET /api/benchmark/{job_id}/manifest.json
GET /api/benchmark/summaries
```

Do not commit local environments, caches, saved benchmark output, or old archived
experiments.

## Changelog

### [Unreleased]

- **BFCL adapter** (`python/bfcl.py`): evaluates single, parallel, multiple, and
  relevance tool-calling categories using local task files. Includes argument
  comparator with numeric type coercion and float tolerance. Stub dataset in
  `bfcl_data/` covers all four categories; no external dependencies required.
- **Benchmark adapter API** (`python/adapter.py`): `BenchmarkAdapter` abstract
  base class with six lifecycle hooks (`prepare`, `select_tasks`, `run_task`,
  `score`, `render`, `cleanup`). Concrete adapters for speed, SQL, and BFCL
  registered in `ADAPTER_REGISTRY`. New endpoint
  `GET /api/benchmark/modules/{module_id}/adapter` returns adapter description or
  an adapter stub for fixture-ready modules. Contract endpoint now lists
  `adapter_implemented` modules.
- **Cross-module dashboard** (`GET /api/benchmark/dashboard`): aggregates
  pass-rate, latency, cost, and token counts across all saved runs, broken down by
  module and model. Supports `?module=`, `?model=`, and `?since=` query filters.
- **Local fixture benchmarks** (`python/local_benchmarks.py`): adds coding micro,
  JSON schema, and prompt replay fixture sets with deterministic validators and
  scorer helpers. Heavy external benchmark suites are kept out of scope.
- Speed adapter is now explicitly metadata-only; live speed runs execute through
  `BenchmarkServer._run_single_benchmark`.

## Development

Install dependencies:

```bash
python -m pip install -r python/requirements.txt pytest
```

Run tests:

```bash
python -m pytest tests -q
```

The backend entrypoint is:

```bash
python -m python.server
```

## Repository Name

Recommended GitHub repository name:

```text
llm-testbench
```
