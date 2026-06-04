import asyncio
import json
from pathlib import Path

import pytest

from python.server import BenchmarkRequest, BenchmarkServer, BenchmarkTarget, JobState
import python.server as server_module


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"


def run(coro):
    return asyncio.run(coro)


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
