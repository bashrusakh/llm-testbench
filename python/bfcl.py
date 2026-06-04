"""BFCL (Berkeley Function Calling Leaderboard) adapter.

Loads tasks from a local data directory, calls the LLM with function/tool
definitions, and scores the returned tool calls against ground-truth answers.

Data directory layout (``bfcl_data/`` by default)::

    questions.jsonl   — one task per line, BFCL question format
    answers.jsonl     — one answer per line, BFCL ground-truth format

Question format::

    {
      "id": "simple_1",
      "category": "single",           // single | parallel | multiple | relevance
      "question": [{"role": "user", "content": "..."}],
      "function": [<OpenAI tool definition>, ...]
    }

Answer format::

    {
      "id": "simple_1",
      "ground_truth": [{"func_name": {"param": "value", ...}}, ...]
    }

Categories (BFCL v1/v2 single-turn subset)
------------------------------------------
single      Model must call exactly one function with correct arguments.
parallel    Model must call N functions (one per ground-truth entry) correctly.
multiple    One call from a menu of several functions.
relevance   Model must NOT call any function (plain text answer).

Scoring is AST-based (argument comparison with type coercion); no live execution.
Multi-turn (v3) and agentic categories (v4) are not implemented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from python.adapter import BenchmarkAdapter, RunContext

# ── constants ─────────────────────────────────────────────────────────────────

BFCL_CATEGORIES = frozenset({"single", "parallel", "multiple", "relevance"})

# Floating-point tolerance for numeric argument comparison
_FLOAT_TOLERANCE = 1e-4


# ── argument comparator ────────────────────────────────────────────────────────

def _coerce(value: Any) -> Any:
    """Best-effort type coercion: strings that look like numbers become numbers."""
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
    return value


def _values_equal(expected: Any, actual: Any) -> bool:
    """Compare two argument values with type coercion and float tolerance."""
    e = _coerce(expected)
    a = _coerce(actual)

    if isinstance(e, float) or isinstance(a, float):
        try:
            return abs(float(e) - float(a)) <= _FLOAT_TOLERANCE
        except (TypeError, ValueError):
            return False

    if isinstance(e, (list, tuple)) and isinstance(a, (list, tuple)):
        if len(e) != len(a):
            return False
        # Try both ordered and unordered comparison
        ordered = all(_values_equal(ev, av) for ev, av in zip(e, a))
        if ordered:
            return True
        # Fallback: set-style (only for flat comparable elements)
        try:
            return sorted(str(x) for x in e) == sorted(str(x) for x in a)
        except Exception:
            return False

    if isinstance(e, dict) and isinstance(a, dict):
        if set(e.keys()) != set(a.keys()):
            return False
        return all(_values_equal(e[k], a.get(k)) for k in e)

    return e == a


def _args_match(expected_args: Dict[str, Any], actual_args: Dict[str, Any]) -> bool:
    """Return True when every expected argument is present and matches in actual."""
    for key, expected_val in expected_args.items():
        if key not in actual_args:
            return False
        if not _values_equal(expected_val, actual_args[key]):
            return False
    return True


# ── tool call extractor ────────────────────────────────────────────────────────

def _extract_tool_calls(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract tool calls from an OpenAI-style response dict.

    Returns a list of ``{"name": str, "arguments": dict}`` entries.
    Falls back to parsing JSON from the text content if ``tool_calls`` is absent.
    """
    native = response.get("tool_calls") or []
    if native:
        calls = []
        for tc in native:
            func = tc.get("function", {})
            name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            if name:
                calls.append({"name": name, "arguments": args})
        return calls

    # Fallback: scan text for JSON objects that look like function calls
    content = str(response.get("content") or "")
    calls = []
    for match in re.finditer(r'\{[^{}]*\}', content, re.DOTALL):
        try:
            obj = json.loads(match.group())
            if "name" in obj and isinstance(obj.get("arguments"), dict):
                calls.append({"name": obj["name"], "arguments": obj["arguments"]})
        except json.JSONDecodeError:
            pass
    return calls


# ── scorer ────────────────────────────────────────────────────────────────────

def score_bfcl_result(
    category: str,
    ground_truth: List[Dict[str, Any]],
    actual_calls: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Score one BFCL task.

    Returns ``(success, error_message)``.

    Parameters
    ----------
    category:
        Task category (``"single"``, ``"parallel"``, ``"multiple"``,
        ``"relevance"``).
    ground_truth:
        List of ``{func_name: {param: value}}`` dicts from the answers file.
    actual_calls:
        List of ``{"name": str, "arguments": dict}`` extracted from the LLM
        response.
    """
    if category == "relevance":
        # Model must NOT call any function
        if actual_calls:
            names = ", ".join(c["name"] for c in actual_calls)
            return False, f"Expected no tool calls; got: {names}"
        return True, ""

    if not ground_truth:
        return False, "No ground truth provided"

    if category == "single":
        # Exactly one call required
        if len(actual_calls) != 1:
            return False, f"Expected 1 tool call, got {len(actual_calls)}"
        expected = ground_truth[0]
        func_name, expected_args = next(iter(expected.items()))
        actual = actual_calls[0]
        if actual["name"] != func_name:
            return False, f"Wrong function: expected '{func_name}', got '{actual['name']}'"
        if not _args_match(expected_args, actual["arguments"]):
            return False, f"Argument mismatch for '{func_name}'"
        return True, ""

    if category in ("parallel", "multiple"):
        # All ground-truth calls must appear in actual (order-insensitive)
        remaining = list(actual_calls)
        for expected in ground_truth:
            func_name, expected_args = next(iter(expected.items()))
            matched = False
            for i, actual in enumerate(remaining):
                if actual["name"] == func_name and _args_match(expected_args, actual["arguments"]):
                    remaining.pop(i)
                    matched = True
                    break
            if not matched:
                args_summary = ", ".join(f"{k}={v!r}" for k, v in expected_args.items())
                return False, f"Missing or wrong call for '{func_name}({args_summary})'"
        return True, ""

    return False, f"Unknown category: {category}"


# ── data loader ───────────────────────────────────────────────────────────────

def load_bfcl_tasks(data_dir: Path) -> List[Dict[str, Any]]:
    """Load questions and answers from *data_dir* and merge them.

    Returns a list of task dicts, each containing:
    ``id``, ``category``, ``question``, ``function``, ``ground_truth``.
    """
    q_path = data_dir / "questions.jsonl"
    a_path = data_dir / "answers.jsonl"

    if not q_path.exists():
        raise FileNotFoundError(f"BFCL questions file not found: {q_path}")
    if not a_path.exists():
        raise FileNotFoundError(f"BFCL answers file not found: {a_path}")

    questions: Dict[str, Dict[str, Any]] = {}
    for line in q_path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        questions[obj["id"]] = obj

    answers: Dict[str, List[Any]] = {}
    for line in a_path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        answers[obj["id"]] = obj.get("ground_truth", [])

    tasks = []
    for task_id, q in questions.items():
        task = dict(q)
        task["ground_truth"] = answers.get(task_id, [])
        tasks.append(task)

    return tasks


# ── build OpenAI tools list from BFCL function definitions ────────────────────

def _bfcl_functions_to_tools(functions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert BFCL function dicts to OpenAI tool-calling format."""
    tools = []
    for func in functions:
        tools.append({
            "type": "function",
            "function": {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {"type": "object", "properties": {}}),
            },
        })
    return tools


# ── BfclAdapter ───────────────────────────────────────────────────────────────

class BfclAdapter(BenchmarkAdapter):
    """Adapter for the Berkeley Function Calling Leaderboard (BFCL) benchmark.

    Required context options
    ~~~~~~~~~~~~~~~~~~~~~~~~
    data_dir : str | Path
        Path to the directory containing ``questions.jsonl`` and
        ``answers.jsonl`` in BFCL format.
    tool_llm_callback : async callable
        Same signature as the SQL benchmark's ``tool_llm_callback``.
        Called as ``callback(system_prompt, messages, tools, model, provider,
        endpoint, timeout_ms)``.
    categories : list[str], optional
        Subset of ``["single", "parallel", "multiple", "relevance"]`` to run.
        Defaults to all four.
    task_ids : list[str], optional
        Specific task IDs to run.  Defaults to all tasks in *data_dir*.
    """

    module_id = "bfcl"

    _SYSTEM_PROMPT = (
        "You are a helpful assistant with access to a set of tools. "
        "When the user's request requires calling a function, call it using the "
        "provided tools. Only call functions when necessary. "
        "If the request does not require any function call, answer normally in text."
    )

    async def prepare(self, ctx: RunContext) -> None:
        data_dir = ctx.options.get("data_dir")
        if not data_dir:
            raise ValueError("data_dir is required for the BFCL adapter")
        data_path = Path(str(data_dir))
        if not data_path.exists():
            raise ValueError(f"data_dir does not exist: {data_path}")

        tool_cb = ctx.options.get("tool_llm_callback")
        if tool_cb is None:
            raise ValueError("tool_llm_callback is required for the BFCL adapter")

        tasks = load_bfcl_tasks(data_path)
        # Filter by category if requested
        categories = ctx.options.get("categories") or list(BFCL_CATEGORIES)
        invalid = set(categories) - BFCL_CATEGORIES
        if invalid:
            raise ValueError(f"Unknown BFCL categories: {invalid}")
        tasks = [t for t in tasks if t.get("category", "single") in categories]

        # Filter by task IDs if requested
        task_ids = ctx.options.get("task_ids")
        if task_ids:
            id_set = set(task_ids)
            tasks = [t for t in tasks if t["id"] in id_set]

        ctx.state["tasks"] = tasks
        ctx.state["tool_llm_callback"] = tool_cb

    async def select_tasks(self, ctx: RunContext) -> List[Any]:
        return [t["id"] for t in ctx.state["tasks"]]

    async def run_task(self, ctx: RunContext, task_id: Any) -> Dict[str, Any]:
        tasks_by_id = {t["id"]: t for t in ctx.state["tasks"]}
        task = tasks_by_id[task_id]
        tool_cb = ctx.state["tool_llm_callback"]

        category = task.get("category", "single")
        functions = task.get("function", [])
        question_messages = task.get("question", [])
        ground_truth = task.get("ground_truth", [])

        tools = _bfcl_functions_to_tools(functions)

        try:
            response = await tool_cb(
                system_prompt=self._SYSTEM_PROMPT,
                messages=question_messages,
                tools=tools,
                model=ctx.model,
                provider=ctx.provider,
                endpoint=ctx.endpoint,
                timeout_ms=ctx.timeout_ms,
            )
        except Exception as exc:
            return {
                "task_id": task_id,
                "category": category,
                "outcome": "error",
                "success": False,
                "error": f"LLM callback failed: {exc}",
                "ground_truth": ground_truth,
                "actual_calls": [],
            }

        actual_calls = _extract_tool_calls(response)
        success, error = score_bfcl_result(category, ground_truth, actual_calls)

        return {
            "task_id": task_id,
            "category": category,
            "outcome": "pass" if success else "fail",
            "success": success,
            "error": error,
            "ground_truth": ground_truth,
            "actual_calls": actual_calls,
            "tool_calls": len(actual_calls),
        }

    async def score(self, ctx: RunContext, result: Dict[str, Any]) -> Dict[str, Any]:
        # Scoring happens inline in run_task; nothing extra to do here.
        result.setdefault("success", result.get("outcome") == "pass")
        return result

    def render(self, ctx: RunContext, results: List[Dict[str, Any]]) -> str:
        from collections import defaultdict
        total = len(results)
        by_cat: Dict[str, Dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0, "error": 0})
        for r in results:
            cat = r.get("category", "unknown")
            outcome = r.get("outcome", "error")
            by_cat[cat][outcome] = by_cat[cat].get(outcome, 0) + 1

        passed = sum(1 for r in results if r.get("success") is True)
        lines = [
            f"BFCL benchmark — model: {ctx.model}",
            f"Tasks: {total}  passed: {passed}  failed: {total - passed}",
            f"Pass rate: {passed / total:.1%}" if total else "Pass rate: n/a",
            "",
            "Per-category breakdown:",
        ]
        for cat in sorted(by_cat):
            cb = by_cat[cat]
            cat_total = cb["pass"] + cb["fail"] + cb["error"]
            lines.append(
                f"  {cat:12s}  {cb['pass']}/{cat_total}"
                f"  ({cb['pass'] / cat_total:.0%})" if cat_total else f"  {cat}: n/a"
            )
        return "\n".join(lines)

    async def cleanup(self, ctx: RunContext) -> None:
        ctx.state.pop("tasks", None)
        ctx.state.pop("tool_llm_callback", None)
