# LLM Testbench - AI Agent Reference

## Core Purpose

LLM Testbench is a local-first Python web app for comparing local LLM endpoints on speed and SQL accuracy. It serves a single-page UI, discovers OpenAI-compatible/Ollama-compatible endpoints, runs benchmarks, stores results locally, and exports run artifacts.

Keep the project lightweight: no Docker requirement, no remote services, no large downloaded datasets, and no broad benchmark-platform expansion unless explicitly requested.

## Architecture Overview

- The browser UI is static: `index.html`, `static/app.js`, and `static/style.css`.
- The backend is Python/aiohttp and starts from `python/server.py`.
- HTTP route handling and orchestration glue live in `python/benchmark_server.py`.
- Long-running benchmark execution and LLM HTTP calls live in `python/job_runner.py`.
- Benchmark request/state dataclasses and module/preset metadata live in `python/models.py`.
- SQL benchmark logic, DuckDB setup, tool-calling loop, SQL normalization, and scoring live in `python/sql_benchmark.py`.
- Adapter interfaces and benchmark-module shims live in `python/adapter.py`.
- Results are stored locally under `benchmarks/`.

## Tech Stack

- Python 3.10+; CI currently runs Python 3.12.
- Server: `aiohttp`.
- HTTP client: `httpx`.
- SQL validation: `duckdb`, `sqlparse`.
- Tests: `pytest`.
- Frontend: plain HTML/CSS/JavaScript, no bundler.

Dependencies are listed in `python/requirements.txt`.

## Repository Layout

| Path | What |
|---|---|
| `python/server.py` | Backend entry point, aiohttp app setup, constants, route wiring |
| `python/benchmark_server.py` | HTTP handlers, server-side orchestration, endpoint discovery |
| `python/job_runner.py` | Job execution, LLM calls, report building |
| `python/sql_benchmark.py` | SQL benchmark runner, DuckDB execution, retry/tool loop |
| `python/models.py` | Request/state dataclasses, module metadata, presets |
| `python/adapter.py` | Benchmark adapter API and Speed/SQL adapters |
| `static/app.js` | Browser application logic |
| `static/style.css` | Browser styling |
| `index.html` | Single-page UI shell |
| `sql_benchmark_data/` | SQL questions and CSV fixtures |
| `coding_data/` | Tiny Python coding fixtures |
| `json_schema_data/` | JSON instruction-following fixtures |
| `prompt_replay_data/` | Prompt replay fixtures |
| `tests/` | Pytest test suite |
| `benchmarks/` | Local saved run artifacts |
| `.github/workflows/` | CI and release workflows |

## Documentation Map

Read relevant documentation before changing behavior:

- `README.md` - product overview, runtime behavior, API, development commands, project scope.
- `CONTRIBUTING.md` - branch, commit, PR, and local development conventions.
- `CHANGELOG.md` - released behavior and migration notes.
- `ROADMAP.md` - planned work and intended direction.

For SQL benchmark changes, read the SQL Accuracy section in `README.md` and inspect nearby tests in `tests/test_sql_benchmark.py`.

## Build / Dev Commands

Install dependencies:

```bash
python -m pip install -r python/requirements.txt pytest
```

Run all tests:

```bash
python -m pytest
```

Run SQL benchmark tests only:

```bash
python -m pytest tests/test_sql_benchmark.py -q
```

Run the backend directly:

```bash
python -m python.server
```

Cross-platform launchers:

```bash
./run.sh
```

```bat
run.bat
```

## Runtime Entry Points

- Web UI: `index.html` plus assets under `static/`.
- Server module: `python/server.py`.
- aiohttp app factory: `python.server:create_app`.
- Job execution: `python/job_runner.py:run_job`.
- SQL benchmark runner: `python/sql_benchmark.py:SqlBenchmarkRunner`.

## Benchmark Behavior Notes

- Speed benchmarks measure latency, TTFT, total generation time, prompt tokens, completion tokens, and decode TPS.
- SQL accuracy supports `tool-calling` and `grammar` modes.
- SQL tool-calling retries DuckDB execution errors by feeding the error back to the model.
- Sampling settings are intentionally not sent by the app; sampling must be configured on the LLM server side.
- Per-question SQL timeout excludes cold model-load time after the first successful model response.
- Avoid changing scoring semantics, retry behavior, result schemas, or export format without updating tests and documentation.

## Agent Constraints

- Prefer the smallest correct change.
- Keep diffs tight; avoid drive-by refactors.
- Preserve existing runtime behavior unless the user explicitly asks for a behavior change.
- Do not add dependencies unless explicitly needed and justified.
- Do not add Docker, browser automation, remote service, or large dataset requirements.
- Do not commit secrets, API keys, local endpoint credentials, or benchmark outputs that are not intentionally tracked.
- Do not modify generated/local runtime artifacts under `benchmarks/` unless the task is specifically about persisted result fixtures.
- Do not run git/GitHub commands unless explicitly asked.

## Development Rules

- Inspect nearby code and tests before editing.
- Backend changes should keep API responses, persisted records, and dashboard consumers in sync.
- UI changes should preserve the existing plain JS/CSS architecture; do not introduce a frontend framework or bundler.
- Keep request parsing and validation in backend/core code, not only in UI controls.
- Use explicit error messages for partial failures, retries, fallback behavior, and stopped jobs.
- Prefer early returns and straightforward branching over clever control flow.
- Add comments only where they clarify non-obvious behavior.

## Testing Expectations

- Run the narrowest relevant pytest target after focused changes.
- Run `python -m pytest` before finalizing changes that touch shared backend behavior, request models, persistence, or UI/backend contracts.
- For SQL benchmark changes, run at least `python -m pytest tests/test_sql_benchmark.py -q`.
- For server/API changes, include relevant backend integration tests under `tests/`.
- If tests cannot be run because dependencies are missing, state the exact command attempted and the missing dependency/error.

## PR / Review Expectations

- Follow `CONTRIBUTING.md` commit and PR title conventions.
- PR descriptions should include summary, why, and validation.
- Before opening a PR, verify local status/diff and ensure only intended files are included.
- Before PR creation, request/perform the required code review step according to the user's local instructions.

## Recent Changes

- Releases and behavior changes: `CHANGELOG.md`.
- Recent commit history: `git log --oneline`.
