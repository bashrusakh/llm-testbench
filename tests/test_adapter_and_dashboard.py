"""Tests for the BenchmarkAdapter API and /api/benchmark/dashboard endpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from python.adapter import (
    BenchmarkAdapter,
    RunContext,
    SpeedAdapter,
    SqlAdapter,
    ADAPTER_REGISTRY,
    get_adapter,
)
from python.server import BenchmarkServer
import python.server as server_module


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"
DATA_DIR = Path(__file__).resolve().parents[1] / "sql_benchmark_data"


def run(coro):
    return asyncio.run(coro)


# ── adapter registry ──────────────────────────────────────────────────────────

def test_adapter_registry_contains_speed_and_sql():
    assert "speed" in ADAPTER_REGISTRY
    assert "sql" in ADAPTER_REGISTRY


def test_get_adapter_returns_none_for_unknown_module():
    assert get_adapter("terminal-bench") is None
    assert get_adapter("nonexistent") is None


def test_speed_adapter_is_concrete_benchmark_adapter():
    adapter = get_adapter("speed")
    assert isinstance(adapter, BenchmarkAdapter)
    assert isinstance(adapter, SpeedAdapter)


def test_sql_adapter_is_concrete_benchmark_adapter():
    adapter = get_adapter("sql")
    assert isinstance(adapter, BenchmarkAdapter)
    assert isinstance(adapter, SqlAdapter)


def test_adapter_describe_returns_required_fields():
    for adapter in ADAPTER_REGISTRY.values():
        desc = adapter.describe()
        assert desc["module_id"] == adapter.module_id
        assert set(desc["hooks"]) == {
            "prepare", "select_tasks", "run_task", "score", "render", "cleanup"
        }
        assert desc["status"] == "concrete_adapter"
        assert desc["class"] == type(adapter).__name__


# ── SpeedAdapter lifecycle ────────────────────────────────────────────────────

def _speed_ctx(**overrides) -> RunContext:
    opts = {
        "prompt": "Hello",
        "repeat_count": 2,
        "warmup_runs": 1,
    }
    opts.update(overrides)
    return RunContext(
        module_id="speed",
        model="stub-model",
        provider="openai-compatible",
        endpoint="http://127.0.0.1:1234",
        api_key="",
        timeout_ms=5000,
        options=opts,
    )


def test_speed_prepare_accepts_valid_ctx():
    adapter = SpeedAdapter()
    run(adapter.prepare(_speed_ctx()))  # must not raise


def test_speed_prepare_rejects_missing_prompt():
    adapter = SpeedAdapter()
    with pytest.raises(ValueError, match="prompt"):
        run(adapter.prepare(_speed_ctx(prompt="")))


def test_speed_prepare_rejects_repeat_count_zero():
    adapter = SpeedAdapter()
    with pytest.raises(ValueError, match="repeat_count"):
        run(adapter.prepare(_speed_ctx(repeat_count=0)))


def test_speed_select_tasks_returns_warmup_plus_measured():
    adapter = SpeedAdapter()
    ctx = _speed_ctx(repeat_count=3, warmup_runs=1)
    tasks = run(adapter.select_tasks(ctx))
    # 1 warmup (run_index=0) + 3 measured (run_index=1,2,3)
    assert len(tasks) == 4
    assert tasks[0][1] == 0       # warmup
    assert tasks[1][1] == 1       # first real run
    assert tasks[-1][1] == 3


def test_speed_select_tasks_no_warmup():
    adapter = SpeedAdapter()
    ctx = _speed_ctx(repeat_count=2, warmup_runs=0)
    tasks = run(adapter.select_tasks(ctx))
    assert len(tasks) == 2
    assert all(t[1] > 0 for t in tasks)


def test_speed_score_sets_success_from_outcome():
    adapter = SpeedAdapter()
    ctx = _speed_ctx()
    result = run(adapter.score(ctx, {"outcome": "pass"}))
    assert result["success"] is True

    result2 = run(adapter.score(ctx, {"outcome": "fail"}))
    assert result2["success"] is False


def test_speed_render_shows_model_and_run_count():
    adapter = SpeedAdapter()
    ctx = _speed_ctx()
    results = [
        {"run_index": 1, "success": True, "decode_tps": 50.0},
        {"run_index": 2, "success": True, "decode_tps": 60.0},
    ]
    text = adapter.render(ctx, results)
    assert "stub-model" in text
    assert "2" in text  # run count
    assert "60" in text  # best decode_tps


def test_speed_cleanup_is_noop():
    adapter = SpeedAdapter()
    run(adapter.cleanup(_speed_ctx()))  # must not raise


# ── SqlAdapter lifecycle ──────────────────────────────────────────────────────

def _sql_ctx(**overrides) -> RunContext:
    opts = {
        "data_dir": str(DATA_DIR),
        "sql_mode": "grammar",
        "thinking_mode": "off",
        "question_ids": [1],
        "llm_callback": None,
    }
    opts.update(overrides)
    return RunContext(
        module_id="sql",
        model="stub-model",
        provider="openai-compatible",
        endpoint="http://127.0.0.1:1234",
        api_key="",
        timeout_ms=120000,
        options=opts,
    )


def test_sql_prepare_opens_runner_and_stores_in_state():
    adapter = SqlAdapter()
    ctx = _sql_ctx()
    run(adapter.prepare(ctx))
    assert "runner" in ctx.state
    run(adapter.cleanup(ctx))
    assert "runner" not in ctx.state


def test_sql_prepare_rejects_missing_data_dir():
    adapter = SqlAdapter()
    ctx = _sql_ctx(data_dir="")
    with pytest.raises(ValueError, match="data_dir"):
        run(adapter.prepare(ctx))


def test_sql_prepare_rejects_nonexistent_data_dir():
    adapter = SqlAdapter()
    ctx = _sql_ctx(data_dir="/nonexistent/path/xyz")
    with pytest.raises(ValueError, match="does not exist"):
        run(adapter.prepare(ctx))


def test_sql_select_tasks_returns_all_questions_when_none_specified():
    adapter = SqlAdapter()
    ctx = _sql_ctx(question_ids=None)
    run(adapter.prepare(ctx))
    try:
        tasks = run(adapter.select_tasks(ctx))
        assert len(tasks) > 0
        assert all(isinstance(t, int) for t in tasks)
    finally:
        run(adapter.cleanup(ctx))


def test_sql_select_tasks_respects_question_ids_filter():
    adapter = SqlAdapter()
    ctx = _sql_ctx(question_ids=[1])
    run(adapter.prepare(ctx))
    try:
        tasks = run(adapter.select_tasks(ctx))
        assert tasks == [1]
    finally:
        run(adapter.cleanup(ctx))


def test_sql_run_task_grammar_mode_returns_result_dict():
    """run_task in grammar mode calls SqlBenchmarkRunner.run_question."""
    runner_holder = {}

    async def fake_llm(system, user, *, model, provider, endpoint, timeout_ms):
        return runner_holder["runner"].questions_by_id[1]["sql"]

    adapter = SqlAdapter()
    ctx = _sql_ctx(sql_mode="grammar", question_ids=[1], llm_callback=fake_llm)
    run(adapter.prepare(ctx))
    runner_holder["runner"] = ctx.state["runner"]
    try:
        result = run(adapter.run_task(ctx, 1))
        assert result["question_id"] == 1
        assert result["success"] is True
        assert result["outcome"] == "pass"
    finally:
        run(adapter.cleanup(ctx))


def test_sql_score_passes_through_existing_success_field():
    adapter = SqlAdapter()
    ctx = _sql_ctx()
    result = run(adapter.score(ctx, {"success": True, "outcome": "pass"}))
    assert result["success"] is True


def test_sql_render_shows_pass_rate():
    adapter = SqlAdapter()
    ctx = _sql_ctx()
    results = [
        {"question_id": 1, "success": True},
        {"question_id": 2, "success": False, "error": "row_count mismatch"},
    ]
    text = adapter.render(ctx, results)
    assert "stub-model" in text
    assert "50" in text or "1/2" in text or "pass" in text.lower()


def test_sql_cleanup_closes_runner():
    adapter = SqlAdapter()
    ctx = _sql_ctx()
    run(adapter.prepare(ctx))
    runner = ctx.state["runner"]
    run(adapter.cleanup(ctx))
    assert ctx.state.get("runner") is None
    # Second cleanup must be a no-op
    run(adapter.cleanup(ctx))


# ── adapter detail endpoint ───────────────────────────────────────────────────

def test_adapter_detail_endpoint_returns_concrete_for_speed():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"module_id": "speed"}})()
    response = run(server.benchmark_adapter_detail(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["adapter"]["module_id"] == "speed"
    assert payload["adapter"]["status"] == "concrete_adapter"
    assert payload["adapter"]["class"] == "SpeedAdapter"
    assert "prepare" in payload["adapter"]["hooks"]


def test_adapter_detail_endpoint_returns_concrete_for_sql():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"module_id": "sql"}})()
    response = run(server.benchmark_adapter_detail(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["adapter"]["status"] == "concrete_adapter"
    assert payload["adapter"]["class"] == "SqlAdapter"


def test_adapter_detail_endpoint_returns_planned_stub_for_terminal_bench():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"module_id": "terminal-bench"}})()
    response = run(server.benchmark_adapter_detail(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["adapter"]["status"] == "planned_adapter"
    assert payload["adapter"]["class"] is None
    assert "prepare" in payload["adapter"]["hooks"]


def test_adapter_detail_endpoint_404_for_unknown_module():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"module_id": "missing"}})()
    response = run(server.benchmark_adapter_detail(request))
    payload = json.loads(response.text)

    assert response.status == 404
    assert payload["error"]["code"] == "not_found"


def test_contract_endpoint_exposes_adapter_implemented_list():
    server = BenchmarkServer(INDEX_HTML)
    response = run(server.benchmark_contract(None))
    payload = json.loads(response.text)

    assert "adapter_implemented" in payload["modules"]
    assert "speed" in payload["modules"]["adapter_implemented"]
    assert "sql" in payload["modules"]["adapter_implemented"]
    assert payload["endpoints"]["module_adapter"] == "/api/benchmark/modules/{module_id}/adapter"
    assert payload["endpoints"]["dashboard"] == "/api/benchmark/dashboard"


# ── /api/benchmark/dashboard ─────────────────────────────────────────────────

def _make_record(job_id: str, module: str, results: list, created_at: str = "2026-06-04T00:00:00+00:00") -> dict:
    return {
        "job_id": job_id,
        "status": "completed",
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": created_at,
        "request": {"benchmark_type": module, "models": []},
        "progress": {"completed": len(results), "total": len(results)},
        "results": results,
        "errors": [],
    }


def test_dashboard_empty_returns_zero_totals(tmp_path):
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    request = type("Request", (), {"rel_url": type("U", (), {"query": {}})()})()
    response = run(server.benchmark_dashboard(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["dashboard"]["total_runs"] == 0
    assert payload["dashboard"]["by_module"] == {}


def test_dashboard_aggregates_pass_rate_and_latency(tmp_path):
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    record = _make_record("job-1", "sql", [
        {"model": "model-a", "success": True,  "latency_ms": 100, "cost": 0.01, "input_tokens": 10, "output_tokens": 5},
        {"model": "model-a", "success": False, "latency_ms": 200, "cost": 0.02, "input_tokens": 12, "output_tokens": 6},
        {"model": "model-b", "success": True,  "latency_ms": 300, "cost": 0.03, "input_tokens": 14, "output_tokens": 7},
    ])
    run(server._save_job_record("job-1", record))

    request = type("Request", (), {"rel_url": type("U", (), {"query": {}})()})()
    response = run(server.benchmark_dashboard(request))
    payload = json.loads(response.text)

    db = payload["dashboard"]
    assert db["total_runs"] == 1
    sql_module = db["by_module"]["sql"]

    assert sql_module["result_count"] == 3
    assert sql_module["pass_count"] == 2
    assert sql_module["fail_count"] == 1
    assert sql_module["pass_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert sql_module["avg_latency_ms"] == pytest.approx(200.0)
    assert sql_module["total_cost"] == pytest.approx(0.06, abs=1e-8)
    assert sql_module["total_tokens"] == 54  # (10+5)+(12+6)+(14+7)

    ma = sql_module["by_model"]["model-a"]
    assert ma["count"] == 2
    assert ma["pass_rate"] == 0.5
    assert ma["avg_latency_ms"] == pytest.approx(150.0)


def test_dashboard_module_filter(tmp_path):
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    run(server._save_job_record("job-sql", _make_record("job-sql", "sql", [
        {"model": "m", "success": True},
    ])))
    run(server._save_job_record("job-speed", _make_record("job-speed", "speed", [
        {"model": "m", "success": True},
    ])))

    req = type("Request", (), {"rel_url": type("U", (), {"query": {"module": "sql"}})()})()
    response = run(server.benchmark_dashboard(req))
    db = json.loads(response.text)["dashboard"]

    assert "sql" in db["by_module"]
    assert "speed" not in db["by_module"]


def test_dashboard_model_filter(tmp_path):
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    run(server._save_job_record("job-1", _make_record("job-1", "sql", [
        {"model": "model-a", "success": True},
        {"model": "model-b", "success": False},
    ])))
    run(server._save_job_record("job-2", _make_record("job-2", "sql", [
        {"model": "model-b", "success": True},
    ])))

    req = type("Request", (), {"rel_url": type("U", (), {"query": {"model": "model-a"}})()})()
    response = run(server.benchmark_dashboard(req))
    db = json.loads(response.text)["dashboard"]

    sql_m = db["by_module"]["sql"]
    assert db["total_runs"] == 1
    assert sql_m["run_count"] == 1
    assert sql_m["result_count"] == 1
    assert "model-a" in sql_m["by_model"]
    assert "model-b" not in sql_m["by_model"]


def test_dashboard_since_filter(tmp_path):
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    run(server._save_job_record("old", _make_record("old", "sql", [
        {"model": "m", "success": True},
    ], created_at="2025-01-01T00:00:00+00:00")))
    run(server._save_job_record("new", _make_record("new", "sql", [
        {"model": "m", "success": True},
    ], created_at="2026-06-01T00:00:00+00:00")))

    req = type("Request", (), {"rel_url": type("U", (), {"query": {"since": "2026-01-01T00:00:00+00:00"}})()})()
    response = run(server.benchmark_dashboard(req))
    db = json.loads(response.text)["dashboard"]

    assert db["total_runs"] == 1
    assert db["by_module"]["sql"]["result_count"] == 1


def test_dashboard_multi_run_aggregation(tmp_path):
    """Pass-rate is computed across all runs' results, not per run."""
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    run(server._save_job_record("job-1", _make_record("job-1", "sql", [
        {"model": "m", "success": True},
        {"model": "m", "success": True},
    ])))
    run(server._save_job_record("job-2", _make_record("job-2", "sql", [
        {"model": "m", "success": False},
        {"model": "m", "success": False},
    ])))

    req = type("Request", (), {"rel_url": type("U", (), {"query": {}})()})()
    response = run(server.benchmark_dashboard(req))
    db = json.loads(response.text)["dashboard"]

    sql_m = db["by_module"]["sql"]
    assert sql_m["run_count"] == 2
    assert sql_m["result_count"] == 4
    assert sql_m["pass_count"] == 2
    assert sql_m["pass_rate"] == 0.5
