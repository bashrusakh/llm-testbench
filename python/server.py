#!/usr/bin/env python3
"""LLM Testbench server."""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import csv
import hashlib
import io
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from aiohttp import web

from python.sql_benchmark import SqlBenchmarkRunner, ToolLlmCallback
from python.adapter import ADAPTER_REGISTRY, get_adapter
from python.local_benchmarks import LOCAL_FIXTURE_SPECS, load_local_tasks, validate_local_fixtures

LOG = logging.getLogger("llm_testbench")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "index.html"
SQL_BENCHMARK_DATA_DIR = PROJECT_ROOT / "sql_benchmark_data"
DEFAULT_ALLOWED_ORIGINS = {"*"}
DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_PORT_CANDIDATES = [1234, 8080, 11434, 5000, 5001]
DEFAULT_HOST_CANDIDATES = ["127.0.0.1", "localhost"]
LOCAL_SCAN_CONNECT_TIMEOUT_S = 0.5
LOCAL_SCAN_READ_TIMEOUT_S = .5
REASONING_EFFORTS = {"disabled", "none", "minimal", "low", "medium", "high", "xhigh"}
REASONING_FALLBACK_STATE: contextvars.ContextVar[Optional[Dict[str, bool]]] = contextvars.ContextVar(
    "reasoning_fallback_state",
    default=None,
)
OPENAI_MODELS_PATH = "/v1/models"
OPENAI_CHAT_PATH = "/v1/chat/completions"
OLLAMA_TAGS_PATH = "/api/tags"
OLLAMA_CHAT_PATH = "/api/chat"
API_CONTRACT_VERSION = 1
MODULE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
EXPORT_SCHEMA_VERSION = 1
ADAPTER_LIFECYCLE_HOOKS = ["prepare", "select_tasks", "run_task", "score", "render", "cleanup"]

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
        if benchmark_type == "sql" and len(models) < 1:
            raise ValueError(f"{benchmark_type} benchmark requires at least one model")

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
        max_tokens = max(1, int(data.get("max_tokens", 65536) or 65536))

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
class JobState:
    request: BenchmarkRequest
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "queued"
    created_at: str = field(default_factory=lambda: ts_utc())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    stop_requested: bool = False
    progress_total: int = 0
    progress_completed: int = 0
    current_model: Optional[str] = None
    current_phase: str = "queued"
    current_message: str = "Queued"
    current_run_index: Optional[int] = None
    current_benchmark_type: Optional[str] = None
    results: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    report_text: str = ""
    task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
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
                "current_provider_id": getattr(self, "current_provider_id", None),
                "current_provider_label": getattr(self, "current_provider_label", None),
                "current_phase": self.current_phase,
                "current_message": self.current_message,
                "current_run_index": self.current_run_index,
                "current_benchmark_type": self.current_benchmark_type or self.request.benchmark_type,
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

    def set_phase(
        self,
        phase: str,
        message: str,
        *,
        model: Optional[str] = None,
        run_index: Optional[int] = None,
        benchmark_type: Optional[str] = None,
    ) -> None:
        self.current_phase = phase
        self.current_message = message
        self.current_run_index = run_index
        self.current_benchmark_type = benchmark_type or self.request.benchmark_type
        if model is not None:
            self.current_model = model


def ts_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cors_middleware(allowed_origins: set[str] = DEFAULT_ALLOWED_ORIGINS):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
        origin = request.headers.get("Origin")
        if origin in allowed_origins or "*" in allowed_origins or origin is None:
            response.headers["Access-Control-Allow-Origin"] = origin if (origin and "*" not in allowed_origins) else "*"
            if "*" not in allowed_origins:
                response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Credentials"] = "false"
        return response

    return middleware


# Base for inverted-timestamp filename prefixes. 9_999_999_999_999 ms is ~year
# 2286, comfortably beyond any real run, so (BASE - epoch_ms) stays positive and
# 13 digits wide. Newer runs have a smaller prefix, so a plain ascending sort of
# filenames (the OS/explorer default) lists newest first.
_FILENAME_TS_BASE = 9_999_999_999_999


def _inverted_prefix(created_at_iso: Optional[str]) -> str:
    """Return a zero-padded inverted-timestamp prefix for ordering filenames."""
    epoch_ms = 0
    if created_at_iso:
        try:
            dt = datetime.fromisoformat(created_at_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            epoch_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            epoch_ms = 0
    inverted = _FILENAME_TS_BASE - epoch_ms
    if inverted < 0:
        inverted = 0
    return f"{inverted:013d}"


def _reasoning_payload_value(reasoning_effort: str) -> Optional[str]:
    effort = (reasoning_effort or "disabled").strip().lower()
    if effort in {"", "disabled"}:
        return None
    return effort


def _with_reasoning(payload: Dict[str, Any], reasoning_effort: str) -> Dict[str, Any]:
    effort = _reasoning_payload_value(reasoning_effort)
    if not effort:
        return payload
    enriched = dict(payload)
    enriched["reasoning"] = {"effort": effort}
    return enriched


def _looks_like_reasoning_rejection(status_code: int, body_text: str) -> bool:
    if status_code not in (400, 422):
        return False
    body_lower = body_text.lower()
    if "reasoning" in body_lower or "reasoning_effort" in body_lower:
        return True
    return "effort" in body_lower and any(
        keyword in body_lower
        for keyword in ("unknown", "unsupported", "not support", "extra", "invalid", "unrecognized")
    )


class BenchmarkServer:
    def __init__(self, index_html_path: Path) -> None:
        self.index_html_path = index_html_path
        self.jobs: Dict[str, JobState] = {}
        self.results_store_dir = PROJECT_ROOT / "benchmarks"
        self._results_lock = asyncio.Lock()

    async def _post_openai_chat_with_reasoning_fallback(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: Dict[str, Any],
        *,
        reasoning_effort: str,
        model: str,
        fallback_state: Optional[Dict[str, bool]] = None,
    ) -> httpx.Response:
        request_payload = _with_reasoning(payload, reasoning_effort)
        response = await client.post(url, json=request_payload)
        if "reasoning" not in request_payload:
            return response

        body_text = response.text if response.status_code in (400, 422) else ""
        if not _looks_like_reasoning_rejection(response.status_code, body_text):
            return response

        LOG.warning(
            "Model %s rejected reasoning effort %s (%s) — retrying without reasoning",
            model,
            reasoning_effort,
            response.status_code,
        )
        if fallback_state is None:
            fallback_state = REASONING_FALLBACK_STATE.get()
        if fallback_state is not None:
            fallback_state["used"] = True
        return await client.post(url, json=payload)

    def _record_filename(self, job_id: str, created_at_iso: Optional[str]) -> str:
        """Filename for a job record: '<inverted_ts>_<job_id>.json'."""
        return f"{_inverted_prefix(created_at_iso)}_{job_id}.json"

    def _find_record_path(self, job_id: str) -> Optional[Path]:
        """Locate an existing record file for job_id (new scheme or legacy)."""
        results_dir = self.results_store_dir
        if not results_dir.exists():
            return None
        # New scheme: <prefix>_<job_id>.json
        matches = list(results_dir.glob(f"*_{job_id}.json"))
        if matches:
            return matches[0]
        # Legacy scheme: <job_id>.json
        legacy = results_dir / f"{job_id}.json"
        return legacy if legacy.exists() else None

    async def _load_results_store(self) -> List[Dict[str, Any]]:
        async with self._results_lock:
            results_dir = self.results_store_dir
            if not results_dir.exists():
                return []
            try:
                paths = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                return []
            items: List[Dict[str, Any]] = []
            for path in paths:
                try:
                    content = await asyncio.to_thread(path.read_text, "utf-8")
                    data = json.loads(content)
                    if isinstance(data, dict):
                        items.append(data)
                except Exception as exc:
                    LOG.warning("Failed reading result file %s: %s", path, exc)
            return items

    async def migrate_legacy_filenames(self) -> int:
        """Rename legacy '<job_id>.json' records to the inverted-timestamp scheme.

        Run once at startup so old runs sort correctly alongside new ones. A
        legacy file is one whose stem is a bare job_id (no inverted prefix).
        """
        results_dir = self.results_store_dir
        migrated = 0
        async with self._results_lock:
            if not results_dir.exists():
                return 0
            for path in list(results_dir.glob("*.json")):
                name = path.name
                # New-scheme files start with a 13-digit prefix + '_'. Skip those.
                stem = name[:-5]  # strip '.json'
                prefix = stem.split("_", 1)[0]
                if len(prefix) == 13 and prefix.isdigit():
                    continue
                try:
                    data = json.loads(await asyncio.to_thread(path.read_text, "utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                job_id = data.get("job_id") or stem
                new_name = self._record_filename(job_id, data.get("created_at"))
                if new_name == name:
                    continue
                target = results_dir / new_name
                try:
                    if target.exists():
                        path.unlink()  # a new-scheme copy already exists
                    else:
                        path.rename(target)
                    migrated += 1
                except OSError as exc:
                    LOG.warning("Failed migrating legacy record %s: %s", path, exc)
        if migrated:
            LOG.info("Migrated %d legacy benchmark filename(s) to timestamp scheme", migrated)
        return migrated

    async def reconcile_stale_records(self) -> int:
        """Mark on-disk records stuck in a live status as 'interrupted'.

        After a server restart, in-memory jobs are gone but their last-flushed
        disk record may still say 'running' (the finalizing `finally` never ran).
        Such records would otherwise show as a forever-running, unstoppable job in
        history. We rewrite them to 'interrupted' once at startup. Only records
        with no corresponding live in-memory job are touched.
        """
        live_statuses = {"queued", "running", "stopping"}
        results_dir = self.results_store_dir
        fixed = 0
        async with self._results_lock:
            if not results_dir.exists():
                return 0
            for path in results_dir.glob("*.json"):
                try:
                    content = await asyncio.to_thread(path.read_text, "utf-8")
                    data = json.loads(content)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                job_id = data.get("job_id")
                if data.get("status") in live_statuses and job_id not in self.jobs:
                    data["status"] = "interrupted"
                    if not data.get("finished_at"):
                        data["finished_at"] = ts_utc()
                    try:
                        await asyncio.to_thread(
                            path.write_text, json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
                        )
                        fixed += 1
                    except OSError as exc:
                        LOG.warning("Failed reconciling stale record %s: %s", path, exc)
        if fixed:
            LOG.info("Reconciled %d stale 'running' record(s) -> 'interrupted'", fixed)
        return fixed

    async def _save_job_record(self, job_id: str, record: Dict[str, Any]) -> None:
        async with self._results_lock:
            results_dir = self.results_store_dir
            results_dir.mkdir(parents=True, exist_ok=True)
            filename = self._record_filename(job_id, record.get("created_at"))
            path = results_dir / filename
            # Remove any stale file for this job under a different name (legacy
            # <job_id>.json or an older prefix), so incremental saves don't leave
            # duplicates behind.
            for other in results_dir.glob(f"*_{job_id}.json"):
                if other.name != filename:
                    try: other.unlink()
                    except OSError: pass
            legacy = results_dir / f"{job_id}.json"
            if legacy.exists() and legacy.name != filename:
                try: legacy.unlink()
                except OSError: pass
            payload = json.dumps(record, ensure_ascii=False, indent=2)
            await asyncio.to_thread(path.write_text, payload, "utf-8")

    async def _flush_job_record(self, job: JobState) -> None:
        """Write current job state to disk (called after each question for incremental saves)."""
        await self._append_job_to_results_store(job)

    async def _append_job_to_results_store(self, job: JobState) -> None:
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "status": job.status,
            "request": {
                "benchmark_type": job.request.benchmark_type,
                "base_url": job.request.base_url,
                "provider": job.request.provider,
                "models": job.request.models,
                "mode": job.request.mode,
                "concurrency": job.request.concurrency,
                "max_tokens": job.request.max_tokens,
                "temperature": job.request.temperature,
                "top_p": job.request.top_p,
                "presence_penalty": job.request.presence_penalty,
                "frequency_penalty": job.request.frequency_penalty,
                "timeout_ms": job.request.timeout_ms,
                "repeat_count": job.request.repeat_count,
                "warmup_runs": job.request.warmup_runs,
                "max_concurrent_predictions": job.request.max_concurrent_predictions,
                "mtp": job.request.mtp,
                "k_cache_quantization": job.request.k_cache_quantization,
                "v_cache_quantization": job.request.v_cache_quantization,
                "batch_size": job.request.batch_size,
                "flash_attn": job.request.flash_attn,
                "question_ids": job.request.question_ids,
                "sql_mode": job.request.sql_mode,
                "thinking_mode": job.request.thinking_mode,
                "reasoning_effort": job.request.reasoning_effort,
                "question_timeout_ms": job.request.question_timeout_ms,
            },
            "progress": {
                "completed": job.progress_completed,
                "total": job.progress_total,
                "current_model": job.current_model,
                "current_provider_id": getattr(job, "current_provider_id", None),
                "current_provider_label": getattr(job, "current_provider_label", None),
            },
            "results": job.results,
            "errors": job.errors,
            "report_text": job.report_text,
        }
        await self._save_job_record(job.job_id, record)

    @staticmethod
    def _build_results_jsonl(record: Dict[str, Any]) -> str:
        request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
        progress_meta = record.get("progress") if isinstance(record.get("progress"), dict) else {}
        results = record.get("results") if isinstance(record.get("results"), list) else []
        lines: List[str] = []
        for index, result in enumerate(results):
            row = {
                "job_id": record.get("job_id"),
                "status": record.get("status"),
                "created_at": record.get("created_at"),
                "started_at": record.get("started_at"),
                "finished_at": record.get("finished_at"),
                "benchmark_type": request_meta.get("benchmark_type") or (result.get("benchmark_type") if isinstance(result, dict) else None),
                "request": request_meta,
                "progress": progress_meta,
                "result_index": index,
                "result": result,
            }
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _build_results_table(record: Dict[str, Any], *, delimiter: str = ",") -> str:
        request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
        results = record.get("results") if isinstance(record.get("results"), list) else []
        fieldnames = [
            "job_id",
            "status",
            "benchmark_type",
            "result_index",
            "model",
            "provider",
            "provider_label",
            "endpoint",
            "outcome",
            "success",
            "error",
            "latency_s",
            "total_time_s",
            "prompt_tokens",
            "completion_tokens",
            "tokens_per_second",
            "question_id",
            "difficulty",
            "thinking_mode",
            "reasoning_effort",
            "reasoning_fallback",
            "generated_sql",
            "expected_sql",
            "result_json",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, delimiter=delimiter, lineterminator="\n")
        writer.writeheader()
        for index, result in enumerate(results):
            item = result if isinstance(result, dict) else {}
            writer.writerow({
                "job_id": record.get("job_id"),
                "status": record.get("status"),
                "benchmark_type": request_meta.get("benchmark_type") or item.get("benchmark_type"),
                "result_index": index,
                "model": item.get("model"),
                "provider": item.get("provider"),
                "provider_label": item.get("provider_label"),
                "endpoint": item.get("endpoint"),
                "outcome": item.get("outcome") or item.get("status"),
                "success": item.get("success"),
                "error": item.get("error"),
                "latency_s": item.get("latency_s") or item.get("latency"),
                "total_time_s": item.get("total_time_s") or item.get("total_time"),
                "prompt_tokens": item.get("prompt_tokens") or item.get("input_tokens"),
                "completion_tokens": item.get("completion_tokens") or item.get("output_tokens"),
                "tokens_per_second": item.get("tokens_per_second"),
                "question_id": item.get("question_id"),
                "difficulty": item.get("difficulty"),
                "thinking_mode": item.get("thinking_mode"),
                "reasoning_effort": item.get("reasoning_effort"),
                "reasoning_fallback": item.get("reasoning_fallback"),
                "generated_sql": item.get("generated_sql"),
                "expected_sql": item.get("expected_sql"),
                "result_json": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            })
        return buffer.getvalue()

    @staticmethod
    def _build_results_csv(record: Dict[str, Any]) -> str:
        return BenchmarkServer._build_results_table(record, delimiter=",")

    @staticmethod
    def _build_results_tsv(record: Dict[str, Any]) -> str:
        return BenchmarkServer._build_results_table(record, delimiter="\t")

    @staticmethod
    def _build_run_manifest(record: Dict[str, Any]) -> str:
        request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
        progress_meta = record.get("progress") if isinstance(record.get("progress"), dict) else {}
        results = record.get("results") if isinstance(record.get("results"), list) else []
        errors = record.get("errors") if isinstance(record.get("errors"), list) else []
        models = sorted({
            str(item.get("model"))
            for item in results
            if isinstance(item, dict) and item.get("model")
        })
        providers = sorted({
            str(item.get("provider_label") or item.get("provider") or item.get("endpoint"))
            for item in results
            if isinstance(item, dict) and (item.get("provider_label") or item.get("provider") or item.get("endpoint"))
        })
        outcomes: Dict[str, int] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            key = str(item.get("outcome") or item.get("status") or "unknown")
            outcomes[key] = outcomes.get(key, 0) + 1
        manifest = {
            "schema_version": 1,
            "generated_at": ts_utc(),
            "job_id": record.get("job_id"),
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "benchmark_type": request_meta.get("benchmark_type"),
            "request": request_meta,
            "progress": progress_meta,
            "result_count": len(results),
            "error_count": len(errors),
            "models": models,
            "providers": providers,
            "outcomes": outcomes,
            "export_endpoints": {
                "jsonl": f"/api/benchmark/{record.get('job_id')}/results.jsonl",
                "csv": f"/api/benchmark/{record.get('job_id')}/results.csv",
                "tsv": f"/api/benchmark/{record.get('job_id')}/results.tsv",
                "manifest": f"/api/benchmark/{record.get('job_id')}/manifest.json",
                "summary": f"/api/benchmark/{record.get('job_id')}/summary.json",
            },
        }
        return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _build_run_summary(record: Dict[str, Any]) -> str:
        def number(value: Any) -> Optional[float]:
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def first_number(*values: Any) -> Optional[float]:
            for value in values:
                parsed = number(value)
                if parsed is not None:
                    return parsed
            return None

        def average(values: List[float]) -> Optional[float]:
            if not values:
                return None
            return round(sum(values) / len(values), 4)

        request_meta = record.get("request") if isinstance(record.get("request"), dict) else {}
        progress_meta = record.get("progress") if isinstance(record.get("progress"), dict) else {}
        results = record.get("results") if isinstance(record.get("results"), list) else []
        errors = record.get("errors") if isinstance(record.get("errors"), list) else []
        rows = [item for item in results if isinstance(item, dict)]

        successes = [item for item in rows if item.get("success") is True]
        failures = [item for item in rows if item.get("success") is False]
        latency_values = [
            value
            for value in (first_number(item.get("latency_ms"), item.get("latency_s")) for item in rows)
            if value is not None
        ]
        total_time_values = [
            value
            for value in (first_number(item.get("total_time_ms"), item.get("total_time_s")) for item in rows)
            if value is not None
        ]
        ttft_values = [
            value
            for value in (number(item.get("ttft_ms")) for item in rows)
            if value is not None
        ]
        decode_tps_values = [
            value
            for value in (first_number(item.get("decode_tps"), item.get("tokens_per_second")) for item in rows)
            if value is not None
        ]
        total_prompt_tokens = sum(
            int(value)
            for value in (first_number(item.get("prompt_tokens"), item.get("input_tokens")) for item in rows)
            if value is not None
        )
        total_completion_tokens = sum(
            int(value)
            for value in (first_number(item.get("completion_tokens"), item.get("output_tokens")) for item in rows)
            if value is not None
        )
        total_cost = round(sum(
            value
            for value in (number(item.get("cost")) for item in rows)
            if value is not None
        ), 8)

        by_model: Dict[str, Dict[str, Any]] = {}
        for item in rows:
            model = str(item.get("model") or "unknown")
            bucket = by_model.setdefault(model, {
                "count": 0,
                "pass_count": 0,
                "fail_count": 0,
                "latency_ms_values": [],
                "total_cost": 0.0,
            })
            bucket["count"] += 1
            if item.get("success") is True:
                bucket["pass_count"] += 1
            elif item.get("success") is False:
                bucket["fail_count"] += 1
            latency = first_number(item.get("latency_ms"), item.get("latency_s"))
            if latency is not None:
                bucket["latency_ms_values"].append(latency)
            cost = number(item.get("cost"))
            if cost is not None:
                bucket["total_cost"] += cost

        model_summary: Dict[str, Dict[str, Any]] = {}
        for model, bucket in sorted(by_model.items()):
            count = bucket["count"]
            model_summary[model] = {
                "count": count,
                "pass_count": bucket["pass_count"],
                "fail_count": bucket["fail_count"],
                "pass_rate": round(bucket["pass_count"] / count, 4) if count else None,
                "avg_latency_ms": average(bucket["latency_ms_values"]),
                "total_cost": round(bucket["total_cost"], 8),
            }

        result_count = len(rows)
        summary = {
            "schema_version": 1,
            "generated_at": ts_utc(),
            "job_id": record.get("job_id"),
            "status": record.get("status"),
            "benchmark_type": request_meta.get("benchmark_type"),
            "request": request_meta,
            "progress": progress_meta,
            "result_count": result_count,
            "pass_count": len(successes),
            "fail_count": len(failures),
            "pass_rate": round(len(successes) / result_count, 4) if result_count else None,
            "error_count": len(errors),
            "latency": {
                "avg_latency_ms": average(latency_values),
                "avg_total_time_ms": average(total_time_values),
                "avg_ttft_ms": average(ttft_values),
                "avg_decode_tps": average(decode_tps_values),
            },
            "tokens": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
            },
            "cost": {
                "total": total_cost,
            },
            "models": model_summary,
        }
        return json.dumps(summary, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def json_error(message: str, *, code: str = "bad_request", status: int = 400) -> web.Response:
        return web.json_response(
            {
                "status": "error",
                "error": {"code": code, "message": message},
                "timestamp": ts_utc(),
            },
            status=status,
        )

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "timestamp": ts_utc()})

    async def benchmark_modules(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "modules": [module.to_dict() for module in BENCHMARK_MODULES],
            "startable": sorted(STARTABLE_BENCHMARK_TYPES),
            "timestamp": ts_utc(),
        })

    async def benchmark_module_detail(self, request: web.Request) -> web.Response:
        module_id = str(request.match_info["module_id"]).strip().lower()
        module = BENCHMARK_MODULES_BY_ID.get(module_id)
        if module is None:
            return self.json_error("Unknown benchmark module", code="not_found", status=404)
        return web.json_response({
            "status": "ok",
            "module": module.to_dict(),
            "timestamp": ts_utc(),
        })

    async def benchmark_contract(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "contract_version": API_CONTRACT_VERSION,
            "schema_versions": {
                "module": MODULE_SCHEMA_VERSION,
                "result": RESULT_SCHEMA_VERSION,
                "export": EXPORT_SCHEMA_VERSION,
            },
            "adapter_lifecycle_hooks": ADAPTER_LIFECYCLE_HOOKS,
            "modules": {
                "count": len(BENCHMARK_MODULES),
                "startable": sorted(STARTABLE_BENCHMARK_TYPES),
                "planned": sorted(module.module_id for module in BENCHMARK_MODULES if module.status == "planned"),
                "adapter_implemented": sorted(ADAPTER_REGISTRY.keys()),
            },
            "presets": {
                "count": len(BENCHMARK_PRESETS),
                "ids": [preset.preset_id for preset in BENCHMARK_PRESETS],
                "scopes": sorted({preset.scope for preset in BENCHMARK_PRESETS}),
            },
            "endpoints": {
                "modules": "/api/benchmark/modules",
                "module_detail": "/api/benchmark/modules/{module_id}",
                "module_adapter": "/api/benchmark/modules/{module_id}/adapter",
                "presets": "/api/benchmark/presets",
                "preset_detail": "/api/benchmark/presets/{preset_id}",
                "start": "/api/benchmark/start",
                "active": "/api/benchmark/active",
                "results": "/api/benchmark/results",
                "summaries": "/api/benchmark/summaries",
                "dashboard": "/api/benchmark/dashboard",
                "fixtures": "/api/fixtures",
                "fixture_validation": "/api/fixtures/validate",
                "status": "/api/benchmark/{job_id}",
                "stop": "/api/benchmark/{job_id}/stop",
            },
            "exports": {
                "jsonl": "/api/benchmark/{job_id}/results.jsonl",
                "csv": "/api/benchmark/{job_id}/results.csv",
                "tsv": "/api/benchmark/{job_id}/results.tsv",
                "summary": "/api/benchmark/{job_id}/summary.json",
                "manifest": "/api/benchmark/{job_id}/manifest.json",
            },
            "timestamp": ts_utc(),
        })

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path.name}:{line_number}: expected JSON object")
            rows.append(item)
        return rows

    def _build_fixture_manifest(self) -> Dict[str, Any]:
        sql_questions_path = SQL_BENCHMARK_DATA_DIR / "questions.json"

        def sql_questions_from_loaded(loaded: Any) -> List[Dict[str, Any]]:
            if isinstance(loaded, list):
                return [item for item in loaded if isinstance(item, dict)]
            if isinstance(loaded, dict) and isinstance(loaded.get("questions"), list):
                return [item for item in loaded["questions"] if isinstance(item, dict)]
            return []

        sql_questions: List[Dict[str, Any]] = []
        if sql_questions_path.exists():
            loaded = json.loads(sql_questions_path.read_text("utf-8"))
            sql_questions = sql_questions_from_loaded(loaded)

        fixtures = {
            "sql": {
                "path": str(SQL_BENCHMARK_DATA_DIR.relative_to(PROJECT_ROOT)),
                "questions_file": "questions.json",
                "task_count": len(sql_questions),
                "local_only": True,
            },
        }
        for module_id, spec in LOCAL_FIXTURE_SPECS.items():
            tasks = load_local_tasks(PROJECT_ROOT, module_id)
            fixture_path = PROJECT_ROOT / str(spec["path"])
            fixtures[module_id] = {
                "path": str(fixture_path.parent.relative_to(PROJECT_ROOT)),
                "tasks_file": fixture_path.name,
                "task_count": len(tasks),
                "local_only": True,
            }

        return {
            "schema_version": 1,
            "fixtures": fixtures,
        }

    def _validate_fixtures(self) -> Dict[str, Any]:
        errors: List[str] = []
        manifest = self._build_fixture_manifest()

        sql_path = SQL_BENCHMARK_DATA_DIR / "questions.json"
        if not sql_path.exists():
            errors.append("sql: missing questions.json")
        else:
            try:
                loaded = json.loads(sql_path.read_text("utf-8"))
                questions = loaded if isinstance(loaded, list) else loaded.get("questions") if isinstance(loaded, dict) else None
                if not isinstance(questions, list):
                    errors.append("sql: questions.json must be an array or object with questions array")
                for item in questions or []:
                    if not isinstance(item, dict) or not {"id", "question", "sql"}.issubset(item):
                        errors.append("sql: each question needs id, question, and sql")
                        break
            except Exception as exc:
                errors.append(f"sql: {exc}")

        errors.extend(validate_local_fixtures(PROJECT_ROOT))

        return {
            "ok": not errors,
            "errors": errors,
            "manifest": manifest,
        }

    async def fixture_manifest(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "manifest": self._build_fixture_manifest(), "timestamp": ts_utc()})

    async def fixture_validation(self, _request: web.Request) -> web.Response:
        validation = self._validate_fixtures()
        return web.json_response({"status": "ok" if validation["ok"] else "error", "validation": validation, "timestamp": ts_utc()})

    async def benchmark_presets(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "presets": [preset.to_dict() for preset in BENCHMARK_PRESETS],
            "timestamp": ts_utc(),
        })

    async def benchmark_preset_detail(self, request: web.Request) -> web.Response:
        preset_id = str(request.match_info["preset_id"]).strip().lower()
        preset = BENCHMARK_PRESETS_BY_ID.get(preset_id)
        if preset is None:
            return self.json_error("Unknown benchmark preset", code="not_found", status=404)
        return web.json_response({
            "status": "ok",
            "preset": preset.to_dict(),
            "timestamp": ts_utc(),
        })

    async def benchmark_adapter_detail(self, request: web.Request) -> web.Response:
        """Return the concrete adapter description for a module, or a planned stub."""
        module_id = str(request.match_info["module_id"]).strip().lower()
        module = BENCHMARK_MODULES_BY_ID.get(module_id)
        if module is None:
            return self.json_error("Unknown benchmark module", code="not_found", status=404)
        adapter = get_adapter(module_id)
        if adapter is None:
            return web.json_response({
                "status": "ok",
                "module_id": module_id,
                "adapter": {
                    "module_id": module_id,
                    "hooks": ["prepare", "select_tasks", "run_task", "score", "render", "cleanup"],
                    "status": "planned_adapter",
                    "class": None,
                },
                "timestamp": ts_utc(),
            })
        return web.json_response({
            "status": "ok",
            "module_id": module_id,
            "adapter": adapter.describe(),
            "timestamp": ts_utc(),
        })

    async def benchmark_dashboard(self, _request: web.Request) -> web.Response:
        """Aggregate pass-rate, latency, token, and cost metrics across all saved runs.

        Optional query parameters:
          - module: filter by benchmark_type (e.g. "sql", "speed")
          - model:  filter by model name
          - since:  ISO-8601 timestamp; only include runs created at or after this time

        Response shape::

            {
              "status": "ok",
              "dashboard": {
                "total_runs": <int>,
                "by_module": {
                  "<module_id>": {
                    "run_count": <int>,
                    "result_count": <int>,
                    "pass_count": <int>,
                    "fail_count": <int>,
                    "pass_rate": <float|null>,
                    "avg_latency_ms": <float|null>,
                    "total_cost": <float>,
                    "total_tokens": <int>,
                    "by_model": {
                      "<model>": { pass_rate, avg_latency_ms, total_cost, count, pass_count }
                    }
                  }
                }
              }
            }
        """
        from urllib.parse import parse_qs
        qs = dict(_request.rel_url.query)
        filter_module = qs.get("module", "").strip().lower() or None
        filter_model = qs.get("model", "").strip() or None
        filter_since = qs.get("since", "").strip() or None

        items = await self._load_results_store()

        def _avg(values: list) -> Optional[float]:
            return round(sum(values) / len(values), 4) if values else None

        def _number(v: Any) -> Optional[float]:
            if v in (None, ""):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        # Bucket structure: by_module[module_id][model] -> {counts, values}
        by_module: Dict[str, Dict[str, Any]] = {}
        total_runs = 0

        for record in items:
            if not isinstance(record, dict):
                continue
            request_meta = record.get("request") or {}
            module_id = str(request_meta.get("benchmark_type") or "unknown").lower()
            created_at = record.get("created_at") or ""

            # Apply filters
            if filter_module and module_id != filter_module:
                continue
            if filter_since and created_at and created_at < filter_since:
                continue

            results = record.get("results") or []
            filtered_results = [
                row
                for row in results
                if isinstance(row, dict)
                and (not filter_model or str(row.get("model") or "unknown") == filter_model)
            ]
            if filter_model and not filtered_results:
                continue

            total_runs += 1

            module_bucket = by_module.setdefault(module_id, {
                "run_count": 0,
                "result_count": 0,
                "pass_count": 0,
                "fail_count": 0,
                "_latency_values": [],
                "_cost_sum": 0.0,
                "_token_sum": 0,
                "by_model": {},
            })
            module_bucket["run_count"] += 1

            for row in filtered_results:
                model = str(row.get("model") or "unknown")

                module_bucket["result_count"] += 1
                success = row.get("success")
                if success is True:
                    module_bucket["pass_count"] += 1
                elif success is False:
                    module_bucket["fail_count"] += 1

                latency = _number(row.get("latency_ms")) or _number(row.get("latency_s"))
                if latency is not None:
                    module_bucket["_latency_values"].append(latency)

                cost = _number(row.get("cost"))
                if cost is not None:
                    module_bucket["_cost_sum"] += cost

                prompt_t = _number(row.get("prompt_tokens") or row.get("input_tokens")) or 0.0
                compl_t = _number(row.get("completion_tokens") or row.get("output_tokens")) or 0.0
                module_bucket["_token_sum"] += int(prompt_t + compl_t)

                model_bucket = module_bucket["by_model"].setdefault(model, {
                    "count": 0,
                    "pass_count": 0,
                    "fail_count": 0,
                    "_latency_values": [],
                    "_cost_sum": 0.0,
                })
                model_bucket["count"] += 1
                if success is True:
                    model_bucket["pass_count"] += 1
                elif success is False:
                    model_bucket["fail_count"] += 1
                if latency is not None:
                    model_bucket["_latency_values"].append(latency)
                cost2 = _number(row.get("cost"))
                if cost2 is not None:
                    model_bucket["_cost_sum"] += cost2

        # Finalise — collapse private accumulator fields into public ones
        dashboard_modules: Dict[str, Any] = {}
        for module_id, mb in by_module.items():
            rc = mb["result_count"]
            by_model_out: Dict[str, Any] = {}
            for model, model_b in sorted(mb["by_model"].items()):
                mc = model_b["count"]
                by_model_out[model] = {
                    "count": mc,
                    "pass_count": model_b["pass_count"],
                    "fail_count": model_b["fail_count"],
                    "pass_rate": round(model_b["pass_count"] / mc, 4) if mc else None,
                    "avg_latency_ms": _avg(model_b["_latency_values"]),
                    "total_cost": round(model_b["_cost_sum"], 8),
                }
            dashboard_modules[module_id] = {
                "run_count": mb["run_count"],
                "result_count": rc,
                "pass_count": mb["pass_count"],
                "fail_count": mb["fail_count"],
                "pass_rate": round(mb["pass_count"] / rc, 4) if rc else None,
                "avg_latency_ms": _avg(mb["_latency_values"]),
                "total_cost": round(mb["_cost_sum"], 8),
                "total_tokens": mb["_token_sum"],
                "by_model": by_model_out,
            }

        return web.json_response({
            "status": "ok",
            "dashboard": {
                "total_runs": total_runs,
                "by_module": dashboard_modules,
            },
            "timestamp": ts_utc(),
        })

    async def index(self, _request: web.Request) -> web.Response:
        if not self.index_html_path.exists():
            return web.Response(text="Missing index.html", status=500)
        return web.FileResponse(self.index_html_path)

    async def scan_endpoints(self, _request: web.Request) -> web.Response:
        endpoints = await self._scan_candidates()
        return web.json_response(
            {
                "status": "ok",
                "endpoints": [endpoint.to_dict() for endpoint in endpoints],
                "timestamp": ts_utc(),
            }
        )

    async def discover_models(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            base_url = str(body.get("base_url", "")).strip().rstrip("/")
            provider = str(body.get("provider", "auto")).strip().lower() or "auto"
            api_key = str(body.get("api_key", ""))
            if not base_url:
                return self.json_error("base_url is required")
            if "://" not in base_url:
                base_url = f"http://{base_url}"
            normalized_provider = await self._detect_provider(base_url, provider, api_key)
            models = await self._discover_models(base_url, normalized_provider, api_key)
            return web.json_response(
                {
                    "status": "ok",
                    "provider": normalized_provider,
                    "models": models,
                    "timestamp": ts_utc(),
                }
            )
        except ValueError as exc:
            return self.json_error(str(exc), code="invalid_request", status=400)
        except RuntimeError as exc:
            return self.json_error(str(exc), code="backend_unavailable", status=503)
        except Exception as exc:
            LOG.exception("Model discovery failed")
            return self.json_error(str(exc), code="internal_error", status=500)

    async def benchmark_start(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            spec = BenchmarkRequest.from_dict(payload)
        except ValueError as exc:
            return self.json_error(str(exc), code="invalid_request", status=400)
        except Exception:
            return self.json_error("Request body must be valid JSON", code="invalid_json", status=400)

        job = JobState(request=spec)
        if spec.benchmark_type == "sql":
            job.progress_total = len(spec.question_ids) if spec.question_ids is not None else 0
        else:
            job.progress_total = sum(len(target.models) for target in spec.targets) * spec.repeat_count
        self.jobs[job.job_id] = job
        job.task = asyncio.create_task(self._run_job(job))
        return web.json_response(
            {
                "status": "ok",
                "job_id": job.job_id,
                "timestamp": ts_utc(),
            },
            status=202,
        )

    async def benchmark_status(self, request: web.Request) -> web.Response:
        job = self.jobs.get(request.match_info["job_id"])
        if job is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)
        return web.json_response({"status": "ok", "job": job.to_dict(), "timestamp": ts_utc()})

    async def _load_job_record(self, job_id: str, *, log_label: str) -> Optional[Dict[str, Any]]:
        live_job = self.jobs.get(job_id)
        if live_job is not None:
            return live_job.to_dict()
        path = self._find_record_path(job_id)
        if path and path.exists():
            try:
                content = await asyncio.to_thread(path.read_text, "utf-8")
                loaded = json.loads(content)
                if isinstance(loaded, dict):
                    return loaded
            except Exception as exc:
                LOG.warning("Failed reading %s export record %s: %s", log_label, path, exc)
        return None

    async def benchmark_results_jsonl(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        record = await self._load_job_record(job_id, log_label="JSONL")
        if record is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)

        text = self._build_results_jsonl(record)
        filename = f"{job_id}.results.jsonl"
        return web.Response(
            text=text,
            content_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def benchmark_results_csv(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        record = await self._load_job_record(job_id, log_label="CSV")
        if record is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)

        text = self._build_results_csv(record)
        filename = f"{job_id}.results.csv"
        return web.Response(
            text=text,
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def benchmark_results_tsv(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        record = await self._load_job_record(job_id, log_label="TSV")
        if record is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)

        text = self._build_results_tsv(record)
        filename = f"{job_id}.results.tsv"
        return web.Response(
            text=text,
            content_type="text/tab-separated-values",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def benchmark_manifest(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        record = await self._load_job_record(job_id, log_label="manifest")
        if record is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)

        text = self._build_run_manifest(record)
        filename = f"{job_id}.manifest.json"
        return web.Response(
            text=text,
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def benchmark_summary(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        record = await self._load_job_record(job_id, log_label="summary")
        if record is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)

        text = self._build_run_summary(record)
        filename = f"{job_id}.summary.json"
        return web.Response(
            text=text,
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def benchmark_stop(self, request: web.Request) -> web.Response:
        job = self.jobs.get(request.match_info["job_id"])
        if job is None:
            return self.json_error("Unknown job_id", code="not_found", status=404)
        job.stop_requested = True
        if job.status in {"queued", "running"}:
            job.status = "stopping"
        return web.json_response({"status": "ok", "job_id": job.job_id, "timestamp": ts_utc()})

    async def benchmark_results_list(self, _request: web.Request) -> web.Response:
        items = await self._load_results_store()
        return web.json_response({"status": "ok", "results": items, "timestamp": ts_utc()})

    async def benchmark_summaries_list(self, _request: web.Request) -> web.Response:
        items = await self._load_results_store()
        summaries = [json.loads(self._build_run_summary(item)) for item in items]
        return web.json_response({"status": "ok", "summaries": summaries, "timestamp": ts_utc()})

    async def benchmark_active(self, _request: web.Request) -> web.Response:
        """List in-memory jobs that are still live (queued/running/stopping).

        Lets the frontend re-attach to an active run after a page reload even
        when its per-tab sessionStorage hint is gone (e.g. new tab, fresh
        browser). Data is already in memory, so this is cheap.
        """
        active: List[Dict[str, Any]] = []
        for job in self.jobs.values():
            if job.status in {"queued", "running", "stopping"}:
                active.append({
                    "job_id": job.job_id,
                    "benchmark_type": job.request.benchmark_type,
                    "status": job.status,
                    "started_at": job.started_at,
                    "current_model": job.current_model,
                    "progress": {
                        "completed": job.progress_completed,
                        "total": job.progress_total,
                    },
                })
        # Most recently started first.
        active.sort(key=lambda j: j.get("started_at") or "", reverse=True)
        return web.json_response({"status": "ok", "active": active, "timestamp": ts_utc()})

    async def benchmark_results_clear(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        clear_all = bool(body.get("all", False))
        job_ids = body.get("job_ids", [])
        if job_ids is None:
            job_ids = []
        if not isinstance(job_ids, list):
            return self.json_error("job_ids must be an array", code="invalid_request", status=400)
        job_ids = {str(item).strip() for item in job_ids if str(item).strip()}

        results_dir = self.results_store_dir
        async with self._results_lock:
            if clear_all:
                paths = list(results_dir.glob("*.json")) if results_dir.exists() else []
                removed = len(paths)
                for path in paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        LOG.warning("Failed deleting result file %s: %s", path, exc)
            else:
                if not job_ids:
                    return self.json_error("Provide job_ids or set all=true", code="invalid_request", status=400)
                removed = 0
                for job_id in job_ids:
                    path = self._find_record_path(job_id)
                    if path and path.exists():
                        try:
                            path.unlink()
                            removed += 1
                        except OSError as exc:
                            LOG.warning("Failed deleting result file %s: %s", path, exc)
        remaining_count = len(list(results_dir.glob("*.json"))) if results_dir.exists() else 0
        return web.json_response(
            {"status": "ok", "removed": removed, "remaining": remaining_count, "timestamp": ts_utc()}
        )

    async def _scan_candidates(self) -> List[EndpointCandidate]:
        candidates = self._build_scan_candidates()
        timeout = httpx.Timeout(
            connect=LOCAL_SCAN_CONNECT_TIMEOUT_S,
            read=LOCAL_SCAN_READ_TIMEOUT_S,
            write=LOCAL_SCAN_READ_TIMEOUT_S,
            pool=LOCAL_SCAN_CONNECT_TIMEOUT_S,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            providers = await asyncio.gather(
                *(self._probe_provider(candidate["base_url"], client=client) for candidate in candidates)
            )

        results: List[EndpointCandidate] = []
        seen_dedupe_keys: set[tuple[tuple[str, int], ...]] = set()
        for candidate, provider in zip(candidates, providers):
            if not provider:
                continue
            dedupe_key = self._dedupe_key_for_scan_candidate(candidate["host"], candidate["resolved_targets"])
            if dedupe_key and dedupe_key in seen_dedupe_keys:
                continue
            if dedupe_key:
                seen_dedupe_keys.add(dedupe_key)
            label = "OpenAI-compatible endpoint" if provider == "openai-compatible" else "Ollama endpoint"
            models_path = OPENAI_MODELS_PATH if provider == "openai-compatible" else OLLAMA_TAGS_PATH
            results.append(
                EndpointCandidate(
                    base_url=candidate["base_url"],
                    provider=provider,
                    reachable=True,
                    models_path=models_path,
                    label=label,
                )
            )
        return results

    def _resolve_network_targets(self, host: str, port: int) -> frozenset[tuple[str, int]]:
        try:
            return frozenset(
                (item[4][0], int(item[4][1]))
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        except socket.gaierror:
            return frozenset()

    def _dedupe_key_for_scan_candidate(
        self,
        host: str,
        resolved_targets: frozenset[tuple[str, int]],
    ) -> tuple[tuple[str, int], ...]:
        if host == "localhost":
            ipv4_targets = tuple(sorted(target for target in resolved_targets if ":" not in target[0]))
            if ipv4_targets:
                return ipv4_targets
        return tuple(sorted(resolved_targets))

    def _build_scan_candidates(self) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen_base_urls: set[str] = set()

        for host in DEFAULT_HOST_CANDIDATES:
            for port in DEFAULT_PORT_CANDIDATES:
                base_url = f"http://{host}:{port}"
                if base_url in seen_base_urls:
                    continue
                seen_base_urls.add(base_url)
                candidates.append(
                    {
                        "base_url": base_url,
                        "host": host,
                        "port": port,
                        "resolved_targets": self._resolve_network_targets(host, port),
                    }
                )
        return candidates

    async def _probe_provider(self, base_url: str, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
        try:
            provider = await self._detect_provider(base_url, "auto", "", client=client)
            return provider
        except Exception:
            return None

    async def _detect_provider(
        self,
        base_url: str,
        requested_provider: str,
        api_key: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> str:
        if requested_provider in {"openai", "openai-compatible"}:
            return "openai-compatible"
        if requested_provider == "ollama":
            return "ollama"

        if client is not None:
            if await self._looks_like_openai(base_url, api_key, client):
                return "openai-compatible"
            if await self._looks_like_ollama(base_url, client):
                return "ollama"
        else:
            timeout = httpx.Timeout(5.0)
            async with httpx.AsyncClient(timeout=timeout) as owned_client:
                if await self._looks_like_openai(base_url, api_key, owned_client):
                    return "openai-compatible"
                if await self._looks_like_ollama(base_url, owned_client):
                    return "ollama"
        raise RuntimeError(f"Could not detect provider type at {base_url}")

    async def _looks_like_openai(self, base_url: str, api_key: str, client: httpx.AsyncClient) -> bool:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = await client.get(f"{base_url}{OPENAI_MODELS_PATH}", headers=headers)
            if resp.status_code >= 400:
                return False
            data = resp.json()
            return isinstance(data.get("data"), list)
        except Exception:
            return False

    async def _looks_like_ollama(self, base_url: str, client: httpx.AsyncClient) -> bool:
        try:
            resp = await client.get(f"{base_url}{OLLAMA_TAGS_PATH}")
            if resp.status_code >= 400:
                return False
            data = resp.json()
            return isinstance(data.get("models"), list)
        except Exception:
            return False

    async def _discover_models(self, base_url: str, provider: str, api_key: str) -> List[str]:
        timeout = httpx.Timeout(10.0)
        headers = {}
        if api_key and provider == "openai-compatible":
            headers["Authorization"] = f"Bearer {api_key}"
        path = OPENAI_MODELS_PATH if provider == "openai-compatible" else OLLAMA_TAGS_PATH
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(f"{base_url}{path}")
            if resp.status_code >= 400:
                raise RuntimeError(f"Model discovery failed at {base_url}{path}: HTTP {resp.status_code}")
            data = resp.json()
        models: List[str] = []
        if provider == "openai-compatible":
            for item in data.get("data", []):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    models.append(model_id.strip())
        else:
            for item in data.get("models", []):
                model_id = item.get("model") or item.get("name")
                if isinstance(model_id, str) and model_id.strip():
                    models.append(model_id.strip())
        if not models:
            raise RuntimeError(f"No models found at {base_url}{path}")
        return models

    async def _run_job(self, job: JobState) -> None:
        job.status = "running"
        job.started_at = ts_utc()
        job.set_phase("starting", f"Starting {job.request.benchmark_type} benchmark")
        try:
            if job.request.benchmark_type == "sql":
                await self._run_sql_job(job)
            else:
                for target in job.request.targets:
                    job.current_provider_id = target.provider_id
                    job.current_provider_label = target.provider_label
                    job.set_phase("detecting_provider", f"Detecting provider for {target.provider_label}")
                    normalized_provider = await self._detect_provider(target.base_url, target.provider, target.api_key)
                    job.set_phase("discovering_models", f"Discovering models at {target.provider_label}")
                    available_models = await self._discover_models(target.base_url, normalized_provider, target.api_key)
                    missing = [model for model in target.models if model not in available_models]
                    if missing:
                        raise RuntimeError(f"Requested models not found for {target.provider_label}: {', '.join(missing)}")

                    runtime_target = BenchmarkTarget(
                        provider_id=target.provider_id,
                        provider_label=target.provider_label,
                        base_url=target.base_url,
                        provider=normalized_provider,
                        api_key=target.api_key,
                        models=target.models,
                    )
                    if job.request.mode == "sequential":
                        await self._run_sequential(job, runtime_target)
                    else:
                        await self._run_parallel(job, runtime_target)
                    if job.stop_requested:
                        break

            if job.stop_requested and job.status == "stopping":
                job.status = "stopped"
                job.set_phase("stopped", "Stopped")
            elif job.status not in {"failed", "stopped"}:
                job.status = "completed"
                job.set_phase("completed", "Completed")
            job.report_text = self._build_report(job)
        except Exception as exc:
            LOG.exception("Benchmark job failed")
            job.errors.append(str(exc))
            job.status = "failed"
            job.set_phase("failed", str(exc))
            job.report_text = self._build_report(job)
        finally:
            job.finished_at = ts_utc()
            if job.results or job.errors:
                await self._append_job_to_results_store(job)

    async def _run_sql_job(self, job: JobState) -> None:
        target = job.request.targets[0]
        normalized_provider = await self._detect_provider(target.base_url, target.provider, target.api_key)
        available_models = await self._discover_models(target.base_url, normalized_provider, target.api_key)

        requested_models = target.models
        missing = [m for m in requested_models if m not in available_models]
        if missing:
            raise RuntimeError(f"Requested model(s) not found for {target.provider_label}: {', '.join(missing)}")

        # Determine which thinking variants to run
        thinking_mode_req = job.request.thinking_mode
        if thinking_mode_req == "both":
            thinking_variants: List[str] = ["off", "on"]
        else:
            thinking_variants = [thinking_mode_req]

        runtime_target = BenchmarkTarget(
            provider_id=target.provider_id,
            provider_label=target.provider_label,
            base_url=target.base_url,
            provider=normalized_provider,
            api_key=target.api_key,
            models=requested_models,
        )
        job.current_provider_id = runtime_target.provider_id
        job.current_provider_label = runtime_target.provider_label

        with SqlBenchmarkRunner(
            llm_callback=lambda system, user, *, model, provider, endpoint, timeout_ms: self._call_llm_single(
                system,
                user,
                runtime_target,
                model,
                timeout_ms,
                reasoning_effort=job.request.reasoning_effort,
            ),
            data_dir=SQL_BENCHMARK_DATA_DIR,
        ) as runner:
            question_ids = job.request.question_ids
            if question_ids is None:
                question_ids = sorted(runner.questions_by_id)
            else:
                question_ids = [int(question_id) for question_id in question_ids]

            job.progress_total = len(requested_models) * len(thinking_variants) * len(question_ids)

            # Shared across all questions so each model's load is treated as cold
            # only once; the per-question timeout then excludes that load time.
            warmed_models: set = set()

            for model in requested_models:
                job.current_model = model
                for thinking_mode in thinking_variants:
                    for question_id in question_ids:
                        if job.stop_requested:
                            return
                        reasoning_fallback_state = {"used": False}
                        fallback_token = REASONING_FALLBACK_STATE.set(reasoning_fallback_state)
                        try:
                            if job.request.sql_mode == "grammar":
                                result = await runner.run_question(
                                    question_id=question_id,
                                    model=model,
                                    provider=runtime_target.provider,
                                    endpoint=runtime_target.base_url,
                                    timeout_ms=job.request.timeout_ms,
                                    thinking_mode=thinking_mode,
                                )
                            else:
                                result = await runner.run_question_tool_calling(
                                    question_id=question_id,
                                    model=model,
                                    provider=runtime_target.provider,
                                    endpoint=runtime_target.base_url,
                                    timeout_ms=job.request.timeout_ms,
                                    tool_llm_callback=lambda system_prompt, messages, tools, model, provider, endpoint, timeout_ms: self._call_llm_tool_calling(
                                        system_prompt=system_prompt,
                                        messages=messages,
                                        tools=tools,
                                        target=runtime_target,
                                        model=model,
                                        timeout_ms=timeout_ms,
                                        reasoning_effort=job.request.reasoning_effort,
                                        fallback_state=reasoning_fallback_state,
                                    ),
                                    abort_signal=job,
                                    thinking_mode=thinking_mode,
                                    question_timeout_ms=job.request.question_timeout_ms,
                                    warmed_models=warmed_models,
                                )
                        finally:
                            REASONING_FALLBACK_STATE.reset(fallback_token)
                        result["reasoning_effort"] = job.request.reasoning_effort
                        result["reasoning_fallback"] = bool(reasoning_fallback_state.get("used"))
                        job.results.append(self._sql_result_row(job, runtime_target, result))
                        job.progress_completed += 1
                        asyncio.ensure_future(self._flush_job_record(job))

    async def _call_llm_single(
        self,
        system: str,
        user: str,
        target: BenchmarkTarget,
        model: str,
        timeout_ms: int,
        *,
        reasoning_effort: str = "disabled",
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if target.provider == "openai-compatible":
            headers = {"Content-Type": "application/json"}
            if target.api_key:
                headers["Authorization"] = f"Bearer {target.api_key}"
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 4096,
                "stream": False,
            }
            timeout = httpx.Timeout(
            connect=30.0,
            read=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
            write=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
            pool=30.0,
            )
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await self._post_openai_chat_with_reasoning_fallback(
                    client,
                    f"{target.base_url}{OPENAI_CHAT_PATH}",
                    payload,
                    reasoning_effort=reasoning_effort,
                    model=model,
                )
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"HTTP {response.status_code} from {target.base_url}{OPENAI_CHAT_PATH}: {response.text[:300]}"
                    )
                data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("OpenAI-compatible response did not include choices")
            message = choices[0].get("message") or {}
            # Some models (LM Studio, llama.cpp) put thinking in reasoning_content
            content = message.get("content") or message.get("reasoning_content") or ""
            return _coerce_message_content_to_text(content)

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "stream": False,
        }
        timeout = httpx.Timeout(
            connect=30.0,
            read=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
            write=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
            pool=30.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{target.base_url}{OLLAMA_CHAT_PATH}", json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code} from {target.base_url}{OLLAMA_CHAT_PATH}: {response.text[:300]}")
            data = response.json()
        message = data.get("message") or {}
        return _coerce_message_content_to_text(message.get("content"))

    async def _call_llm_tool_calling(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        target: BenchmarkTarget,
        model: str,
        timeout_ms: int,
        *,
        reasoning_effort: str = "disabled",
        fallback_state: Optional[Dict[str, bool]] = None,
    ) -> Dict[str, Any]:
        """Call LLM with tools (OpenAI-compatible tool-calling endpoint).

        Models that don't support tool_choice (e.g. Gemma, some Ollama models) often
        return HTTP 400/422 with a message containing 'tool' or 'function'. In that
        case we re-try without tools so the tool-calling loop can extract SQL from
        plain text via the fallback branch in run_question_tool_calling.
        """
        all_messages = [{"role": "system", "content": system_prompt}] + messages
        headers = {"Content-Type": "application/json"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"

        tool_payload = {
            "model": model,
            "messages": all_messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": 4096,
            "stream": False,
        }
        timeout = httpx.Timeout(
            connect=30.0,
            read=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
            write=None if timeout_ms <= 0 else max(timeout_ms / 1000.0, 300.0),
            pool=30.0,
        )

        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await self._post_openai_chat_with_reasoning_fallback(
                client,
                f"{target.base_url}{OPENAI_CHAT_PATH}",
                tool_payload,
                reasoning_effort=reasoning_effort,
                model=model,
                fallback_state=fallback_state,
            )

            # Some models reject tool_choice / tools entirely (400/422/501).
            # Fall back to plain chat so the text-extraction branch can handle it.
            if response.status_code in (400, 422, 501):
                body_text = response.text
                body_lower = body_text.lower()
                # LM Studio: model was unloaded/reloaded mid-request — wait and retry once
                if any(kw in body_lower for kw in ("unloaded", "reloaded")):
                    LOG.warning(
                        "Model %s reported unloaded/reloaded — waiting 3s and retrying",
                        model,
                    )
                    await asyncio.sleep(3)
                    response = await self._post_openai_chat_with_reasoning_fallback(
                        client,
                        f"{target.base_url}{OPENAI_CHAT_PATH}",
                        tool_payload,
                        reasoning_effort=reasoning_effort,
                        model=model,
                        fallback_state=fallback_state,
                    )
                elif any(kw in body_lower for kw in ("tool", "function", "not support", "unsupported", "render")):
                    LOG.warning(
                        "Model %s at %s rejected tool-calling (%s) — retrying without tools",
                        model, target.base_url, response.status_code,
                    )
                    plain_payload = {"model": model, "messages": all_messages, "temperature": 0.1, "max_tokens": 4096, "stream": False}
                    response = await self._post_openai_chat_with_reasoning_fallback(
                        client,
                        f"{target.base_url}{OPENAI_CHAT_PATH}",
                        plain_payload,
                        reasoning_effort=reasoning_effort,
                        model=model,
                        fallback_state=fallback_state,
                    )

            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code} from {target.base_url}{OPENAI_CHAT_PATH}: {response.text[:400]}"
                )
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Response from {model} did not include choices: {str(data)[:200]}")
        message = choices[0].get("message", {})

        # reasoning_content is the separate thinking field (LM Studio, llama.cpp)
        content = message.get("content") or message.get("reasoning_content") or ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls") or [],
            "usage": data.get("usage", {}),
            "model": data.get("model_alias") or data.get("model") or model,
            "reasoning_fallback": bool(fallback_state and fallback_state.get("used")),
        }

    def _sql_result_row(self, job: JobState, target: BenchmarkTarget, result: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(result)
        row.setdefault("timestamp", ts_utc())
        row["job_id"] = job.job_id
        row["benchmark_type"] = "sql"
        row["provider_id"] = target.provider_id
        row["provider_label"] = target.provider_label
        row["provider_type"] = target.provider
        row["provider"] = target.provider
        row["endpoint"] = target.base_url
        row["model"] = result.get("model") or target.models[0]
        row["thinking_mode"] = result.get("thinking_mode", "off")
        row["reasoning_effort"] = result.get("reasoning_effort", job.request.reasoning_effort)
        row["reasoning_fallback"] = bool(result.get("reasoning_fallback", False))
        return row

    async def _run_sequential(self, job: JobState, target: BenchmarkTarget) -> None:
        job.current_provider_id = target.provider_id
        job.current_provider_label = target.provider_label
        for model in target.models:
            if job.stop_requested:
                return
            job.current_model = model
            for _ in range(job.request.warmup_runs):
                if job.stop_requested:
                    return
                job.set_phase("warming_up", f"Warming up {model}", model=model, run_index=0, benchmark_type="speed")
                await self._run_single_benchmark(job, target, model, 0)
            for run_index in range(1, job.request.repeat_count + 1):
                if job.stop_requested:
                    return
                job.set_phase("running_request", f"Running speed test for {model} (run {run_index}/{job.request.repeat_count})", model=model, run_index=run_index, benchmark_type="speed")
                result = await self._run_single_benchmark(job, target, model, run_index)
                job.results.append(result)
                job.progress_completed += 1
                job.set_phase("result_recorded", f"Recorded speed result for {model} (run {run_index}/{job.request.repeat_count})", model=model, run_index=run_index, benchmark_type="speed")

    async def _run_parallel(self, job: JobState, target: BenchmarkTarget) -> None:
        job.current_provider_id = target.provider_id
        job.current_provider_label = target.provider_label
        semaphore = asyncio.Semaphore(job.request.concurrency)

        async def worker(model: str, run_index: int) -> Dict[str, Any]:
            async with semaphore:
                if job.stop_requested:
                    return self._stopped_result(job, target, model, run_index)
                job.current_model = model
                job.current_provider_id = target.provider_id
                job.current_provider_label = target.provider_label
                job.set_phase(
                    "warming_up" if run_index == 0 else "running_request",
                    f"{'Warming up' if run_index == 0 else 'Running speed test for'} {model}"
                    + ("" if run_index == 0 else f" (run {run_index}/{job.request.repeat_count})"),
                    model=model,
                    run_index=run_index,
                    benchmark_type="speed",
                )
                return await self._run_single_benchmark(job, target, model, run_index)

        tasks = [
            asyncio.create_task(worker(model, run_index))
            for model in target.models
            for _ in range(job.request.warmup_runs)
            for run_index in [0]
        ] + [
            asyncio.create_task(worker(model, run_index))
            for model in target.models
            for run_index in range(1, job.request.repeat_count + 1)
        ]
        for task in asyncio.as_completed(tasks):
            result = await task
            if result.get("success") == "stopped":
                continue
            if result.get("run_index") == 0:
                continue
            job.results.append(result)
            job.progress_completed += 1
            job.set_phase("result_recorded", f"Recorded speed result for {result.get('model', 'model')}", model=result.get("model"), run_index=result.get("run_index"), benchmark_type="speed")
            if job.stop_requested:
                for pending in tasks:
                    if not pending.done():
                        pending.cancel()
                return

    async def _run_single_benchmark(
        self,
        job: JobState,
        target: BenchmarkTarget,
        model: str,
        run_index: int,
    ) -> Dict[str, Any]:
        prompt_hash = hashlib.sha256(job.request.prompt.encode("utf-8")).hexdigest()
        started = time.perf_counter()
        start_stamp = ts_utc()
        try:
            if target.provider == "openai-compatible":
                metrics = await self._benchmark_openai(job.request, target, model, job=job, run_index=run_index)
            else:
                metrics = await self._benchmark_ollama(job.request, target, model, job=job, run_index=run_index)
            job.set_phase("scoring", f"Scoring speed result for {model}", model=model, run_index=run_index, benchmark_type="speed")
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return {
                "timestamp": start_stamp,
                "job_id": job.job_id,
                "provider_id": target.provider_id,
                "provider_label": target.provider_label,
                "provider_type": target.provider,
                "endpoint": target.base_url,
                "provider": target.provider,
                "mode": job.request.mode,
                "model": model,
                "prompt_chars": len(job.request.prompt),
                "prompt_hash": prompt_hash,
                "max_tokens": job.request.max_tokens,
                "temperature": job.request.temperature,
                "top_p": job.request.top_p,
                "presence_penalty": job.request.presence_penalty,
                "frequency_penalty": job.request.frequency_penalty,
                "timeout_ms": job.request.timeout_ms,
                "run_index": run_index,
                "success": True,
                "error": "",
                "latency_ms": metrics.get("latency_ms"),
                "total_time_ms": metrics.get("total_time_ms", latency_ms),
                "ttft_ms": metrics.get("ttft_ms"),
                "prefill_tps": metrics.get("prefill_tps"),
                "decode_tps": metrics.get("decode_tps"),
                "prompt_tokens": metrics.get("prompt_tokens"),
                "completion_tokens": metrics.get("completion_tokens"),
                "completion_tokens_capped": metrics.get("completion_tokens_capped"),
                "decode_tokens_measured": metrics.get("decode_tokens_measured"),
                "warmup_runs": job.request.warmup_runs,
                "max_concurrent_predictions": job.request.max_concurrent_predictions,
                "mtp": job.request.mtp,
                "k_cache_quantization": job.request.k_cache_quantization,
                "v_cache_quantization": job.request.v_cache_quantization,
                "batch_size": job.request.batch_size,
                "flash_attn": job.request.flash_attn,
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return {
                "timestamp": start_stamp,
                "job_id": job.job_id,
                "provider_id": target.provider_id,
                "provider_label": target.provider_label,
                "provider_type": target.provider,
                "endpoint": target.base_url,
                "provider": target.provider,
                "mode": job.request.mode,
                "model": model,
                "prompt_chars": len(job.request.prompt),
                "prompt_hash": prompt_hash,
                "max_tokens": job.request.max_tokens,
                "temperature": job.request.temperature,
                "top_p": job.request.top_p,
                "presence_penalty": job.request.presence_penalty,
                "frequency_penalty": job.request.frequency_penalty,
                "timeout_ms": job.request.timeout_ms,
                "run_index": run_index,
                "success": False,
                "error": str(exc),
                "latency_ms": latency_ms,
                "total_time_ms": latency_ms,
                "ttft_ms": None,
                "prefill_tps": None,
                "decode_tps": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "completion_tokens_capped": None,
                "decode_tokens_measured": None,
                "warmup_runs": job.request.warmup_runs,
                "max_concurrent_predictions": job.request.max_concurrent_predictions,
                "mtp": job.request.mtp,
                "k_cache_quantization": job.request.k_cache_quantization,
                "v_cache_quantization": job.request.v_cache_quantization,
                "batch_size": job.request.batch_size,
                "flash_attn": job.request.flash_attn,
            }

    def _stopped_result(self, job: JobState, target: BenchmarkTarget, model: str, run_index: int) -> Dict[str, Any]:
        return {
            "timestamp": ts_utc(),
            "job_id": job.job_id,
            "provider_id": target.provider_id,
            "provider_label": target.provider_label,
            "provider_type": target.provider,
            "endpoint": target.base_url,
            "provider": target.provider,
            "mode": job.request.mode,
            "model": model,
            "prompt_chars": len(job.request.prompt),
            "prompt_hash": hashlib.sha256(job.request.prompt.encode("utf-8")).hexdigest(),
            "max_tokens": job.request.max_tokens,
            "temperature": job.request.temperature,
            "top_p": job.request.top_p,
            "presence_penalty": job.request.presence_penalty,
            "frequency_penalty": job.request.frequency_penalty,
            "timeout_ms": job.request.timeout_ms,
            "run_index": run_index,
            "success": "stopped",
            "error": "stopped",
            "latency_ms": None,
            "total_time_ms": None,
            "ttft_ms": None,
            "prefill_tps": None,
            "decode_tps": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "completion_tokens_capped": None,
            "decode_tokens_measured": None,
            "warmup_runs": job.request.warmup_runs,
            "max_concurrent_predictions": job.request.max_concurrent_predictions,
            "mtp": job.request.mtp,
            "k_cache_quantization": job.request.k_cache_quantization,
            "v_cache_quantization": job.request.v_cache_quantization,
            "batch_size": job.request.batch_size,
            "flash_attn": job.request.flash_attn,
        }

    async def _benchmark_openai(
        self,
        spec: BenchmarkRequest,
        target: BenchmarkTarget,
        model: str,
        *,
        job: Optional[JobState] = None,
        run_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": spec.prompt}],
            "max_tokens": spec.max_tokens,
            "temperature": spec.temperature,
            "top_p": spec.top_p,
            "presence_penalty": spec.presence_penalty,
            "frequency_penalty": spec.frequency_penalty,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        timeout = httpx.Timeout(spec.timeout_ms / 1000.0)
        start = time.perf_counter()
        first_token_at: Optional[float] = None
        completion_tokens: Optional[int] = None
        prompt_tokens: Optional[int] = None
        if job is not None:
            job.set_phase("waiting_first_token", f"Waiting for first token from {model}", model=model, run_index=run_index, benchmark_type="speed")
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            async with client.stream("POST", f"{target.base_url}{OPENAI_CHAT_PATH}", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"HTTP {response.status_code} from {target.base_url}{OPENAI_CHAT_PATH}: {body[:300]}")
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    data = json.loads(data_str)
                    choices = data.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content is not None and first_token_at is None:
                            first_token_at = time.perf_counter()
                            if job is not None:
                                job.set_phase("streaming", f"Streaming response from {model}", model=model, run_index=run_index, benchmark_type="speed")
                    usage = data.get("usage") or {}
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get("completion_tokens", completion_tokens)
        end = time.perf_counter()
        latency_ms = (end - start) * 1000.0
        ttft_ms = ((first_token_at or end) - start) * 1000.0 if first_token_at else None
        decode_tps = None
        prefill_tps = None
        completion_tokens_capped = None
        decode_tokens_measured = None
        if first_token_at and completion_tokens is not None:
            decode_seconds = max(end - first_token_at, 1e-6)
            decode_tokens_measured = completion_tokens
            completion_tokens_capped = min(completion_tokens, spec.max_tokens)
            decode_tps = round(decode_tokens_measured / decode_seconds, 2)
        return {
            "latency_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
            "total_time_ms": round(latency_ms, 2),
            "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
            "prefill_tps": prefill_tps,
            "decode_tps": decode_tps,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "completion_tokens_capped": completion_tokens_capped,
            "decode_tokens_measured": decode_tokens_measured,
        }

    async def _benchmark_ollama(
        self,
        spec: BenchmarkRequest,
        target: BenchmarkTarget,
        model: str,
        *,
        job: Optional[JobState] = None,
        run_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": spec.prompt}],
            "options": {
                "num_predict": spec.max_tokens,
                "temperature": spec.temperature,
                "top_p": spec.top_p,
            },
            "stream": False,
        }
        timeout = httpx.Timeout(spec.timeout_ms / 1000.0)
        if job is not None:
            job.set_phase("waiting_response", f"Waiting for Ollama response from {model}", model=model, run_index=run_index, benchmark_type="speed")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{target.base_url}{OLLAMA_CHAT_PATH}", json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code} from {target.base_url}{OLLAMA_CHAT_PATH}: {response.text[:300]}")
            data = response.json()
        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        prompt_eval_duration = data.get("prompt_eval_duration")
        eval_duration = data.get("eval_duration")
        ttft_ms = round((prompt_eval_duration or 0) / 1_000_000.0, 2) if prompt_eval_duration else None
        total_time_ms = None
        if prompt_eval_duration or eval_duration:
            total_time_ms = round(((prompt_eval_duration or 0) + (eval_duration or 0)) / 1_000_000.0, 2)
        prefill_tps = None
        decode_tps = None
        completion_tokens_capped = None
        decode_tokens_measured = None
        if prompt_tokens and prompt_eval_duration:
            prefill_tps = round(prompt_tokens / max(prompt_eval_duration / 1_000_000_000.0, 1e-6), 2)
        if completion_tokens is not None and eval_duration:
            decode_tokens_measured = completion_tokens
            completion_tokens_capped = min(completion_tokens, spec.max_tokens)
            decode_tps = round(completion_tokens / max(eval_duration / 1_000_000_000.0, 1e-6), 2)
        return {
            "latency_ms": ttft_ms,
            "total_time_ms": total_time_ms,
            "ttft_ms": ttft_ms,
            "prefill_tps": prefill_tps,
            "decode_tps": decode_tps,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "completion_tokens_capped": completion_tokens_capped,
            "decode_tokens_measured": decode_tokens_measured,
        }

    def _build_report(self, job: JobState) -> str:
        if job.request.benchmark_type == "sql":
            return self._build_sql_report(job)
        return self._build_speed_report(job)

    def _build_speed_report(self, job: JobState) -> str:
        lines = [
            f"Job: {job.job_id}",
            f"Status: {job.status}",
            f"Benchmark type: {job.request.benchmark_type}",
            f"Endpoint: {job.request.base_url}",
            f"Mode: {job.request.mode}",
            f"Models: {', '.join(job.request.models)}",
            f"Completed: {job.progress_completed}/{job.progress_total}",
        ]
        successful = [item for item in job.results if item.get("success") is True]
        failed = [item for item in job.results if item.get("success") is False]
        lines.append(f"Successful runs: {len(successful)}")
        lines.append(f"Failed runs: {len(failed)}")
        if successful:
            best_latency = min(item["latency_ms"] for item in successful if item.get("latency_ms") is not None)
            lines.append(f"Best latency: {best_latency} ms")
            decode_values = [item["decode_tps"] for item in successful if item.get("decode_tps") is not None]
            if decode_values:
                lines.append(f"Best decode speed: {max(decode_values)} tok/s")
        if job.errors:
            lines.append("Errors:")
            lines.extend(f"- {error}" for error in job.errors)
        return "\n".join(lines)

    def _build_sql_report(self, job: JobState) -> str:
        total = len(job.results)
        successful = [item for item in job.results if item.get("success") is True]
        failed = [item for item in job.results if item.get("success") is False]
        lines = [
            f"Job: {job.job_id}",
            f"Status: {job.status}",
            f"Benchmark type: {job.request.benchmark_type}",
            f"Endpoint: {job.request.base_url}",
            f"Model: {job.request.models[0] if job.request.models else ''}",
            f"Completed: {job.progress_completed}/{job.progress_total}",
            f"Questions passed: {len(successful)}/{total}",
            f"Questions failed: {len(failed)}",
        ]
        if failed:
            lines.append("Failed questions:")
            for item in failed:
                lines.append(f"- q{item.get('question_id')}: {item.get('error', '')}")
        if job.errors:
            lines.append("Errors:")
            lines.extend(f"- {error}" for error in job.errors)
        return "\n".join(lines)

def _coerce_message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(part for part in text_parts if part)
    return str(content or "")


async def create_app() -> web.Application:
    server = BenchmarkServer(INDEX_HTML)
    # One-time startup maintenance: migrate legacy filenames, then mark records
    # left 'running' by a previous crashed/killed run as interrupted.
    try:
        await server.migrate_legacy_filenames()
    except Exception as exc:
        LOG.warning("Legacy filename migration failed: %s", exc)
    try:
        await server.reconcile_stale_records()
    except Exception as exc:
        LOG.warning("Stale-record reconciliation failed: %s", exc)
    app = web.Application(middlewares=[cors_middleware()])
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda _request: web.Response(status=204))
    app.router.add_get("/", server.index)
    app.router.add_get("/health", server.health)
    app.router.add_get("/api/fixtures", server.fixture_manifest)
    app.router.add_get("/api/fixtures/validate", server.fixture_validation)
    app.router.add_get("/api/benchmark/contract", server.benchmark_contract)
    app.router.add_get("/api/benchmark/modules", server.benchmark_modules)
    app.router.add_get("/api/benchmark/modules/{module_id}", server.benchmark_module_detail)
    app.router.add_get("/api/benchmark/presets", server.benchmark_presets)
    app.router.add_get("/api/benchmark/presets/{preset_id}", server.benchmark_preset_detail)
    app.router.add_get("/api/endpoints/scan", server.scan_endpoints)
    app.router.add_post("/api/models/discover", server.discover_models)
    app.router.add_post("/api/benchmark/start", server.benchmark_start)
    app.router.add_get("/api/benchmark/summaries", server.benchmark_summaries_list)
    app.router.add_get("/api/benchmark/results", server.benchmark_results_list)
    app.router.add_get("/api/benchmark/active", server.benchmark_active)
    app.router.add_get("/api/benchmark/dashboard", server.benchmark_dashboard)
    app.router.add_get("/api/benchmark/modules/{module_id}/adapter", server.benchmark_adapter_detail)
    app.router.add_post("/api/benchmark/results/clear", server.benchmark_results_clear)
    app.router.add_get("/api/benchmark/{job_id}/results.jsonl", server.benchmark_results_jsonl)
    app.router.add_get("/api/benchmark/{job_id}/results.csv", server.benchmark_results_csv)
    app.router.add_get("/api/benchmark/{job_id}/results.tsv", server.benchmark_results_tsv)
    app.router.add_get("/api/benchmark/{job_id}/manifest.json", server.benchmark_manifest)
    app.router.add_get("/api/benchmark/{job_id}/summary.json", server.benchmark_summary)
    app.router.add_get("/api/benchmark/{job_id}", server.benchmark_status)
    app.router.add_post("/api/benchmark/{job_id}/stop", server.benchmark_stop)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Testbench backend server")
    parser.add_argument("--host", "-b", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on")
    parser.add_argument("--log-level", "-l", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()
    setup_logging(level=getattr(logging, args.log_level.upper()))
    # Pass coroutine directly to avoid double-event-loop bug on Python 3.10+
    # (asyncio.Lock created in one loop then used in another -> RuntimeError)
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
