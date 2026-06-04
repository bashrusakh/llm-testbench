"""Tests for the BFCL adapter (python/bfcl.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from python.bfcl import (
    BfclAdapter,
    _args_match,
    _extract_tool_calls,
    _values_equal,
    load_bfcl_tasks,
    score_bfcl_result,
)
from python.adapter import ADAPTER_REGISTRY, RunContext


BFCL_DATA_DIR = Path(__file__).resolve().parents[1] / "bfcl_data"


def run(coro):
    return asyncio.run(coro)


# ── argument comparator ───────────────────────────────────────────────────────

@pytest.mark.parametrize("e,a,ok", [
    (1, 1, True),
    (1, "1", True),           # string→int coercion
    (1.0, "1.0", True),       # string→float coercion
    (1.0001, 1.0, True),      # within tolerance
    (1.5, 2.0, False),
    ("Paris", "Paris", True),
    ("paris", "Paris", False), # case-sensitive
    ([1, 2], [1, 2], True),
    ([1, 2], [2, 1], True),   # order-insensitive list
    ([1, 2], [1, 3], False),
    ({"a": 1}, {"a": 1}, True),
    ({"a": 1}, {"a": 2}, False),
])
def test_values_equal(e, a, ok):
    assert _values_equal(e, a) == ok


def test_args_match_ignores_extra_actual_keys():
    # Ground truth only requires "city"; model may return extra params
    assert _args_match({"city": "Paris"}, {"city": "Paris", "unit": "celsius"})


def test_args_match_fails_on_missing_required_key():
    assert not _args_match({"city": "Paris", "unit": "celsius"}, {"city": "Paris"})


def test_args_match_fails_on_wrong_value():
    assert not _args_match({"city": "Paris"}, {"city": "London"})


# ── tool call extractor ───────────────────────────────────────────────────────

def _make_response(tool_calls=None, content=""):
    return {"tool_calls": tool_calls or [], "content": content}


def test_extract_native_tool_calls():
    response = {
        "tool_calls": [{
            "id": "tc1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }],
        "content": "",
    }
    calls = _extract_tool_calls(response)
    assert len(calls) == 1
    assert calls[0]["name"] == "get_weather"
    assert calls[0]["arguments"] == {"city": "Paris"}


def test_extract_multiple_native_tool_calls():
    response = {
        "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "f1", "arguments": '{"a": 1}'}},
            {"id": "2", "type": "function", "function": {"name": "f2", "arguments": '{"b": 2}'}},
        ],
        "content": "",
    }
    calls = _extract_tool_calls(response)
    assert len(calls) == 2
    assert calls[0]["name"] == "f1"
    assert calls[1]["name"] == "f2"


def test_extract_returns_empty_for_text_only_response():
    response = {"tool_calls": [], "content": "The capital of France is Paris."}
    calls = _extract_tool_calls(response)
    assert calls == []


def test_extract_invalid_json_arguments_gives_empty_dict():
    response = {
        "tool_calls": [{
            "id": "tc1",
            "type": "function",
            "function": {"name": "f", "arguments": "not-json"},
        }],
        "content": "",
    }
    calls = _extract_tool_calls(response)
    assert calls[0]["arguments"] == {}


# ── scorer ────────────────────────────────────────────────────────────────────

def test_score_single_pass():
    gt = [{"get_weather": {"city": "Paris", "unit": "celsius"}}]
    calls = [{"name": "get_weather", "arguments": {"city": "Paris", "unit": "celsius"}}]
    success, err = score_bfcl_result("single", gt, calls)
    assert success
    assert err == ""


def test_score_single_wrong_function_name():
    gt = [{"get_weather": {"city": "Paris"}}]
    calls = [{"name": "web_search", "arguments": {"query": "Paris weather"}}]
    success, err = score_bfcl_result("single", gt, calls)
    assert not success
    assert "get_weather" in err


def test_score_single_wrong_argument():
    gt = [{"get_weather": {"city": "Paris"}}]
    calls = [{"name": "get_weather", "arguments": {"city": "London"}}]
    success, err = score_bfcl_result("single", gt, calls)
    assert not success
    assert "mismatch" in err.lower()


def test_score_single_too_many_calls():
    gt = [{"get_weather": {"city": "Paris"}}]
    calls = [
        {"name": "get_weather", "arguments": {"city": "Paris"}},
        {"name": "get_weather", "arguments": {"city": "London"}},
    ]
    success, err = score_bfcl_result("single", gt, calls)
    assert not success


def test_score_single_no_calls():
    gt = [{"get_weather": {"city": "Paris"}}]
    success, err = score_bfcl_result("single", gt, [])
    assert not success


def test_score_parallel_pass():
    gt = [{"get_weather": {"city": "Tokyo"}}, {"get_weather": {"city": "London"}}]
    calls = [
        {"name": "get_weather", "arguments": {"city": "London"}},
        {"name": "get_weather", "arguments": {"city": "Tokyo"}},
    ]
    success, err = score_bfcl_result("parallel", gt, calls)
    assert success


def test_score_parallel_missing_one_call():
    gt = [{"get_weather": {"city": "Tokyo"}}, {"get_weather": {"city": "London"}}]
    calls = [{"name": "get_weather", "arguments": {"city": "Tokyo"}}]
    success, err = score_bfcl_result("parallel", gt, calls)
    assert not success
    assert "London" in err


def test_score_multiple_pass():
    gt = [{"add": {"a": 42, "b": 58}}]
    calls = [{"name": "add", "arguments": {"a": 42, "b": 58}}]
    success, err = score_bfcl_result("multiple", gt, calls)
    assert success


def test_score_multiple_wrong_function():
    gt = [{"add": {"a": 42, "b": 58}}]
    calls = [{"name": "subtract", "arguments": {"a": 42, "b": 58}}]
    success, err = score_bfcl_result("multiple", gt, calls)
    assert not success


def test_score_relevance_pass_no_calls():
    success, err = score_bfcl_result("relevance", [], [])
    assert success


def test_score_relevance_fail_when_model_calls_function():
    calls = [{"name": "get_weather", "arguments": {"city": "Paris"}}]
    success, err = score_bfcl_result("relevance", [], calls)
    assert not success
    assert "get_weather" in err


def test_score_numeric_coercion():
    """Numeric string arguments should match integer ground truth."""
    gt = [{"add": {"a": 42, "b": 58}}]
    calls = [{"name": "add", "arguments": {"a": "42", "b": "58"}}]
    success, _ = score_bfcl_result("multiple", gt, calls)
    assert success


# ── data loader ───────────────────────────────────────────────────────────────

def test_load_bfcl_tasks_returns_merged_records():
    tasks = load_bfcl_tasks(BFCL_DATA_DIR)
    assert len(tasks) == 5
    by_id = {t["id"]: t for t in tasks}

    assert "simple_1" in by_id
    assert by_id["simple_1"]["category"] == "single"
    assert by_id["simple_1"]["ground_truth"] == [{"get_weather": {"city": "Paris", "unit": "celsius"}}]
    assert by_id["parallel_1"]["category"] == "parallel"
    assert len(by_id["parallel_1"]["ground_truth"]) == 2
    assert by_id["relevance_1"]["ground_truth"] == []


def test_load_bfcl_tasks_raises_on_missing_questions(tmp_path):
    (tmp_path / "answers.jsonl").write_text("")
    with pytest.raises(FileNotFoundError, match="questions"):
        load_bfcl_tasks(tmp_path)


def test_load_bfcl_tasks_raises_on_missing_answers(tmp_path):
    (tmp_path / "questions.jsonl").write_text("")
    with pytest.raises(FileNotFoundError, match="answers"):
        load_bfcl_tasks(tmp_path)


# ── BfclAdapter lifecycle ─────────────────────────────────────────────────────

def _make_tool_response(name: str, arguments: dict) -> dict:
    return {
        "tool_calls": [{
            "id": "tc1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
        "content": "",
        "usage": {},
        "model": "stub-model",
    }


def _bfcl_ctx(tool_cb=None, **overrides) -> RunContext:
    async def _default_cb(**kwargs):
        return {"tool_calls": [], "content": "I don't know.", "usage": {}, "model": "stub"}

    opts = {
        "data_dir": str(BFCL_DATA_DIR),
        "tool_llm_callback": tool_cb or _default_cb,
    }
    opts.update(overrides)
    return RunContext(
        module_id="bfcl",
        model="stub-model",
        provider="openai-compatible",
        endpoint="http://127.0.0.1:1234",
        api_key="",
        timeout_ms=5000,
        options=opts,
    )


def test_bfcl_prepare_loads_all_tasks():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx()
    run(adapter.prepare(ctx))
    assert len(ctx.state["tasks"]) == 5
    run(adapter.cleanup(ctx))


def test_bfcl_prepare_filters_by_category():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx(categories=["single"])
    run(adapter.prepare(ctx))
    assert all(t["category"] == "single" for t in ctx.state["tasks"])
    run(adapter.cleanup(ctx))


def test_bfcl_prepare_filters_by_task_ids():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx(task_ids=["simple_1", "relevance_1"])
    run(adapter.prepare(ctx))
    assert {t["id"] for t in ctx.state["tasks"]} == {"simple_1", "relevance_1"}
    run(adapter.cleanup(ctx))


def test_bfcl_prepare_rejects_missing_data_dir():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx(data_dir="")
    with pytest.raises(ValueError, match="data_dir"):
        run(adapter.prepare(ctx))


def test_bfcl_prepare_rejects_missing_tool_callback():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_llm_callback=None)
    ctx.options["tool_llm_callback"] = None
    with pytest.raises(ValueError, match="tool_llm_callback"):
        run(adapter.prepare(ctx))


def test_bfcl_prepare_rejects_unknown_category():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx(categories=["single", "unknown"])
    with pytest.raises(ValueError, match="Unknown BFCL categories"):
        run(adapter.prepare(ctx))


def test_bfcl_select_tasks_returns_ids():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx(task_ids=["simple_1", "parallel_1"])
    run(adapter.prepare(ctx))
    tasks = run(adapter.select_tasks(ctx))
    assert set(tasks) == {"simple_1", "parallel_1"}
    run(adapter.cleanup(ctx))


def test_bfcl_run_task_single_pass():
    async def tool_cb(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        return _make_tool_response("get_weather", {"city": "Paris", "unit": "celsius"})

    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_cb=tool_cb, task_ids=["simple_1"])
    run(adapter.prepare(ctx))
    result = run(adapter.run_task(ctx, "simple_1"))
    run(adapter.cleanup(ctx))

    assert result["task_id"] == "simple_1"
    assert result["category"] == "single"
    assert result["success"] is True
    assert result["outcome"] == "pass"
    assert result["error"] == ""


def test_bfcl_run_task_single_wrong_city():
    async def tool_cb(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        return _make_tool_response("get_weather", {"city": "London", "unit": "celsius"})

    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_cb=tool_cb, task_ids=["simple_1"])
    run(adapter.prepare(ctx))
    result = run(adapter.run_task(ctx, "simple_1"))
    run(adapter.cleanup(ctx))

    assert result["success"] is False
    assert result["outcome"] == "fail"


def test_bfcl_run_task_relevance_no_call_pass():
    async def tool_cb(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        return {"tool_calls": [], "content": "Paris is the capital of France.", "usage": {}, "model": "stub"}

    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_cb=tool_cb, task_ids=["relevance_1"])
    run(adapter.prepare(ctx))
    result = run(adapter.run_task(ctx, "relevance_1"))
    run(adapter.cleanup(ctx))

    assert result["success"] is True


def test_bfcl_run_task_relevance_unexpected_call_fails():
    async def tool_cb(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        return _make_tool_response("get_weather", {"city": "Paris"})

    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_cb=tool_cb, task_ids=["relevance_1"])
    run(adapter.prepare(ctx))
    result = run(adapter.run_task(ctx, "relevance_1"))
    run(adapter.cleanup(ctx))

    assert result["success"] is False


def test_bfcl_run_task_parallel_pass():
    call_count = [0]

    async def tool_cb(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        call_count[0] += 1
        return {
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'}},
                {"id": "2", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "London"}'}},
            ],
            "content": "",
            "usage": {},
            "model": "stub",
        }

    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_cb=tool_cb, task_ids=["parallel_1"])
    run(adapter.prepare(ctx))
    result = run(adapter.run_task(ctx, "parallel_1"))
    run(adapter.cleanup(ctx))

    assert result["success"] is True
    assert result["tool_calls"] == 2


def test_bfcl_run_task_error_on_callback_exception():
    async def bad_cb(*, system_prompt, messages, tools, model, provider, endpoint, timeout_ms):
        raise RuntimeError("LLM unreachable")

    adapter = BfclAdapter()
    ctx = _bfcl_ctx(tool_cb=bad_cb, task_ids=["simple_1"])
    run(adapter.prepare(ctx))
    result = run(adapter.run_task(ctx, "simple_1"))
    run(adapter.cleanup(ctx))

    assert result["outcome"] == "error"
    assert result["success"] is False
    assert "LLM unreachable" in result["error"]


def test_bfcl_score_passes_through():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx()
    result = run(adapter.score(ctx, {"outcome": "pass", "success": True}))
    assert result["success"] is True


def test_bfcl_render_shows_per_category_breakdown():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx()
    results = [
        {"category": "single",   "success": True,  "outcome": "pass"},
        {"category": "single",   "success": False, "outcome": "fail"},
        {"category": "parallel", "success": True,  "outcome": "pass"},
        {"category": "relevance","success": True,  "outcome": "pass"},
    ]
    text = adapter.render(ctx, results)
    assert "stub-model" in text
    assert "single" in text
    assert "parallel" in text
    assert "relevance" in text
    assert "3/4" in text or "75" in text  # 3 out of 4 passed


def test_bfcl_cleanup_clears_state():
    adapter = BfclAdapter()
    ctx = _bfcl_ctx()
    run(adapter.prepare(ctx))
    assert "tasks" in ctx.state
    run(adapter.cleanup(ctx))
    assert "tasks" not in ctx.state


# ── adapter registry ──────────────────────────────────────────────────────────

def test_bfcl_adapter_in_registry():
    assert "bfcl" in ADAPTER_REGISTRY
    assert isinstance(ADAPTER_REGISTRY["bfcl"], BfclAdapter)


def test_bfcl_describe():
    adapter = ADAPTER_REGISTRY["bfcl"]
    desc = adapter.describe()
    assert desc["module_id"] == "bfcl"
    assert desc["status"] == "concrete_adapter"
    assert desc["class"] == "BfclAdapter"
    assert set(desc["hooks"]) == {"prepare", "select_tasks", "run_task", "score", "render", "cleanup"}
