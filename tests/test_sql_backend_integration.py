import asyncio
import csv
import io
import json
from pathlib import Path

import pytest

from python.server import BenchmarkRequest, BenchmarkServer, BenchmarkTarget, JobState
import python.server as server_module


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"


def run(coro):
    return asyncio.run(coro)


def test_benchmark_module_registry_exposes_current_and_planned_modules():
    modules = [module.to_dict() for module in server_module.BENCHMARK_MODULES]
    by_id = {module["id"]: module for module in modules}

    assert by_id["speed"]["startable"] is True
    assert by_id["sql"]["status"] == "implemented"
    assert by_id["coding-micro"]["status"] == "fixture_ready"
    assert by_id["json-schema"]["status"] == "fixture_ready"
    assert by_id["prompt-replay"]["status"] == "fixture_ready"
    assert "tool-calling" in by_id["sql"]["capabilities"]
    assert "DuckDB" in by_id["sql"]["setup_requirements"]
    assert by_id["sql"]["task_selection"]["strategy"] == "question_ids"
    assert by_id["sql"]["scoring"]["primary_metric"] == "pass_rate"
    assert by_id["sql"]["ui_renderer"]["detail_panel"] == "sql_diff"
    assert by_id["sql"]["adapter_lifecycle"]["status"] == "implemented_inline"
    assert by_id["sql"]["adapter_lifecycle"]["entrypoint"] == "BenchmarkServer._run_sql_job"
    assert by_id["coding-micro"]["task_selection"]["strategy"] == "fixture_ids"
    assert by_id["json-schema"]["scoring"]["primary_metric"] == "pass_rate"
    assert by_id["prompt-replay"]["setup_requirements"] == ["prompt_replay_data/tasks.jsonl"]


def test_benchmark_modules_endpoint_returns_registry_metadata():
    server = BenchmarkServer(INDEX_HTML)

    response = run(server.benchmark_modules(None))
    payload = json.loads(response.text)
    by_id = {module["id"]: module for module in payload["modules"]}

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["startable"] == ["speed", "sql"]
    assert by_id["coding-micro"]["status"] == "fixture_ready"
    assert by_id["json-schema"]["status"] == "fixture_ready"
    assert by_id["prompt-replay"]["status"] == "fixture_ready"
    assert by_id["speed"]["result_schema"]
    assert by_id["speed"]["task_selection"]["fields"] == ["models", "prompt", "repeat_count", "warmup_runs"]
    assert by_id["speed"]["ui_renderer"]["kind"] == "speed_table"
    assert by_id["speed"]["adapter_lifecycle"]["hooks"] == server_module.ADAPTER_LIFECYCLE_HOOKS
    assert by_id["speed"]["adapter_lifecycle"]["entrypoint"] == "BenchmarkServer._run_single_benchmark"


def test_benchmark_module_detail_endpoint_returns_single_module():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"module_id": "sql"}})()

    response = run(server.benchmark_module_detail(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["module"]["id"] == "sql"
    assert payload["module"]["startable"] is True
    assert "tool-calling" in payload["module"]["capabilities"]
    assert payload["module"]["task_selection"]["strategy"] == "question_ids"
    assert "DuckDB" in payload["module"]["setup_requirements"]
    assert payload["module"]["scoring"]["primary_metric"] == "pass_rate"
    assert payload["module"]["ui_renderer"]["kind"] == "sql_results"
    assert payload["module"]["adapter_lifecycle"]["hooks"] == server_module.ADAPTER_LIFECYCLE_HOOKS


def test_benchmark_module_detail_endpoint_rejects_unknown_module():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"module_id": "missing"}})()

    response = run(server.benchmark_module_detail(request))
    payload = json.loads(response.text)

    assert response.status == 404
    assert payload["error"]["code"] == "not_found"


def test_benchmark_contract_endpoint_returns_schema_versions_and_routes():
    server = BenchmarkServer(INDEX_HTML)

    response = run(server.benchmark_contract(None))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["contract_version"] == server_module.API_CONTRACT_VERSION
    assert payload["schema_versions"]["module"] == server_module.MODULE_SCHEMA_VERSION
    assert payload["adapter_lifecycle_hooks"] == server_module.ADAPTER_LIFECYCLE_HOOKS
    assert payload["modules"]["startable"] == ["speed", "sql"]
    assert payload["presets"]["ids"] == ["local-smoke", "balanced", "leaderboard-full"]
    assert payload["presets"]["scopes"] == ["comparison", "leaderboard", "smoke"]
    assert payload["endpoints"]["module_detail"] == "/api/benchmark/modules/{module_id}"
    assert payload["endpoints"]["preset_detail"] == "/api/benchmark/presets/{preset_id}"
    assert payload["endpoints"]["fixtures"] == "/api/fixtures"
    assert payload["endpoints"]["fixture_validation"] == "/api/fixtures/validate"
    assert payload["exports"]["summary"] == "/api/benchmark/{job_id}/summary.json"


def test_benchmark_preset_registry_exposes_smoke_and_full_profiles():
    presets = [preset.to_dict() for preset in server_module.BENCHMARK_PRESETS]
    by_id = {preset["id"]: preset for preset in presets}

    assert by_id["local-smoke"]["scope"] == "smoke"
    assert by_id["local-smoke"]["module_defaults"]["speed"]["repeat_count"] == 1
    assert by_id["local-smoke"]["module_defaults"]["sql"]["question_ids"] == [1, 2, 3]
    assert by_id["leaderboard-full"]["module_defaults"]["speed"]["repeat_count"] >= by_id["balanced"]["module_defaults"]["speed"]["repeat_count"]
    assert by_id["leaderboard-full"]["module_defaults"]["sql"]["question_timeout_ms"] >= by_id["balanced"]["module_defaults"]["sql"]["question_timeout_ms"]


def test_benchmark_presets_endpoint_returns_payload_defaults():
    server = BenchmarkServer(INDEX_HTML)

    response = run(server.benchmark_presets(None))
    payload = json.loads(response.text)
    by_id = {preset["id"]: preset for preset in payload["presets"]}

    assert response.status == 200
    assert payload["status"] == "ok"
    assert by_id["balanced"]["module_defaults"]["sql"]["thinking_mode"] == "both"
    assert by_id["leaderboard-full"]["scope"] == "leaderboard"


def test_benchmark_preset_detail_endpoint_returns_single_preset():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"preset_id": "local-smoke"}})()

    response = run(server.benchmark_preset_detail(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["preset"]["id"] == "local-smoke"
    assert payload["preset"]["scope"] == "smoke"
    assert payload["preset"]["module_defaults"]["sql"]["question_ids"] == [1, 2, 3]


def test_benchmark_preset_detail_endpoint_rejects_unknown_preset():
    server = BenchmarkServer(INDEX_HTML)
    request = type("Request", (), {"match_info": {"preset_id": "missing"}})()

    response = run(server.benchmark_preset_detail(request))
    payload = json.loads(response.text)

    assert response.status == 404
    assert payload["error"]["code"] == "not_found"


def test_fixture_manifest_endpoint_reports_local_fixture_counts():
    server = BenchmarkServer(INDEX_HTML)

    response = run(server.fixture_manifest(None))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["manifest"]["schema_version"] == 1
    assert payload["manifest"]["fixtures"]["sql"]["task_count"] > 0
    assert payload["manifest"]["fixtures"]["sql"]["local_only"] is True
    assert payload["manifest"]["fixtures"]["coding-micro"]["task_count"] == 3
    assert payload["manifest"]["fixtures"]["json-schema"]["task_count"] == 3
    assert payload["manifest"]["fixtures"]["prompt-replay"]["task_count"] == 3


def test_fixture_validation_endpoint_accepts_repo_fixtures():
    server = BenchmarkServer(INDEX_HTML)

    response = run(server.fixture_validation(None))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["validation"]["ok"] is True
    assert payload["validation"]["errors"] == []


def test_benchmark_request_rejects_planned_modules_until_adapter_exists():
    with pytest.raises(ValueError, match="benchmark_type"):
        BenchmarkRequest.from_dict({
            "benchmark_type": "coding-micro",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "tool-model",
        })


def test_benchmark_request_accepts_sql_payload_without_prompt():
    spec = BenchmarkRequest.from_dict(
        {
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "api_key": "",
            "model": "sql-model",
            "timeout_ms": 120000,
            "question_ids": [1, 7],
        }
    )

    assert spec.benchmark_type == "sql"
    assert spec.prompt == ""
    assert spec.models == ["sql-model"]
    assert spec.targets[0].models == ["sql-model"]
    assert spec.question_ids == [1, 7]


def test_benchmark_request_accepts_multiple_models_for_sql_mode():
    spec = BenchmarkRequest.from_dict(
        {
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "models": ["model-a", "model-b"],
        }
    )
    assert spec.benchmark_type == "sql"
    assert sorted(spec.models) == ["model-a", "model-b"]


def test_results_jsonl_export_builds_one_line_per_result():
    record = {
        "job_id": "job-123",
        "status": "completed",
        "created_at": "2026-06-04T00:00:00+00:00",
        "started_at": "2026-06-04T00:00:01+00:00",
        "finished_at": "2026-06-04T00:00:02+00:00",
        "request": {"benchmark_type": "sql", "models": ["sql-model"]},
        "progress": {"completed": 2, "total": 2},
        "results": [
            {"benchmark_type": "sql", "model": "sql-model", "outcome": "pass"},
            {"benchmark_type": "sql", "model": "sql-model", "outcome": "fail"},
        ],
    }

    text = BenchmarkServer._build_results_jsonl(record)
    rows = [json.loads(line) for line in text.splitlines()]

    assert text.endswith("\n")
    assert len(rows) == 2
    assert rows[0]["job_id"] == "job-123"
    assert rows[0]["benchmark_type"] == "sql"
    assert rows[0]["request"]["models"] == ["sql-model"]
    assert rows[0]["progress"]["completed"] == 2
    assert rows[0]["result_index"] == 0
    assert rows[0]["result"]["outcome"] == "pass"
    assert rows[1]["result_index"] == 1
    assert rows[1]["result"]["outcome"] == "fail"


def test_results_csv_export_builds_spreadsheet_rows():
    record = {
        "job_id": "job-123",
        "status": "completed",
        "request": {"benchmark_type": "sql", "models": ["sql-model"]},
        "results": [
            {
                "benchmark_type": "sql",
                "model": "sql-model",
                "provider": "openai-compatible",
                "outcome": "pass",
                "success": True,
                "question_id": 7,
                "generated_sql": "SELECT 1",
            },
        ],
    }

    text = BenchmarkServer._build_results_csv(record)
    lines = text.splitlines()
    rows = list(csv.DictReader(io.StringIO(text)))

    assert lines[0].startswith("job_id,status,benchmark_type,result_index")
    assert len(lines) == 2
    assert rows[0]["job_id"] == "job-123"
    assert rows[0]["benchmark_type"] == "sql"
    assert rows[0]["model"] == "sql-model"
    assert rows[0]["provider"] == "openai-compatible"
    assert rows[0]["success"] == "True"
    assert rows[0]["question_id"] == "7"
    result_payload = json.loads(rows[0]["result_json"])
    assert result_payload["success"] is True
    assert result_payload["generated_sql"] == "SELECT 1"


def test_results_tsv_export_uses_tab_delimited_rows():
    record = {
        "job_id": "job-123",
        "status": "completed",
        "request": {"benchmark_type": "sql", "models": ["sql-model"]},
        "results": [
            {
                "benchmark_type": "sql",
                "model": "sql-model",
                "provider": "openai-compatible",
                "outcome": "pass",
                "success": True,
                "question_id": 7,
                "generated_sql": "SELECT 1",
            },
        ],
    }

    text = BenchmarkServer._build_results_tsv(record)
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))

    assert text.splitlines()[0].startswith("job_id\tstatus\tbenchmark_type\tresult_index")
    assert len(rows) == 1
    assert rows[0]["job_id"] == "job-123"
    assert rows[0]["benchmark_type"] == "sql"
    assert rows[0]["model"] == "sql-model"
    assert rows[0]["generated_sql"] == "SELECT 1"
    assert json.loads(rows[0]["result_json"])["success"] is True


def test_run_manifest_export_summarizes_record_without_results_array():
    record = {
        "job_id": "job-123",
        "status": "completed",
        "created_at": "2026-06-04T00:00:00+00:00",
        "started_at": "2026-06-04T00:00:01+00:00",
        "finished_at": "2026-06-04T00:00:02+00:00",
        "request": {"benchmark_type": "sql", "models": ["sql-model"]},
        "progress": {"completed": 2, "total": 2},
        "errors": ["one warning"],
        "results": [
            {"benchmark_type": "sql", "model": "sql-model", "provider_label": "Local", "outcome": "pass"},
            {"benchmark_type": "sql", "model": "sql-model", "provider_label": "Local", "outcome": "fail"},
        ],
    }

    manifest = json.loads(BenchmarkServer._build_run_manifest(record))

    assert manifest["schema_version"] == 1
    assert manifest["job_id"] == "job-123"
    assert manifest["benchmark_type"] == "sql"
    assert manifest["request"]["models"] == ["sql-model"]
    assert manifest["result_count"] == 2
    assert manifest["error_count"] == 1
    assert manifest["models"] == ["sql-model"]
    assert manifest["providers"] == ["Local"]
    assert manifest["outcomes"] == {"pass": 1, "fail": 1}
    assert manifest["export_endpoints"]["tsv"] == "/api/benchmark/job-123/results.tsv"
    assert manifest["export_endpoints"]["summary"] == "/api/benchmark/job-123/summary.json"
    assert "results" not in manifest


def test_run_summary_export_builds_dashboard_metrics_by_model():
    record = {
        "job_id": "job-123",
        "status": "completed",
        "request": {"benchmark_type": "sql", "models": ["sql-a", "sql-b"]},
        "progress": {"completed": 3, "total": 3},
        "errors": ["warning"],
        "results": [
            {
                "model": "sql-a",
                "success": True,
                "latency_ms": 100,
                "total_time_ms": 150,
                "ttft_ms": 25,
                "decode_tps": 40,
                "input_tokens": 10,
                "output_tokens": 5,
                "cost": 0.01,
            },
            {
                "model": "sql-a",
                "success": False,
                "latency_ms": 300,
                "total_time_ms": 450,
                "ttft_ms": 75,
                "decode_tps": 20,
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "cost": 0.02,
            },
            {
                "model": "sql-b",
                "success": True,
                "latency_ms": 200,
                "total_time_ms": 300,
                "ttft_ms": 50,
                "decode_tps": 30,
                "input_tokens": 14,
                "output_tokens": 7,
                "cost": 0.03,
            },
        ],
    }

    summary = json.loads(BenchmarkServer._build_run_summary(record))

    assert summary["result_count"] == 3
    assert summary["pass_count"] == 2
    assert summary["fail_count"] == 1
    assert summary["pass_rate"] == 0.6667
    assert summary["error_count"] == 1
    assert summary["latency"]["avg_latency_ms"] == 200
    assert summary["latency"]["avg_total_time_ms"] == 300
    assert summary["latency"]["avg_ttft_ms"] == 50
    assert summary["latency"]["avg_decode_tps"] == 30
    assert summary["tokens"]["prompt_tokens"] == 36
    assert summary["tokens"]["completion_tokens"] == 18
    assert summary["tokens"]["total_tokens"] == 54
    assert summary["cost"]["total"] == 0.06
    assert summary["models"]["sql-a"]["count"] == 2
    assert summary["models"]["sql-a"]["pass_rate"] == 0.5
    assert summary["models"]["sql-a"]["avg_latency_ms"] == 200
    assert summary["models"]["sql-b"]["pass_rate"] == 1.0


def test_benchmark_summaries_list_returns_compact_saved_run_aggregates(tmp_path):
    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"
    record = {
        "job_id": "job-123",
        "created_at": "2026-06-04T00:00:00+00:00",
        "status": "completed",
        "request": {"benchmark_type": "sql", "models": ["sql-model"]},
        "progress": {"completed": 2, "total": 2},
        "results": [
            {"model": "sql-model", "success": True, "latency_ms": 100},
            {"model": "sql-model", "success": False, "latency_ms": 300},
        ],
        "errors": [],
    }

    run(server._save_job_record("job-123", record))
    response = run(server.benchmark_summaries_list(None))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert len(payload["summaries"]) == 1
    assert payload["summaries"][0]["job_id"] == "job-123"
    assert payload["summaries"][0]["pass_rate"] == 0.5
    assert payload["summaries"][0]["latency"]["avg_latency_ms"] == 200
    assert "results" not in payload["summaries"][0]


def test_sql_job_flow_builds_sql_report_tsv_and_history(tmp_path, monkeypatch):
    captured = {}

    class FakeSqlBenchmarkRunner:
        def __init__(self, llm_callback, data_dir):
            captured["callback"] = llm_callback
            captured["data_dir"] = str(data_dir)
            self.questions_by_id = {7: {"id": 7}}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        async def run_question(self, *, question_id, model, provider, endpoint, timeout_ms, thinking_mode="off"):
            captured["run_question"] = {
                "question_id": question_id,
                "model": model,
                "provider": provider,
                "endpoint": endpoint,
                "timeout_ms": timeout_ms,
            }
            return {
                "benchmark_type": "sql",
                "question_id": question_id,
                "difficulty": "medium",
                "question": "Count rows",
                "model": model,
                "thinking_mode": thinking_mode,
                "provider": provider,
                "endpoint": endpoint,
                "generated_sql": "SELECT 1",
                "expected_sql": "SELECT 1",
                "success": True,
                "error": "",
                "expected_row_count": 1,
                "actual_row_count": 1,
                "expected_columns": ["value"],
                "actual_columns": ["value"],
                "expected_first_row": {"value": 1},
                "actual_first_row": {"value": 1},
                "row_count_match": True,
                "columns_match": True,
                "first_row_match": True,
            }

        async def run_question_tool_calling(self, *, question_id, model, provider, endpoint, timeout_ms, tool_llm_callback, abort_signal=None, thinking_mode="on", question_timeout_ms=0, warmed_models=None):
            captured["run_question_tool_calling"] = {
                "question_id": question_id,
                "model": model,
                "provider": provider,
                "endpoint": endpoint,
                "timeout_ms": timeout_ms,
                "thinking_mode": thinking_mode,
                "question_timeout_ms": question_timeout_ms,
            }
            return {
                "benchmark_type": "sql",
                "question_id": question_id,
                "difficulty": "medium",
                "question": "Count rows",
                "model": model,
                "thinking_mode": thinking_mode,
                "provider": provider,
                "endpoint": endpoint,
                "generated_sql": "SELECT 1",
                "expected_sql": "SELECT 1",
                "success": True,
                "error": "",
                "expected_row_count": 1,
                "actual_row_count": 1,
                "expected_columns": ["value"],
                "actual_columns": ["value"],
                "expected_first_row": {"value": 1},
                "actual_first_row": {"value": 1},
                "row_count_match": True,
                "columns_match": True,
                "first_row_match": True,
                "attempts": 1,
                "tool_calls": 2,
                "first_row_diffs": [],
                "input_tokens": 100,
                "output_tokens": 50,
                "cost": 0.001,
            }

    async def fake_detect_provider(self, base_url, requested_provider, api_key, client=None):
        return "openai-compatible"

    async def fake_discover_models(self, base_url, provider, api_key):
        return ["sql-model"]

    monkeypatch.setattr(server_module, "SqlBenchmarkRunner", FakeSqlBenchmarkRunner)
    monkeypatch.setattr(BenchmarkServer, "_detect_provider", fake_detect_provider)
    monkeypatch.setattr(BenchmarkServer, "_discover_models", fake_discover_models)

    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    spec = BenchmarkRequest.from_dict(
        {
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "sql-model",
            "timeout_ms": 120000,
            "question_ids": [7],
        }
    )
    job = JobState(request=spec)
    job.progress_total = 1

    run(server._run_job(job))

    assert job.status == "completed"
    assert job.progress_completed == 1
    assert len(job.results) == 1
    assert callable(captured["callback"])
    assert captured["run_question_tool_calling"]["question_id"] == 7
    assert captured["run_question_tool_calling"]["model"] == "sql-model"
    assert captured["run_question_tool_calling"]["provider"] == "openai-compatible"
    assert captured["run_question_tool_calling"]["endpoint"] == "http://127.0.0.1:1234"
    assert captured["run_question_tool_calling"]["timeout_ms"] == 120000

    row = job.results[0]
    assert row["job_id"] == job.job_id
    assert row["benchmark_type"] == "sql"
    assert row["provider_id"] == "legacy-target"
    assert row["provider_label"] == "http://127.0.0.1:1234"
    assert row["provider_type"] == "openai-compatible"

    assert "Benchmark type: sql" in job.report_text
    assert "Questions passed: 1/1" in job.report_text

    saved_files = list(server.results_store_dir.glob("*.json"))
    assert len(saved_files) == 1
    stored = json.loads(saved_files[0].read_text("utf-8"))
    assert stored["request"]["benchmark_type"] == "sql"
    assert stored["request"]["question_ids"] == [7]
    assert stored["request"]["sql_mode"] == "tool-calling"


def test_benchmark_request_sql_mode_defaults_to_tool_calling():
    spec = BenchmarkRequest.from_dict({
        "benchmark_type": "sql",
        "base_url": "http://127.0.0.1:1234",
        "provider": "openai-compatible",
        "model": "sql-model",
    })
    assert spec.sql_mode == "tool-calling"


def test_benchmark_request_accepts_grammar_sql_mode():
    spec = BenchmarkRequest.from_dict({
        "benchmark_type": "sql",
        "base_url": "http://127.0.0.1:1234",
        "provider": "openai-compatible",
        "model": "sql-model",
        "sql_mode": "grammar",
    })
    assert spec.sql_mode == "grammar"


def test_benchmark_request_rejects_invalid_sql_mode():
    with pytest.raises(ValueError, match="sql_mode"):
        BenchmarkRequest.from_dict({
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "sql-model",
            "sql_mode": "unknown-mode",
        })


def test_benchmark_request_thinking_mode_defaults_to_off():
    spec = BenchmarkRequest.from_dict({
        "benchmark_type": "sql",
        "base_url": "http://127.0.0.1:1234",
        "provider": "openai-compatible",
        "model": "sql-model",
    })
    assert spec.thinking_mode == "off"


def test_benchmark_request_accepts_thinking_modes():
    for mode in ("off", "on", "both"):
        spec = BenchmarkRequest.from_dict({
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "sql-model",
            "thinking_mode": mode,
        })
        assert spec.thinking_mode == mode


def test_benchmark_request_rejects_invalid_thinking_mode():
    with pytest.raises(ValueError, match="thinking_mode"):
        BenchmarkRequest.from_dict({
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "sql-model",
            "thinking_mode": "fast",
        })


def test_benchmark_request_reasoning_effort_defaults_to_disabled():
    spec = BenchmarkRequest.from_dict({
        "benchmark_type": "sql",
        "base_url": "http://127.0.0.1:1234",
        "provider": "openai-compatible",
        "model": "sql-model",
    })
    assert spec.reasoning_effort == "disabled"


def test_benchmark_request_accepts_reasoning_efforts():
    for effort in ("disabled", "none", "minimal", "low", "medium", "high", "xhigh"):
        spec = BenchmarkRequest.from_dict({
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "sql-model",
            "reasoning_effort": effort,
        })
        assert spec.reasoning_effort == effort


def test_benchmark_request_rejects_invalid_reasoning_effort():
    with pytest.raises(ValueError, match="reasoning_effort"):
        BenchmarkRequest.from_dict({
            "benchmark_type": "sql",
            "base_url": "http://127.0.0.1:1234",
            "provider": "openai-compatible",
            "model": "sql-model",
            "reasoning_effort": "maximum",
        })


def test_openai_tool_call_reasoning_falls_back_when_unsupported(monkeypatch):
    requests = []

    class FakeResponse:
        def __init__(self, status_code, text="", payload=None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            requests.append(json)
            if len(requests) == 1:
                return FakeResponse(400, "Unsupported field: reasoning")
            return FakeResponse(200, payload={
                "model": "resolved-model",
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "results_ok", "arguments": "{}"},
                        }],
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)
    server = BenchmarkServer(INDEX_HTML)
    target = BenchmarkTarget(
        provider_id="p",
        provider_label="Provider",
        base_url="http://example.test",
        provider="openai-compatible",
        api_key="",
        models=["sql-model"],
    )
    fallback_state = {"used": False}

    result = run(server._call_llm_tool_calling(
        system_prompt="system",
        messages=[{"role": "user", "content": "question"}],
        tools=[],
        target=target,
        model="sql-model",
        timeout_ms=120000,
        reasoning_effort="medium",
        fallback_state=fallback_state,
    ))

    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert "reasoning" not in requests[1]
    assert fallback_state["used"] is True
    assert result["reasoning_fallback"] is True
    assert result["tool_calls"][0]["function"]["name"] == "results_ok"


def test_openai_single_prefers_content_over_reasoning_content(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": "SELECT 1",
                        "reasoning_content": "I should think about SELECT 1",
                    }
                }]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            requests.append(json)
            return FakeResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(server_module, "_validate_endpoint_url", lambda *a, **kw: None)
    server = BenchmarkServer(INDEX_HTML)
    target = BenchmarkTarget(
        provider_id="p",
        provider_label="Provider",
        base_url="http://example.test",
        provider="openai-compatible",
        api_key="",
        models=["sql-model"],
    )

    result = run(server._call_llm_single(
        "system",
        "question",
        target,
        "sql-model",
        120000,
        reasoning_effort="disabled",
    ))

    assert result == "SELECT 1"
    assert "reasoning" not in requests[0]


def test_sql_grammar_mode_routes_to_run_question(tmp_path, monkeypatch):
    """When sql_mode='grammar', _run_sql_job calls run_question, not run_question_tool_calling."""
    captured = {}

    class FakeSqlBenchmarkRunner:
        def __init__(self, llm_callback, data_dir):
            captured["callback"] = llm_callback
            self.questions_by_id = {1: {"id": 1}}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        async def run_question(self, *, question_id, model, provider, endpoint, timeout_ms, thinking_mode="off"):
            captured["called"] = "run_question"
            return {
                "benchmark_type": "sql",
                "question_id": question_id,
                "difficulty": "easy",
                "question": "test",
                "model": model,
                "thinking_mode": thinking_mode,
                "provider": provider,
                "endpoint": endpoint,
                "generated_sql": "SELECT 1",
                "expected_sql": "SELECT 1",
                "success": True,
                "outcome": "pass",
                "error": "",
                "expected_row_count": 1,
                "actual_row_count": 1,
                "expected_columns": ["v"],
                "actual_columns": ["v"],
                "expected_first_row": None,
                "actual_first_row": None,
                "row_count_match": True,
                "columns_match": True,
                "first_row_match": True,
                "conversation": [],
            }

        async def run_question_tool_calling(self, **kwargs):
            captured["called"] = "run_question_tool_calling"
            raise AssertionError("should not be called in grammar mode")

    async def fake_detect_provider(self, base_url, requested_provider, api_key, client=None):
        return "openai-compatible"

    async def fake_discover_models(self, base_url, provider, api_key):
        return ["sql-model"]

    monkeypatch.setattr(server_module, "SqlBenchmarkRunner", FakeSqlBenchmarkRunner)
    monkeypatch.setattr(BenchmarkServer, "_detect_provider", fake_detect_provider)
    monkeypatch.setattr(BenchmarkServer, "_discover_models", fake_discover_models)

    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"

    spec = BenchmarkRequest.from_dict({
        "benchmark_type": "sql",
        "base_url": "http://127.0.0.1:1234",
        "provider": "openai-compatible",
        "model": "sql-model",
        "sql_mode": "grammar",
        "question_ids": [1],
    })
    job = JobState(request=spec)
    job.progress_total = 1

    run(server._run_job(job))

    assert job.status == "completed"
    assert captured.get("called") == "run_question"


def test_speed_job_exposes_progress_phase_before_first_result(tmp_path, monkeypatch):
    captured = {}

    async def fake_detect_provider(self, base_url, requested_provider, api_key, client=None):
        return "openai-compatible"

    async def fake_discover_models(self, base_url, provider, api_key):
        return ["speed-model"]

    async def fake_benchmark_openai(self, spec, target, model, *, job=None, run_index=None):
        assert job is not None
        assert run_index == 1
        await job.set_phase(
            "waiting_first_token",
            f"Waiting for first token from {model}",
            model=model,
            run_index=run_index,
            benchmark_type="speed",
        )
        captured["progress_before_result"] = job.to_dict()["progress"]
        captured["result_count_before_result"] = len(job.results)
        return {
            "latency_ms": 1000,
            "total_time_ms": 2500,
            "ttft_ms": 1000,
            "prefill_tps": None,
            "decode_tps": 12.5,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "completion_tokens_capped": 20,
            "decode_tokens_measured": 20,
        }

    monkeypatch.setattr(BenchmarkServer, "_detect_provider", fake_detect_provider)
    monkeypatch.setattr(BenchmarkServer, "_discover_models", fake_discover_models)
    monkeypatch.setattr(BenchmarkServer, "_benchmark_openai", fake_benchmark_openai)

    server = BenchmarkServer(INDEX_HTML)
    server.results_store_dir = tmp_path / "benchmarks"
    spec = BenchmarkRequest.from_dict({
        "benchmark_type": "speed",
        "base_url": "http://127.0.0.1:1234",
        "provider": "openai-compatible",
        "model": "speed-model",
        "prompt": "hello",
        "repeat_count": 1,
        "warmup_runs": 0,
    })
    job = JobState(request=spec)
    job.progress_total = 1

    run(server._run_job(job))

    progress = captured["progress_before_result"]
    assert captured["result_count_before_result"] == 0
    assert progress["current_phase"] == "waiting_first_token"
    assert progress["current_message"] == "Waiting for first token from speed-model"
    assert progress["current_run_index"] == 1
    assert progress["current_benchmark_type"] == "speed"
    assert job.status == "completed"
