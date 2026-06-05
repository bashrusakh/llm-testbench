"""Benchmark adapter API.

Each benchmark module implements BenchmarkAdapter — a six-hook lifecycle that
the server calls for every run.  The two concrete implementations here
(SpeedAdapter, SqlAdapter) wrap the existing inline logic so callers can treat
all modules uniformly.

Lifecycle hooks
---------------
prepare(ctx)         – Validate / resolve the run context before any LLM call.
select_tasks(ctx)    – Return the ordered list of task identifiers to run.
run_task(ctx, task)  – Execute a single task and return a raw result dict.
score(ctx, result)   – Validate / enrich a raw result dict with scoring fields.
render(ctx, results) – Build the human-readable report string.
cleanup(ctx)         – Release any resources (DB connections, temp files, …).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


# ── context dataclass ──────────────────────────────────────────────────────────

class RunContext:
    """Carries all parameters for one benchmark run.

    Adapters may attach private state to ``ctx.state`` (a plain dict) so that
    hooks share data (e.g. the DuckDB connection opened in ``prepare`` and
    closed in ``cleanup``) without the server needing to know about it.
    """

    def __init__(
        self,
        *,
        module_id: str,
        model: str,
        provider: str,
        endpoint: str,
        api_key: str,
        timeout_ms: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.module_id = module_id
        self.model = model
        self.provider = provider
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_ms = timeout_ms
        self.options: Dict[str, Any] = options or {}
        # Adapters may stash private state here.
        self.state: Dict[str, Any] = {}


# ── abstract base ──────────────────────────────────────────────────────────────

class BenchmarkAdapter(ABC):
    """Abstract base class for benchmark module adapters."""

    #: The module ID this adapter serves (e.g. "speed", "sql").
    module_id: str = ""

    # -- lifecycle hooks -------------------------------------------------------

    @abstractmethod
    async def prepare(self, ctx: RunContext) -> None:
        """Validate ctx and allocate any shared resources (DB, sandbox, …).

        Raise ``ValueError`` for bad configuration or ``RuntimeError`` if a
        required backend is unreachable.  Called once before ``select_tasks``.
        """

    @abstractmethod
    async def select_tasks(self, ctx: RunContext) -> List[Any]:
        """Return an ordered list of task identifiers.

        Each element is passed unchanged to ``run_task`` and ``score``.
        For the SQL module this is a list of ``question_id`` ints; for the
        speed module it is a list of ``(model, run_index)`` tuples.
        """

    @abstractmethod
    async def run_task(self, ctx: RunContext, task: Any) -> Dict[str, Any]:
        """Execute one task and return a raw result dict.

        The dict must include at least ``"outcome"`` (``"pass"``, ``"fail"``,
        or ``"error"``).  Other keys are module-specific and documented in the
        module's ``result_schema``.
        """

    @abstractmethod
    async def score(self, ctx: RunContext, result: Dict[str, Any]) -> Dict[str, Any]:
        """Validate / enrich a raw result dict with scoring fields.

        May add ``"success"``, ``"cost"``, ``"latency_ms"``, and similar.
        Returns the (possibly mutated) result dict.
        """

    @abstractmethod
    def render(self, ctx: RunContext, results: List[Dict[str, Any]]) -> str:
        """Build a human-readable summary string for the completed run."""

    @abstractmethod
    async def cleanup(self, ctx: RunContext) -> None:
        """Release any resources allocated in ``prepare`` (DB, files, …)."""

    # -- introspection helper --------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """Return a dict describing this adapter's lifecycle status."""
        return {
            "module_id": self.module_id,
            "hooks": ["prepare", "select_tasks", "run_task", "score", "render", "cleanup"],
            "status": "concrete_adapter",
            "class": type(self).__name__,
        }


# ── speed adapter ──────────────────────────────────────────────────────────────

class SpeedAdapter(BenchmarkAdapter):
    """Adapter shim for the speed (latency/throughput) benchmark.

    The actual LLM calls and metric extraction live in
    ``BenchmarkServer._benchmark_openai`` / ``_benchmark_ollama``; this adapter
    exposes the same logic through the standard lifecycle interface.
    """

    module_id = "speed"

    def describe(self) -> Dict[str, Any]:
        desc = super().describe()
        desc.update({
            "status": "metadata_only",
            "entrypoint": "BenchmarkServer._run_single_benchmark",
            "note": "Live speed runs are executed inline by BenchmarkServer.",
        })
        return desc

    async def prepare(self, ctx: RunContext) -> None:
        repeat_count = int(ctx.options.get("repeat_count", 1))
        warmup_runs = int(ctx.options.get("warmup_runs", 0))
        if repeat_count < 1:
            raise ValueError("repeat_count must be >= 1")
        if warmup_runs < 0:
            raise ValueError("warmup_runs must be >= 0")
        prompt = ctx.options.get("prompt", "")
        if not prompt or not str(prompt).strip():
            raise ValueError("prompt is required for the speed benchmark")

    async def select_tasks(self, ctx: RunContext) -> List[Any]:
        repeat_count = int(ctx.options.get("repeat_count", 1))
        warmup_runs = int(ctx.options.get("warmup_runs", 0))
        # Warmup runs have run_index=0; real runs use 1-based indices.
        warmup = [(ctx.model, 0)] * warmup_runs
        measured = [(ctx.model, i) for i in range(1, repeat_count + 1)]
        return warmup + measured

    async def run_task(self, ctx: RunContext, task: Any) -> Dict[str, Any]:
        # Placeholder — real execution delegates to BenchmarkServer helpers.
        raise NotImplementedError(
            "SpeedAdapter is metadata-only; live speed runs use "
            "BenchmarkServer._run_single_benchmark"
        )

    async def score(self, ctx: RunContext, result: Dict[str, Any]) -> Dict[str, Any]:
        result.setdefault("success", result.get("outcome") == "pass")
        return result

    def render(self, ctx: RunContext, results: List[Dict[str, Any]]) -> str:
        measured = [r for r in results if r.get("run_index", 0) > 0]
        passed = [r for r in measured if r.get("success") is True]
        lines = [
            f"Speed benchmark — model: {ctx.model}",
            f"Runs: {len(measured)}  passed: {len(passed)}  failed: {len(measured) - len(passed)}",
        ]
        decode_values = [r["decode_tps"] for r in passed if r.get("decode_tps") is not None]
        if decode_values:
            lines.append(f"Best decode speed: {max(decode_values):.2f} tok/s")
        return "\n".join(lines)

    async def cleanup(self, ctx: RunContext) -> None:
        # No resources to release for the speed adapter.
        pass


# ── sql adapter ────────────────────────────────────────────────────────────────

class SqlAdapter(BenchmarkAdapter):
    """Adapter shim for the SQL accuracy benchmark.

    The heavy lifting (DuckDB, tool-calling loop) lives in
    ``SqlBenchmarkRunner``; this adapter exposes the same logic through the
    standard lifecycle interface and manages the runner's lifetime.
    """

    module_id = "sql"

    async def prepare(self, ctx: RunContext) -> None:
        from pathlib import Path
        from python.sql_benchmark import SqlBenchmarkRunner

        data_dir = ctx.options.get("data_dir")
        if not data_dir:
            raise ValueError("data_dir is required for the SQL adapter")
        data_path = Path(str(data_dir))
        if not data_path.exists():
            raise ValueError(f"data_dir does not exist: {data_path}")
        llm_callback = ctx.options.get("llm_callback")
        runner = SqlBenchmarkRunner(llm_callback=llm_callback, data_dir=data_path)
        ctx.state["runner"] = runner

    async def select_tasks(self, ctx: RunContext) -> List[Any]:
        runner = ctx.state["runner"]
        question_ids = ctx.options.get("question_ids")
        if question_ids is None:
            return sorted(runner.questions_by_id.keys())
        return [int(qid) for qid in question_ids]

    async def run_task(self, ctx: RunContext, task: Any) -> Dict[str, Any]:
        runner = ctx.state["runner"]
        sql_mode = ctx.options.get("sql_mode", "tool-calling")
        thinking_mode = ctx.options.get("thinking_mode", "off")
        if sql_mode == "grammar":
            return await runner.run_question(
                question_id=int(task),
                model=ctx.model,
                provider=ctx.provider,
                endpoint=ctx.endpoint,
                timeout_ms=ctx.timeout_ms,
                thinking_mode=thinking_mode,
            )
        tool_llm_callback = ctx.options.get("tool_llm_callback")
        return await runner.run_question_tool_calling(
            question_id=int(task),
            model=ctx.model,
            provider=ctx.provider,
            endpoint=ctx.endpoint,
            timeout_ms=ctx.timeout_ms,
            tool_llm_callback=tool_llm_callback,
            thinking_mode=thinking_mode,
        )

    async def score(self, ctx: RunContext, result: Dict[str, Any]) -> Dict[str, Any]:
        # SqlBenchmarkRunner already fills success/outcome/error.
        result.setdefault("success", result.get("outcome") == "pass")
        return result

    def render(self, ctx: RunContext, results: List[Dict[str, Any]]) -> str:
        total = len(results)
        passed = sum(1 for r in results if r.get("success") is True)
        failed = sum(1 for r in results if r.get("success") is False)
        lines = [
            f"SQL accuracy benchmark — model: {ctx.model}",
            f"Questions: {total}  passed: {passed}  failed: {failed}",
            f"Pass rate: {passed / total:.1%}" if total else "Pass rate: n/a",
        ]
        fail_items = [r for r in results if r.get("success") is not True]
        if fail_items:
            lines.append("Failed:")
            for r in fail_items:
                lines.append(f"  q{r.get('question_id')}: {r.get('error', '')}")
        return "\n".join(lines)

    async def cleanup(self, ctx: RunContext) -> None:
        runner = ctx.state.pop("runner", None)
        if runner is not None:
            runner.close()


# ── registry ───────────────────────────────────────────────────────────────────

#: Maps module_id -> BenchmarkAdapter instance.
def _build_registry() -> Dict[str, BenchmarkAdapter]:
    return {
        adapter.module_id: adapter
        for adapter in [SpeedAdapter(), SqlAdapter()]
    }


ADAPTER_REGISTRY: Dict[str, BenchmarkAdapter] = _build_registry()


def get_adapter(module_id: str) -> Optional[BenchmarkAdapter]:
    """Return the adapter for *module_id*, or ``None`` if not implemented."""
    return ADAPTER_REGISTRY.get(module_id)
