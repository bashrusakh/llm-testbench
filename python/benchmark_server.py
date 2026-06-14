"""``BenchmarkServer``: HTTP route handlers and orchestration glue.

The class is intentionally thin. Persistence (save/load/migrate/reconcile/
export) lives in :mod:`python.persistence`. Per-job execution
(run_*/benchmark_*/call_llm_*/build_report) lives in
:mod:`python.job_runner`. Data models live in :mod:`python.models`.

A handler that needs persistence calls ``persistence.<func>(self, ...)``;
one that needs job execution calls ``job_runner.<func>(self, ...)``. The
class mostly just wires routes to those free functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

import httpx
from aiohttp import web

from python.adapter import ADAPTER_REGISTRY, get_adapter
from python.json_io import _read_json, ts_utc
from python.local_benchmarks import LOCAL_FIXTURE_SPECS, load_local_tasks, validate_local_fixtures
from python.models import (
    ADAPTER_LIFECYCLE_HOOKS,
    BENCHMARK_MODULES,
    BENCHMARK_MODULES_BY_ID,
    BENCHMARK_PRESETS,
    BenchmarkRequest,
    BenchmarkTarget,
    EndpointCandidate,
    JobState,
)
from python.persistence import (
    append_job_to_results_store,
    build_results_csv,
    build_results_jsonl,
    build_results_tsv,
    build_run_manifest,
    build_run_summary,
    clear_results,
    find_record_path,
    flush_job_record,
    load_job_record,
    load_results_store,
    migrate_legacy_filenames,
    reconcile_stale_records,
    save_job_record,
)
from python.ssrf import _validate_endpoint_url

if TYPE_CHECKING:  # pragma: no cover
    from python.server import (
        DEFAULT_HOST_CANDIDATES,
        DEFAULT_PORT_CANDIDATES,
        LOCAL_SCAN_CONNECT_TIMEOUT_S,
        LOCAL_SCAN_READ_TIMEOUT_S,
        OPENAI_MODELS_PATH,
        OLLAMA_TAGS_PATH,
    )

LOG = logging.getLogger("llm_testbench")


class BenchmarkServer:
    def __init__(
        self,
        index_html_path: Optional[Path] = None,
        results_store_dir: Optional[Path] = None,
        sql_benchmark_data_dir: Optional[Path] = None,
        host_candidates: Optional[List[str]] = None,
        port_candidates: Optional[List[int]] = None,
        local_scan_connect_timeout_s: Optional[float] = None,
        local_scan_read_timeout_s: Optional[float] = None,
        openai_models_path: Optional[str] = None,
        ollama_tags_path: Optional[str] = None,
    ) -> None:
        # Lazy import to avoid circular: server.py -> benchmark_server.py -> job_runner.py -> server.py
        from python.server import (
            DEFAULT_HOST_CANDIDATES,
            DEFAULT_PORT_CANDIDATES,
            INDEX_HTML,
            LOCAL_SCAN_CONNECT_TIMEOUT_S,
            LOCAL_SCAN_READ_TIMEOUT_S,
            OLLAMA_TAGS_PATH,
            OPENAI_MODELS_PATH,
            RESULTS_STORE_DIR,
            SQL_BENCHMARK_DATA_DIR,
        )
        self.index_html_path = index_html_path if index_html_path is not None else INDEX_HTML
        self.results_store_dir = results_store_dir if results_store_dir is not None else RESULTS_STORE_DIR
        self.sql_benchmark_data_dir = sql_benchmark_data_dir if sql_benchmark_data_dir is not None else SQL_BENCHMARK_DATA_DIR
        self.host_candidates = host_candidates if host_candidates is not None else list(DEFAULT_HOST_CANDIDATES)
        self.port_candidates = port_candidates if port_candidates is not None else list(DEFAULT_PORT_CANDIDATES)
        self.local_scan_connect_timeout_s = local_scan_connect_timeout_s if local_scan_connect_timeout_s is not None else LOCAL_SCAN_CONNECT_TIMEOUT_S
        self.local_scan_read_timeout_s = local_scan_read_timeout_s if local_scan_read_timeout_s is not None else LOCAL_SCAN_READ_TIMEOUT_S
        self.openai_models_path = openai_models_path if openai_models_path is not None else OPENAI_MODELS_PATH
        self.ollama_tags_path = ollama_tags_path if ollama_tags_path is not None else OLLAMA_TAGS_PATH
        self.jobs: Dict[str, JobState] = {}
        self._results_lock = asyncio.Lock()

    # ---------- delegate helpers (back-compat for tests that hit server_module._xxx) ----------

    async def _save_job_record(self, job_id: str, record: Dict[str, Any]) -> None:
        await save_job_record(self, job_id, record)

    async def _flush_job_record(self, job: JobState) -> None:
        await flush_job_record(self, job)

    async def _append_job_to_results_store(self, job: JobState) -> None:
        await append_job_to_results_store(self, job)

    async def _load_results_store(self) -> List[Dict[str, Any]]:
        return await load_results_store(self)

    async def migrate_legacy_filenames(self) -> int:
        return await migrate_legacy_filenames(self)

    async def reconcile_stale_records(self) -> int:
        return await reconcile_stale_records(self)

    def _find_record_path(self, job_id: str) -> Optional[Path]:
        return find_record_path(self, job_id)

    def _record_filename(self, job_id: str, created_at_iso: Optional[str]) -> str:
        from python.persistence import record_filename
        return record_filename(self, job_id, created_at_iso)

    @staticmethod
    def _build_results_jsonl(record: Dict[str, Any]) -> str:
        return build_results_jsonl(record)

    @staticmethod
    def _build_results_table(record: Dict[str, Any], *, delimiter: str = ",") -> str:
        from python.persistence import build_results_table
        return build_results_table(record, delimiter=delimiter)

    @staticmethod
    def _build_results_csv(record: Dict[str, Any]) -> str:
        return build_results_csv(record)

    @staticmethod
    def _build_results_tsv(record: Dict[str, Any]) -> str:
        return build_results_tsv(record)

    @staticmethod
    def _build_run_manifest(record: Dict[str, Any]) -> str:
        return build_run_manifest(record)

    @staticmethod
    def _build_run_summary(record: Dict[str, Any]) -> str:
        return build_run_summary(record)

    # ---------- job runner delegates (back-compat for monkeypatching) ----------
    # Tests do ``monkeypatch.setattr(BenchmarkServer, "_benchmark_openai", fake)``
    # etc.; these thin wrappers keep that working.

    async def _run_job(self, job: JobState) -> None:
        from python.job_runner import run_job
        await run_job(self, job)

    async def _run_sql_job(self, job: JobState) -> None:
        from python.job_runner import run_sql_job
        await run_sql_job(self, job)

    async def _run_sequential(self, job: JobState, target: BenchmarkTarget) -> None:
        from python.job_runner import run_sequential
        await run_sequential(self, job, target)

    async def _run_parallel(self, job: JobState, target: BenchmarkTarget) -> None:
        from python.job_runner import run_parallel
        await run_parallel(self, job, target)

    async def _run_single_benchmark(
        self, job: JobState, target: BenchmarkTarget, model: str, run_index: int,
    ) -> Dict[str, Any]:
        from python.job_runner import run_single_benchmark
        return await run_single_benchmark(self, job, target, model, run_index)

    def _stopped_result(
        self, job: JobState, target: BenchmarkTarget, model: str, run_index: int, prompt_hash: str,
    ) -> Dict[str, Any]:
        from python.job_runner import stopped_result
        return stopped_result(job, target, model, run_index, prompt_hash)

    async def _benchmark_openai(
        self, spec, target: BenchmarkTarget, model: str, *,
        job: Optional[JobState] = None, run_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        from python.job_runner import benchmark_openai
        return await benchmark_openai(self, spec, target, model, job=job, run_index=run_index)

    async def _benchmark_ollama(
        self, spec, target: BenchmarkTarget, model: str, *,
        job: Optional[JobState] = None, run_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        from python.job_runner import benchmark_ollama
        return await benchmark_ollama(self, spec, target, model, job=job, run_index=run_index)

    async def _call_llm_single(
        self, system: str, user: str, target: BenchmarkTarget, model: str, timeout_ms: int, *,
        reasoning_effort: str = "disabled",
    ) -> str:
        from python.job_runner import call_llm_single
        return await call_llm_single(self, system, user, target, model, timeout_ms, reasoning_effort=reasoning_effort)

    async def _call_llm_tool_calling(
        self, system_prompt: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
        target: BenchmarkTarget, model: str, timeout_ms: int, *,
        reasoning_effort: str = "disabled", fallback_state: Optional[Dict[str, bool]] = None,
    ) -> Dict[str, Any]:
        from python.job_runner import call_llm_tool_calling
        return await call_llm_tool_calling(
            self, system_prompt, messages, tools, target, model, timeout_ms,
            reasoning_effort=reasoning_effort, fallback_state=fallback_state,
        )

    def _sql_result_row(self, job: JobState, target: BenchmarkTarget, result: Dict[str, Any]) -> Dict[str, Any]:
        from python.job_runner import sql_result_row
        return sql_result_row(job, target, result)

    def _build_report(self, job: JobState) -> str:
        from python.job_runner import build_report
        return build_report(job)

    def _build_speed_report(self, job: JobState) -> str:
        from python.job_runner import build_speed_report
        return build_speed_report(job)

    def _build_sql_report(self, job: JobState) -> str:
        from python.job_runner import build_sql_report
        return build_sql_report(job)

    async def _post_openai_chat_with_reasoning_fallback(
        self, client: httpx.AsyncClient, url: str, payload: Dict[str, Any], *,
        reasoning_effort: str, model: str, fallback_state: Optional[Dict[str, bool]] = None,
    ) -> httpx.Response:
        from python.job_runner import post_openai_chat_with_reasoning_fallback
        return await post_openai_chat_with_reasoning_fallback(
            client, url, payload,
            reasoning_effort=reasoning_effort, model=model, fallback_state=fallback_state,
        )

    @staticmethod
    def _validate_endpoint_url(base_url: str) -> None:
        return _validate_endpoint_url(base_url)

    # ---------- error responses & health ----------

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

    async def version(self, _request: web.Request) -> web.Response:
        """Resolve the running app's version via python._version.get_version_info.

        Source order: $LLM_TESTBENCH_VERSION → ``git describe --tags --abbrev=0``
        → ``VERSION`` file → "dev". Cached after the first call, so polling
        this endpoint is cheap.
        """
        from python._version import get_version_info
        info = get_version_info()
        return web.json_response({
            "status": "ok",
            "version": info["version"],
            "source": info["source"],
            "timestamp": ts_utc(),
        })

    # ---------- benchmark module/preset registry handlers ----------

    async def benchmark_modules(self, _request: web.Request) -> web.Response:
        from python.models import STARTABLE_BENCHMARK_TYPES
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
        from python.models import (
            ADAPTER_LIFECYCLE_HOOKS,
            API_CONTRACT_VERSION,
            EXPORT_SCHEMA_VERSION,
            MODULE_SCHEMA_VERSION,
            RESULT_SCHEMA_VERSION,
            STARTABLE_BENCHMARK_TYPES,
        )
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

    # ---------- fixture handlers ----------

    def _build_fixture_manifest(self) -> Dict[str, Any]:
        from python.server import PROJECT_ROOT, SQL_BENCHMARK_DATA_DIR
        sql_questions_path = SQL_BENCHMARK_DATA_DIR / "questions.json"

        def sql_questions_from_loaded(loaded: Any) -> List[Dict[str, Any]]:
            if isinstance(loaded, list):
                return [item for item in loaded if isinstance(item, dict)]
            if isinstance(loaded, dict) and isinstance(loaded.get("questions"), list):
                return [item for item in loaded["questions"] if isinstance(item, dict)]
            return []

        sql_questions: List[Dict[str, Any]] = []
        if sql_questions_path.exists():
            loaded = _read_json(sql_questions_path)
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
        from python.server import PROJECT_ROOT, SQL_BENCHMARK_DATA_DIR
        errors: List[str] = []
        manifest = self._build_fixture_manifest()

        sql_path = SQL_BENCHMARK_DATA_DIR / "questions.json"
        if not sql_path.exists():
            errors.append("sql: missing questions.json")
        else:
            try:
                loaded = _read_json(sql_path)
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

    # ---------- preset registry handlers ----------

    async def benchmark_presets(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "presets": [preset.to_dict() for preset in BENCHMARK_PRESETS],
            "timestamp": ts_utc(),
        })

    async def benchmark_preset_detail(self, request: web.Request) -> web.Response:
        from python.models import BENCHMARK_PRESETS_BY_ID
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
                    "hooks": list(ADAPTER_LIFECYCLE_HOOKS),
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

    # ---------- dashboard ----------

    async def benchmark_dashboard(self, _request: web.Request) -> web.Response:
        """Aggregate pass-rate, latency, token, and cost metrics across all saved runs.

        Optional query parameters:
          - module: filter by benchmark_type (e.g. "sql", "speed")
          - model:  filter by model name
          - since:  ISO-8601 timestamp; only include runs created at or after this time
        """
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

        def _ms_from_ms_or_s(ms_value: Any, s_value: Any) -> Optional[float]:
            """Normalise legacy *_s rows to milliseconds before averaging."""
            ms = _number(ms_value)
            if ms is not None:
                return ms
            seconds = _number(s_value)
            if seconds is not None:
                return seconds * 1000.0
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

                latency = _ms_from_ms_or_s(row.get("latency_ms"), row.get("latency_s"))
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

    # ---------- index / endpoint scanning / discovery ----------

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

    # ---------- benchmark start/status/stop ----------

    async def benchmark_start(self, request: web.Request) -> web.Response:
        from python.job_runner import run_job
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
        job.task = asyncio.create_task(run_job(self, job))
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
        return await load_job_record(self, job_id, log_label=log_label)

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

    # ---------- results list / clear / active ----------

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
        try:
            counts = await clear_results(self, job_ids, clear_all)
        except ValueError as exc:
            return self.json_error(str(exc), code="invalid_request", status=400)
        return web.json_response(
            {"status": "ok", **counts, "timestamp": ts_utc()}
        )

    async def shutdown(self) -> None:
        """Drain in-flight save tasks for every job.

        Registered as ``app.on_cleanup`` in :func:`create_app`. Without
        this, a SIGTERM between the ``ensure_future`` for an incremental
        save and its ``path.write_text`` would drop the row.
        """
        if not self.jobs:
            return
        await asyncio.gather(*(j.drain_pending_saves() for j in self.jobs.values()), return_exceptions=True)

    # ---------- endpoint scanning & provider detection ----------

    async def _scan_candidates(self) -> List[EndpointCandidate]:
        candidates = self._build_scan_candidates()
        timeout = httpx.Timeout(
            connect=self.local_scan_connect_timeout_s,
            read=self.local_scan_read_timeout_s,
            write=self.local_scan_read_timeout_s,
            pool=self.local_scan_connect_timeout_s,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            providers = await asyncio.gather(
                *(self._probe_provider(candidate["base_url"], client=client) for candidate in candidates)
            )

        results: List[EndpointCandidate] = []
        seen_dedupe_keys: set = set()
        for candidate, provider in zip(candidates, providers):
            if not provider:
                continue
            dedupe_key = self._dedupe_key_for_scan_candidate(candidate["host"], candidate["resolved_targets"])
            if dedupe_key and dedupe_key in seen_dedupe_keys:
                continue
            if dedupe_key:
                seen_dedupe_keys.add(dedupe_key)
            label = "OpenAI-compatible endpoint" if provider == "openai-compatible" else "Ollama endpoint"
            models_path = self.openai_models_path if provider == "openai-compatible" else self.ollama_tags_path
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

    def _resolve_network_targets(self, host: str, port: int) -> frozenset:
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
        resolved_targets: frozenset,
    ):
        if host == "localhost":
            ipv4_targets = tuple(sorted(target for target in resolved_targets if ":" not in target[0]))
            if ipv4_targets:
                return ipv4_targets
        return tuple(sorted(resolved_targets))

    def _build_scan_candidates(self) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen_base_urls: set = set()

        for host in self.host_candidates:
            for port in self.port_candidates:
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
        # base_url is always one of the built-in local candidates built in
        # _build_scan_candidates (host_candidates x port_candidates), so the
        # SSRF guard _validate_endpoint_url is intentionally NOT applied here:
        # every candidate is loopback by construction, and applying the guard
        # would make the automatic local scan always return []. The guard
        # still applies to user-supplied URLs via _benchmark_openai /
        # _benchmark_ollama, which is the path that needs it.
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
            resp = await client.get(f"{base_url}{self.openai_models_path}", headers=headers)
            if resp.status_code >= 400:
                return False
            data = resp.json()
            return isinstance(data.get("data"), list)
        except Exception:
            return False

    async def _looks_like_ollama(self, base_url: str, client: httpx.AsyncClient) -> bool:
        try:
            resp = await client.get(f"{base_url}{self.ollama_tags_path}")
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
        path = self.openai_models_path if provider == "openai-compatible" else self.ollama_tags_path
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


# Re-export to keep the existing `BenchmarkTarget` import surface for tests.
__all__ = ["BenchmarkServer", "BenchmarkTarget"]
