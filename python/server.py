#!/usr/bin/env python3
"""LLM Testbench server — entry point.

This module is intentionally small. The heavy lifting is split across:

- :mod:`python.models`      — dataclasses, registries, request parsing
- :mod:`python.aggregates`  — per-model speed-run aggregation
- :mod:`python.json_io`     — ``ts_utc``, JSON read helpers
- :mod:`python.ssrf`        — endpoint URL safety check
- :mod:`python.speed_row`   — per-run row builder
- :mod:`python.persistence` — record save/load/migrate/reconcile/clear/export
- :mod:`python.job_runner`  — per-job execution, LLM calls, run_*/benchmark_*
- :mod:`python.benchmark_server` — ``BenchmarkServer`` HTTP route handlers

``server.py`` wires paths, config constants, the aiohttp app, the
shutdown hook, and the CLI ``main``. It also re-exports a handful of
names that older tests and ``server_module.httpx`` monkeypatching
expect to find on this module.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import httpx  # re-exported so ``server_module.httpx.AsyncClient`` monkeypatching works
from aiohttp import web

# Re-export names tests and ``server_module.httpx`` monkeypatching expect.
# These are the public surface; keep them in sync with what the test files
# import. (``tests/test_speed_unit.py`` etc.)
from python.aggregates import _compute_speed_aggregates  # noqa: F401
from python.benchmark_server import BenchmarkServer  # noqa: F401
from python.json_io import _load_json, _read_json, ts_utc  # noqa: F401
from python.models import (  # noqa: F401
    ADAPTER_LIFECYCLE_HOOKS,
    API_CONTRACT_VERSION,
    BENCHMARK_MODULES,
    BENCHMARK_PRESETS,
    EXPORT_SCHEMA_VERSION,
    MODULE_SCHEMA_VERSION,
    BenchmarkRequest,
    BenchmarkTarget,
    JobState,
)
from python.sql_benchmark import SqlBenchmarkRunner  # noqa: F401  (re-export for test monkeypatch)
from python.ssrf import _validate_endpoint_url  # noqa: F401

LOG = logging.getLogger("llm_testbench")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "index.html"
SQL_BENCHMARK_DATA_DIR = PROJECT_ROOT / "sql_benchmark_data"
RESULTS_STORE_DIR = PROJECT_ROOT / "benchmarks"
DEFAULT_ALLOWED_ORIGINS = {"*"}
DEFAULT_PORT_CANDIDATES = [1234, 8080, 11434, 5000, 5001]
DEFAULT_HOST_CANDIDATES = ["127.0.0.1", "localhost"]
LOCAL_SCAN_CONNECT_TIMEOUT_S = 0.5
LOCAL_SCAN_READ_TIMEOUT_S = .5
OPENAI_MODELS_PATH = "/v1/models"
OLLAMA_TAGS_PATH = "/api/tags"


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cors_middleware(allowed_origins: set = DEFAULT_ALLOWED_ORIGINS):
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


def _build_server() -> BenchmarkServer:
    """Construct a BenchmarkServer with the standard path/config bundle."""
    return BenchmarkServer(
        index_html_path=INDEX_HTML,
        results_store_dir=RESULTS_STORE_DIR,
        sql_benchmark_data_dir=SQL_BENCHMARK_DATA_DIR,
        host_candidates=DEFAULT_HOST_CANDIDATES,
        port_candidates=DEFAULT_PORT_CANDIDATES,
        local_scan_connect_timeout_s=LOCAL_SCAN_CONNECT_TIMEOUT_S,
        local_scan_read_timeout_s=LOCAL_SCAN_READ_TIMEOUT_S,
        openai_models_path=OPENAI_MODELS_PATH,
        ollama_tags_path=OLLAMA_TAGS_PATH,
    )


async def create_app() -> web.Application:
    server = _build_server()
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
    app.on_cleanup.append(lambda _app: server.shutdown())
    # Serve /static/style.css and /static/app.js extracted from index.html
    static_dir = PROJECT_ROOT / "static"
    if static_dir.is_dir():
        app.router.add_static("/static/", static_dir, show_index=False)
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
