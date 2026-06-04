# Roadmap

LLM Testbench is a multi-benchmark workbench. Speed and SQL Accuracy are the
first modules; the items below are not implemented yet unless noted.

## Implemented

- Speed benchmark for local and OpenAI-compatible inference endpoints.
- SQL Accuracy benchmark with DuckDB execution and result validation.
- SQL tool-calling fallback, duplicate-SQL loop handling, stop/reload recovery,
  history, SQL details, thinking mode, and provider reasoning effort.

## Near-Term TODO

- [ ] BFCL adapter for function/tool calling.
  - Evaluate single, parallel, multi-call, REST/API, SQL, and relevance-detection
    cases.
  - Useful next step because the project already has tool-calling infrastructure.
  - Source: <https://sky.cs.berkeley.edu/project/berkeley-function-calling-leaderboard/>

- [ ] Terminal-Bench adapter for terminal-agent tasks.
  - Add a sandboxed shell execution layer.
  - Track command count, wall time, success, logs, and final answer.
  - Source: <https://terminalbench.lol/>

- [ ] LiveCodeBench adapter for coding tasks.
  - Start with code generation and self-repair subsets.
  - Keep this separate from repository-level SWE tasks because it is cheaper and
    easier to run locally.
  - Source: <https://github.com/LiveCodeBench/LiveCodeBench>

- [ ] BigCodeBench adapter.
  - Use as a software-engineering-oriented code generation module.
  - Source: <https://github.com/bigcode-project/bigcodebench>

## SWE / Repository-Agent TODO

- [ ] SWE-rebench / SWE-rebench V2 support.
  - Prefer fresh/decontaminated rolling tasks for current-model comparisons.
  - Needs Docker/container orchestration and repeated-run reporting.
  - Source: <https://swe-rebench.com/about>

- [ ] SWE-bench / SWE-bench Verified support.
  - Classic issue-to-patch benchmark using real GitHub issues.
  - Good for comparability, but contamination and runtime cost must be surfaced.
  - Source: <https://www.swebench.com/>

- [ ] Multi-SWE-bench or SWE-bench Multilingual support.
  - Add multilingual repository-level tasks across Python, JavaScript,
    TypeScript, Go, Rust, C, C++, Java, and related ecosystems.
  - Source: <https://github.com/multi-swe-bench/multi-swe-bench>

- [ ] Agent scaffold adapters.
  - mini-SWE-agent / SWE-agent style issue-solving.
  - OpenHands-style richer runtime as a future option.
  - SWE-ReX or equivalent sandbox runtime for safer execution.

## Experimental / Longer-Term

- [ ] CodeClash for multi-round goal-oriented software engineering.
  - Source: <https://arxiv.org/abs/2511.00839>

- [ ] General agent benchmarks.
  - GAIA for assistant/tool reasoning.
  - WebArena for browser agents.
  - OSWorld for desktop/computer-use agents.
  - tau-bench for business workflow tool use.

## Platform Work

- [x] Benchmark module registry and metadata endpoint.
- [x] Benchmark module detail endpoint.
- [x] Benchmark module setup requirements metadata.
- [x] Benchmark module task-selection metadata.
- [x] Benchmark module scoring metadata.
- [x] Benchmark module UI renderer metadata.
- [x] Benchmark adapter lifecycle metadata.
- [x] API contract endpoint with schema versions and route map.
- [x] Run summary export with pass-rate, latency, token, cost, and per-model aggregates.
- [x] Saved-run summaries endpoint for dashboard-style history views.
- [ ] Full benchmark module adapter API so each suite can provide:
  - run loop;
  - concrete adapter lifecycle implementation.

- [ ] Container/sandbox support for coding and terminal tasks.
- [ ] Cost, token, latency, and pass-rate dashboards across modules.
- [x] JSONL result export for saved benchmark runs.
- [x] CSV result export for saved benchmark runs.
- [x] TSV result export for saved benchmark runs.
- [x] Reproducible run manifest export.
- [x] Presets for small local smoke tests versus full leaderboard-style runs.
