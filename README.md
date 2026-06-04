# LLM Testbench

Local web workbench for evaluating LLMs across practical benchmark modules.

LLM Testbench is meant to grow beyond a single benchmark. The current build can
run latency/throughput tests and SQL accuracy tests against local or
OpenAI-compatible inference servers. The roadmap adds coding, software
engineering, terminal-agent, and tool-calling evaluations.

## Current Modules

- Speed benchmark: measures latency, total generation time, prompt throughput,
  and decode throughput.
- SQL Accuracy: asks models to solve AdventureWorks-style analytical SQL tasks,
  executes generated SQL against DuckDB, and checks row count, columns, and first
  row values.

## Planned Modules

The next representative benchmark families are tracked in [ROADMAP.md](ROADMAP.md):

- BFCL for function/tool calling.
- Terminal-Bench for terminal and DevOps-style agent tasks.
- LiveCodeBench and BigCodeBench for coding ability.
- SWE-bench, SWE-rebench, and Multi-SWE-bench for repository-level issue fixing.
- CodeClash and broader agent suites as longer-term experimental modules.

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
- `python/requirements.txt` - Python dependencies.
- `python/__init__.py` - backend package marker.
- `sql_benchmark_data/` - SQL benchmark questions and AdventureWorks tables.
- `tests/` - backend, SQL, and frontend regression tests.

## Exports

Saved benchmark runs can be exported from the History table as JSONL, CSV, TSV,
or a run manifest. JSONL keeps the complete payload with one benchmark result per
line. CSV and TSV provide common columns for spreadsheet workflows and keep the
original result payload in the `result_json` column. The manifest records the run
configuration and summary without embedding every result.

```text
GET /api/benchmark/{job_id}/results.jsonl
GET /api/benchmark/{job_id}/results.csv
GET /api/benchmark/{job_id}/results.tsv
GET /api/benchmark/{job_id}/manifest.json
```

Do not commit local environments, caches, saved benchmark output, or old archived
experiments.

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
