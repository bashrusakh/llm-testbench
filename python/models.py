"""Data models used across the server.

All dataclasses and request/preset registries live here so the rest of
the codebase can import them without dragging in aiohttp. ``server.py``
re-exports the names that tests and other modules use to keep imports
backward-compatible.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from python.aggregates import _compute_speed_aggregates
from python.json_io import ts_utc

API_CONTRACT_VERSION = 1
MODULE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
EXPORT_SCHEMA_VERSION = 1
ADAPTER_LIFECYCLE_HOOKS = ["prepare", "select_tasks", "run_task", "score", "render", "cleanup"]
REASONING_EFFORTS = {"disabled", "none", "minimal", "low", "medium", "high", "xhigh"}
DEFAULT_TIMEOUT_MS = 120_000


@dataclass
class EndpointCandidate:
    base_url: str
    provider: str
    reachable: bool
    models_path: str
    label: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "provider": self.provider,
            "reachable": self.reachable,
            "models_path": self.models_path,
            "label": self.label,
        }


@dataclass
class BenchmarkTarget:
    provider_id: str
    provider_label: str
    base_url: str
    provider: str
    api_key: str
    models: List[str]


@dataclass(frozen=True)
class BenchmarkModule:
    module_id: str
    label: str
    status: str
    description: str
    capabilities: List[str]
    result_schema: List[str]
    startable: bool
    setup_requirements: List[str] = field(default_factory=list)
    task_selection: Dict[str, Any] = field(default_factory=dict)
    scoring: Dict[str, Any] = field(default_factory=dict)
    ui_renderer: Dict[str, Any] = field(default_factory=dict)
    adapter_lifecycle: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        lifecycle = self.adapter_lifecycle or {
            "status": "planned_adapter",
            "hooks": ADAPTER_LIFECYCLE_HOOKS,
        }
        return {
            "id": self.module_id,
            "label": self.label,
            "status": self.status,
            "description": self.description,
            "capabilities": self.capabilities,
            "result_schema": self.result_schema,
            "startable": self.startable,
            "setup_requirements": self.setup_requirements,
            "task_selection": self.task_selection,
            "scoring": self.scoring,
            "ui_renderer": self.ui_renderer,
            "adapter_lifecycle": lifecycle,
        }


@dataclass(frozen=True)
class BenchmarkPreset:
    preset_id: str
    label: str
    description: str
    scope: str
    module_defaults: Dict[str, Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.preset_id,
            "label": self.label,
            "description": self.description,
            "scope": self.scope,
            "module_defaults": self.module_defaults,
        }


BENCHMARK_MODULES: Tuple[BenchmarkModule, ...] = (
    BenchmarkModule(
        module_id="speed",
        label="Speed",
        status="implemented",
        description="Latency, total generation time, prompt throughput, and decode throughput.",
        capabilities=["latency", "throughput", "multi-provider", "multi-model"],
        result_schema=["latency_ms", "total_time_ms", "ttft_ms", "prefill_tps", "decode_tps"],
        startable=True,
        setup_requirements=["OpenAI-compatible or Ollama-compatible inference endpoint", "At least one discovered model"],
        task_selection={
            "strategy": "payload",
            "fields": ["models", "prompt", "repeat_count", "warmup_runs"],
            "supports_question_ids": False,
        },
        scoring={
            "primary_metric": "decode_tps",
            "direction": "higher_is_better",
            "secondary_metrics": ["ttft_ms", "total_time_ms", "prefill_tps"],
            "aggregation": "per_model_average",
        },
        ui_renderer={
            "kind": "speed_table",
            "summary_cards": ["avg_decode_tps", "avg_ttft_ms", "avg_total_time_ms"],
            "columns": ["model", "provider_label", "ttft_ms", "decode_tps", "total_time_ms"],
        },
        adapter_lifecycle={
            "status": "implemented_inline",
            "hooks": ADAPTER_LIFECYCLE_HOOKS,
            "entrypoint": "BenchmarkServer._run_single_benchmark",
            "adapter_status": "metadata_only",
        },
    ),
    BenchmarkModule(
        module_id="sql",
        label="SQL Accuracy",
        status="implemented",
        description="AdventureWorks-style analytical SQL generation with DuckDB result validation.",
        capabilities=["tool-calling", "grammar", "thinking-mode", "reasoning-effort", "sql-execution"],
        result_schema=["question_id", "success", "generated_sql", "expected_sql", "row_count_match", "columns_match"],
        startable=True,
        setup_requirements=["DuckDB", "sql_benchmark_data tables and questions", "OpenAI-compatible inference endpoint"],
        task_selection={
            "strategy": "question_ids",
            "fields": ["question_ids", "sql_mode", "thinking_mode", "reasoning_effort"],
            "default": "all_questions",
        },
        scoring={
            "primary_metric": "pass_rate",
            "direction": "higher_is_better",
            "secondary_metrics": ["row_count_match", "columns_match", "first_row_match"],
            "aggregation": "mean_success",
        },
        ui_renderer={
            "kind": "sql_results",
            "summary_cards": ["pass_rate", "pass_count", "fail_count"],
            "columns": ["question_id", "model", "success", "difficulty", "generated_sql"],
            "detail_panel": "sql_diff",
        },
        adapter_lifecycle={
            "status": "implemented_inline",
            "hooks": ADAPTER_LIFECYCLE_HOOKS,
            "entrypoint": "BenchmarkServer._run_sql_job",
        },
    ),
    BenchmarkModule(
        module_id="coding-micro",
        label="Coding Micro",
        status="fixture_ready",
        description="Tiny local Python coding fixtures with deterministic static scoring.",
        capabilities=["code-generation", "local-fixtures", "syntax-check"],
        result_schema=["task_id", "success", "language", "error"],
        startable=False,
        setup_requirements=["coding_data/tasks.jsonl"],
        task_selection={"strategy": "fixture_ids", "fields": ["task_ids"], "default": "all_tasks"},
        scoring={
            "primary_metric": "pass_rate",
            "direction": "higher_is_better",
            "secondary_metrics": ["syntax_valid"],
            "aggregation": "mean_success",
        },
        ui_renderer={
            "kind": "coding_table",
            "summary_cards": ["pass_rate", "pass_count", "fail_count"],
            "columns": ["task_id", "language", "success", "error"],
        },
    ),
    BenchmarkModule(
        module_id="json-schema",
        label="JSON Schema",
        status="fixture_ready",
        description="Local instruction-following tasks scored by JSON parsing and schema-lite checks.",
        capabilities=["instruction-following", "json", "schema-check", "local-fixtures"],
        result_schema=["task_id", "success", "error"],
        startable=False,
        setup_requirements=["json_schema_data/tasks.jsonl"],
        task_selection={"strategy": "fixture_ids", "fields": ["task_ids"], "default": "all_tasks"},
        scoring={
            "primary_metric": "pass_rate",
            "direction": "higher_is_better",
            "secondary_metrics": ["parse_error_rate"],
            "aggregation": "mean_success",
        },
        ui_renderer={
            "kind": "schema_table",
            "summary_cards": ["pass_rate", "valid_json_rate"],
            "columns": ["task_id", "success", "error"],
        },
    ),
    BenchmarkModule(
        module_id="prompt-replay",
        label="Prompt Replay",
        status="fixture_ready",
        description="Fixed local prompts for quick regression comparisons.",
        capabilities=["regression", "instruction-following", "local-fixtures"],
        result_schema=["task_id", "success", "error"],
        startable=False,
        setup_requirements=["prompt_replay_data/tasks.jsonl"],
        task_selection={"strategy": "fixture_ids", "fields": ["task_ids"], "default": "all_tasks"},
        scoring={
            "primary_metric": "pass_rate",
            "direction": "higher_is_better",
            "secondary_metrics": ["missing_phrase_rate"],
            "aggregation": "mean_success",
        },
        ui_renderer={
            "kind": "prompt_replay_table",
            "summary_cards": ["pass_rate", "pass_count", "fail_count"],
            "columns": ["task_id", "success", "error"],
        },
    ),
)
STARTABLE_BENCHMARK_TYPES = {module.module_id for module in BENCHMARK_MODULES if module.startable}
BENCHMARK_MODULES_BY_ID = {module.module_id: module for module in BENCHMARK_MODULES}
BENCHMARK_PRESETS: Tuple[BenchmarkPreset, ...] = (
    BenchmarkPreset(
        preset_id="local-smoke",
        label="Local Smoke",
        description="Tiny local run for wiring checks and quick model sanity tests.",
        scope="smoke",
        module_defaults={
            "speed": {
                "repeat_count": 1,
                "warmup_runs": 0,
                "concurrency": 1,
                "max_tokens": 256,
                "timeout_ms": 30000,
            },
            "sql": {
                "question_ids": [1, 2, 3],
                "sql_mode": "tool-calling",
                "thinking_mode": "off",
                "reasoning_effort": "disabled",
                "timeout_ms": 120000,
                "question_timeout_ms": 30000,
            },
        },
    ),
    BenchmarkPreset(
        preset_id="balanced",
        label="Balanced",
        description="Moderate run for comparing local models without full leaderboard cost.",
        scope="comparison",
        module_defaults={
            "speed": {
                "repeat_count": 3,
                "warmup_runs": 1,
                "concurrency": 1,
                "max_tokens": 1024,
                "timeout_ms": 120000,
            },
            "sql": {
                "question_ids": None,
                "sql_mode": "tool-calling",
                "thinking_mode": "both",
                "reasoning_effort": "disabled",
                "timeout_ms": 120000,
                "question_timeout_ms": 60000,
            },
        },
    ),
    BenchmarkPreset(
        preset_id="leaderboard-full",
        label="Leaderboard Full",
        description="Fuller repeated run intended for published comparisons and saved manifests.",
        scope="leaderboard",
        module_defaults={
            "speed": {
                "repeat_count": 5,
                "warmup_runs": 1,
                "concurrency": 1,
                "max_tokens": 4096,
                "timeout_ms": 300000,
            },
            "sql": {
                "question_ids": None,
                "sql_mode": "tool-calling",
                "thinking_mode": "both",
                "reasoning_effort": "disabled",
                "timeout_ms": 300000,
                "question_timeout_ms": 120000,
            },
        },
    ),
)
BENCHMARK_PRESETS_BY_ID = {preset.preset_id: preset for preset in BENCHMARK_PRESETS}


@dataclass
class BenchmarkRequest:
    benchmark_type: str
    base_url: str
    provider: str
    api_key: str
    models: List[str]
    targets: List[BenchmarkTarget]
    mode: str
    concurrency: int
    prompt: str
    max_tokens: int
    temperature: float
    top_p: float
    presence_penalty: float
    frequency_penalty: float
    timeout_ms: int
    repeat_count: int
    warmup_runs: int
    max_concurrent_predictions: Optional[int]
    mtp: Optional[str]
    k_cache_quantization: Optional[str]
    v_cache_quantization: Optional[str]
    batch_size: Optional[int]
    flash_attn: Optional[bool]
    question_ids: Optional[List[int]]
    sql_mode: str
    thinking_mode: str
    reasoning_effort: str
    question_timeout_ms: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BenchmarkRequest":
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")

        benchmark_type = str(data.get("benchmark_type", "speed")).strip().lower() or "speed"
        if benchmark_type not in STARTABLE_BENCHMARK_TYPES:
            allowed = ", ".join(f"'{module_id}'" for module_id in sorted(STARTABLE_BENCHMARK_TYPES))
            raise ValueError(f"benchmark_type must be one of: {allowed}")

        base_url = str(data.get("base_url", "")).strip().rstrip("/")
        targets_raw = data.get("targets")
        targets: List[BenchmarkTarget] = []
        if benchmark_type == "sql" and targets_raw is not None:
            raise ValueError("sql benchmark does not support targets fanout; provide one model")
        if targets_raw is not None:
            if not isinstance(targets_raw, list) or not targets_raw:
                raise ValueError("targets must be a non-empty array")
            for index, raw_target in enumerate(targets_raw, start=1):
                if not isinstance(raw_target, dict):
                    raise ValueError("each target must be an object")
                target_base_url = str(raw_target.get("base_url", "")).strip().rstrip("/")
                if not target_base_url:
                    raise ValueError(f"target {index}: base_url is required")
                if "://" not in target_base_url:
                    target_base_url = f"http://{target_base_url}"
                target_provider = str(raw_target.get("provider_type", raw_target.get("provider", "auto"))).strip().lower() or "auto"
                if target_provider not in {"auto", "openai-compatible", "openai", "ollama"}:
                    raise ValueError("target provider must be one of: auto, openai-compatible, openai, ollama")
                target_models_raw = raw_target.get("models", [])
                if not isinstance(target_models_raw, list) or not target_models_raw:
                    raise ValueError(f"target {index}: models must be a non-empty array")
                target_models = [str(item).strip() for item in target_models_raw if str(item).strip()]
                if not target_models:
                    raise ValueError(f"target {index}: models must contain at least one non-empty model name")
                targets.append(BenchmarkTarget(
                    provider_id=str(raw_target.get("provider_id", "")).strip() or f"target-{index}",
                    provider_label=str(raw_target.get("provider_label", raw_target.get("label", target_base_url))).strip() or target_base_url,
                    base_url=target_base_url,
                    provider=target_provider,
                    api_key=str(raw_target.get("api_key", "")),
                    models=target_models,
                ))
            base_url = targets[0].base_url
        if not base_url:
            raise ValueError("base_url is required")
        if "://" not in base_url:
            base_url = f"http://{base_url}"

        provider = str(data.get("provider", "auto")).strip().lower() or "auto"
        if provider not in {"auto", "openai-compatible", "openai", "ollama"}:
            raise ValueError("provider must be one of: auto, openai-compatible, openai, ollama")

        models_raw = data.get("models")
        model_raw = str(data.get("model", "")).strip()
        if models_raw in (None, "") and model_raw:
            models_raw = [model_raw]
        if targets:
            models = sorted({model for target in targets for model in target.models})
        else:
            if not isinstance(models_raw, list) or not models_raw:
                raise ValueError("models must be a non-empty array")
            models = [str(item).strip() for item in models_raw if str(item).strip()]
            if not models:
                raise ValueError("models must contain at least one non-empty model name")
            targets = [BenchmarkTarget(
                provider_id="legacy-target",
                provider_label=base_url,
                base_url=base_url,
                provider=provider,
                api_key=str(data.get("api_key", "")),
                models=models,
            )]
        mode = str(data.get("mode", "sequential")).strip().lower()
        if mode not in {"sequential", "parallel"}:
            raise ValueError("mode must be 'sequential' or 'parallel'")

        prompt = str(data.get("prompt", ""))
        if benchmark_type == "speed" and not prompt.strip():
            raise ValueError("prompt is required")

        question_ids_raw = data.get("question_ids")
        question_ids: Optional[List[int]] = None
        if question_ids_raw is not None:
            if not isinstance(question_ids_raw, list):
                raise ValueError("question_ids must be an array")
            try:
                question_ids = [int(question_id) for question_id in question_ids_raw]
            except (TypeError, ValueError) as exc:
                raise ValueError("question_ids must contain integers") from exc

        sql_mode = str(data.get("sql_mode", "tool-calling")).strip().lower() or "tool-calling"
        if sql_mode not in {"tool-calling", "grammar"}:
            raise ValueError("sql_mode must be 'tool-calling' or 'grammar'")

        thinking_mode = str(data.get("thinking_mode", "off")).strip().lower() or "off"
        if thinking_mode not in {"off", "on", "both"}:
            raise ValueError("thinking_mode must be 'off', 'on', or 'both'")

        reasoning_effort = str(data.get("reasoning_effort", "disabled")).strip().lower() or "disabled"
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("reasoning_effort must be one of: disabled, none, minimal, low, medium, high, xhigh")

        # Per-question wall-clock budget for SQL tool-calling, excluding model
        # load. 0 (default) disables it. Negative values are treated as 0.
        question_timeout_ms = max(0, int(data.get("question_timeout_ms", 0) or 0))

        concurrency = max(1, int(data.get("concurrency", 1) or 1))
        repeat_count = max(1, int(data.get("repeat_count", 1) or 1))
        warmup_runs = max(0, int(data.get("warmup_runs", 0) or 0))
        timeout_ms = max(1_000, int(data.get("timeout_ms", DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS))
        max_tokens = max(1, int(data.get("max_tokens", 4096) or 4096))

        max_concurrent_predictions = data.get("max_concurrent_predictions")
        max_concurrent_predictions = max(1, int(max_concurrent_predictions)) if max_concurrent_predictions not in (None, "") else None
        mtp = str(data.get("mtp", "")).strip() or None
        k_cache_quantization = str(data.get("k_cache_quantization", "")).strip() or None
        v_cache_quantization = str(data.get("v_cache_quantization", "")).strip() or None
        batch_size = data.get("batch_size")
        batch_size = max(1, int(batch_size)) if batch_size not in (None, "") else None
        flash_attn_raw = data.get("flash_attn")
        flash_attn = None if flash_attn_raw in (None, "") else bool(flash_attn_raw)

        return cls(
            benchmark_type=benchmark_type,
            base_url=base_url,
            provider=provider,
            api_key=str(data.get("api_key", "")),
            models=models,
            targets=targets,
            mode=mode,
            concurrency=concurrency,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=float(data.get("temperature", 0.7) or 0.7),
            top_p=float(data.get("top_p", 1.0) or 1.0),
            presence_penalty=float(data.get("presence_penalty", 0.0) or 0.0),
            frequency_penalty=float(data.get("frequency_penalty", 0.0) or 0.0),
            timeout_ms=timeout_ms,
            repeat_count=repeat_count,
            warmup_runs=warmup_runs,
            max_concurrent_predictions=max_concurrent_predictions,
            mtp=mtp,
            k_cache_quantization=k_cache_quantization,
            v_cache_quantization=v_cache_quantization,
            batch_size=batch_size,
            flash_attn=flash_attn,
            question_ids=question_ids,
            sql_mode=sql_mode,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            question_timeout_ms=question_timeout_ms,
        )


@dataclass
class RunState:
    """Snapshot of a job's in-flight phase, updated atomically.

    All five fields change together; readers must observe a consistent
    snapshot (phase, message, run_index, model, benchmark_type) rather
    than a torn update from a concurrent writer. ``last_event_at`` is a
    monotonic wall-clock for staleness checks.
    """
    phase: str = "queued"
    message: str = "Queued"
    run_index: Optional[int] = None
    benchmark_type: Optional[str] = None
    last_event_at: float = field(default_factory=time.perf_counter)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "message": self.message,
            "run_index": self.run_index,
            "benchmark_type": self.benchmark_type,
            "last_event_at": self.last_event_at,
        }


@dataclass
class JobState:
    request: BenchmarkRequest
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "queued"
    created_at: str = field(default_factory=ts_utc)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    stop_requested: bool = False
    progress_total: int = 0
    progress_completed: int = 0
    current_model: Optional[str] = None
    current_provider_id: Optional[str] = None
    current_provider_label: Optional[str] = None
    run_state: RunState = field(default_factory=RunState)
    _phase_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _pending_saves: "set[asyncio.Task]" = field(default_factory=set)
    _aggregates_cache: Optional[Tuple[int, Any, List[Dict[str, Any]]]] = None
    results: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    report_text: str = ""
    task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        base = {
            "job_id": self.job_id,
            "benchmark_type": self.request.benchmark_type,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stop_requested": self.stop_requested,
            "progress": {
                "completed": self.progress_completed,
                "total": self.progress_total,
                "current_model": self.current_model,
                "current_provider_id": self.current_provider_id,
                "current_provider_label": self.current_provider_label,
                "current_phase": self.run_state.phase,
                "current_message": self.run_state.message,
                "current_run_index": self.run_state.run_index,
                "current_benchmark_type": self.run_state.benchmark_type or self.request.benchmark_type,
            },
            "request": {
                "benchmark_type": self.request.benchmark_type,
                "base_url": self.request.base_url,
                "provider": self.request.provider,
                "models": self.request.models,
                "question_ids": self.request.question_ids,
                "sql_mode": self.request.sql_mode,
                "thinking_mode": self.request.thinking_mode,
                "reasoning_effort": self.request.reasoning_effort,
                "question_timeout_ms": self.request.question_timeout_ms,
            },
            "results": self.results,
            "errors": self.errors,
            "report_text": self.report_text,
        }
        if self.request.benchmark_type == "speed":
            base["aggregated_speed"] = self._aggregated_speed()
        return base

    def _aggregated_speed(self) -> List[Dict[str, Any]]:
        """Cached aggregate stats; invalidated when results change.

        Cache key is ``(len(results), last_result_timestamp)``. ``to_dict``
        is called on every poll, but ``_compute_speed_aggregates`` walks
        every row; on a 20-model × 5-run job that's 100 rows per tick.
        """
        n = len(self.results)
        last_ts = self.results[-1].get("timestamp") if self.results else None
        if self._aggregates_cache is not None and self._aggregates_cache[0] == n and self._aggregates_cache[1] == last_ts:
            return self._aggregates_cache[2]
        agg = _compute_speed_aggregates(self.results)
        self._aggregates_cache = (n, last_ts, agg)
        return agg

    async def set_phase(
        self,
        phase: str,
        message: str,
        *,
        model: Optional[str] = None,
        run_index: Optional[int] = None,
        benchmark_type: Optional[str] = None,
    ) -> RunState:
        if model is not None:
            self.current_model = model
        new_state = RunState(
            phase=phase,
            message=message,
            run_index=run_index,
            benchmark_type=benchmark_type or self.request.benchmark_type,
            last_event_at=time.perf_counter(),
        )
        async with self._phase_lock:
            self.run_state = new_state
        return new_state

    def get_phase_snapshot(self) -> RunState:
        """Return the current RunState without acquiring the lock.

        Reference assignment in CPython is atomic, so the caller sees
        a fully-constructed RunState from a single prior `set_phase`.
        """
        return self.run_state

    def track_save(self, task: "asyncio.Task") -> None:
        """Register a fire-and-forget save task so it can be awaited on shutdown."""
        self._pending_saves.add(task)
        task.add_done_callback(self._pending_saves.discard)

    async def drain_pending_saves(self) -> None:
        """Wait for any in-flight save tasks to finish.

        Called from the job's ``finally`` and from the server's shutdown
        hook so a SIGTERM does not drop the last incremental write.
        """
        if not self._pending_saves:
            return
        pending = list(self._pending_saves)
        await asyncio.gather(*pending, return_exceptions=True)
