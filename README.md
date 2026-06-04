# LLM Testbench

<p align="center">
  <img src="docs/screenshots/overview.png" alt="LLM Testbench overview" width="920">
</p>

<p align="center">
  <a href="https://github.com/bashrusakh/llm-testbench/tags"><img alt="Release" src="https://img.shields.io/github/v/tag/bashrusakh/llm-testbench?sort=semver&label=release"></a>
  <img alt="Local first" src="https://img.shields.io/badge/local--first-yes-22c55e">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776ab">
  <img alt="No Docker required" src="https://img.shields.io/badge/docker-not%20required-64748b">
</p>

LLM Testbench is a small local web workbench for comparing LLMs without pulling
in heavyweight benchmark infrastructure. It is built for quick local checks:
generation speed, SQL accuracy, tool calling, compact coding fixtures, JSON
schema following, and prompt replay.

The project deliberately stays lightweight. It uses repository-owned fixtures,
local model endpoints, and deterministic tests instead of containers, browser
farms, cloud benchmark services, or large downloaded datasets.

## Highlights

- Local web UI for OpenAI-compatible servers, LM Studio, llama.cpp, and Ollama.
- Speed benchmark with TTFT, total time, prompt tokens, completion tokens, and
  decode tokens per second.
- SQL Accuracy benchmark with DuckDB execution and result validation.
- Local BFCL single-turn tool-calling adapter.
- Fixture-ready coding, JSON schema, and prompt replay suites.
- Saved run history with JSONL, CSV, TSV, manifest, and summary exports.
- Contract endpoints for modules, presets, fixtures, adapters, and dashboard data.

## Screenshots

### Live Results

![Live benchmark results](docs/screenshots/results.png)

### History And Exports

![Saved run history and exports](docs/screenshots/history.png)

## Benchmarks

| Module | Status | What it measures |
| --- | --- | --- |
| Speed | Startable | TTFT, total time, prompt/completion tokens, prefill TPS, decode TPS |
| SQL Accuracy | Startable | SQL generation correctness against local DuckDB fixtures |
| BFCL | Startable | Single-turn function/tool calling against local BFCL-style fixtures |
| Coding Micro | Fixture-ready | Tiny Python coding tasks with syntax and static checks |
| JSON Schema | Fixture-ready | Instruction following scored by JSON parsing and schema-lite checks |
| Prompt Replay | Fixture-ready | Fixed prompts for fast local regression comparisons |

Fixture-ready modules are exposed in metadata and validation endpoints. They are
kept local and deterministic; live generation wiring can be added without
changing the fixture format.

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

1. Start a local inference server such as LM Studio, llama.cpp, Ollama, or
   another OpenAI-compatible endpoint.
2. Open LLM Testbench.
3. Scan local endpoints or enter a base URL manually.
4. Discover models.
5. Select benchmark modules.
6. Run a benchmark and watch live results.
7. Export saved runs from history.

## Speed Metrics

The live speed path runs through `BenchmarkServer._run_single_benchmark`.
`SpeedAdapter` is metadata-only so it cannot accidentally report placeholder
passes.

OpenAI-compatible decode TPS is calculated from streamed completion tokens over
post-first-token stream time. Ollama decode TPS uses `eval_count / eval_duration`,
so model load time and prompt evaluation time are not included in decode TPS.
Use `warmup_runs > 0` when you want cold model loading kept out of measured runs.

## SQL Accuracy Notes

SQL Accuracy supports:

- tool-calling and grammar-style SQL generation modes;
- prompt thinking mode: off, on, or both;
- provider reasoning effort: provider default, none, minimal, low, medium, high,
  xhigh;
- per-question timeout;
- stop/reload recovery;
- mismatch details for row count, columns, first row, and generated SQL.

`Provider default (omit)` does not send a reasoning field. `none` sends an
explicit provider request to disable reasoning. Unsupported servers may reject
unknown reasoning fields, so LLM Testbench retries without the field when needed.

## API

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

Saved benchmark exports:

```text
GET /api/benchmark/{job_id}/results.jsonl
GET /api/benchmark/{job_id}/results.csv
GET /api/benchmark/{job_id}/results.tsv
GET /api/benchmark/{job_id}/summary.json
GET /api/benchmark/{job_id}/manifest.json
GET /api/benchmark/summaries
```

## Repository Contents

- `index.html` - single-page browser UI.
- `run.bat` / `run.sh` - launchers.
- `python/server.py` - backend API and benchmark orchestration.
- `python/adapter.py` - benchmark adapter ABC and speed/SQL/BFCL adapters.
- `python/sql_benchmark.py` - SQL benchmark runner.
- `python/bfcl.py` - BFCL loader, scorer, and argument comparator.
- `python/local_benchmarks.py` - local fixture loaders, validators, and scorers.
- `sql_benchmark_data/` - SQL questions and AdventureWorks tables.
- `bfcl_data/` - local BFCL-style questions and answers.
- `coding_data/` - tiny Python coding tasks.
- `json_schema_data/` - JSON instruction-following tasks.
- `prompt_replay_data/` - fixed regression prompts.
- `docs/screenshots/` - README screenshots.
- `tests/` - backend, adapter, fixture, dashboard, and frontend tests.

## Scope

In scope:

- local model endpoints;
- small repository-owned fixtures;
- deterministic local tests;
- simple adapter and API contracts;
- fast smoke and comparison runs.

Out of scope:

- Terminal-Bench;
- SWE-bench, SWE-rebench, Multi-SWE-bench, SWE-agent, OpenHands, SWE-ReX;
- WebArena, OSWorld, CodeClash, GAIA, tau-bench;
- LiveCodeBench and BigCodeBench as external integrations;
- Docker orchestration, browser farms, desktop VMs, remote services, or large
  downloaded benchmark datasets.

## Development

Install dependencies:

```bash
python -m pip install -r python/requirements.txt pytest
```

Run tests:

```bash
python -m pytest tests -q
```

Run the backend directly:

```bash
python -m python.server
```

## Release v0.1.0

First public release:

- local web UI for lightweight LLM benchmarking;
- startable Speed, SQL Accuracy, and BFCL modules;
- fixture-ready Coding Micro, JSON Schema, and Prompt Replay modules;
- local fixture manifest and validation endpoints;
- saved run exports and dashboard summaries;
- 166 passing tests.
